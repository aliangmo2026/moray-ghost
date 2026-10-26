"""v0.8 MCP 验收靶窗：按钮「保存成功」+ 可回显键盘输入的 Tk 窗。

用法：python _v08_target.py <唯一stem>
- 标题基准：moray-v08-<stem>
- 点击按钮（command 回调）→ 标题追加 "-CLICKED"（真实点击的可观察证据）
- 任意键入（bind_all 全局回显，不依赖焦点）→ 标题追加 "|T:<键入字符>"
- 就绪后 stdout 打印 `READY <hwnd> <btn_cx> <btn_cy>`，随后 mainloop；
  由测试脚本按自己 hwnd 经 WM_CLOSE 销毁。

DPI 红线：import tkinter 前声明 Per-Monitor 感知（150% 缩放下坐标否则 ×1.5 错位）。
"""
from __future__ import annotations

import ctypes
import sys

if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except OSError:
        pass

import tkinter as tk  # noqa: E402

# 本机中文 IME 会把注入字母变成 VK_PROCESSKEY(229)（Tk 收到 keysym='??'、
# char=''，实证 2026-10-26）——靶窗线程启动即激活英文键盘布局（仅本线程，
# 不影响用户其他窗口的 IME），让 pyautogui 注入的字母成为真实字符。
# HKL 是 64 位句柄：必须设 restype/argtypes=c_void_p，否则被 ctypes 截断失效。
_LAYOUT_HKL = None
try:
    _u32 = ctypes.windll.user32
    _u32.LoadKeyboardLayoutW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
    _u32.LoadKeyboardLayoutW.restype = ctypes.c_void_p
    _u32.ActivateKeyboardLayout.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    _u32.ActivateKeyboardLayout.restype = ctypes.c_void_p
    _hkl = _u32.LoadKeyboardLayoutW("00000409", 1)  # KLF_ACTIVATE
    _LAYOUT_HKL = _u32.ActivateKeyboardLayout(_hkl, 0)
except Exception:  # noqa: BLE001
    pass

stem = sys.argv[1] if len(sys.argv) > 1 else "t"
BASE_TITLE = "moray-v08-" + stem
_typed: list = []


def publish_title(suffix: str) -> None:
    root.title(BASE_TITLE + suffix)


root = tk.Tk()
root.title(BASE_TITLE)
root.geometry("560x300+420+360")
root.attributes("-topmost", True)

btn = tk.Button(root, text="保存成功", font=("Segoe UI", 13), width=14,
                command=lambda: publish_title("-CLICKED"))
btn.pack(pady=(46, 18))
entry = tk.Entry(root, font=("Consolas", 11))
entry.pack(fill="x", padx=60)
entry.insert(0, "v08-input:")


def echo_key(event) -> None:
    if event.char and event.char.isprintable():
        _typed.append(event.char)
        publish_title("|T:" + "".join(_typed))


root.bind_all("<Key>", echo_key)
root.update_idletasks()
root.update()


def publish_ready() -> None:
    top = ctypes.windll.user32.GetAncestor(root.winfo_id(), 2)  # GA_ROOT
    bx = btn.winfo_rootx() + btn.winfo_width() // 2
    by = btn.winfo_rooty() + btn.winfo_height() // 2
    print("READY", top, bx, by, flush=True)
    print("LAYOUT_HKL", hex(_LAYOUT_HKL or 0), flush=True)


root.after(300, publish_ready)
root.mainloop()
