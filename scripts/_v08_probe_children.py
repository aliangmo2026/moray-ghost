"""一次性诊断：拉起 _v08_target 并枚举其窗口树（类名/文本/矩形），验证 native 后端可行性。"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import win32gui  # noqa: E402

proc = subprocess.Popen(
    [sys.executable, str(ROOT / "scripts" / "_v08_target.py"), "probe1"],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    text=True, encoding="utf-8", errors="replace")
line = proc.stdout.readline().strip()
print("READY_LINE:", line)
parts = line.split()
top = int(parts[1])
print("TOP:", top, "title:", repr(win32gui.GetWindowText(top)),
      "rect:", win32gui.GetWindowRect(top))
rows = []


def cb(ch, _):
    rows.append((ch, win32gui.GetClassName(ch), win32gui.GetWindowText(ch),
                 win32gui.IsWindowVisible(ch), win32gui.GetWindowRect(ch)))


win32gui.EnumChildWindows(top, cb, None)
for r in rows:
    print("CHILD:", r[0], "class=", repr(r[1]), "text=", repr(r[2]),
          "visible=", r[3], "rect=", r[4])
print("CHILD_COUNT:", len(rows))
win32gui.PostMessage(top, 0x0010, 0, 0)  # WM_CLOSE
time.sleep(1.0)
print("ALIVE:", win32gui.IsWindow(top))
try:
    proc.wait(timeout=5)
except Exception:  # noqa: BLE001
    proc.kill()
