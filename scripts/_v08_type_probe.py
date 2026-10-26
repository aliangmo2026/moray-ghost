"""一次性诊断：直连 pyautogui 向 _v08_target 键入，验证标题回显绑定是否工作。"""
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

import pyautogui  # noqa: E402
import win32gui  # noqa: E402

stem = "pr" + str(int(time.time() * 1000))[-6:]
proc = subprocess.Popen(
    [sys.executable, str(ROOT / "scripts" / "_v08_target.py"), stem],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    text=True, encoding="utf-8", errors="replace")
_extra: list = []


def _pump() -> None:
    try:
        for ln in proc.stdout:
            _extra.append(ln.rstrip())
    except Exception:  # noqa: BLE001
        pass


import threading  # noqa: E402

threading.Thread(target=_pump, daemon=True).start()
time.sleep(1.0)  # READY 已由 pump 收集
hwnd = None
for _ in range(20):
    for ln in _extra:
        if ln.startswith("READY"):
            hwnd = int(ln.split()[1])
    if hwnd:
        break
    time.sleep(0.3)
print("TARGET", hwnd, win32gui.GetWindowText(hwnd), flush=True)

for _ in range(4):
    win32gui.ShowWindow(hwnd, 3)  # SW_RESTORE
    win32gui.SetForegroundWindow(hwnd)
    time.sleep(0.3)
    if win32gui.GetForegroundWindow() == hwnd:
        break
print("FOREGROUND_OK", win32gui.GetForegroundWindow() == hwnd, flush=True)

pyautogui.write("v08ok", interval=0.03)
time.sleep(1.0)
print("TITLE_AFTER_WRITE", repr(win32gui.GetWindowText(hwnd)), flush=True)
print("TARGET_OUTPUT", _extra, flush=True)

# 对照：直接用 Tk 层面检查——键入到 Entry 内容是否变化
win32gui.PostMessage(hwnd, 0x0010, 0, 0)
time.sleep(1.0)
print("ALIVE", win32gui.IsWindow(hwnd), flush=True)
try:
    proc.wait(timeout=5)
except Exception:  # noqa: BLE001
    proc.kill()
