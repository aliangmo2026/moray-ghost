"""v0.15 阶段三验收：自我进化技能库（pynput 录制 → VLM 标注 → SQLite → 回放）。

场景（按执行顺序）：
  U 单元（进程内，零服务；GHOST_SKILL_ANNOTATE_CALLS=0 防测试进程加载 llama）：
    U1 skill_store 全 CRUD + 重名拒绝 + actions JSON 往返
    U2 _replay_payload 事件→payload 映射（含 type/key 锚点、right_click、未知跳过）
    U3 录制状态机：未录制 stop → RecorderError；重复 start → RecorderError
    U4 进程内真实录制往返：start → pyautogui 点击安全点 → stop →
       技能含该坐标 click + 帧文件真实存在（子集断言：容忍用户鼠标噪声）
  A 端到端（自建 Tk 靶窗，HTTP 全链路）：
    A-1 stop-before-start → 409；双 start → 409
    A-2 record start → recording=true
    A-3 /execute_action 点 Alpha、Beta + 打字 "hello"（ghost 物理输入=被录制）
    A-4 record stop → 技能入库：click(Alpha/Beta 坐标子集) + type("hello") +
       帧文件存在 + VLM 标注诚实性（llama→有 target_text；none→confidence=0.3）
    A-5 /skill/list + /skill/{id} 可查
    A-6 /skill/run → 回放走 run_action 全门禁，marker 新增 Alpha、Beta 两行
    A-7 DELETE → 列表消失 + 帧目录清理
  F 清场：停服务、关靶窗（只关自己 hwnd）、光标归位、打印总结。

铁律：靶窗自建 + 只关自己 hwnd；回放期间屏幕不应有真实弹窗。
环境要求：真实可见桌面（无锁屏）。
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

# 测试进程内 U4 录制不触发 VLM（stop 时加载 llama 太重；E2E 在服务进程真标注）
os.environ["GHOST_SKILL_ANNOTATE_CALLS"] = "0"

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

from app.core import config, skill_recorder, skill_store  # noqa: E402

TOKEN = config.ensure_token()
H = {"X-Ghost-Token": TOKEN}
TMP = Path(tempfile.gettempdir())
RESULTS: list = []
PROCS: list = []
STEM = "v15sk" + str(int(time.time()) % 100000)


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


def wait_ocr_warmed(timeout: float = 60.0) -> bool:
    """等服务端 OCR 预热完成（ONNX 冷启动 15-25s；不等则首击顶穿硬超时）。"""
    log = config.LOG_DIR / "ghost.log"
    end = time.time() + timeout
    marker = time.time() - 5  # 只认最近启动的预热行
    while time.time() < end:
        try:
            for ln in log.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]:
                if "ocr warmed" in ln:
                    try:
                        ts = ln.split(" ", 2)[:2]
                        import datetime as _dt
                        t = _dt.datetime.strptime(" ".join(ts), "%Y-%m-%d %H:%M:%S,%f")
                        if t.timestamp() >= marker:
                            return True
                    except ValueError:
                        pass
        except OSError:
            pass
        time.sleep(0.5)
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


def point_owns(pt: tuple, want_hwnd: int) -> bool:
    """点 pt 的最顶层窗口根 hwnd 是否就是 want_hwnd（点击前所有权复核）。"""
    user32 = ctypes.windll.user32
    leaf = user32.WindowFromPoint(ctypes.wintypes.POINT(int(pt[0]), int(pt[1])))
    if not leaf:
        return False
    return int(user32.GetAncestor(leaf, 2)) == int(want_hwnd)


def ensure_clickable(pt: tuple, want_hwnd: int, tries: int = 6) -> bool:
    """重抢前台直到点击落点确实属于靶窗（用户正在用电脑时的自愈）。"""
    for _ in range(tries):
        if point_owns(pt, want_hwnd):
            return True
        force_foreground(want_hwnd)
        time.sleep(0.4)
    return point_owns(pt, want_hwnd)


def marker_lines(path: Path) -> list:
    try:
        return [ln.strip() for ln in path.read_text(
            encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return []


def act_post(path: str, payload: dict, timeout: float = 15):
    return requests.post(f"http://127.0.0.1:8030/api/ghost{path}",
                         headers=H, json=payload, timeout=timeout)


def main() -> int:
    skill_store.init_db()
    cur = cursor_pos()

    # ---------------- U1: skill_store CRUD ----------------
    name_u = "ut_" + STEM
    try:
        sk = skill_store.save_skill(name_u, "unit test skill",
                                    [{"action": "click", "x": 1, "y": 2}],
                                    0.5, "none")
        ok = (skill_store.get_skill(sk["id"]) is not None
              and skill_store.get_skill_by_name(name_u)["id"] == sk["id"]
              and skill_store.get_skill(sk["id"])["actions"] == [
                  {"action": "click", "x": 1, "y": 2}]
              and any(s["id"] == sk["id"] for s in skill_store.list_skills()))
        check("U1 store roundtrip (save/get/by_name/actions/list)", ok, sk["id"])
        try:
            skill_store.save_skill(name_u, "dup", [], 0.0, "none")
            check("U1b 重名拒绝 SkillNameConflictError", False, "no exception")
        except skill_store.SkillNameConflictError:
            check("U1b 重名拒绝 SkillNameConflictError", True)
        check("U1c delete_skill", skill_store.delete_skill(sk["id"])
              and skill_store.get_skill(sk["id"]) is None)
    except Exception as exc:  # noqa: BLE001
        check("U1 store roundtrip", False, f"{type(exc).__name__}: {exc}")

    # ---------------- U2: _replay_payload 映射 ----------------
    try:
        p1 = skill_recorder._replay_payload(
            {"action": "click", "x": 10, "y": 20, "button": "left"}, None, (5, 5))
        p2 = skill_recorder._replay_payload(
            {"action": "click", "x": 10, "y": 20, "button": "right"}, None, (5, 5))
        p3 = skill_recorder._replay_payload(
            {"action": "type", "text": "hi"}, None, (77, 88))
        p4 = skill_recorder._replay_payload(
            {"action": "key", "key": "enter"}, None, (77, 88))
        p5 = skill_recorder._replay_payload(
            {"action": "scroll", "x": 1, "y": 2, "dy": -3}, None, (0, 0))
        p6 = skill_recorder._replay_payload({"action": "weird"}, None, (0, 0))
        ok = (p1["action"] == "click" and p1["x"] == 10
              and p2["action"] == "right_click"
              and p3["action"] == "type" and p3["text"] == "hi"
              and p3["x"] == 77 and p3["y"] == 88  # 锚点=最近点击，绝非 0,0
              and p4["action"] == "hotkey" and p4["text"] == "enter"
              and p5["action"] == "scroll" and p5["dy"] == -3
              and p6 is None)
        check("U2 replay payload 映射（type 锚点≠0,0）", ok,
              f"p3={p3} p6={p6}")
    except Exception as exc:  # noqa: BLE001
        check("U2 replay payload 映射", False, f"{type(exc).__name__}: {exc}")

    # ---------------- U3: 录制状态机 ----------------
    try:
        skill_recorder.stop()  # 未录制
        check("U3a 未录制 stop → RecorderError", False, "no exception")
    except skill_recorder.RecorderError:
        check("U3a 未录制 stop → RecorderError", True)
    except Exception as exc:  # noqa: BLE001
        check("U3a 未录制 stop → RecorderError", False, f"{type(exc).__name__}")
    u4_skill = None
    try:
        skill_recorder.start()
        check("U3b start 成功 + status.recording", skill_recorder.is_recording())
        try:
            skill_recorder.start()
            check("U3c 重复 start → RecorderError", False, "no exception")
        except skill_recorder.RecorderError:
            check("U3c 重复 start → RecorderError", True)
    except Exception as exc:  # noqa: BLE001
        check("U3b start 成功 + status.recording", False,
              f"{type(exc).__name__}: {exc}")

    # ---------------- U4: 进程内真实录制往返 ----------------
    # 靶窗先建（U4 点它的 Gamma，绝不点用户桌面任意位置）；A 段复用同一靶窗。
    marker = TMP / f"_v15_skill_marker_{STEM}.txt"
    marker.unlink(missing_ok=True)
    tgt = spawn([sys.executable, str(ROOT / "scripts" / "_v15_skill_target.py"),
                 str(marker)], drain=False)
    line = tgt.stdout.readline().strip()
    payload = json.loads(line[len("READY "):]) if line.startswith("READY ") else {}
    hwnd = int(payload.get("hwnd") or 0)
    _TGT_HREF["hwnd"] = hwnd
    centers = payload.get("centers") or {}
    check("U4-0 靶窗就绪 + 三按钮中心", hwnd != 0 and len(centers) == 3,
          f"hwnd={hwnd} centers={centers}")
    force_foreground(hwnd)

    if skill_recorder.is_recording():
        try:
            import pyautogui
            cx, cy = centers["Gamma"]  # 点自家靶窗按钮，绝不点用户桌面
            pyautogui.moveTo(cx, cy, duration=0)
            ax, ay = pyautogui.position()  # 读回真实落点（测试进程 DPI 换算容差）
            pyautogui.click()
            time.sleep(0.4)  # 等截图节流窗口 + 监听回调落账
            res = skill_recorder.stop(name=name_u + "_rec")
            u4_skill = res.get("skill")
            acts = (u4_skill or {}).get("actions", [])
            click_ok = any(a.get("action") == "click"
                           and abs(a.get("x", -9999) - ax) <= 2
                           and abs(a.get("y", -9999) - ay) <= 2 for a in acts)
            frame_ok = any(a.get("frame") and Path(a["frame"]).is_file()
                           for a in acts if a.get("action") == "click")
            check("U4 进程内录制：点击坐标入技能（子集）", click_ok,
                  f"actions={[(a.get('action'), a.get('x'), a.get('y')) for a in acts][:6]}")
            check("U4b 点击现场帧文件真实存在", frame_ok)
            check("U4c 诚实降级（测试进程 annotate=0）",
                  u4_skill and u4_skill.get("annotation_backend") == "none"
                  and u4_skill.get("confidence") == 0.3,
                  u4_skill and u4_skill.get("annotation_backend"))
        except Exception as exc:  # noqa: BLE001
            check("U4 进程内录制往返", False, f"{type(exc).__name__}: {exc}")
        finally:
            if u4_skill:
                try:
                    skill_recorder.delete_skill_frames(u4_skill)
                    skill_store.delete_skill(u4_skill["id"])
                except Exception:  # noqa: BLE001
                    pass
    restore_cursor(cur)

    # ---------------- A: HTTP 端到端（复用 U4 靶窗） ----------------
    kill_by_port()
    # 测试进程为了 U4 轻量把 ANNOTATE_CALLS 设了 0；服务子进程要真标注 → 显式恢复
    server = spawn([sys.executable, str(ROOT / "run.py")],
                   env_extra={"GHOST_SKILL_ANNOTATE_CALLS": "6"})
    if not wait_health():
        check("A-0 服务启动", False, "health timeout")
        return _finish(cur, None, None)
    check("A-0 服务启动", True)
    warmed = wait_ocr_warmed()
    check("A-0d OCR 预热完成（防首击冷启动顶穿硬超时）", warmed)
    fg_ok = force_foreground(hwnd)
    check("A-0c 靶窗前台", fg_ok)

    skill_name = "e2e_" + STEM
    rec_sid = None
    try:
        # A-1 状态机 HTTP 语义
        r = act_post("/skill/record/stop", {})
        check("A-1 stop-before-start → 409", r.status_code == 409, r.status_code)
        r = act_post("/skill/record/start", {})
        check("A-1b 首次 start → recording", r.status_code == 200
              and r.json().get("recording"), r.status_code)
        rec_sid = r.json().get("session_id")
        r2 = act_post("/skill/record/start", {})
        check("A-1c 双 start → 409", r2.status_code == 409, r2.status_code)

        # A-3 ghost 物理输入 = 被录制的人类操作等价物
        # （最小权限：capabilities 只声明靶窗 hwnd——与宏回放同一语义）
        alpha, beta = centers.get("Alpha"), centers.get("Beta")
        caps = {"allowed_windows": ["python.exe"]}  # 靶窗进程（最小权限）
        for pt in (alpha, beta):
            if not ensure_clickable(pt, hwnd):
                check("A-3 点击按钮", False,
                      f"target window no longer owns {pt} (user activity?)")
                continue
            r = act_post("/execute_action",
                         {"action": "click", "x": pt[0], "y": pt[1],
                          "monitor": 1, "capabilities": caps}, timeout=30)
            if r.status_code != 200:
                check("A-3 点击按钮", False, f"http={r.status_code} {r.text[:200]}")
        time.sleep(0.3)
        r = act_post("/execute_action",
                     {"action": "type", "text": "hello",
                      "x": alpha[0], "y": alpha[1], "monitor": 1,
                      "capabilities": caps}, timeout=20)
        check("A-3 采集点击+打字完成", r.status_code == 200, r.status_code)
        time.sleep(0.8)  # 等最后一个 click 截图/回调落账

        # A-4 stop → 技能（llama 标注在服务进程真实发生，首载可能 ~1min）
        r = act_post("/skill/record/stop", {"name": skill_name}, timeout=300)
        check("A-4 stop → 200 技能入库", r.status_code == 200,
              f"http={r.status_code} {r.text[:200]}")
        if r.status_code != 200:
            return _finish(cur, server, tgt)
        skill = r.json()["skill"]
        acts = skill["actions"]
        click_names = []
        for a in acts:
            if a.get("action") != "click":
                continue
            for nm, pt in centers.items():
                if abs(a["x"] - pt[0]) <= 2 and abs(a["y"] - pt[1]) <= 2:
                    click_names.append(nm)
        check("A-4a Alpha/Beta 点击被录制（子集）",
              "Alpha" in click_names and "Beta" in click_names,
              f"matched={click_names} total_clicks="
              f"{sum(1 for a in acts if a.get('action') == 'click')}")
        typed = [a for a in acts if a.get("action") == "type"
                 and "hello" in str(a.get("text", ""))]
        check("A-4b 打字 'hello' 被录制", bool(typed), acts[:8])
        frames = [a["frame"] for a in acts
                  if a.get("action") == "click" and a.get("frame")]
        check("A-4c 点击帧文件存在", bool(frames) and Path(frames[0]).is_file(),
              frames[:2])
        anno = skill.get("annotation_backend")
        if anno == "llama":
            hits = [a for a in acts if a.get("target_text")]
            check("A-4d VLM 真标注（llama→有 target_text）", bool(hits),
                  [a.get("target_text") for a in acts
                   if a.get("action") == "click"][:6])
        else:
            check("A-4d VLM 诚实降级（none→confidence=0.3）",
                  anno == "none" and skill.get("confidence") == 0.3,
                  f"anno={anno} conf={skill.get('confidence')}")

        # A-5 查询
        r = requests.get("http://127.0.0.1:8030/api/ghost/skill/list",
                         headers=H, timeout=10)
        in_list = any(s["name"] == skill_name for s in r.json().get("skills", []))
        check("A-5a /skill/list 含新技能", r.status_code == 200 and in_list)
        r = requests.get(f"http://127.0.0.1:8030/api/ghost/skill/{skill['id']}",
                         headers=H, timeout=10)
        check("A-5b /skill/{id} 可查且动作一致",
              r.status_code == 200 and r.json()["skill"]["actions"] == acts)

        # A-6 回放（marker 差集证明真实点击落进靶窗；capabilities=靶窗最小权限；
        # continue_on_error=录制可能混入用户鼠标噪声点击，被能力白名单零输入拒绝）
        before = marker_lines(marker)
        r = act_post("/skill/run", {"name": skill_name, "capabilities": caps,
                                    "continue_on_error": True}, timeout=120)
        after = marker_lines(marker)
        new = after[len(before):]
        check("A-6a 回放 200 + steps_ok≥3（噪声步零输入跳过）",
              r.status_code == 200 and r.json().get("steps_ok", 0) >= 3,
              r.text[:260])
        check("A-6b marker 新增 Alpha→Beta 两行（真实点击）",
              new[:2] == ["Alpha", "Beta"], f"new={new}")

        # A-7 删除 + 帧清理
        r = requests.delete(f"http://127.0.0.1:8030/api/ghost/skill/{skill['id']}",
                            headers=H, timeout=10)
        r2 = requests.get(f"http://127.0.0.1:8030/api/ghost/skill/{skill['id']}",
                          headers=H, timeout=10)
        frame_dir = Path(frames[0]).parent if frames else None
        check("A-7 DELETE → 200 且 404 + 帧目录清理",
              r.status_code == 200 and r2.status_code == 404
              and (frame_dir is None or not frame_dir.exists()),
              f"del={r.status_code} get={r2.status_code} dir_exists="
              f"{frame_dir and frame_dir.exists()}")
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        check("A 端到端（未预期异常）", False, f"{type(exc).__name__}: {exc}")
    finally:
        # 收尾：若录制还挂着，停掉（不入库）
        try:
            st = requests.get("http://127.0.0.1:8030/api/ghost/skill/record/status",
                              headers=H, timeout=5).json()
            if st.get("recording"):
                requests.post("http://127.0.0.1:8030/api/ghost/skill/record/stop",
                              headers=H, json={"name": "cleanup_" + STEM},
                              timeout=30)
                from app.core import skill_store as _ss
                for s in _ss.list_skills():
                    if s["name"] == "cleanup_" + STEM:
                        _ss.delete_skill(s["id"])
        except Exception:  # noqa: BLE001
            pass

    return _finish(cur, server, tgt)


def _finish(cur: tuple, server, tgt) -> int:
    if tgt:
        close_hwnd(int(tgt_pid_hwnd(tgt)))
    if server:
        stop_proc(server)
    for p in PROCS:
        if p.poll() is None:
            try:
                p.terminate()
            except Exception:  # noqa: BLE001
                pass
    kill_by_port()
    restore_cursor(cur)
    marker_unlink()
    total = len(RESULTS)
    passed = sum(1 for r in RESULTS if r)
    failed = total - passed
    print(f"\n==== v15_skills: {passed}/{total} PASS, {failed} FAIL ====")
    return 0 if failed == 0 else 1


_TGT_HREF = {"hwnd": 0}


def tgt_pid_hwnd(tgt) -> int:
    return _TGT_HREF["hwnd"]


def marker_unlink() -> None:
    try:
        (TMP / f"_v15_skill_marker_{STEM}.txt").unlink(missing_ok=True)
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
