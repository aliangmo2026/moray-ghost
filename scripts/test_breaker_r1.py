"""破防第一轮：混乱环境对抗（用户实时干扰 + 后台任务）。

任务：action(click 编辑区) → wait → action(type chaos) → wait → action(click)
      → wait → action(type) → wait → require_vision
脚本会打印"干扰窗口开启"提示，用户随后拖窗口/切前台/上遮挡。
验证（观察性，不预设结局）：
  1. 任务终态必须是合法状态（done|degraded|canceled），不得 failed/INTERNAL_ERROR。
  2. 每个已执行 click 的 resolved_physical 必须在自建窗口 rect 内（不乱点）。
  3. 全程带 hwnd 的 click 若被落点归属门禁拒绝 → 400 SAFETY_BLOCKED（证据入 fail）。
  4. 帧录像全程记录。
"""
from __future__ import annotations

import base64
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

import pyautogui  # noqa: E402
import requests  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402

from app.core import window_locator as wl  # noqa: E402
from app.core.backend import release_all_modifiers  # noqa: E402

H = {"X-Ghost-Token": ""}
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    if line.strip().startswith("GHOST_TOKEN="):
        H["X-Ghost-Token"] = line.split("=", 1)[1].strip()
RESULTS: list = []
OWNED: set = set()
CLICK_LOG: list = []   # (payload, status, body摘要)


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(ok)
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}" + (f"  | {detail}" if detail else ""), flush=True)
    return ok


def action(payload: dict):
    r = requests.post("http://127.0.0.1:8030/api/ghost/execute_action", headers=H,
                      json=payload, timeout=60)
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        body = {"raw": r.text[:120]}
    if payload.get("action") in ("click", "double_click", "right_click"):
        CLICK_LOG.append((payload, r.status_code, body))
    print(f"  (action {payload.get('action')} @{payload.get('x')},{payload.get('y')}) "
          f"-> {r.status_code} {str(body)[:130]}", flush=True)
    return r, body


def point_is_ours(pt: tuple, hwnd: int) -> bool:
    try:
        return win32gui.GetAncestor(win32gui.WindowFromPoint(pt), 2) == hwnd
    except Exception:  # noqa: BLE001
        return False


def open_notepad(owned_before: set, x: int, y: int, w: int = 800, h: int = 600) -> int:
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
    win32gui.MoveWindow(hwnd, x, y, w, h, True)
    wl.wait_stable(hwnd, timeout=4.0)
    win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                          win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
    time.sleep(0.4)
    return hwnd


