"""一次性清场辅助（v0.7.1）：关闭指定 hwnd 的记事本窗口（处理保存对话框）。

用法：python scripts/_v071_close_window.py <hwnd>
只允许对我方产物（moray_t_* 标题）使用；关闭后打印 CLOSED/ALIVE 供验收。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import win32con  # noqa: E402
import win32gui  # noqa: E402


def main() -> int:
    hwnd = int(sys.argv[1])
    title = win32gui.GetWindowText(hwnd)
    if "moray_t_" not in title:
        print("REFUSED not our window:", repr(title), flush=True)
        return 2
    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    for _ in range(12):
        time.sleep(0.4)
        if not win32gui.IsWindow(hwnd):
            print("CLOSED", flush=True)
            return 0
        # 未保存内容 → 保存对话框：alt+n（丢弃）——与各套件 close_owned 同姿势
        try:
            import pyautogui
            pyautogui.hotkey("alt", "n")
        except Exception:  # noqa: BLE001
            pass
    print("ALIVE", win32gui.GetWindowText(hwnd), flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
