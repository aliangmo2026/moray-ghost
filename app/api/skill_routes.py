"""技能库接口（v0.15 阶段三）：录制 start/stop + 技能 CRUD + 回放。

鉴权由 app.main 的 token 中间件统一强校验（/api/ghost/skill/* 在保护集内）。
回放与录制的所有真实输入仍走 controller.run_action / 无输入（录制只读）路径，
冻结期拒绝回放（与 /macro/{id}/replay 同一语义）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core import authz, skill_recorder, skill_store, task_queue

router = APIRouter(prefix="/api/ghost")


class RecordStartBody(BaseModel):
    name: Optional[str] = None  # 预留：停止时默认用此名（stop 仍可覆盖）


class RecordStopBody(BaseModel):
    name: Optional[str] = None


class RunBody(BaseModel):
    skill_id: Optional[str] = None
    name: Optional[str] = None
    capabilities: Optional[Dict[str, Any]] = None
    # 行为克隆噪声容忍：true=失败步（如录制混入的人类噪声点击，被能力白名单
    # 零输入拒绝）记账后继续；默认 false 严格终止。
    continue_on_error: bool = False


@router.post("/skill/record/start")
def skill_record_start(body: Optional[RecordStartBody] = None) -> Dict[str, Any]:
    try:
        return skill_recorder.start()
    except skill_recorder.RecorderError as exc:
        return JSONResponse(status_code=409, content={
            "ok": False, "error": "RECORDER_STATE", "detail": str(exc)})


@router.post("/skill/record/stop")
def skill_record_stop(body: Optional[RecordStopBody] = None) -> Dict[str, Any]:
    try:
        return skill_recorder.stop(name=(body.name if body else None))
    except skill_recorder.RecorderError as exc:
        return JSONResponse(status_code=409, content={
            "ok": False, "error": "RECORDER_STATE", "detail": str(exc)})


@router.get("/skill/record/status")
def skill_record_status() -> Dict[str, Any]:
    return {"ok": True, **skill_recorder.status()}


@router.get("/skill/list")
def skill_list() -> Dict[str, Any]:
    return {"ok": True, "skills": skill_store.list_skills()}


@router.get("/skill/{skill_id}")
def skill_get(skill_id: str) -> Dict[str, Any]:
    skill = skill_store.get_skill(skill_id) or skill_store.get_skill_by_name(skill_id)
    if not skill:
        return JSONResponse(status_code=404, content={
            "ok": False, "error": "SKILL_NOT_FOUND", "detail": skill_id})
    return {"ok": True, "skill": skill}


@router.post("/skill/run")
def skill_run(body: RunBody, request: Request) -> Dict[str, Any]:
    ref = (body.skill_id or body.name or "").strip()
    if not ref:
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "BAD_REQUEST",
            "detail": "skill_id or name is required"})
    # 身份上下文：回放每步走 run_action 同一咽喉，生产环境据此过滤输入源
    authz.bind_request(request)
    # 冻结门禁：emergency stop 期间禁止真实回放（与宏重放同一语义）
    if task_queue.is_frozen():
        return JSONResponse(status_code=503, content={
            "ok": False, "error": "GHOST_FROZEN",
            "detail": "ghost is frozen (" + str(task_queue.frozen_reason()) + ")",
            "frozen_reason": task_queue.frozen_reason()})
    try:
        return skill_recorder.run_skill(ref, capabilities=body.capabilities,
                                        continue_on_error=body.continue_on_error)
    except skill_recorder.RecorderError as exc:
        return JSONResponse(status_code=404, content={
            "ok": False, "error": "SKILL_NOT_FOUND", "detail": str(exc)})
    except Exception as exc:  # noqa: BLE001 environment/SystemNotReadable 等
        return JSONResponse(status_code=503, content={
            "ok": False, "error": "RUN_UNAVAILABLE",
            "detail": type(exc).__name__ + ": " + str(exc)[:300]})


@router.delete("/skill/{skill_id}")
def skill_delete(skill_id: str) -> Dict[str, Any]:
    skill = skill_store.get_skill(skill_id)
    if not skill:
        return JSONResponse(status_code=404, content={
            "ok": False, "error": "SKILL_NOT_FOUND", "detail": skill_id})
    skill_recorder.delete_skill_frames(skill)
    ok = skill_store.delete_skill(skill["id"])
    return {"ok": bool(ok), "deleted": skill["id"], "name": skill["name"]}
