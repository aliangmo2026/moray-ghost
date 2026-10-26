"""直接对运行中的服务做 generic / targeted 两次分析（自建靶窗），打印全量响应。"""
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

from app.core import config  # noqa: E402

TOKEN = config.ensure_token()


def call(payload: dict, tag: str) -> None:
    t0 = time.perf_counter()
    r = requests.post(
        "http://127.0.0.1:8030/api/ghost/vl/analyze",
        headers={"X-Ghost-Token": TOKEN}, json=payload, timeout=300)
    dt = time.perf_counter() - t0
    print("\n== %s http=%s wall=%.2fs ==" % (tag, r.status_code, dt),
          flush=True)
    try:
        print(json.dumps(r.json(), ensure_ascii=False, indent=2), flush=True)
    except ValueError:
        print(r.text[:1000], flush=True)


def main() -> int:
    stem = "p" + str(int(time.time() * 1000))[-8:]
    title = "moray-v08-" + stem
    tgt = subprocess.Popen([sys.executable, str(ROOT / "scripts" /
                                               "_v08_target.py"), stem],
                           stdout=subprocess.PIPE, text=True, encoding="utf-8")
    hwnd = 0
    try:
        line = tgt.stdout.readline().strip()
        parts = line.split()
        print("target:", line, flush=True)
        if not parts or parts[0] != "READY":
            return 1
        hwnd = int(parts[1])
        print("truth button center =", parts[2], parts[3], flush=True)
        for _ in range(3):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.3)
        call({"window": title, "backend": "llama"}, "generic")
        call({"window": title, "backend": "llama", "target": "保存成功"},
             "targeted")
    finally:
        if hwnd and win32gui.IsWindow(hwnd):
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        try:
            tgt.wait(timeout=5)
        except Exception:  # noqa: BLE001
            tgt.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
