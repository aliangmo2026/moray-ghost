"""输入后端抽象：路由/控制层不得直接 import pyautogui，一律走 InputBackend。"""
from __future__ import annotations

import ctypes
import sys
from abc import ABC, abstractmethod
from typing import List, Tuple

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
        pyautogui.write(str(text), interval=interval)

    def hotkey(self, *names: str) -> None:
        pyautogui.hotkey(*names)

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
