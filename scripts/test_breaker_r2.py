"""破防第二轮：界面突变对抗（自开 Edge，缩放 100%→125%）。

用户明确授权：本轮允许对脚本自开的 Edge 新标签页窗口录制/点击/缩放
（点击的是页面搜索框——仅聚焦无导航；缩放用 Ctrl+加号，Ctrl+0 还原）。

流程：录制宏（点击页面搜索框）→ 对照重放 200 → Ctrl+加号×2（125%）
      → 重放期望 409 MACRO_NOT_FOUND（页面内容缩放，锚点失配，绝不瞎点）
      → Ctrl+0 还原 → 重放 200（恢复证明）。
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

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import pyautogui  # noqa: E402
import requests  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402

from app.core import window_locator as wl  # noqa: E402
from app.core.backend import release_all_modifiers  # noqa: E402
from app.core.screen_mss import grab  # noqa: E402

H = {"X-Ghost-Token": ""}
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    if line.strip().startswith("GHOST_TOKEN="):
        H["X-Ghost-Token"] = line.split("=", 1)[1].strip()
RESULTS: list = []
SEARCH_REL = (705, 246)   # 新标签页页面搜索框，相对窗口左上（校准图实测）


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(ok)
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}" + (f"  | {detail}" if detail else ""), flush=True)
    return ok


def edge_execute(payload: dict, hwnd: int):
    p = dict(payload)
    p["hwnd"] = hwnd
    r = requests.post("http://127.0.0.1:8030/api/ghost/execute_action", headers=H,
                      json=p, timeout=60)
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        body = {"raw": r.text[:120]}
    print(f"  (edge {payload.get('action')} @{payload.get('x')},{payload.get('y')}) "
          f"-> {r.status_code} {str(body)[:130]}", flush=True)
    return r, body


def find_edge(owned_before: set) -> int:
    """hwnd 差集绑定：只认 Popen 之后新出现的 Chrome_WidgetWin 顶层窗口。"""
    for _ in range(120):
        time.sleep(0.25)
        for w in wl.list_windows():
            if (w["hwnd"] not in owned_before
                    and w["class_name"].startswith("Chrome_WidgetWin")
                    and w["is_foreground"]):
                return w["hwnd"]
    return 0


def zoom(hwnd: int, keys: tuple) -> None:
    fg = win32gui.GetForegroundWindow()
    if fg != hwnd:
        print(f"  (zoom: 前台不是 Edge fg={fg}，跳过——避免快捷键落到别的窗口)", flush=True)
        return
    for k in keys:
        try:
            pyautogui.hotkey("ctrl", k)
            time.sleep(0.4)
        finally:
            release_all_modifiers()


def main() -> int:
    # 0) 收养校准脚本开出的 Edge 窗口；不在则自己开
    edge_before = {w["hwnd"] for w in wl.list_windows()}
    subprocess.Popen(["cmd", "/c", "start", "msedge"])
    hwnd = find_edge(edge_before)
    if not hwnd:
        print("[FAIL] Edge 窗口未找到（30s）；当前窗口：", flush=True)
        for w in wl.list_windows():
            print("   ", w["hwnd"], w["class_name"][:22], repr(w["title"][:40]),
                  flush=True)
        return 1
    print(f"[edge] hwnd={hwnd}", flush=True)
    win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                          win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
    time.sleep(0.6)
    # 前台校验（快捷键只能发给前台 Edge，避免落到别的窗口）
    if win32gui.GetForegroundWindow() != hwnd:
        force_fg = False
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
            time.sleep(0.25)
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.5)
            force_fg = win32gui.GetForegroundWindow() == hwnd
        except Exception:  # noqa: BLE001
            pass
        ok_all = check("R2-0: Edge 前台就绪", force_fg, f"hwnd={hwnd}")
        if not force_fg:
            print("== round2 ABORT：前台无法锁定 ==", flush=True)
            return 1

    # 1) 录制宏：点击页面搜索框（锚点裁剪带纹理下限校验 std≥12；漂移重试）
    abs_pt = None
    for attempt in range(4):
        rect = wl.get_rect(hwnd)
        if rect is None:
            time.sleep(0.5)
            continue
        w_sz, h_sz = rect[2] - rect[0], rect[3] - rect[1]
        img, _ = grab(1)
        rect_g = wl.get_rect(hwnd)
        if rect != rect_g:
            time.sleep(0.4)
            continue  # 抓屏与 rect 不同相
        cands = [(int(rect[0] + w_sz * 0.5), int(rect[1] + h_sz * 0.35)),
                 (int(rect[0] + w_sz * 0.5), int(rect[1] + h_sz * 0.72)),
                 (int(rect[0] + w_sz * 0.5), int(rect[1] + h_sz * 0.55))]
        for cx, cy in cands:
            crop = img.crop((cx - 80, cy - 30, cx + 80, cy + 30))
            arr_c = cv2.cvtColor(
                np.ascontiguousarray(np.asarray(crop)), cv2.COLOR_RGB2BGR)
            std_c = float(arr_c.std())
            print(f"  (anchor cand @{cx},{cy}) std={std_c:.1f}", flush=True)
            if std_c >= 12.0:
                abs_pt = [cx, cy]
                break
        if abs_pt is not None:
            break
        time.sleep(0.6)
    if abs_pt is None:
        print("[FAIL] 所有候选锚点纹理不足（std<12），拒绝录制", flush=True)
        return 1
    rs = requests.post("http://127.0.0.1:8030/api/ghost/macro/record/start", headers=H,
                       json={"task_id": "t_r2"}, timeout=15)
    r, body = None, None
    for attempt in range(4):
        r, body = edge_execute({"action": "click", "x": abs_pt[0], "y": abs_pt[1],
                                "settle_seconds": 0.3}, hwnd)
        if r.status_code == 200:
            break
        detail = str(body.get("detail") or "")
        if "drift" in detail:
            print(f"  (record click attempt {attempt}: drift, retrying)", flush=True)
            time.sleep(0.6)
            continue
        break
    ok_all = check("R2-1: 录制 click 搜索框 → 200", r.status_code == 200,
                   str(body)[:100])
    r = requests.post("http://127.0.0.1:8030/api/ghost/macro/record/stop", headers=H,
                      json={"task_id": "t_r2", "name": "test_r2_edge_"
                                                          + str(int(time.time()))},
                      timeout=15)
    macro_id = r.json().get("macro_id")
    ok_all &= check("R2-2: macro_id 就绪", bool(macro_id), str(r.json())[:100])

    # 2) 对照重放（100%；漂移类失败重试）
    b = None
    for attempt in range(3):
        r = replay(macro_id, hwnd)
        b = r.json()
        print(f"  (100% 对照重放 attempt {attempt}) {r.status_code} {b}", flush=True)
        if r.status_code == 200 and b.get("status") == "success":
            break
        if "drift" in str(b.get("detail") or "") or str(b.get("error")) == "SAFETY_BLOCKED":
            time.sleep(0.6)
            continue
        break
    ok_all &= check("R2-3: 对照重放 → 200 命中搜索框",
                    r.status_code == 200 and b.get("status") == "success"
                    and abs(b["steps"][0]["center"][0] - abs_pt[0]) <= 3
                    and abs(b["steps"][0]["center"][1] - abs_pt[1]) <= 3,
                    f"center={b['steps'][0].get('center') if b.get('steps') else None} "
                    f"expect={abs_pt}")

    # 3) 界面突变：Ctrl+加号 ×2 → 125%
    zoom(hwnd, ("+", "+"))
    time.sleep(0.8)

    # 4) 重放期望 409 MACRO_NOT_FOUND（漂移类失败重试，其余失败即结果）
    for attempt in range(3):
        r = replay(macro_id, hwnd)
        b2 = r.json()
        print(f"  (125% 重放 attempt {attempt}) {r.status_code} {b2}", flush=True)
        if r.status_code == 200:
            break  # 意外成功：测试失败，如实暴露
        if (str(b2.get("error")) == "SAFETY_BLOCKED"
                and "drift" in str(b2.get("detail") or "")):
            time.sleep(0.6)
            continue
        break
    ok_all &= check("R2-4: 缩放 125% 后重放 → 409 MACRO_NOT_FOUND（绝不瞎点）",
                    r.status_code == 409 and b2.get("error") == "MACRO_NOT_FOUND"
                    and isinstance(b2.get("best_score"), (int, float)),
                    f"best_score={b2.get('best_score')} "
                    f"fail_shot={b2.get('fail_screenshot_path')}")

    # 5) 还原 100% → 重放恢复
    zoom(hwnd, ("0",))
    time.sleep(0.8)
    b3 = None
    for attempt in range(3):
        r = replay(macro_id, hwnd)
        b3 = r.json()
        print(f"  (还原后重放 attempt {attempt}) {r.status_code} {b3}", flush=True)
        if r.status_code == 200 and b3.get("status") == "success":
            break
        if "drift" in str(b3.get("detail") or ""):
            time.sleep(0.6)
            continue
        break
    ok_all &= check("R2-5: Ctrl+0 还原后重放 → 200（恢复证明）",
                    r.status_code == 200 and b3.get("status") == "success",
                    f"center={b3['steps'][0].get('center') if b3.get('steps') else None}")

    # 清场：关闭自开 Edge 窗口（新标签页无未保存状态）
    try:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        time.sleep(1.5)
        print(f"(清场) edge hwnd={hwnd} closed={wl.get_rect(hwnd) is None}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"(清场失败) {exc}", flush=True)

    passed = sum(1 for o in RESULTS if o)
    print(f"== round2 verify: {passed}/{len(RESULTS)} passed ==", flush=True)
    return 0 if passed == len(RESULTS) else 1


def replay(macro_id: str, hwnd: int):
    return requests.post(
        f"http://127.0.0.1:8030/api/ghost/macro/{macro_id}/replay", headers=H,
        json={"hwnd": hwnd}, timeout=60)


if __name__ == "__main__":
    sys.exit(main())
