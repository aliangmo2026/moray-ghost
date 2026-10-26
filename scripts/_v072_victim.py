"""看门狗物理自救验证 · 受害者进程（v0.7.2）。

流程：拉起 ghost_watchdog 监控自己（PID + 创建时间）→ 用 keybd_event 物理按住
Ctrl+Shift → 打印 READY → 休眠等待被测试脚本强杀（模拟任务管理器杀 Ghost）。
强杀后若看门狗工作正常，GetAsyncKeyState 会观察到 Ctrl/Shift 在数秒内被释放。

纪律：只按住/释放本进程注入的键；watchdog 为独立 DETACHED 子进程。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

VK_CONTROL = 0x11
VK_SHIFT = 0x10
KEYEVENTF_KEYDOWN = 0


def main() -> int:
    script = str(ROOT / "scripts" / "ghost_watchdog.py")
    try:
        import psutil
        ctime = str(psutil.Process().create_time())
    except Exception:  # noqa: BLE001
        ctime = "0"
    wd = subprocess.Popen(
        [sys.executable, script, "--pid", str(os.getpid()), "--ctime", ctime],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    print("WATCHDOG_PID", wd.pid, flush=True)
    time.sleep(0.3)
    user32 = ctypes.windll.user32
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYDOWN, 0)
    user32.keybd_event(VK_SHIFT, 0, KEYEVENTF_KEYDOWN, 0)
    print("READY", flush=True)  # Ctrl+Shift 已物理按住，等待被强杀
    while True:
        time.sleep(0.5)


if __name__ == "__main__":
    sys.exit(main())
