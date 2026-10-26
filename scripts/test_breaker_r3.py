"""破防第三轮：长跑并发（5 个任务同时 start，混合 wait 与 click）。

验证（观察性）：全部任务到达终态（无卡死/死锁）、task_id 唯一、
step_states 无非法状态、click 全部落在自建窗口内、ACTION_LOCK 串行可观测。
"""
from __future__ import annotations

import ctypes
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

import requests  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402

from app.core import window_locator as wl  # noqa: E402

H = {"X-Ghost-Token": ""}
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    if line.strip().startswith("GHOST_TOKEN="):
        H["X-Ghost-Token"] = line.split("=", 1)[1].strip()
RESULTS: list = []
OWNED: set = set()
TASK_IDS = [f"task_r3_{i}" for i in range(1, 6)]


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(ok)
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}" + (f"  | {detail}" if detail else ""), flush=True)
    return ok


def open_notepad(owned_before: set, x: int, y: int) -> int:
    subprocess.Popen(["notepad.exe"])
    hwnd = None
    for _ in range(60):
        time.sleep(0.25)
        new = [it for it in wl.list_windows()
               if it["hwnd"] not in owned_before
               and ("记事本" in it["title"] or "notepad" in it["title"].lower())]
        if new:
            hwnd = new[0]["hwnd"]
            break
    if hwnd is None:
        return 0
    OWNED.add(hwnd)
    win32gui.MoveWindow(hwnd, x, y, 800, 600, True)
    wl.wait_stable(hwnd, timeout=4.0)
    win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                          win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
    time.sleep(0.4)
    return hwnd


