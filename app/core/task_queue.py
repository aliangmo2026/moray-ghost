"""任务级 FIFO 队列调度器（v0.5 平台化 → v0.17 并发工作池）：多 Agent 排队 + 并发执行 + 紧急冻结。

与 task_runner 的分工：
- task_runner 管单个任务的状态机与步骤执行（_execute）；
- task_queue 管"同一时刻哪些任务上桌面"：v0.17 起是固定大小工作池
  （GHOST_MAX_CONCURRENT_TASKS，默认 4），按入队顺序取任务并发执行。
  真正能否并行由 controller 的细粒度窗口锁决定：不同 hwnd 的 virtual 动作
  完全并行；同一 hwnd 的动作自动串行；physical 通道仍受全局物理锁约束
  （全系统只有一套鼠标键盘，这是 OS 事实）。

隔离语义（X-Agent-Id，路由层落实）：
- 任务创建时记录归属 agent；跨 Agent 带不同 id 访问 → 403 AGENT_FORBIDDEN；
- 不带请求头视为本机运维方，全量可见（向后兼容旧脚本）。

紧急冻结（emergency stop）：
- freeze()：清空队列（排队任务一律 canceled + EMERGENCY_STOP）并置冻结位，
  所有运行中任务置 cancel_event，在下一个步骤边界安全点落 canceled；
- 冻结期间 start/resume/execute_action/宏重放一律 503 GHOST_FROZEN；
- release() 解冻，队列恢复派发。

本模块不 import task_runner（避免循环依赖）；TaskRun 对象由调用方传入，
调度循环内才延迟 import 其 _execute。
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import authz, config

LOG = logging.getLogger("moray-ghost.task_queue")

_queue: deque = deque()             # 待派发 TaskRun（FIFO；对象直存，免注册表查询）
_qlock = threading.Lock()
_wake = threading.Event()           # enqueue 唤醒工作线程
_stop = threading.Event()
_frozen = threading.Event()
_frozen_at: Optional[str] = None
_frozen_reason: Optional[str] = None   # v0.7.2：emergency_stop / rate_circuit_break / action_hard_timeout
_running: Dict[str, Any] = {}        # v0.17：并发运行中的 TaskRun（task_id → task）
_workers: List[threading.Thread] = []


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def enqueue(task: Any) -> int:
    """任务入队，返回入队后排位（1-based）。"""
    with _qlock:
        _queue.append(task)
        pos = len(_queue)
    _wake.set()
    return pos


def remove(task_id: str) -> bool:
    """从队列摘除任务（cancel 已排队任务时用）；不在队列返回 False。"""
    with _qlock:
        for i, t in enumerate(_queue):
            if t.task_id == task_id:
                del _queue[i]
                return True
    return False


def position(task_id: str) -> Optional[int]:
    """任务当前排位（1-based）；不在队列（running/终态/未知）返回 None。"""
    with _qlock:
        for i, t in enumerate(_queue):
            if t.task_id == task_id:
                return i + 1
    return None


def depth() -> int:
    with _qlock:
        return len(_queue)


def _view_of(task: Any) -> Dict[str, Any]:
    view = task.view()
    return {k: view[k] for k in ("task_id", "agent_id", "goal", "status",
                                 "steps_total", "current_step_id", "started_at")}


def running_views() -> List[Dict[str, Any]]:
    """所有正在执行任务的摘要（v0.17 并发工作池；dashboard / state 用）。"""
    with _qlock:
        tasks = list(_running.values())
    return [_view_of(t) for t in tasks]


def running_view() -> Optional[Dict[str, Any]]:
    """正在执行任务的摘要（向后兼容单任务视图：取第一个）。"""
    views = running_views()
    return views[0] if views else None


def running_count() -> int:
    with _qlock:
        return len(_running)


def snapshot() -> List[Dict[str, Any]]:
    """队列快照（dashboard / state 用）：排队任务按排序列出。"""
    with _qlock:
        return [{"task_id": t.task_id, "agent_id": getattr(t, "agent_id", None),
                 "goal": t.goal, "status": t.status, "queue_position": i + 1}
                for i, t in enumerate(_queue)]


def is_frozen() -> bool:
    return _frozen.is_set()


def frozen_at() -> Optional[str]:
    return _frozen_at


def frozen_reason() -> Optional[str]:
    """v0.7.2：冻结原因（emergency_stop / rate_circuit_break / action_hard_timeout）。"""
    return _frozen_reason


def assert_not_frozen() -> None:
    """冻结期门禁：由路由层在 start/resume/replay/execute_action 前调用。"""
    if _frozen.is_set():
        raise FrozenError()


class FrozenError(Exception):
    """Ghost 已被 emergency stop 冻结（HTTP 503 GHOST_FROZEN）。"""


def freeze(reason: str = "emergency_stop") -> Dict[str, Any]:
    """紧急停止：清空队列 + 冻结 + 请求所有运行中任务在步骤边界停止。"""
    global _frozen_at, _frozen_reason
    canceled: List[str] = []
    with _qlock:
        _frozen.set()
        pending = list(_queue)
        _queue.clear()
        running = list(_running.values())
    _frozen_at = _now()
    _frozen_reason = str(reason)
    for task in pending:
        with task.lock:
            if task.status in ("created", "queued", "degraded"):
                task.cancel_event.set()
                task.status = "canceled"
                task.finished_at = _now()
                task.fail = {"fail_class": "EMERGENCY_STOP",
                             "detail": "task removed from queue by emergency stop"}
                canceled.append(task.task_id)
    for task in running:
        task.cancel_event.set()  # 步骤边界安全点自行落 canceled，绝不切断进行中输入
    LOG.warning("EMERGENCY STOP (%s): queue cleared=%d canceled=%s running=%d %s",
                reason, len(pending), canceled, len(running),
                [getattr(t, "task_id", "?") for t in running])
    return {"frozen": True, "frozen_at": _frozen_at, "reason": reason,
            "canceled_tasks": canceled, "queue_cleared": len(pending),
            "running_cancel_requested": len(running)}


def release() -> None:
    """解除冻结，恢复派发；一并解除频率熔断（重新武装计数器）。"""
    global _frozen_at, _frozen_reason
    _frozen.clear()
    _frozen_at = None
    _frozen_reason = None
    from . import breaker  # 延迟 import：breaker 顶层依赖本模块的兄弟，防环
    breaker.reset()
    _wake.set()
    LOG.warning("emergency stop released; dispatch resumed")


def start_dispatcher() -> None:
    global _workers
    if any(w.is_alive() for w in _workers):
        return
    _stop.clear()
    n = max(1, int(config.MAX_CONCURRENT_TASKS))
    _workers = [threading.Thread(target=_worker_loop,
                                 name="task-worker-" + str(i + 1),
                                 daemon=True)
                for i in range(n)]
    for worker in _workers:
        worker.start()
    LOG.info("task dispatcher started (fifo, %d concurrent workers)", n)


def stop_dispatcher() -> None:
    _stop.set()
    _wake.set()


def _take_next() -> Optional[Any]:
    """取队首可派发任务并原子完成 queued→running 迁移（与 cancel/freeze 互斥）。

    v0.17：任务在拿到的一瞬间就登记进 _running（同一个 _qlock 内），
    freeze() 因此不可能漏掉「已出队但还没登记」的窗口期。
    """
    with _qlock:
        if _frozen.is_set():
            return None
        while _queue:
            task = _queue.popleft()
            with task.lock:
                if task.status == "queued":
                    task.status = "running"
                    if task.started_at is None:
                        task.started_at = _now()
                    _running[task.task_id] = task
                    return task
                # 已被 cancel/freeze 抢先落态：跳过脏条目
    return None


def _worker_loop() -> None:
    from . import task_runner  # 延迟 import：task_runner 顶层 import 本模块，避免环

    while not _stop.is_set():
        task = _take_next()
        if task is None:
            _wake.wait(0.3)
            _wake.clear()
            continue
        # v0.7.1：回放任务准入身份（哪个请求/ token 启动了本任务）——
        # 生产环境输入源过滤与步骤审计都读它；派发前后显式覆盖/清空，
        # 绝不让上一个任务的身份串进下一个。
        authz.set_ctx({**(getattr(task, "admission", None) or {}),
                       "source": "task_queue"})
        try:
            # 派发时环境门禁复检（start 到派发之间可能锁屏）：
            # 不可输入 → degraded 挂起，主环境恢复后 resume 重排队
            try:
                task_runner.environment.assert_input_allowed()
            except task_runner.environment.SystemNotReadable as exc:
                with task.lock:
                    task.status = "degraded"
                    task.fail = {"fail_class": "SYSTEM_NOT_READABLE",
                                 "detail": exc.detail}
                LOG.warning("task %s degraded at dispatch: %s",
                            task.task_id, exc.detail)
                continue
            LOG.info("dispatch task %s agent=%s steps=%d worker=%s",
                     task.task_id, getattr(task, "agent_id", None),
                     len(task.steps), threading.current_thread().name)
            task_runner._execute(task)
        except Exception as exc:  # noqa: BLE001  工作线程绝不能死
            LOG.exception("worker error on task %s: %s",
                          getattr(task, "task_id", "?"), exc)
        finally:
            with _qlock:
                _running.pop(getattr(task, "task_id", ""), None)
            authz.set_ctx(None)  # 身份不跨任务残留
