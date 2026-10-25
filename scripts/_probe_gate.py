"""临时探针：进程内验证 controller 前台门禁。用后即删。"""
from __future__ import annotations

import ctypes
import sys

ctypes.windll.shcore.SetProcessDpiAwareness(2)
sys.path.insert(0, ".")

import app.core.backend as b  # noqa: E402
import app.core.controller as c  # noqa: E402

b.foreground_hwnd = lambda: 12345  # 模拟前台是别的窗口

payload = {"action": "type", "text": "probe", "x": 100, "y": 100,
           "hwnd": 99999, "settle_seconds": 0, "fmt": "jpeg"}
try:
    c.run_action(payload)
    print("NO GATE FIRED — 门禁未生效", flush=True)
except c.ForegroundError as e:
    print(f"FG GATE OK: status={e.status} error={e.error} detail={e.detail}",
          flush=True)
except Exception as e:  # noqa: BLE001
    print(f"OTHER: {type(e).__name__}: {str(e)[:120]}", flush=True)
