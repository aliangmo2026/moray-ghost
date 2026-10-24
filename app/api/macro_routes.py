"""宏系统骨架路由（v0.2）：schema、鉴权、路由就位；状态型操作 501 占位。

- GET  /macro                 列表（已可用，读 SQLite；验收要求返回空数组）
- GET  /macro/{macro_id}      详情（已可用）
- POST /macro/record/start    录制开始（501，v0.3）
- POST /macro/record/stop     录制结束（501，v0.3）
- POST /macro/{id}/replay     重放（501，v0.3；届时必须走 controller.run_action 同一安全管道）
- POST /macro/{id}/heal       主仓回填修补锚点（501，v0.3）

所有 /macro/* 均由 app.main 的 token 中间件强校验 X-Ghost-Token。
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core import environment, macro_matcher, macro_recorder, macro_runner
from app.core import macro_store, screen_mss

router = APIRouter()


class ReplayBody(BaseModel):
    dry_run: bool = False
    allow_fallback: bool = False
    task_id: Optional[str] = None
    # 重放目标窗口覆盖：主仓重开目标应用后把新 hwnd 传进来，所有步骤改用该窗口
    # （不做 class 模糊重绑定，避免绑到用户窗口）。缺省用各步录制时的 hwnd。
    hwnd: Optional[int] = None


class VisionPayload(BaseModel):
    found: bool = True
    center: Optional[List[int]] = Field(default=None,
                                        description="全屏物理像素（Iris 认定的按钮新位置）")
    crop: Optional[str] = Field(default=None,
                                description="mode=reanchor 必填：新锚点图 base64 PNG")


class HealBody(BaseModel):
    task_id: Optional[str] = None
    step_id: int = Field(..., ge=1)
    reason: Optional[str] = None
    mode: str = Field(..., pattern="^(relocate|reanchor)$")
    base_version: int = Field(..., ge=1, description="乐观锁：须等于当前 macros.version")
    vision: VisionPayload


class RecordStartBody(BaseModel):
    task_id: str = Field(..., min_length=1)


class RecordStopBody(BaseModel):
    task_id: str = Field(..., min_length=1)
    name: Optional[str] = None
    app_signature: Optional[str] = None


class ProbeBody(BaseModel):
    screenshot_crop: str = Field(..., description="模板小图（base64 PNG，物理像素裁剪）")
    match_threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    monitor: int = Field(default=1, ge=1)
    window_hwnd: Optional[int] = Field(default=None,
                                       description="给出时只在该窗口当前 rect 内匹配（每次实时取 rect，不缓存）")


@router.get("/macro")
def list_macros() -> Dict[str, Any]:
    macros = macro_store.list_macros()
    return {"ok": True, "count": len(macros), "macros": macros}


@router.get("/macro/{macro_id}")
def get_macro(macro_id: str):
    row = macro_store.get_macro(macro_id)
    if row is None:
        return JSONResponse(status_code=404, content={
            "ok": False, "error": "NOT_FOUND", "detail": f"macro {macro_id!r} not found"})
    row["steps"] = json.loads(row.pop("steps_json") or "[]")
    return {"ok": True, "macro": row}


@router.post("/macro/record/start")
def record_start(body: RecordStartBody):
    """开启录制会话。录制期间 /execute_action 的每个成功动作自动追加步骤。"""
    try:
        info = macro_recorder.start(body.task_id)
    except macro_recorder.RecordingError as exc:
        return JSONResponse(status_code=exc.status, content={
            "ok": False, "error": exc.error, "detail": exc.detail})
    return {"ok": True, "recording": True, **info}


@router.post("/macro/record/stop")
def record_stop(body: RecordStopBody):
    """结束录制并把步骤写入 SQLite macros 表，返回 macro_id。"""
    try:
        steps = macro_recorder.stop(body.task_id)
    except macro_recorder.RecordingError as exc:
        return JSONResponse(status_code=exc.status, content={
            "ok": False, "error": exc.error, "detail": exc.detail})
    if not steps:
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "BAD_REQUEST",
            "detail": "no steps recorded for task " + repr(body.task_id)})
    name = body.name or ("macro_" + body.task_id + "_" + str(int(time.time())))
    try:
        macro_id = macro_store.create_macro(name, body.app_signature,
                                            json.dumps(steps))
    except macro_store.MacroNameConflictError as exc:
        return JSONResponse(status_code=409, content={
            "ok": False, "error": "NAME_CONFLICT", "detail": str(exc)})
    return {"ok": True, "macro_id": macro_id, "name": name, "steps": len(steps)}


@router.post("/macro/probe")
def probe(body: ProbeBody):
    """锚点匹配查询（只读，不执行任何动作，不做重放）。

    四态语义（铁律）：
    - matched    → 200，center 为全屏物理像素（无论是否用窗口 ROI），可直接喂
                   /execute_action（不带 source_width）
    - not_found  → 200，抓屏成功但搜索区内没有该锚点——这才允许触发"降级让主仓重新识图"
    - capture_failed → 503，抓屏本身失败——主仓应重试当前步骤，绝不能降级识图
    - window_not_found → 409，指定 hwnd 但窗口无效/最小化——主仓应恢复窗口后重试
    """
    t0 = time.perf_counter()
    mon_count = len(screen_mss.monitors())
    if body.monitor > mon_count - 1:
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "BAD_REQUEST",
            "detail": f"monitor out of range: {body.monitor} (1..{mon_count - 1})"})
    try:
        img, _origin = screen_mss.grab(body.monitor)
    except Exception as exc:  # noqa: BLE001  抓屏失败是独立事件，不与"没找到"混同
        elapsed = int((time.perf_counter() - t0) * 1000)
        return JSONResponse(status_code=503, content={
            "ok": False, "status": "capture_failed",
            "error": type(exc).__name__ + ": " + str(exc), "elapsed_ms": elapsed})
    try:
        result = macro_matcher.find(img, body.screenshot_crop, body.match_threshold,
                                    hwnd=body.window_hwnd)
    except ValueError as exc:
        # 畸形 crop（非法 base64/PNG）是客户端错误，返回 400 而不是 500
        elapsed = int((time.perf_counter() - t0) * 1000)
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "BAD_REQUEST", "detail": str(exc),
            "elapsed_ms": elapsed})
    elapsed = int((time.perf_counter() - t0) * 1000)
    if result.get("status") == "window_not_found":
        return JSONResponse(status_code=409, content={
            "ok": False, "error": "WINDOW_NOT_FOUND", "status": "window_not_found",
            "detail": result.get("reason"), "elapsed_ms": elapsed})
    result["matched_in_window"] = bool(body.window_hwnd is not None
                                       and result.get("status") == "matched")
    result["ok"] = True
    result["elapsed_ms"] = elapsed
    return result


@router.post("/macro/{macro_id}/replay")
def replay(macro_id: str, body: ReplayBody):
    """安全重放：锚点匹配驱动，逐步走 controller 同一安全管道。

    - dry_run=true：只做窗口存活 + 锚点匹配报告，不产生任何输入。
    - 409 WINDOW_NOT_FOUND：目标窗口不存在/最小化。
    - 409 MACRO_NOT_FOUND：锚点匹配失败（附 best_score + 现场截图），终止整个宏，
      交主仓重新识图；绝不自行用 recorded_center 兜底。
    - 503 CAPTURE_FAILED：抓屏失败（已重试），主仓应重试本宏。
    """
    try:
        return macro_runner.replay(macro_id, dry_run=body.dry_run,
                                   allow_fallback=body.allow_fallback,
                                   task_id=body.task_id,
                                   hwnd_override=body.hwnd)
    except macro_runner.ReplayError as exc:
        content: Dict[str, Any] = {"ok": False, "error": exc.error,
                                   "detail": exc.detail}
        content.update(exc.data)
        return JSONResponse(status_code=exc.status, content=content)


@router.post("/macro/{macro_id}/heal")
def heal(macro_id: str, body: HealBody):
    """视觉反哺 Heal（v0.4 M4）：主仓 Iris 以视觉结果回填锚点，ghost 不做视觉推断。

    - relocate：主仓只给 vision.center → ghost 当前屏即用即裁 64x64 新锚点。
    - reanchor：主仓直接给 vision.crop → 校验可解码且非退化后替换。
    - 乐观锁 base_version 不匹配 → 409；version 成功 +1；全量写 macro_heal_log。
    """
    t0 = time.perf_counter()
    # relocate 需要现场抓屏：环境门禁（reanchor 纯数据操作，不依赖桌面）
    if body.mode == "relocate":
        try:
            environment.assert_input_allowed()
        except environment.SystemNotReadable as exc:
            return JSONResponse(status_code=503, content={
                "ok": False, "error": "SYSTEM_NOT_READABLE", "detail": exc.detail,
                "environment": exc.snapshot})

    macro = macro_store.get_macro(macro_id)
    if macro is None:
        return JSONResponse(status_code=404, content={
            "ok": False, "error": "NOT_FOUND",
            "detail": "macro " + repr(macro_id) + " not found"})

    raw_steps = macro.get("steps_json")
    if isinstance(raw_steps, str):
        steps = json.loads(raw_steps or "[]")
    else:
        steps = raw_steps or []
    target = None
    for s in steps:
        if s.get("step_id") == body.step_id:
            target = s
            break
    if target is None:
        return JSONResponse(status_code=404, content={
            "ok": False, "error": "NOT_FOUND",
            "detail": "step " + repr(body.step_id) + " not found in macro"})
    tgt = target.get("target") or {}
    if tgt.get("type") != "anchor":
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "BAD_REQUEST",
            "detail": "step " + repr(body.step_id) + " has no anchor target to heal"})
    old_b64 = tgt.get("image_b64") or ""

    new_center: Optional[List[int]] = None
    if body.mode == "relocate":
        if not body.vision.center or len(body.vision.center) != 2:
            return JSONResponse(status_code=400, content={
                "ok": False, "error": "BAD_REQUEST",
                "detail": "mode=relocate requires vision.center [x,y]"})
        try:
            img, _origin = screen_mss.grab(1)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(status_code=503, content={
                "ok": False, "error": "CAPTURE_FAILED",
                "detail": type(exc).__name__ + ": " + str(exc)})
        union = screen_mss.monitor_rect(0)
        cx, cy = int(body.vision.center[0]), int(body.vision.center[1])
        if not (union["left"] <= cx < union["left"] + union["width"]
                and union["top"] <= cy < union["top"] + union["height"]):
            return JSONResponse(status_code=400, content={
                "ok": False, "error": "BAD_REQUEST",
                "detail": "vision.center out of screen: " + str((cx, cy))})
        b64, _c = macro_matcher.crop_around(img, cx, cy, 64)
        new_center = [cx, cy]
    else:
        if not body.vision.crop:
            return JSONResponse(status_code=400, content={
                "ok": False, "error": "BAD_REQUEST",
                "detail": "mode=reanchor requires vision.crop"})
        try:
            macro_matcher.decode_anchor(body.vision.crop)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={
                "ok": False, "error": "BAD_REQUEST", "detail": str(exc)})
        b64 = body.vision.crop
        if body.vision.center and len(body.vision.center) == 2:
            new_center = [int(body.vision.center[0]), int(body.vision.center[1])]

    old_hash = hashlib.sha256(old_b64.encode("utf-8")).hexdigest()[:16]
    new_hash = hashlib.sha256(b64.encode("utf-8")).hexdigest()[:16]
    try:
        version = macro_store.heal_macro(macro_id, body.task_id, body.step_id,
                                         body.mode, b64, new_center,
                                         body.base_version, old_hash, new_hash)
    except macro_store.MacroNotFoundError as exc:
        return JSONResponse(status_code=404, content={
            "ok": False, "error": "NOT_FOUND", "detail": str(exc)})
    except macro_store.HealVersionConflictError as exc:
        return JSONResponse(status_code=409, content={
            "ok": False, "error": "VERSION_CONFLICT", "detail": str(exc)})
    except macro_store.HealStepError as exc:
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "BAD_REQUEST", "detail": str(exc)})

    return {"ok": True, "healed": True, "macro_id": macro_id, "version": version,
            "step_id": body.step_id, "mode": body.mode,
            "recorded_center": new_center,
            "old_anchor_hash": old_hash, "new_anchor_hash": new_hash,
            "elapsed_ms": int((time.perf_counter() - t0) * 1000)}
