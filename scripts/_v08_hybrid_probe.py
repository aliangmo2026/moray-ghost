"""对运行中的 Ghost 服务直接调用 hybrid 混合定位（自建 Paint 窗口）。"""
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

from app.core import config, safety_gate, window_locator as wl  # noqa: E402

TOKEN = config.ensure_token()


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else "矩形"
    p = subprocess.Popen(["mspaint.exe"])
    hwnd = 0
    try:
        end = time.time() + 25
        while time.time() < end:
            for w in wl.list_windows():
                info = safety_gate.window_info(w["hwnd"])
                if str((info or {}).get("process") or "").lower() == \
                        "mspaint.exe" and w["rect"][2] - w["rect"][0] > 300:
                    hwnd = w["hwnd"]
                    break
            if hwnd:
                break
            time.sleep(0.4)
        if not hwnd:
            print("NO_PAINT")
            return 1
        for _ in range(5):
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                win32gui.SetForegroundWindow(hwnd)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.3)
            if win32gui.GetForegroundWindow() == hwnd:
                break
        time.sleep(0.5)
        t0 = time.perf_counter()
        r = requests.post(
            "http://127.0.0.1:8030/api/ghost/vl/analyze",
            headers={"X-Ghost-Token": TOKEN},
            json={"hwnd": hwnd, "backend": "llama", "target": target,
                  "hybrid": True}, timeout=300)
        dt = time.perf_counter() - t0
        print("http=%s wall=%.2fs" % (r.status_code, dt), flush=True)
        print(json.dumps(r.json(), ensure_ascii=False, indent=2), flush=True)
    finally:
        if hwnd and win32gui.IsWindow(hwnd):
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        if p.poll() is None:
            p.kill()
            try:
                p.wait(timeout=8)
            except Exception:  # noqa: BLE001
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
