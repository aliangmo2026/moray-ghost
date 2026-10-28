"""v0.15 阶段三：自我进化技能库（行为克隆）——pynput 全局静默录制 + VLM 语义标注。

链路：用户经 /skill/record/start 开启后台监听 → pynput 全局监听线程静默记录
点击坐标/输入文本/滚动 + 点击现场截图（节流）→ /skill/record/stop 结束 →
对每次点击裁剪现场图喂本地 VLM（vl_driver.describe）标注 target_text →
语义化 JSON 技能入 SQLite（skill_store）→ 主仓/HTTP 可按名调用 →
/skill/run 逐步走 controller.run_action 同一安全管道回放。

设计红线：
- 录制只读：监听线程绝不产生任何输入（pynput listener 是纯观察者）；
- 回放只走 controller.run_action 唯一咽喉（冻结/UAC/黑名单/Fail-Safe/
  熔断/审计截图全套门禁，与 /execute_action 同级）；
- VLM 不可用（无 llama 后端/模型未挂载）→ 如实降级：技能以原始坐标入库、
  annotation_backend="none"、低置信度，绝不编造语义；
- 技能与截图仅存本机 data/，绝不上传；
- 录制会话有事件上限（SKILL_MAX_EVENTS）防失控，自动停止。
"""
from __future__ import annotations

import base64
import io
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import config, screen_mss
from . import skill_store

LOG = logging.getLogger("moray-ghost.skill_recorder")

FRAMES_DIR = config.DATA_DIR / "skill_frames"
_FRAME_KEEP_DAYS = 7  # 技能帧目录保留窗口（过期清理，防磁盘无界增长）

_state_lock = threading.Lock()
_recording = False
_session: Dict[str, Any] = {}
_mouse_listener: Any = None
_keyboard_listener: Any = None

# 点击截图队列：pynput 回调线程就是 Windows 输入钩子的消息线程——回调里做
# BitBlt/JPEG 落盘（秒级）会阻塞整条钩子链，把 keyboard.write 等注入输入拖死
# （v0.15 实测：录制期间 type 动作 22.5s 硬超时冻结）。回调只入队（微秒级），
# 重活全部在独立采集线程做；stop 时排空并 join，保证标注前帧已就绪。
_frame_q: "queue.Queue[Any]" = queue.Queue()
_frame_worker: Optional[threading.Thread] = None
_FRAME_DRAIN_TIMEOUT = 6.0


class RecorderError(Exception):
    """录制器状态错误（重复 start / 未在录制时 stop）。"""


def is_recording() -> bool:
    return _recording


def status() -> Dict[str, Any]:
    with _state_lock:
        if not _recording:
            return {"recording": False}
        return {
            "recording": True,
            "session_id": _session.get("sid"),
            "started_at": _session.get("started_at"),
            "events": len(_session.get("events", [])),
            "frames": len(_session.get("frames", [])),
        }


def _flush_text(buf: List[str]) -> None:
    """把打字缓冲合成为一条 type 事件（录制会话内联调，已持锁）。"""
    text = "".join(buf)
    buf.clear()
    if text:
        _session["events"].append(
            {"action": "type", "text": text, "t": time.time()})


def _on_click(x: int, y: int, button: Any, pressed: bool) -> None:
    if not pressed:
        return
    btn = str(button).rsplit(".", 1)[-1]
    with _state_lock:
        if not _recording:
            return
        _flush_text(_session.setdefault("_text", []))
        ev: Dict[str, Any] = {"action": "click", "x": int(x), "y": int(y),
                              "button": btn, "t": time.time()}
        if len(_session["events"]) >= config.SKILL_MAX_EVENTS:
            _session["events"].append(ev)
            _session["over_limit"] = True  # 监听回调里绝不做重活：置旗标，stop 时收口
            return
        _session["events"].append(ev)
        # 截图节流：BitBlt 只在距上一张超过阈值时执行——且只入队，重活在采集线程
        now = time.time()
        if now - _session.get("last_shot", 0.0) >= config.SKILL_SHOT_MIN_INTERVAL:
            _session["last_shot"] = now
            _frame_q.put((ev,))  # 帧路径由采集线程回填 ev["frame"]


