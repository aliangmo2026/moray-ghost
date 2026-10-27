"""v0.15 阶段一 System1 微型反射脑验收（自建 Tk 弹窗靶，绝不触碰真实软件）。

场景（按执行顺序）：
  U 单元判定（零服务）：
    U1 system1_match 命中（特征词+关闭按钮同窗共现）
    U2 仅特征词 → no_trigger（AND 语义）
    U3 仅按钮   → no_trigger（AND 语义）
    U4 普通文本 → no_trigger（防误伤正文）
    U5 超大"按钮"bbox → 尺寸启发排除 → no_button
    U6 标题触发词路径（title 命中 + 元素按钮）
    U7 反射对无效 hwnd 永不抛异常、零输入（window_gone）
    U8 配置关闭 → 反射立即返回 disabled（零扫描零输入）
  A 正向消解：前台真弹窗 + require_vision 任务 → 任务 done（不 degraded）、
    弹窗窗口销毁、marker=「关闭」（优先级验证）、步骤结果含 system1.dismissed、
    ghost.log 有 SYSTEM1_REFLEX dismissed、审计有 system1_dismiss。
  B 无弹窗回退：普通小窗（有关闭按钮无特征词）→ 照旧 degraded REQUIRE_VISION
    （fail.system1.dismissed=false）、窗口存活、无 marker。
  C UAC 拒绝：假「用户账户控制」窗（含特征词+关闭按钮）→ 拒绝（refused_uac）、
    窗口存活、无 marker、审计 system1_refused。
  D 黑名单跳过：标题含「删除」的窗 → 拒绝（refused_high_risk）、窗口存活。
  E 开关关闭：GHOST_SYSTEM1_AUTO_DISMISS=false 重启服务 → 弹窗照旧挂起
    （degraded、fail 无 system1 键、弹窗存活、无 marker、日志零 SYSTEM1_REFLEX）。
  F 清场：只关自己 hwnd、停服务、端口释放、鼠标归位 + 打印拦截日志取证。

铁律：靶窗自建 + hwnd 差集绑定；测试期间屏幕不应有真实弹窗（规则会真的去点）。
环境要求：**真实可见桌面**（无锁屏/无安全桌面）——锁屏下 OCR 读到的是锁屏像素，
正向场景 A 必挂（no_button 回退，零误点——run_action 落点归属检查兜底）；
拒绝路径（uac/blacklist，检查在 OCR 之前）与回退路径在锁屏下仍可验证。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except OSError:
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psutil  # noqa: E402
import requests  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402

from app.core import config, safety_gate, vl_driver  # noqa: E402

TOKEN = config.ensure_token()
H = {"X-Ghost-Token": TOKEN}
LOG_PATH = config.LOG_DIR / "ghost.log"
AUDIT_PATH = config.AUDIT_PATH
TMP = Path(tempfile.gettempdir())
RESULTS: list = []
PROCS: list = []
SERVER: dict = {"proc": None}


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  | {detail}" if detail else ""), flush=True)
    return bool(ok)


def port_free(port: int = 8030) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def kill_by_port(port: int = 8030) -> None:
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.laddr and c.laddr.port == port and c.pid:
                psutil.Process(c.pid).kill()
                time.sleep(0.6)
    except Exception as exc:  # noqa: BLE001
        print("  (kill_by_port: " + type(exc).__name__ + ")", flush=True)


def spawn(args: list, env_extra: dict | None = None,
          drain: bool = True) -> subprocess.Popen:
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.Popen(args, cwd=str(ROOT), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env,
                            text=True, encoding="utf-8", errors="replace")
    PROCS.append(proc)
    if not drain:
        return proc

    def _drain() -> None:
        try:
            for _ in proc.stdout:
                pass
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=_drain, daemon=True).start()
    return proc


def start_server(env_extra: dict | None = None) -> subprocess.Popen:
    return spawn([sys.executable, str(ROOT / "run.py")], env_extra)


def wait_health(timeout: float = 40.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = requests.get(
                "http://127.0.0.1:8030/api/ghost/health", timeout=2)
            if r.status_code == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.35)
    return False


def stop_proc(proc: subprocess.Popen, port: int | None = 8030) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=6)
    if port and not port_free(port):
        kill_by_port(port)


def cursor_pos() -> tuple:
    pt = ctypes.wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def restore_cursor(pos: tuple) -> None:
    try:
        ctypes.windll.user32.SetCursorPos(int(pos[0]), int(pos[1]))
    except Exception:  # noqa: BLE001
        pass


def force_foreground(hwnd: int) -> bool:
    for _ in range(5):
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
        except Exception:  # noqa: BLE001
            try:
                ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)
                ctypes.windll.user32.keybd_event(0x12, 0, 2, 0)
                win32gui.SetForegroundWindow(hwnd)
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.25)
        if win32gui.GetForegroundWindow() == hwnd:
            return True
    return False


def close_hwnd(hwnd: int) -> bool:
    if not hwnd or not win32gui.IsWindow(hwnd):
        return True
    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    for _ in range(25):
        time.sleep(0.2)
        if not win32gui.IsWindow(hwnd):
            return True
    return not win32gui.IsWindow(hwnd)


def spawn_popup(stem: str, mode: str, marker: Path) -> tuple:
    """弹窗靶窗：返回 (hwnd, Popen)；READY 握手。"""
    tgt = spawn([sys.executable,
                 str(ROOT / "scripts" / "_v15_popup_target.py"),
                 stem, mode, str(marker)], drain=False)
    line = tgt.stdout.readline().strip()
    payload = json.loads(line[len("READY "):]) if line.startswith(
        "READY ") else {}
    return int(payload.get("hwnd") or 0), tgt


def task_post(path: str, payload: dict):
    return requests.post(
        f"http://127.0.0.1:8030/api/ghost/task{path}", headers=H,
        json=payload, timeout=30)


def poll_status(task_id: str, want: set, timeout: float = 60.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = requests.get(f"http://127.0.0.1:8030/api/ghost/task/{task_id}",
                         headers=H, timeout=15)
        try:
            last = r.json()
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
            continue
        if last.get("status") in want:
            return last
        time.sleep(0.4)
    return last


def make_require_vision_task(tid: str) -> dict:
    r = task_post("", {"task_id": tid, "goal": "v15-system1",
                       "capabilities": {"allowed_windows": []},
                       "steps": [{"kind": "require_vision",
                                  "prompt": "确认弹窗已处理"}]})
    return r.json() | {"http_status": r.status_code}


def tail_new(path: Path, offset: int) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            return f.read()
    except OSError:
        return ""


def audit_events(offset: int) -> list:
    out = []
    for line in tail_new(AUDIT_PATH, offset).splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


# ---------------- U 单元判定（零服务） ----------------


def unit_tests() -> None:
    def el(text, x, y, w, h):
        return {"text": text, "type": "text", "bbox": [x, y, w, h],
                "center": [x + w // 2, y + h // 2]}

    popup = [el("每日提示：发现新版本", 10, 10, 260, 24),
             el("稍后", 40, 200, 70, 30),
             el("关闭", 150, 200, 70, 30)]
    m = safety_gate.system1_match(popup, title="")
    # trigger 按 SYSTEM1_TRIGGER_TOKENS 顺序取首个命中（"发现新版本"先于"每日提示"）
    check("U1 命中：特征词+关闭按钮同窗共现 + 关闭优先于稍后",
          m["matched"]
          and m["trigger"]["token"] in safety_gate.SYSTEM1_TRIGGER_TOKENS
          and m["buttons"][0]["text"] == "关闭",
          f"reason={m['reason']} trigger={m['trigger']} "
          f"btn0={m['buttons'][0]['text'] if m['buttons'] else None}")

    m2 = safety_gate.system1_match(
        [el("每日提示：发现新版本", 10, 10, 260, 24)], title="")
    check("U2 仅特征词（无按钮）→ no_button", not m2["matched"]
          and m2["reason"] == "no_button", f"reason={m2['reason']}")

    m3 = safety_gate.system1_match(
        [el("稍后", 40, 200, 70, 30), el("关闭", 150, 200, 70, 30)], title="")
    check("U3 仅按钮 → no_trigger", not m3["matched"]
          and m3["reason"] == "no_trigger", f"reason={m3['reason']}")

    m4 = safety_gate.system1_match(
        [el("用户名", 10, 10, 100, 20), el("登录", 40, 200, 70, 30)], title="")
    check("U4 普通文本 → no_trigger（防误伤）", not m4["matched"]
          and m4["reason"] == "no_trigger", f"reason={m4['reason']}")

    m5 = safety_gate.system1_match(
        [el("每日提示", 10, 10, 100, 20),
         el("关闭", 100, 100, 500, 120)], title="")
    check("U5 超大按钮 bbox → 尺寸启发排除", not m5["matched"]
          and m5["reason"] == "no_button", f"reason={m5['reason']}")

    m6 = safety_gate.system1_match(
        [el("关闭", 150, 200, 70, 30)], title="moray-v15 软件更新")
    check("U6 标题触发词路径", m6["matched"]
          and m6["trigger"]["source"] == "title"
          and m6["trigger"]["token"] == "软件更新",
          f"trigger={m6['trigger']}")

    out7 = vl_driver.system1_reflex(hwnd=0xFFFFFFF, reason="unit_invalid")
    check("U7 无效 hwnd → 不抛异常零输入",
          out7.get("dismissed") is False and out7.get("ok") is True,
          f"reason={out7.get('reason')}")

    saved = config.SYSTEM1_AUTO_DISMISS
    config.SYSTEM1_AUTO_DISMISS = False
    try:
        out8 = vl_driver.system1_reflex(reason="unit_disabled")
        check("U8 配置关闭 → disabled 零扫描",
              out8.get("enabled") is False and out8.get("dismissed") is False
              and out8.get("reason") == "disabled",
              f"reason={out8.get('reason')}")
    finally:
        config.SYSTEM1_AUTO_DISMISS = saved


# ---------------- 集成场景 ----------------


def scenario_normal(cursor0: tuple) -> None:
    marker = TMP / "v15_normal.marker"
    marker.unlink(missing_ok=True)
    hwnd, _tgt = spawn_popup("a" + str(int(time.time() * 1000))[-6:],
                             "normal", marker)
    check("A-1 normal 弹窗靶窗就位", hwnd > 0 and win32gui.IsWindow(hwnd),
          f"hwnd={hwnd}")
    if not hwnd:
        return
    force_foreground(hwnd)

    body = make_require_vision_task("task_v15_a")
    check("A-2 POST /task → 201", body.get("http_status") == 201,
          str(body)[:100])
    r = task_post("/task_v15_a/start", {})
    check("A-3 start → 202", r.status_code == 202, str(r.json())[:80])

    ta = poll_status("task_v15_a", {"done", "degraded"})
    check("A-4 任务 done（不 degraded）",
          ta is not None and ta.get("status") == "done",
          f"status={ta.get('status')} fail={ta.get('fail')}")
    states = (ta or {}).get("step_states") or []
    s1res = (states[0].get("result") or {}).get("system1") if states else None
    check("A-5 步骤结果含 system1.dismissed=true",
          bool(s1res) and s1res.get("dismissed") is True
          and s1res.get("trigger") in safety_gate.SYSTEM1_TRIGGER_TOKENS,
          f"system1={json.dumps(s1res, ensure_ascii=False)[:220] if s1res else None}")
    check("A-6 弹窗窗口已被销毁", not win32gui.IsWindow(hwnd),
          f"IsWindow={win32gui.IsWindow(hwnd)}")
    marker_txt = marker.read_text(encoding="utf-8").strip() if marker.exists() else ""
    check("A-7 marker=关闭（优先级验证：关闭先于稍后）",
          marker_txt == "关闭", f"marker={marker_txt!r}")
    close_hwnd(hwnd)  # 场景收尾：绝不把夹具留给后续场景当候选（污染候选序）


def scenario_no_popup() -> None:
    marker = TMP / "v15_benign.marker"
    marker.unlink(missing_ok=True)
    hwnd, _tgt = spawn_popup("b" + str(int(time.time() * 1000))[-6:],
                             "benign", marker)
    check("B-1 benign 窗就位", hwnd > 0 and win32gui.IsWindow(hwnd),
          f"hwnd={hwnd}")
    if not hwnd:
        return
    force_foreground(hwnd)
    body = make_require_vision_task("task_v15_b")
    check("B-2 POST /task → 201", body.get("http_status") == 201,
          str(body)[:100])
    r = task_post("/task_v15_b/start", {})
    check("B-3 start → 202", r.status_code == 202, str(r.json())[:80])
    tb = poll_status("task_v15_b", {"done", "degraded"})
    fail = (tb or {}).get("fail") or {}
    s1f = fail.get("system1") or {}
    check("B-4 照旧 degraded REQUIRE_VISION（回退旧路径）",
          tb is not None and tb.get("status") == "degraded"
          and fail.get("fail_class") == "REQUIRE_VISION",
          f"status={tb.get('status')} fail_class={fail.get('fail_class')}")
    # reason 不钉死：候选顺序随焦点变化（锁屏下 fg=LockApp，可能扫不到 benign 窗
    # 本身），只要"未消解 + 旧路径 degraded"即可证明 AND 语义与回退正确
    check("B-5 fail.system1.dismissed=false（无特征词不消解）",
          s1f.get("dismissed") is False,
          f"reason={s1f.get('reason')}")
    check("B-6 窗口存活 + 无 marker（未被点击）",
          win32gui.IsWindow(hwnd) and not marker.exists(),
          f"alive={win32gui.IsWindow(hwnd)} marker={marker.exists()}")
    close_hwnd(hwnd)


def _scenario_refused(mode: str, stem: str, tid: str,
                      want_reason: str) -> None:
    marker = TMP / f"v15_{mode}.marker"
    marker.unlink(missing_ok=True)
    hwnd, _tgt = spawn_popup(stem, mode, marker)
    check(f"{mode}-1 窗就位", hwnd > 0 and win32gui.IsWindow(hwnd),
          f"hwnd={hwnd}")
    if not hwnd:
        return
    force_foreground(hwnd)
    body = make_require_vision_task(tid)
    check(f"{mode}-2 POST /task → 201", body.get("http_status") == 201,
          str(body)[:100])
    r = task_post(f"/{tid}/start", {})
    check(f"{mode}-3 start → 202", r.status_code == 202, str(r.json())[:80])
    t = poll_status(tid, {"done", "degraded"})
    fail = (t or {}).get("fail") or {}
    s1f = fail.get("system1") or {}
    check(f"{mode}-4 照旧 degraded + 拒绝原因",
          t is not None and t.get("status") == "degraded"
          and s1f.get("reason") == want_reason,
          f"reason={s1f.get('reason')} status={t.get('status')}")
    check(f"{mode}-5 窗口存活 + 无 marker（零点击）",
          win32gui.IsWindow(hwnd) and not marker.exists(),
          f"alive={win32gui.IsWindow(hwnd)} marker={marker.exists()}")
    close_hwnd(hwnd)


def scenario_disabled() -> None:
    marker = TMP / "v15_disabled.marker"
    marker.unlink(missing_ok=True)
    hwnd, _tgt = spawn_popup("e" + str(int(time.time() * 1000))[-6:],
                             "normal", marker)
    check("E-1 弹窗就位（开关关闭时）", hwnd > 0 and win32gui.IsWindow(hwnd),
          f"hwnd={hwnd}")
    if not hwnd:
        return
    force_foreground(hwnd)
    body = make_require_vision_task("task_v15_e")
    check("E-2 POST /task → 201", body.get("http_status") == 201,
          str(body)[:100])
    r = task_post("/task_v15_e/start", {})
    check("E-3 start → 202", r.status_code == 202, str(r.json())[:80])
    te = poll_status("task_v15_e", {"done", "degraded"})
    fail = (te or {}).get("fail") or {}
    check("E-4 照旧 degraded 且 fail 无 system1 键（零行为变化）",
          te is not None and te.get("status") == "degraded"
          and "system1" not in fail,
          f"fail_keys={sorted(fail.keys())}")
    check("E-5 弹窗存活 + 无 marker",
          win32gui.IsWindow(hwnd) and not marker.exists(),
          f"alive={win32gui.IsWindow(hwnd)} marker={marker.exists()}")
    close_hwnd(hwnd)


def main() -> int:
    print("== moray-ghost v0.15 阶段一 System1 微型反射脑验收 ==", flush=True)
    cursor0 = cursor_pos()
    # Fail-Safe 预防：把鼠标停到屏幕中心（避免四角触发 pyautogui FailSafe）
    try:
        ctypes.windll.user32.SetCursorPos(960, 540)
    except Exception:  # noqa: BLE001
        pass
    log_off = LOG_PATH.stat().st_size if LOG_PATH.exists() else 0
    audit_off = AUDIT_PATH.stat().st_size if AUDIT_PATH.exists() else 0
    try:
        unit_tests()

        check("0.1 端口 8030 空闲", port_free())
        if not port_free():
            kill_by_port(8030)
        srv = start_server()
        SERVER["proc"] = srv
        check("0.2 服务启动（默认开关开）", wait_health())
        if srv.poll() is not None:
            return 1
        h = requests.get("http://127.0.0.1:8030/api/ghost/health",
                         timeout=5).json()
        s1h = h.get("system1") or {}
        check("0.3 /health 回显 system1.auto_dismiss=true",
              s1h.get("auto_dismiss") is True
              and s1h.get("version") == "v0.15",
              f"system1={json.dumps(s1h, ensure_ascii=False)}")

        scenario_normal(cursor0)
        scenario_no_popup()
        _scenario_refused("uac", "c" + str(int(time.time() * 1000))[-6:],
                          "task_v15_c", "refused_uac")
        _scenario_refused("blacklist",
                          "d" + str(int(time.time() * 1000))[-6:],
                          "task_v15_d", "refused_high_risk")

        # E：关开关重启服务
        check("E-0 停旧服务", True)
        stop_proc(SERVER["proc"])
        SERVER["proc"] = None
        check("E-0b 端口已释放", port_free(8030))
        srv2 = start_server({"GHOST_SYSTEM1_AUTO_DISMISS": "false"})
        SERVER["proc"] = srv2
        check("E-0c 服务重启（开关关）", wait_health())
        if srv2.poll() is not None:
            return 1
        h2 = requests.get("http://127.0.0.1:8030/api/ghost/health",
                          timeout=5).json()
        check("E-0d /health 回显 auto_dismiss=false",
              (h2.get("system1") or {}).get("auto_dismiss") is False,
              f"system1={json.dumps(h2.get('system1') or {}, ensure_ascii=False)}")
        scenario_disabled()
    finally:
        for p in PROCS:
            try:
                stop_proc(p, port=8030 if p is SERVER.get("proc") else None)
            except Exception:  # noqa: BLE001
                pass
        if not port_free(8030):
            kill_by_port(8030)
        check("F-1 服务停止、端口释放", port_free(8030))
        restore_cursor(cursor0)

    # 拦截日志取证
    log_new = tail_new(LOG_PATH, log_off)
    s1_lines = [ln for ln in log_new.splitlines() if "SYSTEM1_REFLEX" in ln]
    print("\n== SYSTEM1 拦截日志取证（ghost.log 新增行）==", flush=True)
    for ln in s1_lines:
        print("  " + ln.strip(), flush=True)
    events = audit_events(audit_off)
    s1_events = [e for e in events if str(e.get("event", "")).startswith(
        "system1")]
    print("== SYSTEM1 审计事件（ghost_audit.jsonl 新增）==", flush=True)
    for e in s1_events:
        slim = {k: e.get(k) for k in
                ("ts", "event", "trigger", "button", "reason", "hwnd",
                 "process", "verification", "source", "attempts") if k in e}
        print("  " + json.dumps(slim, ensure_ascii=False), flush=True)

    check("F-2 日志有 SYSTEM1_REFLEX dismissed 行",
          any("dismissed" in ln for ln in s1_lines),
          f"lines={len(s1_lines)}")
    check("F-3 审计有 system1_dismiss 事件",
          any(e.get("event") == "system1_dismiss" for e in s1_events),
          f"events={[e.get('event') for e in s1_events]}")
    check("F-4 审计有 system1_refused 事件（UAC/黑名单拒绝留痕）",
          any(e.get("event") == "system1_refused" for e in s1_events),
          f"events={[e.get('event') for e in s1_events]}")

    passed = sum(1 for x in RESULTS if x)
    print(f"\n== RESULT: {passed}/{len(RESULTS)} passed ==", flush=True)
    return len(RESULTS) - passed


if __name__ == "__main__":
    sys.exit(main())
