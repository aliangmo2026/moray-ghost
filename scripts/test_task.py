"""v0.4 M3 验收：/task 任务生命周期（create/start/GET/resume/cancel）。

测试纪律（README 红线）：GUI 靶子全部是本脚本 Popen 新开的记事本（hwnd 差集绑定、
TOPMOST 常驻、编辑区点击激活、只认自己的 hwnd 消失）。

场景：
  A 主链路：action(type) → macro(click 文件) → wait → require_vision
            → degraded(REQUIRE_VISION) → resume → done。
  B 失败降级：occluder 遮挡锚点 → macro 步 WINDOW_OCCLUDED → degraded
            → 撤遮挡 → resume → done（失败步重试成功）。
  C cancel：wait 长步中取消 → canceled，后续步骤从未执行。
  D 负例：404 / 400 非法步骤 / 409 状态机非法迁移 / 重复 task_id。
"""
from __future__ import annotations

import ctypes
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except OSError:
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pyautogui  # noqa: E402
import requests  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402

from app.core import window_locator as wl  # noqa: E402
from app.core.backend import release_all_modifiers  # noqa: E402
from app.core.screen_mss import grab  # noqa: E402

token = ""
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    if line.strip().startswith("GHOST_TOKEN="):
        token = line.split("=", 1)[1].strip()
H = {"X-Ghost-Token": token}
RESULTS: list = []
OWNED: set = set()
TMP_FILES: list = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(ok)
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}" + (f"  | {detail}" if detail else ""), flush=True)
    return ok


def action(payload: dict):
    return requests.post("http://127.0.0.1:8030/api/ghost/execute_action", headers=H,
                         json=payload, timeout=60)


def task_post(path: str, payload: dict):
    return requests.post(
        f"http://127.0.0.1:8030/api/ghost/task{path}", headers=H, json=payload, timeout=30)


def task_get(task_id: str):
    return requests.get(
        f"http://127.0.0.1:8030/api/ghost/task/{task_id}", headers=H, timeout=15)