def _frame_worker_loop() -> None:
    """点击帧采集线程：与 pynput 回调解耦（钩子线程绝不做 BitBlt/编码/落盘）。"""
    while True:
        item = _frame_q.get()
        if item is None:
            break
        (ev,) = item
        with _state_lock:
            if not _recording:
                continue  # 会话已收口：迟到的帧请求直接丢弃
            sid = _session["sid"]
            seq = len(_session["frames"])
        try:
            img, _origin = screen_mss.grab(0, timeout=4.0)
            d = FRAMES_DIR / sid
            d.mkdir(parents=True, exist_ok=True)
            data = screen_mss.encode(img, "jpeg", 60, 1.0)
            path = d / f"{seq:04d}.jpg"
            path.write_bytes(data)
            with _state_lock:
                _session["frames"].append(str(path))
            ev["frame"] = str(path)
        except Exception as exc:  # noqa: BLE001 截图失败绝不打断录制
            LOG.warning("skill frame grab failed: %s", exc)


def _drain_frames(timeout: float) -> None:
    """排空帧队列并等采集线程退出（stop 收口：标注前帧必须就绪）。"""
    deadline = time.time() + timeout
    while not _frame_q.empty() and time.time() < deadline:
        time.sleep(0.05)
    w = _frame_worker
    if w is not None:
        _frame_q.put(None)  # 退出哨兵
        w.join(timeout=max(0.5, deadline - time.time()))


def _on_scroll(x: int, y: int, dx: int, dy: int) -> None:
    with _state_lock:
        if not _recording:
            return
        _flush_text(_session.setdefault("_text", []))
        if len(_session["events"]) < config.SKILL_MAX_EVENTS:
            _session["events"].append(
                {"action": "scroll", "x": int(x), "y": int(y),
                 "dy": int(dy), "t": time.time()})


_KEY_ENTER_TAB = {"enter", "tab"}


def _on_press(key: Any) -> None:
    with _state_lock:
        if not _recording:
            return
        buf = _session.setdefault("_text", [])
        char = getattr(key, "char", None)
        if char is not None and char.isprintable():
            buf.append(char)
            if sum(len(s) for s in buf) > 4096:
                _flush_text(buf)  # 单条文本事件上限，防无界增长
            return
        name = str(key).rsplit(".", 1)[-1]
        if name in _KEY_ENTER_TAB:
            _flush_text(buf)
            if len(_session["events"]) < config.SKILL_MAX_EVENTS:
                _session["events"].append(
                    {"action": "key", "key": name, "t": time.time()})
        # 修饰键/方向键等：不记录（技能回放里没有对应语义，混入只会污染序列）


def start() -> Dict[str, Any]:
    """开启录制会话（幂等拒绝：已在录制 → RecorderError）。"""
    global _recording, _session, _mouse_listener, _keyboard_listener, _frame_worker
    if not config.SKILL_RECORD_ENABLED:
        raise RecorderError("skill recording is disabled (GHOST_SKILL_RECORD=false)")
    with _state_lock:
        if _recording:
            raise RecorderError("already recording (stop the current session first)")
        _session = {
            "sid": "s" + str(int(time.time())) + "_" + str(int(time.time() * 1e6) % 1e6),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "events": [], "frames": [], "_text": [], "last_shot": 0.0,
            "over_limit": False,
        }
        _prune_old_frames()  # 顺手清掉过期帧目录
    # pynput 延迟 import：模块顶层不引依赖（非支持平台/未安装时服务照常启动）
    from pynput import keyboard, mouse
    _mouse_listener = mouse.Listener(on_click=_on_click, on_scroll=_on_scroll)
    _keyboard_listener = keyboard.Listener(on_press=_on_press)
    _mouse_listener.start()
    _keyboard_listener.start()
    _frame_worker = threading.Thread(target=_frame_worker_loop,
                                     name="moray-ghost-skill-frame",
                                     daemon=True)
    _frame_worker.start()
    with _state_lock:
        _recording = True
    audit = _audit_event  # 本地别名，方便下面调用
    audit("skill_record_start", session_id=_session["sid"])
    LOG.info("SKILL_RECORD start sid=%s", _session["sid"])
    return {"ok": True, "recording": True, "session_id": _session["sid"],
            "started_at": _session["started_at"]}


