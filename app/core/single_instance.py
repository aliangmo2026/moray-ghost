"""Windows 命名互斥体单实例门禁（v0.7.1 物理级绝对控制）。

本机永远只允许运行一个 Ghost 实例：进程入口先 CreateMutexW 抢占
``Global\\MorayGhostSingleInstance``（机器级命名空间，跨会话唯一），已被占用
即拒绝启动；配合端口预检（8030 已有监听 → 直接报错退出），从物理上杜绝
多实例抢占同一套桌面输入。

互斥体句柄由本进程持有至进程退出——正常退出、崩溃、被强杀，内核都会随
进程消亡自动释放句柄，因此不存在"僵尸互斥体"，无需清理逻辑。
"""
from __future__ import annotations

import ctypes
import sys
from typing import Optional

MUTEX_NAME = "Global\\MorayGhostSingleInstance"
ERROR_ALREADY_EXISTS = 183

# 句柄必须存为 c_void_p 级别的原始值并被模块引用持有：进程存活期间不 CloseHandle，
# 这正是互斥体的"锁定期"。
_handle: Optional[int] = None
_acquired = False

_k32 = None
if sys.platform == "win32":
    # use_last_error=True：CreateMutexW 之后 ctypes.get_last_error() 才能读到
    # 本线程真实 SetLastError 值（windll 默认不保存，可能读到陈旧值）。
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    _k32.CreateMutexW.restype = ctypes.c_void_p
    _k32.CloseHandle.argtypes = [ctypes.c_void_p]
    _k32.CloseHandle.restype = ctypes.c_bool


class InstanceAlreadyRunning(RuntimeError):
    """同机已有 Ghost 实例在运行（互斥体被占用）。"""


def acquire() -> bool:
    """抢占单实例互斥体。True=本进程持有；False=他处已持有。同进程内幂等。"""
    global _handle, _acquired
    if _acquired:
        return True
    if sys.platform != "win32":
        # 非 Windows 平台无 CreateMutexW：退回端口门禁（port_free）兜底。
        _acquired = True
        return True
    handle = _k32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        raise OSError("CreateMutexW failed (GetLastError="
                      + str(ctypes.get_last_error()) + ")")
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        _k32.CloseHandle(handle)  # 他处持有：立刻放手，绝不共享句柄装作成功
        return False
    _handle = handle
    _acquired = True
    return True


def held_by_self() -> bool:
    return _acquired


def port_free(host: str, port: int, timeout: float = 0.5) -> bool:
    """端口预检：能连通即已被占用（本进程尚未监听，连通=别处在听）。"""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return False
    except OSError:
        return True


def assert_single_instance(host: str, port: int) -> None:
    """启动门禁：互斥体或端口任一被占 → InstanceAlreadyRunning（带可读原因）。"""
    if not acquire():
        raise InstanceAlreadyRunning(
            "another moray-ghost instance is already running "
            "(single-instance mutex " + MUTEX_NAME + " is held)")
    if not port_free(host, port):
        raise InstanceAlreadyRunning(
            "port " + str(port) + " on " + str(host)
            + " is already in use (another listener — possibly a background "
              "moray-ghost — is running)")
