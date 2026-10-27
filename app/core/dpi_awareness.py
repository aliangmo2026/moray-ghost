"""进程 DPI 感知（v0.14）：一次设置、全进程统一的物理像素坐标系。

Windows 混合 DPI 三条红线：
1. 必须在 import mss / import pyautogui / 创建任何窗口或 DC **之前**设置，
   否则 BitBlt 截图与鼠标坐标可能落在两个缩放坐标系里（150% 笔记本 +
   100% 外接屏必错位）。本模块只依赖 ctypes/sys，可安全放在所有重依赖
   之前 import。
2. 首选 Per-Monitor V2（SetProcessDpiAwarenessContext(-4)）：让
   GetSystemMetrics / BitBlt / SetCursorPos / GetWindowRect / mouse_event
   归一化全部返回**物理像素**，第二块屏的 100% 缩放不会被按主屏 DPI
   拉伸。旧链 shcore.SetProcessDpiAwareness(2) 语义等价，但必须检查
   HRESULT——ctypes 默认 restype=c_int，windll 直调**不会**把
   E_ACCESSDENIED 转成异常，旧代码的 `except OSError` 分支永远不触发，
   失败时进程会"以为自己 DPI 感知了"（混合 DPI 下静默偏移 ×1.5 的根因）。
3. 全部失败才退 user32.SetProcessDPIAware()（系统级感知：截图按主屏
   DPI 虚拟化，次屏内容可能被位图拉伸）。MODE 如实记录实际生效档位，
   /environment 与诊断脚本据此上报，绝不谎报。

设置幂等：模块 import 时执行一次；进程内重复调用直接返回已记录档位。
"""
from __future__ import annotations

import ctypes
import sys
from typing import Any, Dict, Tuple

# 实际生效的 DPI 感知档位（诊断脚本 / /environment 回显）
#   per_monitor_v2 → 物理像素，混合 DPI 全对齐（Win10 1703+）
#   per_monitor    → 同 v2（Win8.1 API），次屏 DPI 通知略弱
#   system         → 系统 DPI 感知：混合 DPI 下次屏可能被拉伸（次优）
#   unaware        → Windows 虚拟化坐标：与物理像素必然不一致（最差）
MODE = "unaware"
DPI_AWARE = False  # 兼容旧引用（screen_mss.DPI_AWARE）：非 unaware 即 True

# Win10 1703+：DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == (HANDLE)-4
_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79


def _apply() -> None:
    global MODE, DPI_AWARE
    if sys.platform != "win32":
        MODE = "unaware"
        DPI_AWARE = False
        return

    # ---- 第一档：Per-Monitor V2（Win10 1703+）----
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        set_ctx = user32.SetProcessDpiAwarenessContext
        set_ctx.argtypes = (ctypes.c_void_p,)
        set_ctx.restype = ctypes.c_bool
        if set_ctx(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
            MODE = "per_monitor_v2"
            DPI_AWARE = True
            return
        # 返回 False：上下文不受支持（老系统）或进程已设置过（access denied）。
        # 两者都继续走旧链兜底；不吞 ERROR_INVALID_PARAMETER 之外的诊断信息。
    except (AttributeError, OSError):
        pass  # 函数不存在（Win10 1703 之前）

    # ---- 第二档：shcore.SetProcessDpiAwareness(2)，HRESULT 严格检查 ----
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        set_aware = shcore.SetProcessDpiAwareness
        set_aware.argtypes = (ctypes.c_int,)
        set_aware.restype = ctypes.HRESULT  # 非 S_OK 自动抛 OSError
        set_aware(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        MODE = "per_monitor"
        DPI_AWARE = True
        return
    except (AttributeError, OSError):
        pass  # shcore 不存在（Win7）或 E_ACCESSDENIED（进程已感知）

    # ---- 第三档：系统级感知（最后兜底）----
    try:
        if ctypes.windll.user32.SetProcessDPIAware():
            MODE = "system"
            DPI_AWARE = True
            return
    except (AttributeError, OSError):
        pass

    MODE = "unaware"
    DPI_AWARE = False


_apply()  # 模块 import 即设置（红线：先于 mss / pyautogui / 任何窗口创建）


def status() -> Dict[str, Any]:
    """当前 DPI 感知档位 + 虚拟桌面物理矩形（诊断用，绝不谎报）。"""
    out: Dict[str, Any] = {"mode": MODE, "aware": DPI_AWARE}
    if sys.platform == "win32":
        try:
            out["virtual_screen"] = virtual_screen()
        except Exception as exc:  # noqa: BLE001
            out["virtual_screen_error"] = type(exc).__name__ + ": " + str(exc)
    return out


def virtual_screen() -> Dict[str, int]:
    """虚拟桌面（全屏联合区）物理矩形；DPI 感知进程下为物理像素。"""
    if sys.platform != "win32":
        raise OSError("virtual_screen is Windows-only")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
    user32.GetSystemMetrics.restype = ctypes.c_int
    left = int(user32.GetSystemMetrics(_SM_XVIRTUALSCREEN))
    top = int(user32.GetSystemMetrics(_SM_YVIRTUALSCREEN))
    width = int(user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN))
    height = int(user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN))
    if width <= 0 or height <= 0:
        raise OSError("virtual screen metrics unavailable: "
                      f"{width}x{height}@{left},{top}")
    return {"left": left, "top": top, "width": width, "height": height}


def in_virtual_screen(x: int, y: int) -> bool:
    try:
        v = virtual_screen()
    except OSError:
        return True  # 指标不可用时不越权拦截（交由上层原有防线）
    return (v["left"] <= x < v["left"] + v["width"]
            and v["top"] <= y < v["top"] + v["height"])


def clamp_to_virtual(x: int, y: int) -> Tuple[int, int]:
    v = virtual_screen()
    cx = max(v["left"], min(x, v["left"] + v["width"] - 1))
    cy = max(v["top"], min(y, v["top"] + v["height"] - 1))
    return int(cx), int(cy)
