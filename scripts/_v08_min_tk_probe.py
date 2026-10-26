"""对照实验：最小裸 Tk + bind_all(<Key>) + pyautogui.write，判定注入键是否进 Tk。"""
from __future__ import annotations

import ctypes
import sys
import threading
import time

if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except OSError:
        pass

import pyautogui  # noqa: E402
import win32gui  # noqa: E402

import tkinter as tk  # noqa: E402

# 与 _v08_target 同款：本线程激活英文布局（HKL 64 位句柄需 c_void_p）
try:
    _u32 = ctypes.windll.user32
    _u32.LoadKeyboardLayoutW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
    _u32.LoadKeyboardLayoutW.restype = ctypes.c_void_p
    _u32.ActivateKeyboardLayout.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    _u32.ActivateKeyboardLayout.restype = ctypes.c_void_p
    _hkl = _u32.LoadKeyboardLayoutW("00000409", 1)
    _act = _u32.ActivateKeyboardLayout(_hkl, 0)
    print("LAYOUT_ACT", hex(_act or 0), flush=True)
except Exception as exc:  # noqa: BLE001
    print("LAYOUT_FAIL", exc, flush=True)

root = tk.Tk()
root.title("min-tk-probe")
root.geometry("300x160+700+500")
got: list = []


def on_key(e) -> None:
    got.append(repr(e.char) + "/" + repr(e.keysym) + "/code="
               + str(getattr(e, "keycode", None)))
    root.title("KEYS:" + str(len(got)))


root.bind_all("<Key>", on_key)


def drive() -> None:
    time.sleep(0.8)
    hwnd = ctypes.windll.user32.GetAncestor(root.winfo_id(), 2)
    for _ in range(5):
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.3)
        if win32gui.GetForegroundWindow() == hwnd:
            break
    fg_ok = win32gui.GetForegroundWindow() == hwnd
    imm = ctypes.windll.imm32
    imm.ImmAssociateContext.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    imm.ImmAssociateContext.restype = ctypes.c_bool
    detached = imm.ImmAssociateContext(hwnd, None)
    base = len(got)
    pyautogui.write("xyz", interval=0.04)
    time.sleep(0.8)
    print("FG_OK", fg_ok, "IME_DETACHED", detached, flush=True)
    print("A_PYAUTOGUI", got[base:], flush=True)
    base = len(got)
    try:
        import keyboard as kb
        kb.write("abc")
        time.sleep(0.8)
    except Exception as exc:  # noqa: BLE001
        print("KB_FAIL", exc, flush=True)
    print("B_KEYBOARD", got[base:], flush=True)
    print("TITLE", repr(win32gui.GetWindowText(hwnd)), flush=True)
    root.destroy()


threading.Thread(target=drive, daemon=True).start()
root.mainloop()
print("DONE", flush=True)
