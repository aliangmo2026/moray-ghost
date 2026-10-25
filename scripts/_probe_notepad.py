"""探针：Win11 新记事本带文件参数启动时，第 N 个实例是独立窗口还是并入既有窗口的标签页。"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import win32con  # noqa: E402
import win32gui  # noqa: E402

from app.core import window_locator as wl  # noqa: E402

tmp_dir = ROOT / "data" / "tmp"
tmp_dir.mkdir(parents=True, exist_ok=True)
opened: list = []

try:
    before = {w["hwnd"] for w in wl.list_windows()}
    print("before:", len(before), "windows", flush=True)
    for i in (1, 2, 3):
        f = tmp_dir / ("probe_t_" + str(int(time.time() * 1000)) + ".txt")
        f.write_text("", encoding="utf-8")
        opened.append(f)
        subprocess.Popen(["notepad.exe", str(f)])
        time.sleep(3.0)
        now = wl.list_windows()
        new = [w for w in now if w["hwnd"] not in before]
        hit = [w for w in new if f.stem in w["title"]]
        retitled = [w for w in now if w["hwnd"] in before and f.stem in w["title"]]
        print("launch", i, "-> new_hwnd:",
              [(w["hwnd"], w["title"][:50]) for w in hit],
              "| old_hwnd_retitled:", [(w["hwnd"], w["title"][:50]) for w in retitled],
              flush=True)
        before = {w["hwnd"] for w in wl.list_windows()}
finally:
    for w in wl.list_windows():
        if "probe_t_" in w["title"]:
            try:
                win32gui.PostMessage(w["hwnd"], win32con.WM_CLOSE, 0, 0)
                opened_hwnd = w["hwnd"]
                print("closed", opened_hwnd, flush=True)
            except Exception as exc:  # noqa: BLE001
                print("close failed:", exc, flush=True)
    time.sleep(1.5)
    left = [w["title"][:40] for w in wl.list_windows() if "probe_t_" in w["title"]]
    print("leftover probe windows:", left, flush=True)
    for f in opened:
        try:
            f.unlink(missing_ok=True)
        except OSError:
            pass
