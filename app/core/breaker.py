"""动作频率熔断（v0.7.2 防 AI 死循环）：滑窗频率 + 连续重复双阈值。

判定对象是**已通过全部前置校验、真正要上桌面**的动作（在 ACTION_LOCK 内登记），
指纹 = (channel, action, x, y, text 前缀…)——被 401/400 拒绝的调用不计数，
纯校验失败不会误熔断。

双阈值（任一命中即熔断）：
- 频率：同一指纹在 RATE_LIMIT_WINDOW（默认 1s）内执行超过 RATE_LIMIT_COUNT
  （默认 10）次 —— 死循环式连点；
- 连续：同一指纹连续执行超过 RATE_LIMIT_CONSECUTIVE（默认 50）次（不看窗口）
  —— 慢速但单调的死循环。

熔断动作：置 tripped 位（永久直到 release 解冻）→ 调用方负责
audit + task_queue.freeze("rate_circuit_break")——冻结后一切后续动作
503 GHOST_FROZEN（reason=rate_circuit_break），主仓经 dashboard/state 与
错误详情收到通知。emergency_stop release 会一并 reset 本模块（重新武装）。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Deque, Dict, Optional, Tuple

from . import config

_lock = threading.Lock()
_windows: Dict[Tuple, Deque[float]] = {}
_consecutive_fp: Optional[Tuple] = None
_consecutive_n = 0
_tripped = False
_trip_info: Optional[Dict[str, Any]] = None


def make_fingerprint(channel: str, action: str, x: Any, y: Any,
                     text: Any = None) -> Tuple:
    """动作指纹：同目标同动作才算"重复"。text 只取前缀（防长文本 OOM）。"""
    txt = str(text)[:32] if text is not None else None
    return (str(channel), str(action), x, y, txt)


def hit(fp: Tuple) -> Optional[Dict[str, Any]]:
    """登记一次真实执行。命中熔断 → 返回 trip 信息并置位；正常 → None。

    调用方必须持有 controller.ACTION_LOCK（串行登记，窗口计数无并发歧义）。
    """
    global _consecutive_fp, _consecutive_n, _tripped, _trip_info
    now = time.monotonic()
    with _lock:
        if _tripped:
            return _trip_info
        # 滑窗计数
        dq = _windows.get(fp)
        if dq is None:
            dq = deque()
            _windows[fp] = dq
        cutoff = now - config.RATE_LIMIT_WINDOW
        while dq and dq[0] < cutoff:
            dq.popleft()
        dq.append(now)
        # 连续计数
        if fp == _consecutive_fp:
            _consecutive_n += 1
        else:
            _consecutive_fp = fp
            _consecutive_n = 1
        window_n = len(dq)
        if window_n > config.RATE_LIMIT_COUNT:
            return _lock_trip({
                "rule": "window", "fingerprint": list(map(str, fp)),
                "window_seconds": config.RATE_LIMIT_WINDOW,
                "window_hits": window_n,
                "limit": config.RATE_LIMIT_COUNT,
            })
        if _consecutive_n > config.RATE_LIMIT_CONSECUTIVE:
            return _lock_trip({
                "rule": "consecutive", "fingerprint": list(map(str, fp)),
                "consecutive_hits": _consecutive_n,
                "limit": config.RATE_LIMIT_CONSECUTIVE,
            })
        return None


def _lock_trip(info: Dict[str, Any]) -> Dict[str, Any]:
    """（持锁调用）置熔断位并封存 trip 信息。"""
    global _tripped, _trip_info
    _tripped = True
    _trip_info = dict(info)
    _trip_info["tripped_at"] = time.time()
    return _trip_info


def trip_now(info: Dict[str, Any]) -> Dict[str, Any]:
    """外部强制熔断（超时等异常路径复用同一熔断态）。"""
    global _tripped, _trip_info
    with _lock:
        if not _tripped:
            _tripped = True
            _trip_info = dict(info)
            _trip_info.setdefault("tripped_at", time.time())
        return _trip_info


def is_tripped() -> bool:
    with _lock:
        return _tripped


def trip_info() -> Optional[Dict[str, Any]]:
    with _lock:
        return dict(_trip_info) if _trip_info else None


def reset() -> None:
    """解冻时重新武装（emergency_stop release 会调用）。"""
    global _consecutive_fp, _consecutive_n, _tripped, _trip_info
    with _lock:
        _windows.clear()
        _consecutive_fp = None
        _consecutive_n = 0
        _tripped = False
        _trip_info = None


def snapshot() -> Dict[str, Any]:
    """状态快照（dashboard/state 用）。"""
    with _lock:
        return {"tripped": _tripped, "trip_info": dict(_trip_info)
                if _trip_info else None,
                "tracked_fingerprints": len(_windows)}


# ---- 融合处置（熔断/超时共用）：审计 + 冻结，通知主仓 ----------------------
def engage(reason: str, info: Dict[str, Any]) -> Dict[str, Any]:
    """熔断/硬超时的统一处置：audit 落盘 + 冻结服务（frozen_reason=reason）。"""
    from . import audit_sink, task_queue  # 延迟 import 避免模块环
    payload = dict(info)
    payload["reason"] = reason
    try:
        audit_sink.write(reason, **payload)
    except Exception:  # noqa: BLE001
        pass
    frozen = task_queue.freeze(reason)
    frozen["trip_info"] = info
    return frozen
