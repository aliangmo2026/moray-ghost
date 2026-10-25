"""mock_moray.py —— 主仓标准对接 SDK（演示脚本）。

假设本脚本是 MoRay 主仓：完整走一遍"环境探活 → 下发任务 → 异步启动 →
轮询 → degraded 处置（require_vision 直接 resume；如遇 MACRO_NOT_FOUND
则演示 heal relocate 自愈）→ done"的业务闭环，并打印每一次交互的完整 JSON。

所有 URL 均为 /api/ghost/ 前缀（与主仓同端口共存不冲突）。
鉴权：X-Ghost-Token（ghost 首次启动自动生成于 .env）。

纪律：GUI 靶子全部是本脚本 Popen 新开的记事本（hwnd 差集绑定、TOPMOST、
前台门禁、只认自己的 hwnd 消失）——README 测试红线全文适用。

用法：.venv\\Scripts\\python scripts\\mock_moray.py
"""
from __future__ import annotations

import ctypes
import json
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
from app.core.screen_mss import grab  # noqa: E402

BASE = "http://127.0.0.1:8030/api/ghost"
H = {"X-Ghost-Token": ""}
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    if line.strip().startswith("GHOST_TOKEN="):
        H["X-Ghost-Token"] = line.split("=", 1)[1].strip()

OWNED: set = set()


def banner(title: str) -> None:
    print("\n" + "=" * 72, flush=True)
    print(f"== {title}", flush=True)
    print("=" * 72, flush=True)


def show(r: requests.Response, what: str) -> dict:
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        body = {"_non_json": r.text[:160]}
    print(f"\n--- {what}\n{r.request.method} {r.request.url} -> {r.status_code}\n"
          + json.dumps(body, ensure_ascii=False, indent=2)[:1600], flush=True)
    return body