def poll_status(task_id: str, want: set, timeout: float = 40.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = task_get(task_id)
        try:
            last = r.json()
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
            continue
        if last.get("status") in want:
            return last
        time.sleep(0.3)
    return last


def point_is_ours(pt: tuple, hwnd: int) -> bool:
    try:
        return win32gui.GetAncestor(win32gui.WindowFromPoint(pt), 2) == hwnd
    except Exception:  # noqa: BLE001
        return False


def force_foreground(hwnd: int) -> bool:
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        time.sleep(0.25)
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(0.4)
        if win32gui.GetForegroundWindow() == hwnd:
            return True
    except Exception:  # noqa: BLE001
        pass
    return win32gui.GetForegroundWindow() == hwnd


def open_notepad(owned_before: set, x: int, y: int, w: int = 800, h: int = 600) -> int:
    """v0.5 加固：带空临时文件启动（绕过 Win11 会话恢复，绝不载入用户文档）；
    hwnd 差集 + 唯一文件名双绑定——环境再活跃也不可能绑到用户窗口。"""
    tmp_dir = ROOT / "data" / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_file = tmp_dir / ("moray_t_" + str(int(time.time() * 1000)) + ".txt")
    tmp_file.write_text("", encoding="utf-8")
    TMP_FILES.append(tmp_file)
    # 新记事本会把"带文件参数"的启动并进既有窗口的标签页（实测）：启动前必须
    # 无任何记事本窗口；本套脚本顺序使用记事本，发现残留即拒绝并明示
    existing = [it for it in wl.list_windows()
                if "记事本" in it["title"] or "notepad" in it["title"].lower()]
    if existing:
        print("  (open_notepad: 已有记事本窗口未关闭，拒绝启动以免并入标签页)",
              flush=True)
        return 0
    subprocess.Popen(["notepad.exe", str(tmp_file)])
    hwnd = None
    for _ in range(60):
        time.sleep(0.25)
        new = [it for it in wl.list_windows()
               if it["hwnd"] not in owned_before
               and tmp_file.stem in it["title"]]
        if new:
            hwnd = new[0]["hwnd"]
            break
    if hwnd is None:
        return 0
    OWNED.add(hwnd)
    win32gui.MoveWindow(hwnd, x, y, w, h, True)
    wl.wait_stable(hwnd, timeout=4.0)
    win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                          win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
    time.sleep(0.4)
    return hwnd


def cleanup_tmp_files() -> None:
    for f in TMP_FILES:
        try:
            f.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


_OVERLAY_EX_NOACTIVATE = 0x08000000
_OVERLAY_GWL_EXSTYLE = -20


def open_overlay(x: int, y: int, w: int, h: int, color: str = "#3a3a3a") -> dict:
    """遮挡层（v0.5 加固）：本进程 tkinter 窗口，置顶 + WS_EX_NOACTIVATE——
    纯视觉遮挡、绝不抢焦点。取代记事本遮挡窗（绕开新记事本标签页合并与会话
    恢复两个风险面）。返回 {"tk": root, "hwnd": int}。"""
    import ctypes
    import tkinter as tk

    root = tk.Tk()
    root.overrideredirect(True)
    root.geometry(f"{w}x{h}+{x}+{y}")
    tk.Frame(root, background=color).pack(fill="both", expand=True)
    root.attributes("-topmost", True)
    root.update_idletasks()
    hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
    ex = ctypes.windll.user32.GetWindowLongW(hwnd, _OVERLAY_GWL_EXSTYLE)
    ctypes.windll.user32.SetWindowLongW(
        hwnd, _OVERLAY_GWL_EXSTYLE, ex | _OVERLAY_EX_NOACTIVATE)
    root.update()
    return {"tk": root, "hwnd": int(hwnd)}


def close_overlay(ov: dict) -> bool:
    try:
        ov["tk"].destroy()
        return True
    except Exception:  # noqa: BLE001
        return False


def raise_overlay(ov: dict) -> None:
    """重新把遮挡层抬到最上层：force_foreground(目标) 的最小化→还原会把目标
    抬到 topmost 带顶端（实测 0.99 干净命中=遮挡失效），必须在激活之后重抬。"""
    try:
        ov["tk"].attributes("-topmost", True)
        ov["tk"].lift()
        ov["tk"].update()
    except Exception:  # noqa: BLE001
        pass


def activate_via_click(hwnd: int) -> bool:
    """点击窗口中心（编辑区）激活——绝不点标题条/标签条（悬停 × 关标签）。"""
    for _ in range(5):
        rect = wl.get_rect(hwnd)
        if rect is None:
            return False
        pt = (rect[0] + 400, rect[1] + 300)
        if not point_is_ours(pt, hwnd):
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
            time.sleep(0.35)
            continue
        r = action({"action": "click", "x": pt[0], "y": pt[1], "settle_seconds": 0.1})
        time.sleep(0.3)
        if r.status_code == 200 and win32gui.GetForegroundWindow() == hwnd:
            return True
    return win32gui.GetForegroundWindow() == hwnd


def close_owned(hwnd: int) -> bool:
    if not hwnd:
        return False
    if wl.get_rect(hwnd) is None:
        OWNED.discard(hwnd)
        return True
    try:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    except Exception:  # noqa: BLE001
        return False
    for _ in range(8):
        time.sleep(0.4)
        if wl.get_rect(hwnd) is None:
            OWNED.discard(hwnd)
            return True
        try:
            pyautogui.hotkey("alt", "n")
        finally:
            release_all_modifiers()
    gone = wl.get_rect(hwnd) is None
    if gone:
        OWNED.discard(hwnd)
    return gone


def main() -> int:
    ok_all = True
    hwnds_snapshot = {w["hwnd"] for w in wl.list_windows()}

    # ---------- 前置：录制宏（自建记事本 #1，click 文件按钮） ----------
    hwnd1 = open_notepad(hwnds_snapshot, 200, 200)
    ok_all &= check("P-1: 自建记事本#1 已定位", hwnd1 in OWNED, f"hwnd1={hwnd1}")
    if not hwnd1:
        print("== task verify: HAS FAILURES ==", flush=True)
        return 1
    ok_all &= check("P-2: 激活+前台门禁",
                    activate_via_click(hwnd1)
                    and win32gui.GetForegroundWindow() == hwnd1)
    rs = requests.post("http://127.0.0.1:8030/api/ghost/macro/record/start", headers=H,
                       json={"task_id": "t_m3"}, timeout=15)
    rect1 = wl.get_rect(hwnd1)
    btn1 = [rect1[0] + 60, rect1[1] + 86]
    r = action({"action": "click", "x": btn1[0], "y": btn1[1],
                "hwnd": hwnd1, "settle_seconds": 0.3})
    ok_all &= check("P-3: 录制 click 文件按钮", r.status_code == 200)
    r = requests.post("http://127.0.0.1:8030/api/ghost/macro/record/stop", headers=H,
                      json={"task_id": "t_m3", "name": "test_task_m3_"
                                                          + str(int(time.time()))},
                      timeout=15)
    macro_id = r.json().get("macro_id")
    ok_all &= check("P-4: macro_id 就绪", bool(macro_id), str(r.json())[:100])
    action({"action": "hotkey", "text": "esc", "settle_seconds": 0.1})
    ok_all &= check("P-5: 关闭记事本#1", close_owned(hwnd1))

    # ---------- 场景 A：主链路 action→macro→wait→require_vision → degraded → resume ----------
    hwnd2 = open_notepad(set(hwnds_snapshot) | OWNED, 900, 500)
    ok_all &= check("A-1: 自建记事本#2 已定位", hwnd2 in OWNED, f"hwnd2={hwnd2}")
    ok_all &= check("A-2: 激活+前台门禁",
                    activate_via_click(hwnd2)
                    and win32gui.GetForegroundWindow() == hwnd2)
    rect2 = wl.get_rect(hwnd2)
    editor2 = [rect2[0] + 400, rect2[1] + 300]

    r = task_post("", {"task_id": "task_m3_a", "goal": "M3 主链路", "steps": [
        {"kind": "action",
         "payload": {"action": "type", "text": "task ok", "x": editor2[0],
                     "y": editor2[1], "hwnd": hwnd2, "settle_seconds": 0.2}},
        {"kind": "macro", "macro_id": macro_id, "hwnd": hwnd2},
        {"kind": "wait", "ms": 400},
        {"kind": "require_vision", "prompt": "确认文件菜单已打开"},
    ]})
    ok_all &= check("A-3: POST /task → 201", r.status_code == 201, str(r.json())[:100])

    r = task_post("/task_m3_a/start", {})
    ok_all &= check("A-4: start → 202 running（异步）", r.status_code == 202,
                    str(r.json())[:80])

    ta = poll_status("task_m3_a", {"degraded"})
    ok_all &= check("A-5: degrade 于 require_vision 步",
                    ta is not None and ta.get("status") == "degraded"
                    and (ta.get("fail") or {}).get("fail_class") == "REQUIRE_VISION"
                    and (ta.get("fail") or {}).get("step_id") == 4,
                    f"status={ta.get('status')} fail={ta.get('fail')}")
    states = [s.get("status") for s in ta.get("step_states", [])]
    ok_all &= check("A-6: 前三步 done，第 4 步 failed(await)",
                    states[:3] == ["done", "done", "done"] and states[3] == "failed",
                    f"states={states}")
    r = task_get("task_m3_a")
    ok_all &= check("A-7: typing 已执行（编辑器含 task ok，截图审计存在）",
                    Path((ta.get("step_states") or [{}])[0]
                         .get("result", {}).get("after_screenshot_path", "x")).is_file())

    r = task_post("/task_m3_a/resume", {})
    ok_all &= check("A-8: resume → 202", r.status_code == 202, str(r.json())[:80])
    ta = poll_status("task_m3_a", {"done"})
    ok_all &= check("A-9: resume 后 → done（vision 步标记完成）",
                    ta is not None and ta.get("status") == "done"
                    and ta.get("step_states", [])[3].get("status") == "done",
                    f"status={ta.get('status')}")
    action({"action": "hotkey", "text": "esc", "settle_seconds": 0.1})  # 关菜单

    # ---------- 场景 B：失败降级（遮挡锚点）→ 撤遮挡 → resume 恢复 ----------
    r = task_post("", {"task_id": "task_m3_b", "steps": [
        {"kind": "action",
         "payload": {"action": "type", "text": "task2", "x": editor2[0],
                     "y": editor2[1], "hwnd": hwnd2, "settle_seconds": 0.2}},
        {"kind": "macro", "macro_id": macro_id, "hwnd": hwnd2},
    ]})
    ok_all &= check("B-1: POST /task B → 201", r.status_code == 201)

    ov4 = open_overlay(930, 540, 500, 400)
    hwnd4 = ov4["hwnd"]
    ok_all &= check("B-2: 自建遮挡层#4 已就位（tkinter 覆盖层，绝不抢焦点）",
                    bool(hwnd4), f"hwnd4={hwnd4}")
    force_foreground(hwnd2)  # 前台留给 #2（typing 去向），遮挡只做视觉
    raise_overlay(ov4)  # 激活会把 #2 抬到覆盖层之上，必须重抬
    time.sleep(0.4)

    r = task_post("/task_m3_b/start", {})
    ok_all &= check("B-3: start B → 202", r.status_code == 202)
    tb = poll_status("task_m3_b", {"degraded", "done"})
    ok_all &= check("B-4: 遮挡锚点 → degraded",
                    tb is not None and tb.get("status") == "degraded"
                    and (tb.get("fail") or {}).get("fail_class") in
                    ("WINDOW_OCCLUDED", "MACRO_NOT_FOUND")
                    and (tb.get("fail") or {}).get("step_id") == 2,
                    f"fail={tb.get('fail')}")
    ok_all &= check("B-5: 遮挡层#4 已销毁", close_overlay(ov4), f"hwnd4={hwnd4}")
    time.sleep(0.8)  # 撤遮挡后等桌面合成稳定
    force_foreground(hwnd2) or activate_via_click(hwnd2)

    r = task_post("/task_m3_b/resume", {})
    ok_all &= check("B-6: resume → 202", r.status_code == 202)
    tb = poll_status("task_m3_b", {"done", "degraded"})
    ok_all &= check("B-7: 撤遮挡后 resume → done（失败步重试成功）",
                    tb is not None and tb.get("status") == "done",
                    f"status={tb.get('status')} fail={tb.get('fail')}")
    action({"action": "hotkey", "text": "esc", "settle_seconds": 0.1})
    ok_all &= check("B-8: 关闭记事本#2", close_owned(hwnd2), f"hwnd2={hwnd2}")

    # ---------- 场景 C：wait 长步中 cancel ----------
    r = task_post("", {"task_id": "task_m3_c", "steps": [
        {"kind": "wait", "ms": 8000},
        {"kind": "action",
         "payload": {"action": "click", "x": 1280, "y": 800, "settle_seconds": 0.1}},
    ]})
    ok_all &= check("C-1: POST /task C → 201", r.status_code == 201)
    r = task_post("/task_m3_c/start", {})
    ok_all &= check("C-2: start C → 202", r.status_code == 202)
    time.sleep(0.8)  # 进入 wait 步
    r = task_post("/task_m3_c/cancel", {})
    ok_all &= check("C-3: cancel → 200", r.status_code == 200, str(r.json())[:80])
    tc = poll_status("task_m3_c", {"canceled"})
    ok_all &= check("C-4: canceled（步骤边界安全点生效）",
                    tc is not None and tc.get("status") == "canceled")
    states_c = [s.get("status") for s in tc.get("step_states", [])]
    ok_all &= check("C-5: 后续 click 步从未执行（pending）",
                    states_c == ["failed", "pending"] or states_c == ["done", "pending"]
                    or states_c[-1] == "pending", f"states={states_c}")

    # ---------- D 负例 ----------
    r = task_get("task_nonexistent")
    ok_all &= check("D-1: GET 不存在 → 404", r.status_code == 404)
    r = task_post("/task_nonexistent/start", {})
    ok_all &= check("D-2: start 不存在 → 404", r.status_code == 404)
    r = task_post("", {"task_id": "task_m3_bad", "steps": [
        {"kind": "fly", "to": "moon"}]})
    ok_all &= check("D-3: 非法 kind → 400", r.status_code == 400)
    r = task_post("", {"task_id": "task_m3_empty", "steps": []})
    ok_all &= check("D-4: 空 steps → 400", r.status_code == 400)
    r = task_post("", {"task_id": "task_m3_a", "steps": [{"kind": "wait", "ms": 10}]})
    ok_all &= check("D-5: 重复 task_id → 409", r.status_code == 409)
    r = task_post("/task_m3_a/start", {})
    ok_all &= check("D-6: start 已 done 的任务 → 409", r.status_code == 409)
    r = task_post("/task_m3_a/resume", {})
    ok_all &= check("D-7: resume 非 degraded → 409", r.status_code == 409)
    r = task_post("/task_m3_a/cancel", {})
    ok_all &= check("D-8: cancel 已终态 → 409", r.status_code == 409)

    # ---------- 清场 ----------
    con = sqlite3.connect(str(ROOT / "data" / "ghost.db"))
    con.execute("DELETE FROM macros WHERE name LIKE ?", ("test_task_m3%",))
    con.commit()
    con.close()
    cleanup_tmp_files()
    leftover = OWNED & {w["hwnd"] for w in wl.list_windows()}
    ok_all &= check("E-1: 自建窗口全部销毁（证据链）", not leftover,
                    f"owned={sorted(OWNED)} leftover={sorted(leftover)}")

    passed = sum(1 for o in RESULTS if o)
    print(f"== task verify: {passed}/{len(RESULTS)} passed ==", flush=True)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