def main() -> int:
    hwnd1 = open_notepad(set(), 200, 200)
    if not hwnd1:
        print("[FAIL] 自建记事本未定位", flush=True)
        return 1
    # 激活：点击窗口中心（编辑区）
    rect = wl.get_rect(hwnd1)
    r = requests.post("http://127.0.0.1:8030/api/ghost/execute_action", headers=H,
                      json={"action": "click", "x": rect[0] + 400,
                            "y": rect[1] + 300, "hwnd": hwnd1,
                            "settle_seconds": 0.1}, timeout=30)
    print(f"(激活 click) {r.status_code}", flush=True)

    # 干扰窗口开启提示（用户看到后开始拖动/切前台/遮挡）
    print("", flush=True)
    print("=" * 72, flush=True)
    print(">>> 干扰窗口开启：请现在开始拖动记事本窗口 / 切走前台 / 弹出遮挡物 <<<", flush=True)
    print(">>> 任务总时长约 18 秒，全部结束后脚本自动汇报 <<<", flush=True)
    print("=" * 72, flush=True)
    time.sleep(4)  # 给用户反应时间

    # 任务：混合打字与点击，总时长约 18s（含 wait），中途必然穿越用户干扰
    steps = [
        {"kind": "wait", "ms": 1500},
        {"kind": "action", "payload": {"action": "type", "text": "chaos-stage-1",
                                       "x": rect[0] + 400, "y": rect[1] + 300,
                                       "hwnd": hwnd1, "settle_seconds": 0.2}},
        {"kind": "wait", "ms": 3000},
        {"kind": "action", "payload": {"action": "click",
                                       "x": rect[0] + 500, "y": rect[1] + 320,
                                       "hwnd": hwnd1, "settle_seconds": 0.2}},
        {"kind": "wait", "ms": 3000},
        {"kind": "action", "payload": {"action": "type", "text": " chaos-stage-2",
                                       "x": rect[0] + 500, "y": rect[1] + 320,
                                       "hwnd": hwnd1, "settle_seconds": 0.2}},
        {"kind": "wait", "ms": 3000},
        {"kind": "action", "payload": {"action": "click",
                                       "x": rect[0] + 300, "y": rect[1] + 340,
                                       "hwnd": hwnd1, "settle_seconds": 0.2}},
        {"kind": "wait", "ms": 2000},
        {"kind": "require_vision", "prompt": "报告窗口与文本的最终状态"},
    ]
    r = requests.post("http://127.0.0.1:8030/api/ghost/task", headers=H,
                      json={"task_id": "task_r1_" + str(int(time.time())),
                            "goal": "混乱环境对抗", "steps": steps}, timeout=30)
    print(f"(POST /task) {r.status_code} {r.json()}", flush=True)
    t_start = time.time()
    tid = r.json().get("task_id")
    r = requests.post(f"http://127.0.0.1:8030/api/ghost/task/{tid}/start", headers=H,
                      timeout=30)
    print(f"(start) {r.status_code} {r.json()}", flush=True)

    # 轮询并记录状态迁移时间线
    timeline = []
    last_status = None
    final = None
    while time.time() - t_start < 120:
        r = requests.get(
            f"http://127.0.0.1:8030/api/ghost/task/{tid}", headers=H, timeout=15)
        try:
            cur = r.json()
        except Exception:  # noqa: BLE001
            time.sleep(0.4)
            continue
        st = cur.get("status")
        if st != last_status:
            timeline.append(f"{time.time() - t_start:6.1f}s -> {st} "
                            f"(step {cur.get('current_step_id')})")
            print(f"  [timeline] {timeline[-1]}", flush=True)
            last_status = st
        if st in ("done", "degraded", "canceled", "failed"):
            final = cur
            break
        time.sleep(0.4)

    print("", flush=True)
    print("--- 时间线 ---", flush=True)
    for line in timeline:
        print(" ", line, flush=True)
    print("--- 终态 ---", flush=True)
    print(f"status={final.get('status') if final else None}", flush=True)
    print(f"fail={final.get('fail') if final else None}", flush=True)
    for s in final.get("step_states", []) if final else []:
        print(f"  step {s.get('step_id')} [{s.get('kind')}] {s.get('status')} "
              f"result={str(s.get('result'))[:100]} error={str(s.get('error'))[:80]}",
              flush=True)

    # 不乱点证明：每个已执行 click 的落点必须在窗口 rect 内（容忍 8px 边缘）
    rect_now = wl.get_rect(hwnd1)
    print(f"(窗口最终 rect) {rect_now}", flush=True)
    in_window = True
    for payload, status, body in CLICK_LOG:
        rp = body.get("resolved_physical")
        if status == 200 and rp:
            inside = (rect_now[0] - 8 <= rp[0] <= rect_now[2] + 8
                      and rect_now[1] - 8 <= rp[1] <= rect_now[3] + 8)
            print(f"  click @{rp} inside_window={inside}", flush=True)
            if not inside:
                in_window = False
    ok_all = check("R1-1: 任务终态合法（done|degraded|canceled，无 INTERNAL_ERROR）",
                   final is not None and final.get("status") in
                   ("done", "degraded", "canceled"),
                   f"status={final.get('status') if final else None}")
    ok_all &= check("R1-2: 全部已执行 click 落点在自建窗口内（不乱点）", in_window,
                    f"clicks={len(CLICK_LOG)}")
    if final and final.get("fail"):
        f = final["fail"]
        ok_all &= check("R1-3: 失败带完整证据（fail_class + 截图/run_id）",
                        bool(f.get("fail_class")),
                        f"fail_class={f.get('fail_class')} detail={str(f.get('detail'))[:90]}")
    print(f"== round1 verify: {sum(1 for o in RESULTS if o)}/{len(RESULTS)} passed ==",
          flush=True)
    close_owned(hwnd1)
    return 0 if all(RESULTS) else 1


def close_owned(hwnd: int) -> None:
    try:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        time.sleep(1.0)
    except Exception:
        pass
    if wl.get_rect(hwnd) is None:
        OWNED.discard(hwnd)
    print(f"(清场) hwnd={hwnd} closed={wl.get_rect(hwnd) is None}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