def wait_status(task_id: str, want: set, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        r = requests.get(f"{BASE}/task/{task_id}", headers=H, timeout=15)
        try:
            last = r.json()
        except Exception:  # noqa: BLE001
            time.sleep(0.4)
            continue
        if last.get("status") in want:
            return last
        time.sleep(0.4)
    return last


def open_notepad(owned_before: set, x: int, y: int) -> int:
    subprocess.Popen(["notepad.exe"])
    hwnd = None
    for _ in range(60):
        time.sleep(0.25)
        new = [w for w in wl.list_windows()
               if w["hwnd"] not in owned_before
               and ("记事本" in w["title"] or "notepad" in w["title"].lower())]
        if new:
            hwnd = new[0]["hwnd"]
            break
    if hwnd:
        OWNED.add(hwnd)
        win32gui.MoveWindow(hwnd, x, y, 800, 600, True)
        wl.wait_stable(hwnd, timeout=4.0)
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
        time.sleep(0.4)
    return hwnd


def close_owned(hwnd: int) -> None:
    try:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        for _ in range(8):
            time.sleep(0.4)
            if wl.get_rect(hwnd) is None:
                OWNED.discard(hwnd)
                return
            try:
                import pyautogui  # noqa: F401
                pyautogui.hotkey("alt", "n")
            finally:
                from app.core.backend import release_all_modifiers  # noqa: E402
                release_all_modifiers()
    except Exception as exc:  # noqa: BLE001
        print(f"(close error) {exc}", flush=True)


def main() -> int:
    # ============ 第一步：环境探活 ============
    banner("第一步 GET /api/ghost/environment —— 桌面可读性探活")
    r = requests.get(f"{BASE}/environment", headers=H, timeout=30)
    env = show(r, "环境快照")
    if not env.get("input_allowed"):
        print("\n[终止] 桌面不可输入（锁屏/安全桌面），主仓应等待后重试。", flush=True)
        return 1
    print(">>> 桌面可读，开始下发任务（测试窗口即将弹出，请勿手动操作）<<<", flush=True)

    # 前置：录一个宏（自建记事本 #1，click 文件按钮）——主仓事先通过录制接口获得的资产
    hwnd1 = open_notepad(set(), 200, 200)
    if not hwnd1:
        print("[终止] 记事本 #1 未定位", flush=True)
        return 1
    requests.post(f"{BASE}/macro/record/start", headers=H,
                  json={"task_id": "mock_moray"}, timeout=15)
    macro_id = None
    for attempt in range(4):
        rect1 = wl.get_rect(hwnd1)
        if rect1 is None:
            break
        click_pt = [rect1[0] + 60, rect1[1] + 86]
        r = requests.post(f"{BASE}/execute_action", headers=H,
                          json={"action": "click", "x": click_pt[0],
                                "y": click_pt[1], "hwnd": hwnd1,
                                "settle_seconds": 0.3}, timeout=60)
        try:
            body = r.json()
        except Exception:  # noqa: BLE001
            body = {}
        print(f"\n(录制点击 attempt {attempt}) {r.status_code} "
              f"{str(body)[:110]}", flush=True)
        if r.status_code == 200:
            break
        # 落点被覆盖/漂移：重抬 TOPMOST 后重试
        win32gui.SetWindowPos(hwnd1, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
        time.sleep(0.6)
    r = requests.post(f"{BASE}/macro/record/stop", headers=H,
                      json={"task_id": "mock_moray",
                            "name": "mock_moray_macro_" + str(int(time.time()))},
                      timeout=15)
    macro_id = r.json().get("macro_id")
    print(f"(宏已入库) macro_id={macro_id}", flush=True)
    if not macro_id:
        print("[终止] 录制失败（主仓应检查覆盖/环境后重试）", flush=True)
        for h in list(OWNED):
            close_owned(h)
        return 1
    requests.post(f"{BASE}/execute_action", headers=H,
                  json={"action": "hotkey", "text": "esc", "settle_seconds": 0.1},
                  timeout=30)
    close_owned(hwnd1)

    # 前置：目标窗口 #2（新位置）——主仓重开应用后应传新 hwnd
    hwnd2 = open_notepad(set() | OWNED, 900, 500)
    if not hwnd2:
        print("[终止] 记事本 #2 未定位", flush=True)
        return 1
    rect2 = wl.get_rect(hwnd2)
    editor2 = [rect2[0] + 400, rect2[1] + 300]
    btn2 = [rect2[0] + 60, rect2[1] + 86]

    # ============ 第二步：下发任务 ============
    banner("第二步 POST /api/ghost/task —— 下发多步任务（macro + 中文输入 + wait + vision）")
    r = requests.post(f"{BASE}/task", headers=H, json={
        "task_id": "mock_moray_task_" + str(int(time.time())),
        "goal": "在记事本里输入中文并点击文件按钮（模拟主仓业务）",
        "steps": [
            {"kind": "macro", "macro_id": macro_id, "hwnd": hwnd2},
            {"kind": "action", "payload": {"action": "type",
                                           "text": "你好，我是 MoRay 主仓下发的中文输入 ✓",
                                           "x": editor2[0], "y": editor2[1],
                                           "hwnd": hwnd2, "settle_seconds": 0.2}},
            {"kind": "wait", "ms": 500},
            {"kind": "require_vision", "prompt": "确认文件菜单与文本状态"},
        ]}, timeout=30)
    body = show(r, "创建任务")
    task_id = body.get("task_id")

    # ============ 第三步：异步启动 ============
    banner("第三步 POST /api/ghost/task/{id}/start —— 异步启动（202 立即返回）")
    r = requests.post(f"{BASE}/task/{task_id}/start", headers=H, timeout=30)
    show(r, "启动")

    # ============ 第四步：轮询 → degraded → 处置 → resume（循环） → done ============
    banner("第四步 GET /api/ghost/task/{id} —— 轮询 + degraded 处置循环")
    final = None
    for round_no in range(1, 7):
        final = wait_status(task_id, {"done", "degraded", "failed", "canceled"})
        print(json.dumps(final, ensure_ascii=False, indent=2)[:1400], flush=True)
        if final.get("status") == "done":
            break
        if final.get("status") != "degraded":
            break
        fail = final.get("fail") or {}
        fc = fail.get("fail_class")
        print(f"\n[主仓处置 round {round_no}] degraded，fail_class={fc}", flush=True)
        if fc == "REQUIRE_VISION":
            print("[主仓处置] require_vision：主仓 Iris 已确认现场 → resume", flush=True)
        elif fc == "MACRO_NOT_FOUND":
            banner("模拟 Iris 视觉 → POST /api/ghost/macro/{id}/heal (relocate)")
            rect_now = wl.get_rect(hwnd2) or wl.get_rect(hwnd1) or [900, 500]
            new_center = [rect_now[0] + 60, rect_now[1] + 86]
            r = requests.post(f"{BASE}/macro/{macro_id}/heal", headers=H,
                              json={"task_id": task_id, "step_id": 1,
                                    "reason": "MACRO_NOT_FOUND",
                                    "mode": "relocate",
                                    "base_version": fail.get("base_version", 1),
                                    "vision": {"found": True,
                                               "center": new_center}}, timeout=60)
            show(r, "heal relocate")
        elif fc == "SAFETY_BLOCKED" and "drift" in str(fail.get("detail") or ""):
            print("[主仓处置] 光标漂移（用户鼠标实时移动）→ 稍候 resume 重试该步",
                  flush=True)
            time.sleep(1.5)
        else:
            print("[主仓处置] 其他失败类：等待后 resume 重试", flush=True)
            time.sleep(1.0)
        r = requests.post(f"{BASE}/task/{task_id}/resume", headers=H, timeout=30)
        show(r, "resume")
    else:
        final = wait_status(task_id, {"done", "degraded", "failed", "canceled"})

    banner("最终状态")
    print(json.dumps(final, ensure_ascii=False, indent=2)[:1600], flush=True)
    ok = final.get("status") == "done"

    # ============ 收尾 ============
    requests.post(f"{BASE}/task/{task_id}/cancel", headers=H, timeout=10)  # 终态 409，忽略
    for h in list(OWNED):
        close_owned(h)
    con = __import__("sqlite3").connect(str(ROOT / "data" / "ghost.db"))
    con.execute("DELETE FROM macros WHERE name LIKE ?", ("mock_moray_macro%",))
    con.commit()
    con.close()
    leftover = OWNED & {w["hwnd"] for w in wl.list_windows()}
    print(f"\n(清场) 自建窗口销毁: {not leftover} | 测试宏已清理", flush=True)
    print(f"\n== mock_moray: {'PASS — 主仓对接链路全通' if ok else 'FAILED'} ==",
          flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
