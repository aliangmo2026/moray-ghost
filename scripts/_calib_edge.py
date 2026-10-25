"""临时校准：自开 Edge 新标签页，截取页面区域供目视定位搜索框。用后即删。"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    try:
        ctypes_windll = __import__("ctypes").windll
        ctypes_windll.shcore.SetProcessDpiAwareness(2)
    except OSError:
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import win32con  # noqa: E402
import win32gui  # noqa: E402

from app.core import window_locator as wl  # noqa: E402
from app.core.screen_mss import grab  # noqa: E402

subprocess.Popen(["cmd", "/c", "start", "msedge"])
hwnd = None
for _ in range(80):
    time.sleep(0.25)
    for w in wl.list_windows():
        if w["class_name"] in ("Chrome_WidgetWin_1", "Chrome_WidgetWin_0") and \
                ("新标签页" in w["title"] or "New tab" in w["title"]):
            hwnd = w["hwnd"]
            break
    if hwnd:
        break
print("edge hwnd:", hwnd, wl.get_rect(hwnd) if hwnd else None, flush=True)
if hwnd is None:
    sys.exit(1)
win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                      win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
time.sleep(1.5)
img, _ = grab(1)
img.save(str(ROOT / "data" / "screenshots" / "debug_edge_calib.png"))
print("saved debug_edge_calib.png", flush=True)
