"""v072 C-2 卡死最小复现（诊断用）：连续零位移 move，打印每次状态与耗时。"""
from __future__ import annotations

import time

import requests

H = {"X-Ghost-Token": ""}
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core import config  # noqa: E402

H["X-Ghost-Token"] = config.ensure_token()


def cursor() -> tuple:
    import ctypes
    import ctypes.wintypes
    pt = ctypes.wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def main() -> int:
    x, y = cursor()
    print("MOVE_LOOP start at", x, y, flush=True)
    for i in range(80):
        t0 = time.perf_counter()
        try:
            r = requests.post(
                "http://127.0.0.1:8030/api/ghost/execute_action",
                headers=H, json={"action": "move", "x": x, "y": y,
                                 "settle_seconds": 0}, timeout=25)
            ms = int((time.perf_counter() - t0) * 1000)
            print(f"[{i:02d}] {r.status_code} {ms}ms", flush=True)
            if r.status_code not in (200, 400):
                try:
                    print("     ", str(r.json())[:160], flush=True)
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            ms = int((time.perf_counter() - t0) * 1000)
            print(f"[{i:02d}] EXC {ms}ms {type(exc).__name__}", flush=True)
        time.sleep(0.05)
    print("MOVE_LOOP done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
