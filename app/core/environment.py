"""环境状态感知（v0.4 M2）：桌面可读性 / 锁屏与安全桌面 / 修饰键状态。

判定依据（全部实测校准）：
- desktop_readable：现场探测抓屏是否成功（决定性；失败即不可读）。
- input_desktop_name：OpenInputDesktop + GetUserObjectInformation。
  - None        → 输入桌面打不开 = UAC 安全桌面（uac_secure_desktop=True）
  - "Default"   → 正常交互桌面
  - 其他值      → 锁屏 / 屏保（lock_screen=True）
- foreground_hwnd 为 None：连前台窗口都没有，一并按不可输入处理。
- input_allowed = desktop_readable 且非锁屏且非安全桌面：动作类接口的放行条件，
  不满足时一律 503 SYSTEM_NOT_READABLE，拒绝盲操作。
"""
from __future__ import annotations

import ctypes
import time
from typing import Any, Dict, Optional

from . import backend, screen_mss, window_locator

UOI_NAME = 2
DESKTOP_READOBJECTS = 0x0001


class SystemNotReadable(Exception):
    """系统不可输入（锁屏 / 安全桌面 / 抓屏失败）。snapshot 供响应体回显。"""

    def __init__(self, detail: str, snapshot: Dict[str, Any]):
        super().__init__(detail)
        self.detail = detail
        self.snapshot = snapshot


def _input_desktop_name() -> Optional[str]:
    """当前输入桌面名；打不开（UAC 安全桌面）返回 None。"""
    user32 = ctypes.windll.user32
    hdesk = user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
    if not hdesk:
        return None
    try:
        buf = ctypes.create_unicode_buffer(64)
        needed = ctypes.c_ulong()
        if user32.GetUserObjectInformationW(hdesk, UOI_NAME, buf,
                                            ctypes.sizeof(buf),
                                            ctypes.byref(needed)):
            return buf.value or None
        return None
    finally:
        user32.CloseDesktop(hdesk)


def snapshot(probe_grab: bool = True) -> Dict[str, Any]:
    """采集完整环境快照。probe_grab=True 时做一次决定性探测抓屏。"""
    t0 = time.perf_counter()
    fg = window_locator.foreground_hwnd()
    desktop = _input_desktop_name()
    grab_st = screen_mss.grab_health(60.0)

    readable = True
    probe_error: Optional[str] = None
    if probe_grab:
        try:
            screen_mss.grab(1)
        except Exception as exc:  # noqa: BLE001
            readable = False
            probe_error = type(exc).__name__ + ": " + str(exc)

    uac = desktop is None
    lock = (desktop is not None and desktop != "Default") or (fg is None and not uac)
    input_allowed = bool(readable and not uac and not lock and fg is not None)

    return {
        "ok": True,
        "desktop_readable": readable,
        "probe_error": probe_error,
        "input_desktop_name": desktop,
        "lock_screen": bool(lock),
        "uac_secure_desktop": bool(uac),
        "foreground_present": fg is not None,
        "foreground_hwnd": fg,
        "input_allowed": input_allowed,
        "grab": grab_st,
        # v0.14：DPI 档位 + 各屏物理矩形 + 虚拟桌面一致性（混合 DPI 野外排障）
        "screen": screen_mss.dpi_status(),
        "modifiers_down": backend.pressed_modifiers(),
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
    }


def assert_input_allowed() -> Dict[str, Any]:
    """动作类接口的前置门禁：不允许输入时抛 SystemNotReadable（HTTP 503）。"""
    snap = snapshot()
    if snap["input_allowed"]:
        return snap
    reasons = []
    if not snap["desktop_readable"]:
        reasons.append("screen capture failed: " + str(snap.get("probe_error")))
    if snap["uac_secure_desktop"]:
        reasons.append("UAC secure desktop is active")
    if snap["lock_screen"]:
        reasons.append("workstation is locked (input desktop: "
                       + repr(snap.get("input_desktop_name")) + ")")
    if not snap["foreground_present"]:
        reasons.append("no foreground window")
    raise SystemNotReadable("; ".join(reasons) or "input not allowed", snap)
