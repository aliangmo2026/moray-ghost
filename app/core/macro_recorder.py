"""宏录制会话（内存态，线程锁保护）。

录制期间主仓通过 /execute_action 触发的动作由路由层自动追加步骤；动作本身
仍走 controller 完整安全管道（前后审计截图落盘，不盲点）。锚点取自动作
【前】的全屏截图（重放时匹配的是点击前的样子；动作后抓屏会被打开的菜单/
高亮态污染）。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

_lock = threading.Lock()
_session: Optional[Dict[str, Any]] = None


class RecordingError(Exception):
    def __init__(self, status: int, error: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.error = error
        self.detail = detail


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def start(task_id: str) -> Dict[str, Any]:
    global _session
    with _lock:
        if _session is not None:
            raise RecordingError(409, "ALREADY_RECORDING",
                                 "recording already active for task "
                                 + repr(_session["task_id"]))
        _session = {"task_id": task_id, "steps": [], "started_at": _now()}
        return {"task_id": task_id, "started_at": _session["started_at"]}


def stop(task_id: str) -> List[Dict[str, Any]]:
    global _session
    with _lock:
        if _session is None:
            raise RecordingError(409, "NOT_RECORDING", "no active recording session")
        if _session["task_id"] != task_id:
            raise RecordingError(409, "TASK_MISMATCH",
                                 "active session is " + repr(_session["task_id"])
                                 + ", got " + repr(task_id))
        steps = _session["steps"]
        _session = None
        return steps


def active_task_id() -> Optional[str]:
    with _lock:
        return _session["task_id"] if _session is not None else None


def add_step(step: Dict[str, Any]) -> int:
    with _lock:
        if _session is None:
            raise RecordingError(409, "NOT_RECORDING", "no active recording session")
        step["step_id"] = len(_session["steps"]) + 1
        _session["steps"].append(step)
        return step["step_id"]
