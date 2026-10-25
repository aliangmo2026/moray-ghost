"""任务生命周期接口（v0.5 平台化）：POST /task、start、GET、resume、cancel、GET /tasks。

鉴权由 app.main 的 token 中间件统一强校验（/task/* 前缀）。
start/resume 前置环境门禁：锁屏/安全桌面/不可读 → 503 SYSTEM_NOT_READABLE。

v0.5 多 Agent：
- 请求头 X-Agent-Id 标识调用方；任务创建时记录归属。
- 跨 Agent 带【不同】id 访问 → 403 AGENT_FORBIDDEN；不带请求头 = 本机运维方，
  全量可见（向后兼容旧脚本）。
- start/resume 进入全局 FIFO 队列（task_queue）串行执行；GET 返回 queue_position。
- 冻结期（emergency stop）start/resume → 503 GHOST_FROZEN。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.core import task_queue
from app.core import task_runner
from app.core.task_runner import TaskError

router = APIRouter(prefix="/api/ghost")

AGENT_HEADER = "X-Agent-Id"


class TaskCreateBody(BaseModel):
    model_config = ConfigDict(extra="allow")

    task_id: Optional[str] = Field(default=None, min_length=1)
    goal: Optional[str] = None
    steps: list[Dict[str, Any]]


def _err(exc: TaskError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={
        "ok": False, "error": exc.error, "detail": exc.detail})


def _agent_id(request: Request) -> Optional[str]:
    """请求头里的 Agent 标识；空/缺失 → None（本机运维方视角）。"""
    return (request.headers.get(AGENT_HEADER) or "").strip() or None


def _forbidden(task: task_runner.TaskRun, agent: Optional[str]) -> Optional[JSONResponse]:
    """跨 Agent 隔离：任务有归属且请求带了不同的 agent id → 403。"""
    if agent and task.agent_id and task.agent_id != agent:
        return JSONResponse(status_code=403, content={
            "ok": False, "error": "AGENT_FORBIDDEN",
            "detail": ("task " + repr(task.task_id) + " belongs to agent "
                       + repr(task.agent_id) + "; caller agent is " + repr(agent))})
    return None


def _load(task_id: str, agent: Optional[str]):
    """取任务 + 归属校验。命中 404/403 时返回 (None, response)。"""
    task = task_runner.get_task(task_id)
    if task is None:
        return None, JSONResponse(status_code=404, content={
            "ok": False, "error": "NOT_FOUND",
            "detail": "task " + repr(task_id) + " not found"})
    resp = _forbidden(task, agent)
    if resp is not None:
        return None, resp
    return task, None


def _frozen() -> Optional[JSONResponse]:
    if task_queue.is_frozen():
        return JSONResponse(status_code=503, content={
            "ok": False, "error": "GHOST_FROZEN",
            "detail": "ghost is frozen by emergency stop at "
                      + str(task_queue.frozen_at())
                      + "; POST /api/ghost/emergency_stop/release to unfreeze"})
    return None


@router.post("/task")
def create_task(body: TaskCreateBody, request: Request):
    try:
        steps = task_runner.validate_steps(body.steps)
        task = task_runner.create_task(steps, body.goal, body.task_id,
                                       agent_id=_agent_id(request))
    except TaskError as exc:
        return _err(exc)
    return JSONResponse(status_code=201, content={
        "ok": True, "task_id": task.task_id, "status": task.status,
        "agent_id": task.agent_id, "steps_total": len(task.steps)})


@router.post("/task/{task_id}/start")
def start_task(task_id: str, request: Request):
    agent = _agent_id(request)
    task, denied = _load(task_id, agent)
    if denied is not None:
        return denied
    frozen = _frozen()
    if frozen is not None:
        return frozen
    try:
        task_runner.start_task(task)
    except TaskError as exc:
        return _err(exc)
    return JSONResponse(status_code=202, content={
        "ok": True, "task_id": task.task_id, "status": task.status,
        "queue_position": task_queue.position(task.task_id)})


@router.get("/task/{task_id}")
def get_task(task_id: str, request: Request):
    agent = _agent_id(request)
    task, denied = _load(task_id, agent)
    if denied is not None:
        return denied
    return task.view()


@router.post("/task/{task_id}/resume")
def resume_task(task_id: str, request: Request):
    agent = _agent_id(request)
    task, denied = _load(task_id, agent)
    if denied is not None:
        return denied
    frozen = _frozen()
    if frozen is not None:
        return frozen
    try:
        task_runner.resume_task(task)
    except TaskError as exc:
        return _err(exc)
    return JSONResponse(status_code=202, content={
        "ok": True, "task_id": task.task_id, "status": task.status,
        "queue_position": task_queue.position(task.task_id)})


@router.post("/task/{task_id}/cancel")
def cancel_task(task_id: str, request: Request):
    agent = _agent_id(request)
    task, denied = _load(task_id, agent)
    if denied is not None:
        return denied
    try:
        state = task_runner.cancel_task(task)
    except TaskError as exc:
        return _err(exc)
    return {"ok": True, "task_id": task.task_id, "status": state}


@router.get("/tasks")
def list_tasks(request: Request,
               limit: int = Query(default=50, ge=1, le=500)):
    """任务列表。带 X-Agent-Id 只见自己的任务；不带 = 运维方全量视图。"""
    agent = _agent_id(request)
    items = task_runner.list_tasks(agent_id=agent, limit=limit)
    return {"ok": True, "agent_id": agent, "count": len(items), "tasks": items}


@router.post("/emergency_stop")
def emergency_stop():
    """紧急停止：清空任务队列（排队任务全部 canceled）+ 冻结 Ghost。

    冻结生效后 start/resume/execute_action/宏重放一律 503 GHOST_FROZEN；
    正在跑的任务在下一个步骤边界安全点停止（绝不切断进行中的输入）。
    解冻：POST /api/ghost/emergency_stop/release。
    """
    return {"ok": True, **task_queue.freeze()}


@router.post("/emergency_stop/release")
def emergency_stop_release():
    task_queue.release()
    return {"ok": True, "frozen": False}
