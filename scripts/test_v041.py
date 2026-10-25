"""v0.4.1 功能验证：中文输入（剪贴板 fallback）、剪贴板还原、type 前台门禁。用后即删。

纪律：自建记事本，hwnd 差集绑定，前台门禁，只认自己的 hwnd 消失。
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

import pyperclip  # noqa: E402
import requests  # noqa: E402
import win32gui  # noqa: E402

from app.core import window_locator as wl  # noqa: E402
from app.core.screen_mss import grab  # noqa: E402

H = {"X-Ghost-Token": ""}
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    if line.strip().startswith("GHOST_TOKEN="):
        H["X-Ghost-Token"] = line.split("=", 1)[1].strip()
RESULTS: list = []
OWNED: set = set()


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
    print(f"  (action {payload.get('action')}) -> {r.status_code} "
          f"{str(body)[:110]}", flush=True)
    return r, body


def main() -> int:
    ok_all = True
    hwnds_snapshot = {w["hwnd"] for w in wl.list_windows()}

    # 自建记事本 ×2：#1 前台（打中文），#2 后台（前台门禁反例）
    subprocess.Popen(["notepad.exe"])
    hwnd1 = None
    for _ in range(60):
        time.sleep(0.25)
        new = [w for w in wl.list_windows()
               if w["hwnd"] not in hwnds_snapshot
               and ("记事本" in w["title"] or "notepad" in w["title"].lower())]
        if new:
            hwnd1 = new[0]["hwnd"]
            break
    if hwnd1 is None:
        print("[FAIL] 记事本未定位", flush=True)
        return 1
    OWNED.add(hwnd1)
    import win32con  # noqa: E402
    import win32gui  # noqa: E402
    win32gui.MoveWindow(hwnd1, 200, 200, 800, 600, True)
    wl.wait_stable(hwnd1, timeout=4.0)
    win32gui.SetWindowPos(hwnd1, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                          win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
    time.sleep(0.4)
    rect1 = wl.get_rect(hwnd1)
    editor1 = [rect1[0] + 400, rect1[1] + 300]
    r, body = action({"action": "click", "x": editor1[0], "y": editor1[1],
                      "hwnd": hwnd1, "settle_seconds": 0.1})
    ok_all &= check("V1: 记事本#1 激活+点击", r.status_code == 200
                    and win32gui.GetForegroundWindow() == hwnd1)

    # V2: 中文输入（剪贴板 fallback）——先预置剪贴板验证还原
    pyperclip.copy("MORAY-CLIPBOARD-SENTINEL")
    r, body = action({"action": "type", "text": "你好，moray v0.4.1！中文✓",
                      "x": editor1[0], "y": editor1[1], "hwnd": hwnd1,
                      "settle_seconds": 0.3})
    ok_all &= check("V2: 中文 type → 200", r.status_code == 200,
                    f"error={body.get('error')}")
    clip_now = pyperclip.paste()
    ok_all &= check("V3: 剪贴板已还原（预置哨兵仍在）",
                    clip_now == "MORAY-CLIPBOARD-SENTINEL",
                    f"clip_now={clip_now!r}")

    # 目视确认：截编辑器区域存图（含中文文本）
    rect1n = wl.get_rect(hwnd1)
    img, _ = grab(1)
    img.crop((rect1n[0], rect1n[1] + 150, rect1n[0] + 800, rect1n[1] + 400)).save(
        str(ROOT / "data" / "screenshots" / "debug_chinese_type.png"))
    print("  (已存 debug_chinese_type.png 供目视确认中文)", flush=True)

    # V4: 后台窗口 type → 409 WINDOW_NOT_FOREGROUND
    subprocess.Popen(["notepad.exe"])
    hwnd2 = None
    for _ in range(60):
        time.sleep(0.25)
        new = [w for w in wl.list_windows()
               if w["hwnd"] not in (hwnds_snapshot | OWNED)
               and ("记事本" in w["title"] or "notepad" in w["title"].lower())]
        if new:
            hwnd2 = new[0]["hwnd"]
            break
    if hwnd2:
        OWNED.add(hwnd2)
        import win32gui as wg2  # noqa: E402
        wg2.MoveWindow(hwnd2, 900, 500, 800, 600, True)
        wl.wait_stable(hwnd2, timeout=4.0)
        time.sleep(0.6)
        # 真激活 #1（编辑区点击；给已 TOPMOST 的窗口重设 TOPMOST 是无操作，
        # 不会把前台还给 #1——实测踩过）
        if win32gui.GetForegroundWindow() != hwnd1:
            r, body = action({"action": "click", "x": 600, "y": 500,
                              "hwnd": hwnd1, "settle_seconds": 0.1})
            time.sleep(0.3)
        ok_all &= check("V4pre: 前台确认为 #1（#2 处于非前台）",
                        win32gui.GetForegroundWindow() == hwnd1,
                        f"fg={win32gui.GetForegroundWindow()} hwnd1={hwnd1}")
        r, body = action({"action": "type", "text": "不应该打出来",
                          "x": 1300, "y": 800, "hwnd": hwnd2, "settle_seconds": 0.1})
        ok_all &= check("V4: 后台窗口 type → 409 WINDOW_NOT_FOREGROUND",
                        r.status_code == 409
                        and body.get("error") == "WINDOW_NOT_FOREGROUND",
                        f"fg={body.get('foreground')}")
        # ASCII type 同样受门禁保护
        r, body = action({"action": "type", "text": "ascii leak",
                          "x": 1300, "y": 800, "hwnd": hwnd2, "settle_seconds": 0.1})
        ok_all &= check("V5: ASCII type 同样被前台门禁拦截",
                        r.status_code == 409
                        and body.get("error") == "WINDOW_NOT_FOREGROUND")

    # 清场（带未保存文本的窗口：WM_CLOSE + Alt+N 不保存）
    for h in (hwnd2, hwnd1):
        if not h:
            continue
        try:
            win32gui.PostMessage(h, win32con.WM_CLOSE, 0, 0)
            for _ in range(8):
                time.sleep(0.4)
                if wl.get_rect(h) is None:
                    OWNED.discard(h)
                    break
                try:
                    pyautogui.hotkey("alt", "n")
                finally:
                    release_all_modifiers()
        except Exception:  # noqa: BLE001
            pass
    leftover = OWNED & {w["hwnd"] for w in wl.list_windows()}
    ok_all &= check("V6: 自建窗口全部销毁", not leftover,
                    f"owned={sorted(OWNED)} leftover={sorted(leftover)}")

    passed = sum(1 for o in RESULTS if o)
    print(f"== v041 verify: {passed}/{len(RESULTS)} passed ==", flush=True)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
