"""抓一张自建 MS Paint 窗口截图（不推理），用于确认工具栏像素尺寸/布局。"""
from __future__ import annotations

import ctypes
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

import subprocess  # noqa: E402

import win32con  # noqa: E402
import win32gui  # noqa: E402

from app.core import safety_gate, screen_mss, window_locator as wl  # noqa: E402

OUT = ROOT / "data" / "screenshots"


def main() -> int:
    p = subprocess.Popen(["mspaint.exe"])
    win = None
    try:
        end = time.time() + 25
        while time.time() < end:
            for w in wl.list_windows():
                info = safety_gate.window_info(w["hwnd"])
                if str((info or {}).get("process") or "").lower() == \
                        "mspaint.exe" and w["rect"][2] - w["rect"][0] > 300:
                    win = w
                    break
            if win:
                break
            time.sleep(0.4)
        if not win:
            print("NO_PAINT")
            return 1
        hwnd = win["hwnd"]
        for _ in range(5):
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                win32gui.SetForegroundWindow(hwnd)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.4)
            if win32gui.GetForegroundWindow() == hwnd:
                break
        time.sleep(1.0)
        rect = win32gui.GetWindowRect(hwnd)
        img, origin = screen_mss.grab(1)
        left = max(0, rect[0] - origin["left"])
        top = max(0, rect[1] - origin["top"])
        right = min(img.width, rect[2] - origin["left"])
        bottom = min(img.height, rect[3] - origin["top"])
        crop = img.crop((left, top, right, bottom))
        OUT.mkdir(parents=True, exist_ok=True)
        crop.save(OUT / "_paint_window.png")
        print("title=", win["title"])
        print("rect=", rect, "size=", crop.size, "origin=", origin)
        print("saved", OUT / "_paint_window.png")
        # 工具条区域（顶部 25%）
        tb = crop.crop((0, 0, crop.width, int(crop.height * 0.25)))
        tb.save(OUT / "_paint_toolbar.png")
        print("toolbar size=", tb.size)
        # 640 宽整窗缩放（供实测）
        ratio = 640.0 / crop.width
        small = crop.resize((640, max(1, round(crop.height * ratio))))
        small.save(OUT / "_paint_640.png")
        print("640 size=", small.size)
    finally:
        if p.poll() is None:
            p.kill()
            try:
                p.wait(timeout=8)
            except Exception:  # noqa: BLE001
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