def _audit_event(event: str, **fields: Any) -> None:
    try:
        from . import audit_sink
        audit_sink.write(event, **fields)
    except Exception:  # noqa: BLE001 审计失败绝不影响主流程
        pass


def stop(name: Optional[str] = None) -> Dict[str, Any]:
    """结束录制：停监听 → 排空帧队列 → VLM 标注 → 技能入库。返回技能 JSON。"""
    global _recording, _session, _mouse_listener, _keyboard_listener, _frame_worker
    for lst in (_mouse_listener, _keyboard_listener):
        if lst is not None:
            try:
                lst.stop()
            except Exception:  # noqa: BLE001
                pass
    _mouse_listener = _keyboard_listener = None
    _drain_frames(_FRAME_DRAIN_TIMEOUT)  # 帧就绪后再收口（worker 需 _recording=True 才处理）
    with _state_lock:
        if not _recording:
            raise RecorderError("not recording")
        _recording = False
        _flush_text(_session.get("_text", []))
        events = list(_session.get("events", []))
        sid = _session["sid"]
        started = _session.get("started_at")
        over_limit = bool(_session.get("over_limit"))
    _frame_worker = None

    actions = [dict(ev) for ev in events if ev.get("action") != "click"
               or "x" in ev]  # 形状自检（pynput 回调给的都是完整事件）
    annotated, backend_used, confidence = _annotate(actions)
    desc = _describe(actions, annotated)
    skill_name = (name or "").strip() or "learned_" + time.strftime("%m%d_%H%M%S")
    try:
        skill = skill_store.save_skill(skill_name, desc, actions, confidence,
                                       backend_used)
    except skill_store.SkillNameConflictError:
        skill_name = skill_name + "_" + str(int(time.time()) % 10000)
        skill = skill_store.save_skill(skill_name, desc, actions, confidence,
                                       backend_used)
    audit = _audit_event
    audit("skill_saved", skill_id=skill["id"], name=skill["name"],
          steps=skill["steps_total"], confidence=confidence,
          annotation_backend=backend_used)
    LOG.info("SKILL_RECORD stop sid=%s skill=%s steps=%s anno=%s",
             sid, skill["name"], skill["steps_total"], backend_used)
    return {"ok": True, "skill": skill, "session_id": sid,
            "started_at": started, "over_limit": over_limit,
            "frames": len(_session.get("frames", []))}


# ---------------- VLM 语义标注（诚实降级） ----------------

_CROP_HALF = 160  # 点击周围裁剪半宽/半高（px）：足够看清控件，噪声最小
_ANNOTATE_QUESTION = (
    "这张截图中央附近是什么界面控件？只回答控件名（如：保存按钮、"
    "搜索输入框、关闭按钮、菜单项），看不清就回答：未知")


def _annotate(actions: List[Dict[str, Any]]) -> Tuple[int, str, float]:
    """对点击动作逐个调用本地 VLM 标注 target_text。

    返回 (标注成功数, backend 标识, 置信度)。VLM 不可用 → 立即停止尝试、
    全部动作保持原始坐标（confidence=0.3 表示"可回放但无语义"）。
    """
    clicks = [a for a in actions if a.get("action") == "click" and a.get("frame")]
    if not clicks:
        return 0, "none", 0.3
    from . import vl_driver  # 延迟 import：llama 模型加载只在真标注时发生
    done = 0
    for a in clicks[: max(0, config.SKILL_ANNOTATE_MAX_CALLS)]:
        b64 = _crop_around(a)
        if not b64:
            continue
        try:
            res = vl_driver.describe(backend="llama", question=_ANNOTATE_QUESTION,
                                     image_b64=b64, max_tokens=24)
            text = str(res.get("description", "")).strip()
            if text:
                a["target_text"] = text[:60]
                done += 1
        except Exception as exc:  # noqa: BLE001
            LOG.info("skill annotate unavailable (%s) — raw-coordinate fallback",
                     type(exc).__name__)
            break  # 后端不可用/模型未挂载：后面同样失败，别再烧时间
    if done > 0:
        return done, "llama", 0.7
    return 0, "none", 0.3


