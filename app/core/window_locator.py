"""窗口定位（win32gui）：枚举 / 取 rect / 等稳定。供 ROI 匹配使用。

约定：
- rect 一律 [l, t, r, b] 物理像素（win32 GetWindowRect 原值，不 clamp——
  只有 matcher 知道屏幕尺寸，边界收敛在 matcher 侧做）。
- get_rect 每次调用都实时 GetWindowRect，**禁止缓存**（用户会拖窗口，
  某些应用会自己改大小）。
- 枚举排除：最小化（IsIconic）、不可见（IsWindowVisible）、WS_EX_TOOLWINDOW
  系统工具窗、零尺寸、无标题辅助表面。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import win32con
import win32gui


def list_windows() -> List[Dict[str, Any]]:
    """列出当前可定位的顶层窗口（见模块 docstring 的排除规则）。"""
    out: List[Dict[str, Any]] = []
    fg = win32gui.GetForegroundWindow()

    def _cb(hwnd: int, _acc: Any) -> bool:
        if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
            return True
        if win32gui.IsIconic(hwnd):
            return True  # 最小化
        ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        if ex & win32con.WS_EX_TOOLWINDOW:
            return True  # 系统工具窗（托盘浮层等）
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        if right - left <= 0 or bottom - top <= 0:
            return True  # 零尺寸
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return True  # 无标题辅助表面
        out.append({
            "hwnd": int(hwnd),
            "title": title,
            "class_name": win32gui.GetClassName(hwnd),
            "rect": [int(left), int(top), int(right), int(bottom)],
            "is_minimized": False,  # 最小化窗口已在上面排除
            "is_foreground": hwnd == fg,
        })
        return True

    win32gui.EnumWindows(_cb, None)
    return out


def get_rect(hwnd: int) -> Optional[List[int]]:
    """当前 rect；hwnd 无效 / 已最小化 / 零尺寸 → None。每次实时获取，不缓存。"""
    hwnd = int(hwnd) if hwnd else 0
    if not hwnd or not win32gui.IsWindow(hwnd):
        return None
    if win32gui.IsIconic(hwnd):
        return None
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    if right - left <= 0 or bottom - top <= 0:
        return None
    return [int(left), int(top), int(right), int(bottom)]


def wait_stable(hwnd: int, timeout: float = 2.0, stable_frames: int = 3,
                max_drift_px: int = 1) -> bool:
    """连续 stable_frames 次 GetWindowRect 漂移 ≤ max_drift_px 才算稳定；超时 False。"""
    deadline = time.monotonic() + timeout
    prev = get_rect(hwnd)
    if prev is None:
        return False
    stable = 1
    while time.monotonic() < deadline:
        time.sleep(0.15)
        cur = get_rect(hwnd)
        if cur is None:
            return False
        drift = max(abs(cur[0] - prev[0]), abs(cur[1] - prev[1]),
                    abs(cur[2] - prev[2]), abs(cur[3] - prev[3]))
        stable = stable + 1 if drift <= max_drift_px else 1
        if stable >= stable_frames:
            return True
        prev = cur
    return False
