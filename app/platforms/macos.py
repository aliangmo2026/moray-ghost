"""macOS 平台驱动骨架（v0.15 阶段二占位——**未在 macOS 上验证**）。

实现路径（与 windows.py 同一契约，验证后填充）：
- 抓屏：mss（跨平台，BitBlt/CoreGraphics 后端；无需 DPI 红线——macOS
  坐标即物理点）+ PIL 编码，复用与 windows 相同的单线程收口模式；
- 输入：pyautogui（跨平台键鼠）；macOS 需要辅助功能权限（辅助功能授权
  未通过时 pyautogui 会抛 failures/异常——启用前必须在目标机验证）；
- 剪贴板：pyperclip（macOS 原生支持）。

红线：未在真实 macOS 上通过验收前，方法一律 NotImplementedError——
绝不假装能跑（Windows 上的 Mock 测试只验证契约与动态加载，不验证行为）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from PIL import Image

from .base import BaseInput, BaseScreen


class MacOSScreen(BaseScreen):
    """macOS 抓屏骨架（mss 路径，待目标平台验证）。"""

    def grab(self, monitor_index: int = 1,
             timeout: float = 8.0) -> Tuple[Image.Image, Dict[str, int]]:
        raise NotImplementedError(
            "macOS screen backend is a phase-2 skeleton (mss path planned); "
            "not verified on macOS")

    def monitors(self) -> List[Dict[str, Any]]:
        raise NotImplementedError("macOS screen backend skeleton")

    def monitor_rect(self, index: int) -> Dict[str, int]:
        raise NotImplementedError("macOS screen backend skeleton")

    def monitor_at(self, x: int, y: int) -> Optional[Dict[str, int]]:
        raise NotImplementedError("macOS screen backend skeleton")

    def virtual_screen(self) -> Dict[str, int]:
        raise NotImplementedError("macOS screen backend skeleton")

    def dpi_status(self) -> Dict[str, Any]:
        raise NotImplementedError("macOS screen backend skeleton")

    def grab_health(self, window_sec: float = 60.0) -> Dict[str, Any]:
        raise NotImplementedError("macOS screen backend skeleton")

    def encode(self, img: Image.Image, fmt: str = "jpeg",
               quality: int = 70, scale: float = 1.0) -> bytes:
        raise NotImplementedError("macOS screen backend skeleton")

    def save_bytes(self, data: bytes, purpose: str, fmt: str) -> Any:
        raise NotImplementedError("macOS screen backend skeleton")

    def save_audit(self, img: Image.Image, purpose: str, fmt: str,
                   quality: int) -> Any:
        raise NotImplementedError("macOS screen backend skeleton")

    def b64_of(self, img: Image.Image, fmt: str, quality: int,
               scale: float) -> str:
        raise NotImplementedError("macOS screen backend skeleton")


class MacOSInput(BaseInput):
    """macOS 输入骨架（pyautogui 路径，需辅助功能权限，待目标平台验证）。"""

    def position(self) -> Tuple[int, int]:
        raise NotImplementedError("macOS input backend skeleton")

    def move_to(self, x: int, y: int) -> None:
        raise NotImplementedError("macOS input backend skeleton")

    def click(self, clicks: int = 1, button: str = "left") -> None:
        raise NotImplementedError("macOS input backend skeleton")

    def mouse_down(self, button: str = "left") -> None:
        raise NotImplementedError("macOS input backend skeleton")

    def mouse_up(self, button: str = "left") -> None:
        raise NotImplementedError("macOS input backend skeleton")

    def scroll(self, dy: int) -> None:
        raise NotImplementedError("macOS input backend skeleton")

    def write_text(self, text: str, interval: float = 0.02) -> None:
        raise NotImplementedError("macOS input backend skeleton")

    def hotkey(self, *names: str) -> None:
        raise NotImplementedError("macOS input backend skeleton")

    def key_names(self) -> List[str]:
        raise NotImplementedError("macOS input backend skeleton")

    def release_all_modifiers(self) -> None:
        raise NotImplementedError("macOS input backend skeleton")

    def release_pointer_buttons(self) -> None:
        raise NotImplementedError("macOS input backend skeleton")

    def pressed_modifiers(self) -> List[str]:
        raise NotImplementedError("macOS input backend skeleton")

    def point_root(self, pt: tuple) -> Optional[int]:
        raise NotImplementedError("macOS input backend skeleton")

    def foreground_hwnd(self) -> Optional[int]:
        raise NotImplementedError("macOS input backend skeleton")


def create_screen() -> MacOSScreen:
    """平台工厂：app.platforms.get_screen() 的 macOS 入口。"""
    return MacOSScreen()


def create_input() -> MacOSInput:
    """平台工厂：app.platforms.get_input() 的 macOS 入口。"""
    return MacOSInput()