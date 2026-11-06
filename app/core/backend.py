"""输入后端——v0.15 阶段二起真身迁至 app/platforms/windows.py，本模块只做再导出。

保留本路径是为了零回归：所有 `from app.core import backend` 的旧引用拿到的
就是平台模块里的同一个函数/类对象（BACKEND 单例、FAILSAFE 状态、私有
mouse_event 签名全程只有一份）。路由/控制层新代码请用 app.platforms.get_input()。
"""
from __future__ import annotations

from app.platforms.windows import (  # noqa: F402  顺序红线由平台模块内部保证
    BACKEND,
    FailSafeException,
    InputBackend,
    PyAutoGUIBackend,
    _mouse_event_absolute,
    _mouse_event_relative,
    pyautogui,  # 旧模块曾以 import 副作用暴露；controller 读写 FAILSAFE_POINTS/PAUSE
    click,
    foreground_hwnd,
    hotkey,
    key_names,
    move_to,
    mouse_down,
    mouse_up,
    point_root,
    position,
    pressed_modifiers,
    release_all_modifiers,
    release_pointer_buttons,
    scroll,
    write_text,
)

# v0.17 虚拟输入通道（Windows PostMessage；physical 后端零改动）。
from app.core import virtual_input  # noqa: E402
from app.core.virtual_input import (  # noqa: F401
    VirtualInputError,
    activate_window,
    available as virtual_available,
    capture_signature as virtual_capture,
    compare_signatures as virtual_compare,
    points_target_window,
    read_text as virtual_read_text,
    synthetic_pointer_available,
    virtual_click,
    virtual_drag,
    virtual_pointer_click,
    virtual_pointer_drag,
    virtual_scroll,
    virtual_type,
)

__all__ = [
    "BACKEND", "FailSafeException", "InputBackend", "PyAutoGUIBackend",
    "_mouse_event_absolute", "_mouse_event_relative", "pyautogui", "click",
    "foreground_hwnd", "hotkey", "key_names", "move_to", "mouse_down",
    "mouse_up", "point_root", "position", "pressed_modifiers",
    "release_all_modifiers", "release_pointer_buttons", "scroll", "write_text",
    "virtual_input", "VirtualInputError", "activate_window",
    "virtual_available", "virtual_capture", "virtual_compare",
    "virtual_read_text", "virtual_click", "virtual_drag", "virtual_scroll",
    "virtual_type", "synthetic_pointer_available", "points_target_window",
    "virtual_pointer_click", "virtual_pointer_drag",
]