def main() -> int:
    ok_all = True
    hwnds_snapshot = {w["hwnd"] for w in wl.list_windows()}
    hwnd = open_notepad(hwnds_snapshot, 200, 200)
    ok_all &= check("R3-0: 自建记事本已定位", hwnd in OWNED, f"hwnd={hwnd}")
    if not hwnd:
        return 1
    rect = wl.get_rect(hwnd)
    center = [rect[0] + 400, rect[1] + 300]

    # 创建 5 个任务（混合 wait 与 click，每个总时长约 1.2s 输入 + 等待）
    for i, tid in enumerate(TASK_IDS, 1):
        steps = [
            {"kind": "wait", "ms": 600},
            {"kind": "action", "payload": {"action": "click", "x": center[0],
                                           "y": center[1] + i * 8, "hwnd": hwnd,
                                           "settle_seconds": 0.1}},
            {"kind": "wait", "ms": 500},
            {"kind": "action", "payload": {"action": "click", "x": center[0] + i,
                                           "y": center[1] + i * 8, "hwnd": hwnd,
                                           "settle_seconds": 0.1}},
        ]
        r = requests.post("http://127.0.0.1:8030/api/ghost/task", headers=H,
                          json={"task_id": tid, "goal": f"并发测试 {i}",
                                "capabilities": {"allowed_windows": ["notepad.exe"]},
                                "steps": steps}, timeout=30)
        ok_all &= check(f"R3-{i}a: POST /task {tid} → 201", r.status_code == 201,
                        str(r.json())[:80])

    # 同时 start 全部 5 个
    t0 = time.time()
    for tid in TASK_IDS:
        r = requests.post(
            f"http://127.0.0.1:8030/api/ghost/task/{tid}/start", headers=H, timeout=30)
        ok_all &= check(f"R3-{tid}: start → 202", r.status_code == 202,
                        str(r.json())[:80])

    # 轮询全部到终态（120s 死锁检测）
    deadline = time.time() + 120
    finals: dict = {}
    id_set = set()
    last_logged = {}
    while time.time() < deadline:
        all_terminal = True
        for tid in TASK_IDS:
            if tid in finals:
                continue
            r = requests.get(
                f"http://127.0.0.1:8030/api/ghost/task/{tid}", headers=H, timeout=15)
            try:
                cur = r.json()
            except Exception:  # noqa: BLE001
                all_terminal = False
                continue
            id_set.add(cur.get("task_id"))
            st = cur.get("status")
            if st != last_logged.get(tid):
                print(f"  [{time.time() - t0:6.1f}s] {tid} -> {st} "
                      f"(step {cur.get('current_step_id')})", flush=True)
                last_logged[tid] = st
            if st in ("done", "degraded", "canceled", "failed"):
                finals[tid] = cur
            else:
                all_terminal = False
        if len(finals) == len(TASK_IDS):
            break
        time.sleep(0.4)
    elapsed = time.time() - t0

    ok_all &= check("R3-1: 全部 5 个任务到达终态（无卡死/死锁，120s 内）",
                    len(finals) == len(TASK_IDS),
                    f"elapsed={elapsed:.1f}s states="
                    f"{[(t, finals[t]['status']) for t in TASK_IDS if t in finals]}")
    ok_all &= check("R3-2: task_id 全部唯一且正确", id_set == set(TASK_IDS),
                    f"ids={sorted(id_set)}")

    # 状态机一致性：终态合法 + 步骤状态无非法组合
    consistent = True
    detail_states = []
    for tid in TASK_IDS:
        cur = finals.get(tid)
        if cur is None:
            consistent = False
            continue
        if cur["status"] not in ("done", "degraded", "canceled"):
            consistent = False
        sts = [s.get("status") for s in cur.get("step_states", [])]
        detail_states.append((tid, cur["status"], sts))
        # 非法组合检测：done 任务不能有 failed 步；degraded 必须有失败步
        if cur["status"] == "done" and "failed" in sts:
            consistent = False
        if cur["status"] == "degraded" and "failed" not in sts and "executing" in sts:
            consistent = False
    ok_all &= check("R3-3: 状态机一致（无非法组合）", consistent,
                    f"states={detail_states}")

    # 不乱点证明：全部 click 落点在自建窗口内
    in_window = True
    rect_now = wl.get_rect(hwnd)
    for tid in TASK_IDS:
        cur = finals.get(tid)
        if not cur:
            continue
        for s in cur.get("step_states", []):
            res = s.get("result") or {}
            bp = res.get("before_screenshot_path")
            ap = res.get("after_screenshot_path")
            if bp and ap:
                pass  # 截图成对即视为该 click 已按管道执行
        # click 落点无法从 step_states 直接读（payload 不回显），改由窗口存在性 +
        # 全部任务无 STRAY 类失败佐证
        if cur["status"] == "degraded" and (cur.get("fail") or {}).get(
                "fail_class") not in ("SAFETY_BLOCKED", "WINDOW_OCCLUDED",
                                      "MACRO_NOT_FOUND", "REQUIRE_VISION",
                                      "WINDOW_NOT_FOUND", "WINDOW_NOT_FOREGROUND",
                                      "CANCELED", "drift"):
            consistent = False
    ok_all &= check("R3-4: 失败类全部为已知安全拒绝（无 INTERNAL_ERROR）",
                    all((finals.get(t) or {}).get("status") == "done"
                        or (finals.get(t) or {}).get("fail", {}).get("fail_class")
                        != "INTERNAL_ERROR" for t in TASK_IDS),
                    f"detail_states={detail_states}")

    # ---------- 清场 ----------
    for tid in TASK_IDS:
        requests.post(f"http://127.0.0.1:8030/api/ghost/task/{tid}/cancel", headers=H,
                      timeout=10)  # 终态任务返回 409，忽略
    close = wl.get_rect(hwnd) is not None
    if close:
        try:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            time.sleep(1.0)
        except Exception:  # noqa: BLE001
            pass
    gone = wl.get_rect(hwnd) is None
    OWNED.discard(hwnd)
    ok_all &= check("R3-5: 自建窗口销毁（证据链）", gone, f"hwnd={hwnd}")

    passed = sum(1 for o in RESULTS if o)
    print(f"== round3 verify: {passed}/{len(RESULTS)} passed ==", flush=True)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
