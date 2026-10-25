"""输入后端抽象：路由/控制层不得直接 import pyautogui，一律走 InputBackend。"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import sys
import time
from abc import ABC, abstractmethod
from typing import List, Optional, Tuple

import pyperclip  # pyautogui 自带依赖；非 ASCII 文本走剪贴板粘贴

if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # 顺序红线：必须在 import pyautogui 之前
    except OSError:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

import pyautogui  # noqa: E402

pyautogui.FAILSAFE = True   # 常开；鼠标甩到角落立即中止整个动作/宏
pyautogui.PAUSE = 0.05

FailSafeException = pyautogui.FailSafeException

# 所有可能被按下的修饰键（含左右变体）。释放清单宁可多不可少。
_MODIFIER_KEYS = ("ctrl", "alt", "shift", "win", "winleft", "winright",
                  "lctrl", "rctrl", "lalt", "ralt", "lshift", "rshift",
                  "lwin", "rwin")


def release_all_modifiers() -> None:
    """强制释放所有修饰键——键盘状态绝不泄漏到系统。

    任何 keyDown/hotkey/write 都必须包在 try...finally 里并在 finally 调用本函数：
    进程异常退出前、Fail-Safe 中止后、异常路径上，一律先释放。
    释放期间临时关闭 Fail-Safe——清理动作本身不能因为光标恰好在角落而中断
    （否则挂起的修饰键比一切都危险）。keyUp 对未按下的键是无害空操作。
    """
    prev = pyautogui.FAILSAFE
    pyautogui.FAILSAFE = False
    try:
        for key in _MODIFIER_KEYS:
            try:
                pyautogui.keyUp(key)
            except Exception:
                pass
    finally:
        pyautogui.FAILSAFE = prev


class InputBackend(ABC):
    @abstractmethod
    def position(self) -> Tuple[int, int]:
        ...

    @abstractmethod
    def move_to(self, x: int, y: int) -> None:
        ...

    @abstractmethod
    def click(self, clicks: int = 1, button: str = "left") -> None:
        ...

    @abstractmethod
    def write_text(self, text: str, interval: float = 0.02) -> None:
        ...

    @abstractmethod
    def hotkey(self, *names: str) -> None:
        ...

    @abstractmethod
    def key_names(self) -> List[str]:
        ...


class PyAutoGUIBackend(InputBackend):
    def position(self) -> Tuple[int, int]:
        p = pyautogui.position()
        return int(p.x), int(p.y)

    def move_to(self, x: int, y: int) -> None:
        pyautogui.moveTo(int(x), int(y), duration=0)

    def click(self, clicks: int = 1, button: str = "left") -> None:
        # 不传坐标：调用前已完成 moveTo + 漂移校验，避免二次移动引入竞态
        pyautogui.click(clicks=int(clicks), interval=0.08 if clicks > 1 else 0.0, button=button)

    def write_text(self, text: str, interval: float = 0.02) -> None:
        text = str(text)
        try:
            if all(ord(ch) < 128 for ch in text):
                pyautogui.write(text, interval=interval)
            else:
                # 非 ASCII（中文/emoji）：pyautogui.write 不支持，走剪贴板粘贴
                _paste_text(text)
        finally:
            release_all_modifiers()  # 键盘状态绝不泄漏到系统

    def hotkey(self, *names: str) -> None:
        try:
            pyautogui.hotkey(*names)
        finally:
            release_all_modifiers()  # 组合键中途失败也必须全部释放

    def key_names(self) -> List[str]:
        return list(pyautogui.KEYBOARD_KEYS)


BACKEND: InputBackend = PyAutoGUIBackend()


def position() -> Tuple[int, int]:
    return BACKEND.position()


def move_to(x: int, y: int) -> None:
    BACKEND.move_to(x, y)


def click(clicks: int = 1, button: str = "left") -> None:
    BACKEND.click(clicks=clicks, button=button)


def write_text(text: str, interval: float = 0.02) -> None:
    BACKEND.write_text(text, interval)


def hotkey(*names: str) -> None:
    BACKEND.hotkey(*names)


def key_names() -> List[str]:
    return BACKEND.key_names()


# GetAsyncKeyState 的虚拟键码（修饰键，含左右变体）
_VK_MODIFIERS = (("ctrl", 0x11), ("shift", 0x10), ("alt", 0x12),
                 ("win", 0x5B), ("win", 0x5C))


def pressed_modifiers() -> List[str]:
    """当前处于按下状态的修饰键（GetAsyncKeyState 高位）。

    既有可能是"挂起"（某处 keyDown 后没释放/进程被杀），也可能是用户物理按着——
    本函数只如实报告按下状态，语义判断交给调用方。
    """
    user32 = ctypes.windll.user32
    out: List[str] = []
    for name, vk in _VK_MODIFIERS:
        if user32.GetAsyncKeyState(vk) & 0x8000:
            out.append(name)
    return sorted(set(out))


GA_ROOT = 2


def point_root(pt: tuple) -> Optional[int]:
    """屏幕上 pt 像素处的最顶层窗口根句柄（点击落点归属）；失败返回 None。"""
    try:
        user32_ = ctypes.windll.user32
        leaf = user32_.WindowFromPoint(ctypes.wintypes.POINT(int(pt[0]), int(pt[1])))
        if not leaf:
            return None
        return int(user32_.GetAncestor(leaf, GA_ROOT))
    except Exception:
        return None


def foreground_hwnd() -> Optional[int]:
    """当前前台窗口 hwnd；无则 None。"""
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    return int(hwnd) if hwnd else None


def _paste_text(text: str) -> None:
    """非 ASCII 文本（中文/emoji）走剪贴板：备份 → 写入 → Ctrl+V → 还原。

    还原策略：原剪贴板是文本则还原；原为空或图片（pyperclip 无法还原图片）
    则清空。粘贴需要目标应用接受 Ctrl+V（记事本/常规编辑器均适用）。
    """
    prev = None
    try:
        prev = pyperclip.paste()
    except Exception:
        prev = None
    pyperclip.copy(text)
    try:
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.15)  # 等目标应用完成粘贴
    finally:
        release_all_modifiers()
        try:
            if isinstance(prev, str) and prev:
                pyperclip.copy(prev)  # 还原原剪贴板文本
            else:
                pyperclip.copy("")  # 原为空/图片：清空
        except Exception:
            pass
