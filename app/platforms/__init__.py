"""跨平台驱动动态加载（v0.15 阶段二）：按 sys.platform 选择平台实现。

用法：
    from app import platforms
    screen = platforms.get_screen()   # BaseScreen 实现（单例，缓存）
    input_ = platforms.get_input()    # BaseInput 实现（单例，缓存）
    platforms.platform_name()         # "windows" | "macos" | "linux"

设计红线：
- 惰性加载：平台模块只在 get_screen/get_input 首次调用时 import（本包
  __init__ 顶部绝不 import 平台实现，避免 import 环与无谓的 pyautogui
  副作用）；
- 未知平台 → PlatformError（绝不静默降级到错误实现）；
- 单例缓存：同一进程内 screen/input 各只有一个实例（与历史 BACKEND 单例
  语义一致——capture 线程、状态都挂在唯一实例背后）。
"""
from __future__ import annotations

import importlib
import sys
from typing import Any, Dict

from .base import BaseInput, BaseScreen

__all__ = ["PlatformError", "platform_name", "get_screen", "get_input",
           "BaseScreen", "BaseInput"]

# sys.platform → 平台模块名（app.platforms.<name>）
_PLATFORM_MAP: Dict[str, str] = {
    "win32": "windows",
    "darwin": "macos",
    "linux": "linux",
}

_cache: Dict[str, Any] = {}


class PlatformError(RuntimeError):
    """未知/不支持的平台（绝不静默降级到错误实现）。"""


def platform_name() -> str:
    """当前平台的驱动模块名；不支持的平台抛 PlatformError。"""
    name = _PLATFORM_MAP.get(sys.platform)
    if not name:
        raise PlatformError(
            f"unsupported platform {sys.platform!r} "
            f"(supported: {sorted(set(_PLATFORM_MAP.values()))})")
    return name


def _load(name: str):
    """按名惰性加载平台模块并缓存（importlib 单次执行）。"""
    key = "mod:" + name
    if key not in _cache:
        _cache[key] = importlib.import_module("." + name, __name__)
    return _cache[key]


def get_screen() -> BaseScreen:
    """当前平台的抓屏实现（单例）。"""
    key = "screen"
    if key not in _cache:
        _cache[key] = _load(platform_name()).create_screen()
    return _cache[key]


def get_input() -> BaseInput:
    """当前平台的输入实现（单例）。"""
    key = "input"
    if key not in _cache:
        _cache[key] = _load(platform_name()).create_input()
    return _cache[key]


def reset_cache() -> None:
    """清空缓存（测试用：验证动态加载与单例语义）。"""
    _cache.clear()
