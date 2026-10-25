"""任务生命周期执行器（v0.5 平台化）。

状态机：created → queued → running → done | degraded | canceled
（start/resume 先入 FIFO 队列（task_queue），单调度线程串行派发；
 degraded ⇄ running 经 resume 重新排队）。
- 步骤类型：macro（走 runner 完整重放管道）/ action（走 controller 安全管道）/
  wait（可被 cancel 打断）/ require_vision（degraded 挂起，等主仓视觉后 resume 跳过）。
- 任一步骤失败 → 任务 degraded 挂起（线程退出），fail 信息（fail_class/现场截图/
  run_id）挂在任务上等主仓处置；resume 从失败步重试（重新排队）。
- cancel 在步骤边界安全点生效，绝不切断进行中的输入；created/queued/degraded 下直接取消。
- Agent 隔离：create_task 记录归属 agent（来自 X-Agent-Id 请求头），跨 Agent 访问
  由路由层拒绝（403）；不带头 = 本机运维方，全量可见。
- 所有输入仍走 controller/runner 的完整安全管道与环境门禁（start/resume 前置校验，
  派发时复检）。
- 注册表为内存态：服务重启即失（任务持久化列入 backlog）。
"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import environment, task_queue
from .controller import ActionError, run_action
from .macro_runner import ReplayError, replay as macro_replay

VALID_KINDS = ("macro", "action", "wait", "require_vision")
TERMINAL_STATES = ("done", "failed", "canceled")


class TaskError(Exception):
    """带 HTTP 语义的任务操作错误。"""

    def __init__(self, status: int, error: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.error = error
        self.detail = detail


class StepFailure(Exception):
    """步骤失败（含 require_vision 的挂起语义）；data 并入步骤状态。"""

    def __init__(self, fail_class: str, detail: str, **data: Any):
        super().__init__(detail)
        self.fail_class = fail_class
        self.detail = detail
        self.data = data


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def validate_steps(steps: Any) -> List[Dict[str, Any]]:
    """POST /task 的步骤规范化与校验；非法抛 TaskError(400)。"""
    if not isinstance(steps, list) or not steps:
        raise TaskError(400, "BAD_REQUEST", "steps must be a non-empty array")
    out: List[Dict[str, Any]] = []
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            raise TaskError(400, "BAD_REQUEST",
                            "step " + str(i + 1) + " must be an object")
        kind = str(s.get("kind", "")).strip().lower()
        if kind not in VALID_KINDS:
            raise TaskError(400, "BAD_REQUEST",
                            "step " + str(i + 1) + ": unknown kind " + repr(kind)
                            + " (valid: " + ", ".join(VALID_KINDS) + ")")
        step: Dict[str, Any] = {"step_id": i + 1, "kind": kind}
        if kind == "macro":
            mid = s.get("macro_id")
            if not mid:
                raise TaskError(400, "BAD_REQUEST",
                                "step " + str(i + 1) + ": macro step requires macro_id")
            hwnd = s.get("hwnd")
            if not isinstance(hwnd, int) or hwnd <= 0:
                raise TaskError(400, "BAD_REQUEST",
                                "step " + str(i + 1)
                                + ": macro step requires a positive hwnd "
                                  "(主仓重开应用后传新 hwnd)")
            step["macro_id"] = str(mid)
            step["hwnd"] = hwnd
        elif kind == "action":
            payload = s.get("payload")
            if not isinstance(payload, dict) or not payload.get("action"):
                raise TaskError(400, "BAD_REQUEST",
                                "step " + str(i + 1)
                                + ": action step requires payload.action")
            step["payload"] = payload
        elif kind == "wait":
            ms = s.get("ms", 0)
            if not isinstance(ms, int) or ms < 0 or ms > 60000:
                raise TaskError(400, "BAD_REQUEST",
                                "step " + str(i + 1) + ": wait.ms must be int 0..60000")
            step["ms"] = ms
        elif kind == "require_vision":
            step["prompt"] = s.get("prompt")
        out.append(step)
    return out


class TaskRun:
    """一个任务的生命周期状态。所有读写经 lock；view() 输出快照。"""

    def __init__(self, task_id: str, steps: List[Dict[str, Any]],
                 goal: Optional[str], agent_id: Optional[str] = None):
        self.task_id = task_id
        self.goal = goal
        self.agent_id = agent_id      # 归属 Agent（X-Agent-Id）；None=本机运维方
        self.steps = steps
        self.status = "created"
        self.step_states: List[Dict[str, Any]] = [
            {"step_id": s["step_id"], "kind": s["kind"], "status": "pending"}
            for s in steps
        ]
        self.current_index = 0          # 下一个要执行的步骤下标
        self.cancel_event = threading.Event()
        self.lock = threading.RLock()
        self.fail: Optional[Dict[str, Any]] = None
        self.created_at = _now()
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None

    def view(self) -> Dict[str, Any]:
        with self.lock:
            current = (self.steps[self.current_index]["step_id"]
                       if self.current_index < len(self.steps) else None)
            return {
                "ok": True,
                "task_id": self.task_id,
                "agent_id": self.agent_id,
                "goal": self.goal,
                "status": self.status,
                "queue_position": task_queue.position(self.task_id),
                "steps_total": len(self.steps),
                "current_step_id": current,
                "step_states": [dict(s) for s in self.step_states],
                "fail": dict(self.fail) if self.fail else None,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            }

    def brief(self) -> Dict[str, Any]:
        """列表用摘要（不含 step_states/fail 大字段）。"""
        with self.lock:
            current = (self.steps[self.current_index]["step_id"]
                       if self.current_index < len(self.steps) else None)
            return {
                "task_id": self.task_id,
                "agent_id": self.agent_id,
                "goal": self.goal,
                "status": self.status,
                "queue_position": task_queue.position(self.task_id),
                "steps_total": len(self.steps),
                "current_step_id": current,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            }


_tasks: Dict[str, TaskRun] = {}
_reg_lock = threading.Lock()


def create_task(steps: List[Dict[str, Any]], goal: Optional[str],
                task_id: Optional[str], agent_id: Optional[str] = None) -> TaskRun:
    tid = str(task_id) if task_id else "task_" + uuid.uuid4().hex[:12]
    with _reg_lock:
        if tid in _tasks:
            raise TaskError(409, "TASK_ID_CONFLICT",
                            "task " + repr(tid) + " already exists")
        task = TaskRun(tid, steps, goal, agent_id=agent_id)
        _tasks[tid] = task
        return task


def get_task(task_id: str) -> Optional[TaskRun]:
    with _reg_lock:
        return _tasks.get(task_id)


def list_tasks(agent_id: Optional[str] = None,
               limit: int = 50) -> List[Dict[str, Any]]:
    """任务列表摘要，按创建时间倒序。

    agent_id 给出时（带 X-Agent-Id 的 Agent 视角）只返回归属该 Agent 的任务；
    None（本机运维方）返回全部。
    """
    with _reg_lock:
        items = [t for t in _tasks.values()
                 if agent_id is None or t.agent_id == agent_id]
    items.sort(key=lambda t: t.created_at, reverse=True)
    return [t.brief() for t in items[:limit]]


def _spawn(task: TaskRun) -> None:
    """（v0.5 已废弃独立线程）保留占位防止外部误用；执行统一走 task_queue 调度。"""
    raise NotImplementedError("v0.5: tasks are dispatched via task_queue (start_task enqueues)")


def start_task(task: TaskRun) -> None:
    try:
        environment.assert_input_allowed()
    except environment.SystemNotReadable as exc:
        raise TaskError(503, "SYSTEM_NOT_READABLE", exc.detail)
    with task.lock:
        if task.status != "created":
            raise TaskError(409, "INVALID_STATE",
                            "start requires status=created, got " + task.status)
        task.status = "queued"      # 派发线程把它排队到 running（FIFO）
    task_queue.enqueue(task)


def resume_task(task: TaskRun) -> None:
    try:
        environment.assert_input_allowed()
    except environment.SystemNotReadable as exc:
        raise TaskError(503, "SYSTEM_NOT_READABLE", exc.detail)
    with task.lock:
        if task.status != "degraded":
            raise TaskError(409, "INVALID_STATE",
                            "resume requires status=degraded, got " + task.status)
        # require_vision 挂起点：主仓已完成视觉处置，标记该步完成并继续
        if task.fail and task.fail.get("await_vision"):
            idx = task.current_index
            task.step_states[idx]["status"] = "done"
            task.step_states[idx]["result"] = {
                "skipped": "vision handled by main repo"}
            task.current_index = idx + 1
        task.fail = None
        task.status = "queued"      # 重新排队（队尾），由调度线程串行派发
    task_queue.enqueue(task)


def cancel_task(task: TaskRun) -> str:
    with task.lock:
        if task.status in TERMINAL_STATES:
            raise TaskError(409, "INVALID_STATE",
                            "task already terminal: " + task.status)
        task.cancel_event.set()
        if task.status in ("created", "degraded", "queued"):
            # 线程未在跑（或挂起/排队）：出队并直接落终态
            if task.status == "queued":
                task_queue.remove(task.task_id)
            task.status = "canceled"
            task.finished_at = _now()
        # running：线程在下一个步骤边界安全点自行落 canceled 状态
        return task.status


def _execute(task: TaskRun) -> None:
    """顺序跑步骤直至终态/degraded 挂起；由 task_queue 调度线程同步调用（v0.5 起不再独立开线程）。"""
    while True:
        with task.lock:
            if task.cancel_event.is_set():
                task.status = "canceled"
                task.finished_at = _now()
                return
            idx = task.current_index
            if idx >= len(task.steps):
                task.status = "done"
                task.finished_at = _now()
                return
            task.step_states[idx]["status"] = "executing"
        step = task.steps[idx]
        try:
            result = _run_step(task, step)
        except StepFailure as f:
            with task.lock:
                macro_step = (f.data or {}).get("step_id")
                task.step_states[idx]["status"] = "failed"
                task.step_states[idx].update(f.data or {})
                task.step_states[idx]["step_id"] = idx + 1  # 任务级 step_id 不被宏内部步号覆盖
                if f.fail_class == "CANCELED":
                    task.status = "canceled"
                    task.finished_at = _now()
                else:
                    task.fail = {"step_id": idx + 1, "fail_class": f.fail_class,
                                 "detail": f.detail}
                    task.fail.update(f.data or {})
                    if macro_step is not None:
                        task.fail["macro_step_id"] = macro_step
                    task.fail["step_id"] = idx + 1
                    task.status = "degraded"
            return
        except Exception as exc:  # noqa: BLE001
            with task.lock:
                task.step_states[idx]["status"] = "failed"
                task.fail = {"step_id": idx + 1, "fail_class": "INTERNAL_ERROR",
                             "detail": type(exc).__name__ + ": " + str(exc)}
                task.status = "degraded"
            return
        with task.lock:
            task.step_states[idx]["status"] = "done"
            task.step_states[idx]["result"] = result
            task.current_index = idx + 1


def _run_step(task: TaskRun, step: Dict[str, Any]) -> Dict[str, Any]:
    kind = step["kind"]
    if kind == "macro":
        try:
            out = macro_replay(step["macro_id"],
                               hwnd_override=step.get("hwnd"),
                               task_id=task.task_id)
        except ReplayError as exc:
            data = dict(exc.data or {})
            raise StepFailure(fail_class=exc.error, detail=exc.detail,
                              **data) from exc
        return {"run_id": out.get("run_id"), "steps_ok": out.get("steps_ok")}
    if kind == "action":
        payload = dict(step.get("payload") or {})
        try:
            out = run_action(payload)
        except ActionError as exc:
            raise StepFailure(fail_class=exc.error, detail=exc.detail,
                              **dict(exc.data)) from exc
        return {"before_screenshot_path": out.get("before_screenshot_path"),
                "after_screenshot_path": out.get("after_screenshot_path")}
    if kind == "wait":
        ms = int(step.get("ms") or 0)
        end = time.time() + ms / 1000.0
        while time.time() < end:
            if task.cancel_event.is_set():
                raise StepFailure(fail_class="CANCELED",
                                  detail="canceled during wait",
                                  canceled=True)
            time.sleep(0.15)
        return {"waited_ms": ms}
    if kind == "require_vision":
        raise StepFailure(fail_class="REQUIRE_VISION",
                          detail="awaiting main-repo vision",
                          await_vision=True, prompt=step.get("prompt"))
    raise StepFailure(fail_class="INTERNAL_ERROR",
                      detail="unknown kind " + repr(kind))
