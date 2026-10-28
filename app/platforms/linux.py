"""Linux 平台驱动骨架（v0.15 阶段二占位——**未在 Linux 上验证**）。

实现路径（与 windows.py 同一契约，验证后填充）：
- 抓屏：mss（X11 后端；Wayland 下 mss/BitBlt 不可用，需 portal/screencast
  方案——启用前必须在目标发行版验证）；
- 输入：pyautogui（X11 可用；Wayland 下需 ydotool/wtype 等外部工具）；
- 剪贴板：pyperclip（X11 需 xclip/xsel）。

红线：未在真实 Linux 上通过验收前，方法一律 NotImplementedError——
绝不假装能跑（Windows 上的 Mock 测试只验证契约与动态加载，不验证行为）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from PIL import Image

from .base import BaseInput, BaseScreen


class LinuxScreen(BaseScreen):
    """Linux 抓屏骨架（mss X11 路径，Wayland 待方案，未验证）。"""

    def grab(self, monitor_index: int = 1,
             timeout: float = 8.0) -> Tuple[Image.Image, Dict[str, int]]:
        raise NotImplementedError(
            "Linux screen backend is a phase-2 skeleton (mss/X11 path "
            "planned; Wayland needs portal); not verified on Linux")

    def monitors(self) -> List[Dict[str, Any]]:
        raise NotImplementedError("Linux screen backend skeleton")

    def monitor_rect(self, index: int) -> Dict[str, int]:
        raise NotImplementedError("Linux screen backend skeleton")

    def monitor_at(self, x: int, y: int) -> Optional[Dict[str, int]]:
        raise NotImplementedError("Linux screen backend skeleton")

    def virtual_screen(self) -> Dict[str, int]:
        raise NotImplementedError("Linux screen backend skeleton")

    def dpi_status(self) -> Dict[str, Any]:
        raise NotImplementedError("Linux screen backend skeleton")

    def grab_health(self, window_sec: float = 60.0) -> Dict[str, Any]:
        raise NotImplementedError("Linux screen backend skeleton")

    def encode(self, img: Image.Image, fmt: str = "jpeg",
               quality: int = 70, scale: float = 1.0) -> bytes:
        raise NotImplementedError("Linux screen backend skeleton")

    def save_bytes(self, data: bytes, purpose: str, fmt: str) -> Any:
        raise NotImplementedError("Linux screen backend skeleton")

    def save_audit(self, img: Image.Image, purpose: str, fmt: str,
                   quality: int) -> Any:
        raise NotImplementedError("Linux screen backend skeleton")

    def b64_of(self, img: Image.Image, fmt: str, quality: int,
               scale: float) -> str:
        raise NotImplementedError("Linux screen backend skeleton")


class LinuxInput(BaseInput):
    """Linux 输入骨架（pyautogui/X11 路径，Wayland 待方案，未验证）。"""

    def position(self) -> Tuple[int, int]:
        raise NotImplementedError("Linux input backend skeleton")

    def move_to(self, x: int, y: int) -> None:
        raise NotImplementedError("Linux input backend skeleton")

    def click(self, clicks: int = 1, button: str = "left") -> None:
        raise NotImplementedError("Linux input backend skeleton")

    def mouse_down(self, button: str = "left") -> None:
        raise NotImplementedError("Linux input backend skeleton")

    def mouse_up(self, button: str = "left") -> None:
        raise NotImplementedError("Linux input backend skeleton")

    def scroll(self, dy: int) -> None:
        raise NotImplementedError("Linux input backend skeleton")

    def write_text(self, text: str, interval: float = 0.02) -> None:
        raise NotImplementedError("Linux input backend skeleton")

    def hotkey(self, *names: str) -> None:
        raise NotImplementedError("Linux input backend skeleton")

    def key_names(self) -> List[str]:
        raise NotImplementedError("Linux input backend skeleton")

    def release_all_modifiers(self) -> None:
        raise NotImplementedError("Linux input backend skeleton")

    def release_pointer_buttons(self) -> None:
        raise NotImplementedError("Linux input backend skeleton")

    def pressed_modifiers(self) -> List[str]:
        raise NotImplementedError("Linux input backend skeleton")

    def point_root(self, pt: tuple) -> Optional[int]:
        raise NotImplementedError("Linux input backend skeleton")

    def foreground_hwnd(self) -> Optional[int]:
        raise NotImplementedError("Linux input backend skeleton")


def create_screen() -> LinuxScreen:
    """平台工厂：app.platforms.get_screen() 的 Linux 入口。"""
    return LinuxScreen()


def create_input() -> LinuxInput:
    """平台工厂：app.platforms.get_input() 的 Linux 入口。"""
    return LinuxInput()