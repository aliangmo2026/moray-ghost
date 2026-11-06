"""v0.17 细粒度动作锁：按窗口（hwnd）隔离，替代「全局一把锁只能单任务」。

背景
----
v0.16 及以前的 ``controller.ACTION_LOCK`` 是整进程唯一的串行锁：任何两个
桌面动作（哪怕一个在记事本打字、一个在画图落墨）都只能排队。v0.17 引入
虚拟输入通道后，不同窗口的输入已经可以在操作系统层面真正并行，锁也必须
随之细化。

锁层级（固定顺序，绝不反向，避免死锁）
------------------------------------
1. **窗口锁**（本模块）：key = ("hwnd", 0x...)，只约束同一目标窗口的动作。
2. **全局锁**（本模块）：key = ("global", 0)，用于无法解析目标窗口的动作
   （无 hwnd 的 move / 无前台窗口的 hotkey 等）。它串行化所有「目标窗口
   未知」的动作；physical 动作同时还要拿下面第 3 层的物理锁，因此不会与
   任何真实输入并行。virtual 动作强制要求 hwnd，永远不会落到全局键。
3. **物理输入锁**（``controller.ACTION_LOCK``，调用方在窗口锁之后获取）：
   虚拟通道不碰它；physical 通道必须持有它。物理鼠标/键盘全系统只有一套，
   这是操作系统事实，不是实现偷懒。

同一个 key 的 RLock 允许重入（同一线程内 fallback 等嵌套调用安全）。
注册表按引用计数回收：无持有者/等待者的条目立即删除，长时间挂机不会累积。

统计
----
``snapshot()`` 供 /environment 与测试观测：当前活跃动作数、峰值并发、
按 hwnd 的持有计数、锁等待超时次数。它只是观测面，不参与调度。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional, Tuple

LockKey = Tuple[str, int]

GLOBAL_KEY: LockKey = ("global", 0)
PHYSICAL_KEY: LockKey = ("physical", 0)

_guard = threading.Lock()
_entries: Dict[LockKey, "_Entry"] = {}
_active: Dict[LockKey, int] = {}
_stats: Dict[str, Any] = {
    "acquired_total": 0,
    "timeouts_total": 0,
    "active_now": 0,
    "max_concurrent": 0,
    "last_wait_ms": 0.0,
}


class _Entry:
    __slots__ = ("lock", "refs")

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.refs = 0


class Lease:
    """一次成功获取的租约；必须由获取线程调用 release()（可安全重复调用）。"""

    __slots__ = ("key", "entry", "wait_ms", "_released")

    def __init__(self, key: LockKey, entry: _Entry, wait_ms: float) -> None:
        self.key = key
        self.entry = entry
        self.wait_ms = wait_ms
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self.entry.lock.release()
        with _guard:
            self.entry.refs -= 1
            active = _active.get(self.key, 1) - 1
            if active > 0:
                _active[self.key] = active
            else:
                _active.pop(self.key, None)
            _stats["active_now"] = max(0, int(_stats["active_now"]) - 1)
            if self.entry.refs <= 0 and _active.get(self.key, 0) <= 0:
                # 无持有者、无等待者：回收条目（RLock 已被本线程释放）
                if _entries.get(self.key) is self.entry:
                    _entries.pop(self.key, None)

    def __enter__(self) -> "Lease":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


def window_key(hwnd: Any) -> Optional[LockKey]:
    """hwnd → 锁 key；非法/缺失返回 None（调用方退化为全局锁）。"""
    try:
        h = int(hwnd) if hwnd is not None else 0
    except (TypeError, ValueError):
        return None
    return ("hwnd", h) if h > 0 else None


def describe_key(key: LockKey) -> str:
    kind, value = key
    if kind == "hwnd":
        return "hwnd=" + str(value)
    return kind


def _acquire(key: LockKey, timeout: float) -> Optional[Lease]:
    timeout = max(0.0, float(timeout))
    t0 = time.monotonic()
    with _guard:
        entry = _entries.get(key)
        if entry is None:
            entry = _entries[key] = _Entry()
        entry.refs += 1
    got = entry.lock.acquire(timeout=timeout)
    wait_ms = round((time.monotonic() - t0) * 1000.0, 1)
    if not got:
        with _guard:
            entry.refs -= 1
            if entry.refs <= 0 and _entries.get(key) is entry:
                _entries.pop(key, None)
            _stats["timeouts_total"] = int(_stats["timeouts_total"]) + 1
            _stats["last_wait_ms"] = wait_ms
        return None
    with _guard:
        _stats["acquired_total"] = int(_stats["acquired_total"]) + 1
        _stats["active_now"] = int(_stats["active_now"]) + 1
        _stats["max_concurrent"] = max(int(_stats["max_concurrent"]),
                                       int(_stats["active_now"]))
        _stats["last_wait_ms"] = wait_ms
        _active[key] = _active.get(key, 0) + 1
    return Lease(key, entry, wait_ms)


def acquire_window(hwnd: Any, timeout: float) -> Optional[Lease]:
    """获取窗口锁；hwnd 缺失 → 全局锁（保守，与一切动作互斥）。"""
    key = window_key(hwnd)
    return _acquire(key or GLOBAL_KEY, timeout)


def acquire_global(timeout: float) -> Optional[Lease]:
    return _acquire(GLOBAL_KEY, timeout)


def snapshot() -> Dict[str, Any]:
    """锁调度观测快照（/environment、测试用）。"""
    with _guard:
        return {
            "policy": "fine_grained_window_lock",
            "active_now": int(_stats["active_now"]),
            "max_concurrent": int(_stats["max_concurrent"]),
            "acquired_total": int(_stats["acquired_total"]),
            "timeouts_total": int(_stats["timeouts_total"]),
            "last_wait_ms": float(_stats["last_wait_ms"]),
            "tracked_keys": len(_entries),
            "held_by_hwnd": {str(k[1]): v for k, v in _active.items()
                             if k[0] == "hwnd" and v},
            "held_global": int(_active.get(GLOBAL_KEY, 0)),
            "physical_serialized": True,
            "virtual_uses_physical_lock": False,
        }