def _crop_around(ev: Dict[str, Any]) -> Optional[str]:
    """点击现场帧裁剪成居中 PNG b64（VLM 只需看清控件，无需全屏）。"""
    try:
        from PIL import Image
        path = Path(str(ev["frame"]))
        img = Image.open(path).convert("RGB")
        # 帧来自 grab(0)（虚拟屏联合区）：帧内坐标 = 物理坐标 - 联合区原点
        v = screen_mss.virtual_screen()
        cx = int(ev["x"]) - int(v["left"])
        cy = int(ev["y"]) - int(v["top"])
        box = (max(0, cx - _CROP_HALF), max(0, cy - _CROP_HALF),
               min(img.width, cx + _CROP_HALF), min(img.height, cy + _CROP_HALF))
        if box[2] - box[0] < 16 or box[3] - box[1] < 16:
            return None
        buf = io.BytesIO()
        img.crop(box).save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:  # noqa: BLE001
        return None


def _describe(actions: List[Dict[str, Any]], annotated: int) -> str:
    n_click = sum(1 for a in actions if a.get("action") == "click")
    n_type = sum(1 for a in actions if a.get("action") == "type")
    targets = [a["target_text"] for a in actions
               if a.get("target_text") and a["target_text"] != "未知"]
    head = f"{n_click} clicks, {n_type} text inputs"
    if targets:
        head += "; targets: " + "、".join(dict.fromkeys(targets))[:120]
    if annotated == 0 and n_click > 0:
        head += "（未标注：VLM 不可用，仅坐标可回放）"
    return head


# ---------------- 回放 ----------------

# 回放时步骤间最小间隔（保证 UI 状态稳定）与最大跟随间隔（录制停顿不无限放大）
_REPLAY_MIN_GAP = 0.25
_REPLAY_MAX_GAP = 1.5


def run_skill(skill_ref: str, capabilities: Optional[Dict[str, Any]] = None,
              continue_on_error: bool = False) -> Dict[str, Any]:
    """按 id 或 name 回放技能：逐步走 controller.run_action 同一安全管道。

    默认严格：任一步 ActionError → 立即终止并如实返回失败步与错误
    （绝不跳过/兜底点击）。
    continue_on_error=true（行为克隆噪声容忍）：失败步记账后继续后续步骤——
    录制里混入的人类鼠标噪声点击会被能力令牌白名单在锁前拒绝（零输入），
    真正的目标步骤照常执行；结果里 failed_steps 如实列出每一步失败。
    """
    from . import controller, environment
    skill = skill_store.get_skill(skill_ref) or skill_store.get_skill_by_name(skill_ref)
    if not skill:
        raise RecorderError(f"skill not found: {skill_ref!r}")
    environment.assert_input_allowed()  # 与录制时同一桌面（锁屏/安全桌面拒回放）
    actions = skill.get("actions") or []
    t_prev: Optional[float] = None
    # type/key 的坐标锚点 = 最近一次点击；技能开头就是打字时用当前光标位
    # （绝不传 0,0——那是 Fail-Safe 角，且会把光标甩到屏幕外角）。
    from . import backend as _backend
    anchor: Tuple[int, int] = tuple(_backend.position())  # type: ignore[assignment]
    ok = 0
    failed: List[Dict[str, Any]] = []
    for i, a in enumerate(actions):
        t = a.get("t")
        if t_prev is not None and t is not None:
            gap = min(max(float(t) - t_prev, _REPLAY_MIN_GAP), _REPLAY_MAX_GAP)
            time.sleep(gap)
        t_prev = float(t) if t is not None else t_prev
        if a.get("action") == "click" and "x" in a:
            anchor = (int(a["x"]), int(a["y"]))
        payload = _replay_payload(a, capabilities, anchor)
        if payload is None:
            continue  # 无回放语义的动作（如未知类型）：跳过不计失败
        try:
            controller.run_action(payload)
            ok += 1
        except Exception as exc:  # noqa: BLE001 ActionError 家族统一收口
            if not continue_on_error:
                return {"ok": False, "skill_id": skill["id"], "name": skill["name"],
                        "steps_total": len(actions), "steps_ok": ok,
                        "failed_at": i, "failed_action": a,
                        "error": type(exc).__name__, "detail": str(exc)[:300]}
            failed.append({"index": i, "action": a.get("action"),
                           "error": type(exc).__name__,
                           "detail": str(exc)[:200]})
        time.sleep(0.1)  # 步后 settle，等界面消化
    return {"ok": not failed, "skill_id": skill["id"], "name": skill["name"],
            "steps_total": len(actions), "steps_ok": ok,
            "failed_steps": failed,
            "skipped": len(actions) - ok - len(failed)}


