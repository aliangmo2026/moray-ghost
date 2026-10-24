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

import json
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core import macro_matcher, macro_runner, macro_store, screen_mss

router = APIRouter()


class ReplayBody(BaseModel):
    dry_run: bool = False
    allow_fallback: bool = False
    task_id: Optional[str] = None


class HealBody(BaseModel):
    steps_json: Optional[str] = None
    note: Optional[str] = None


class ProbeBody(BaseModel):
    screenshot_crop: str = Field(..., description="模板小图（base64 PNG，物理像素裁剪）")
    match_threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    monitor: int = Field(default=1, ge=1)
    window_hwnd: Optional[int] = Field(default=None,
                                       description="给出时只在该窗口当前 rect 内匹配（每次实时取 rect，不缓存）")


def _not_implemented(what: str) -> JSONResponse:
    return JSONResponse(status_code=501, content={
        "ok": False,
        "error": "NOT_IMPLEMENTED",
        "detail": f"{what}: routes/auth/schema in place, implementation lands in v0.3",
    })


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
def record_start():
    return _not_implemented("macro recording (start)")


@router.post("/macro/record/stop")
def record_stop():
    return _not_implemented("macro recording (stop)")


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
    try:
        return macro_runner.replay(macro_id, dry_run=body.dry_run,
                                   allow_fallback=body.allow_fallback, task_id=body.task_id)
    except macro_runner.NotImplementedYet:
        return _not_implemented(f"replay of macro {macro_id!r}")


@router.post("/macro/{macro_id}/heal")
def heal(macro_id: str, body: HealBody):
    try:
        return macro_runner.heal(macro_id, steps_json=body.steps_json, note=body.note)
    except macro_runner.NotImplementedYet:
        return _not_implemented(f"heal of macro {macro_id!r}")
