"""任务生命周期接口（v0.4 M3）：POST /task、start、GET、resume、cancel。

鉴权由 app.main 的 token 中间件统一强校验（/task/* 前缀）。
start/resume 前置环境门禁：锁屏/安全桌面/不可读 → 503 SYSTEM_NOT_READABLE。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core import environment
from app.core import task_runner
from app.core.task_runner import TaskError

router = APIRouter()


class TaskCreateBody(BaseModel):
    task_id: Optional[str] = Field(default=None, min_length=1)
    goal: Optional[str] = None
    steps: List[Dict[str, Any]]


def _err(exc: TaskError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={
        "ok": False, "error": exc.error, "detail": exc.detail})


@router.post("/task")
def create_task(body: TaskCreateBody):
    try:
        steps = task_runner.validate_steps(body.steps)
        task = task_runner.create_task(steps, body.goal, body.task_id)
    except TaskError as exc:
        return _err(exc)
    return JSONResponse(status_code=201, content={
        "ok": True, "task_id": task.task_id, "status": task.status,
        "steps_total": len(task.steps)})


@router.post("/task/{task_id}/start")
def start_task(task_id: str):
    task = task_runner.get_task(task_id)
    if task is None:
        return JSONResponse(status_code=404, content={
            "ok": False, "error": "NOT_FOUND",
            "detail": "task " + repr(task_id) + " not found"})
    try:
        task_runner.start_task(task)
    except TaskError as exc:
        return _err(exc)
    return JSONResponse(status_code=202, content={
        "ok": True, "task_id": task.task_id, "status": task.status})


@router.get("/task/{task_id}")
def get_task(task_id: str):
    task = task_runner.get_task(task_id)
    if task is None:
        return JSONResponse(status_code=404, content={
            "ok": False, "error": "NOT_FOUND",
            "detail": "task " + repr(task_id) + " not found"})
    return task.view()


@router.post("/task/{task_id}/resume")
def resume_task(task_id: str):
    task = task_runner.get_task(task_id)
    if task is None:
        return JSONResponse(status_code=404, content={
            "ok": False, "error": "NOT_FOUND",
            "detail": "task " + repr(task_id) + " not found"})
    try:
        task_runner.resume_task(task)
    except TaskError as exc:
        return _err(exc)
    return JSONResponse(status_code=202, content={
        "ok": True, "task_id": task.task_id, "status": task.status})


@router.post("/task/{task_id}/cancel")
def cancel_task(task_id: str):
    task = task_runner.get_task(task_id)
    if task is None:
        return JSONResponse(status_code=404, content={
            "ok": False, "error": "NOT_FOUND",
            "detail": "task " + repr(task_id) + " not found"})
    try:
        state = task_runner.cancel_task(task)
    except TaskError as exc:
        return _err(exc)
    return {"ok": True, "task_id": task.task_id, "status": state}