def _replay_payload(a: Dict[str, Any], capabilities: Optional[Dict[str, Any]],
                    anchor: Tuple[int, int]) -> Optional[Dict[str, Any]]:
    """技能事件 → run_action payload；无语义的动作返回 None（跳过）。"""
    act = a.get("action")
    base: Dict[str, Any] = {"monitor": 1}
    if capabilities:
        base["capabilities"] = capabilities
    if act == "click":
        btn = str(a.get("button", "left"))
        x, y = int(a["x"]), int(a["y"])
        if btn == "right":
            base.update({"action": "right_click", "x": x, "y": y})
        else:
            # middle 等无对应动作语义的按钮：按左键记录的历史仍回放左键
            base.update({"action": "click", "x": x, "y": y})
        return base
    if act == "type":
        text = str(a.get("text", ""))
        return dict(base, action="type", text=text,
                    x=anchor[0], y=anchor[1]) if text else None
    if act == "key":
        key = str(a.get("key", ""))
        return dict(base, action="hotkey", text=key,
                    x=anchor[0], y=anchor[1]) if key else None
    if act == "scroll":
        return dict(base, action="scroll", x=int(a["x"]), y=int(a["y"]),
                    dy=int(a.get("dy", 0)))
    return None


# ---------------- 帧清理 ----------------

def _prune_old_frames() -> None:
    """skill_frames/ 下超过 _FRAME_KEEP_DAYS 未动的会话目录整体删除。"""
    try:
        if not FRAMES_DIR.is_dir():
            return
        cutoff = time.time() - _FRAME_KEEP_DAYS * 86400
        for d in FRAMES_DIR.iterdir():
            if d.is_dir() and d.stat().st_mtime < cutoff:
                for f in d.glob("*.jpg"):
                    try:
                        f.unlink()
                    except OSError:
                        pass
                try:
                    d.rmdir()
                except OSError:
                    pass  # 目录非空/正被读取：留给下次
    except OSError as exc:
        LOG.warning("skill frame prune failed: %s", exc)


def delete_skill_frames(skill: Dict[str, Any]) -> None:
    """删除技能引用的所有帧文件（技能删除时调用，保持 data/ 干净）。"""
    try:
        seen = set()
        for a in skill.get("actions") or []:
            fp = a.get("frame")
            if fp and fp not in seen:
                seen.add(fp)
                Path(str(fp)).unlink(missing_ok=True)
        for fp in seen:
            p = Path(str(fp)).parent
            if p.is_dir() and not any(p.iterdir()):
                p.rmdir()
    except OSError as exc:
        LOG.warning("skill frame cleanup failed: %s", exc)
