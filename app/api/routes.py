"""契约 v0.2 路由：/health、/screenshot、/execute_action。

鉴权由 app.main 的 token 中间件统一处理（/health、/screenshot 探活放行）。
"""
from __future__ import annotations

import base64
import logging
import time
from typing import Any, Dict, Optional, Union

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from app.core import backend, controller, macro_matcher, macro_recorder
from app.core import environment, screen_mss, window_locator
from app.core.config import DB_PATH, JPEG_QUALITY, SERVICE, VERSION

LOG = logging.getLogger("moray-ghost.routes")

router = APIRouter(prefix="/api/ghost")


@router.get("/health")
def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "service": SERVICE,
        "version": VERSION,
        "fail_safe_enabled": True,
        "token_required": True,
        "backends": {"screenshot": "mss", "input": "pyautogui"},
        "monitors": [m for m in screen_mss.monitors() if m["index"] != 0],
        "cursor": list(backend.position()),
        "db_path": str(DB_PATH),
    }


def _bad_request(detail: str) -> JSONResponse:
    return JSONResponse(status_code=400, content={
        "ok": False, "error": "BAD_REQUEST", "detail": detail})


@router.get("/screenshot")
def screenshot(
    monitor: Union[int, str] = Query(default="1", description="1=主屏；0=全屏联合区；all=各屏数组；或屏号"),
    fmt: str = Query(default="jpeg", pattern="^(png|jpeg)$"),
    quality: int = Query(default=JPEG_QUALITY, ge=10, le=95),
    scale: float = Query(default=1.0, ge=0.1, le=1.0),
    save: bool = Query(default=False),
) -> Dict[str, Any]:
    t0 = time.perf_counter()
    cursor = list(backend.position())
    all_mons = screen_mss.monitors()

    def shot_one(idx: int) -> Dict[str, Any]:
        img, origin = screen_mss.grab(idx)
        data = screen_mss.encode(img, fmt, quality, scale)
        path = screen_mss.save_bytes(data, "screenshot", fmt) if save else None
        w = img.width
        h = img.height
        if scale < 1.0:
            w = max(1, round(img.width * scale))
            h = max(1, round(img.height * scale))
        return {
            "ok": True,
            "monitor": idx,
            "monitor_origin": [origin["left"], origin["top"]],
            "original_width": img.width,
            "original_height": img.height,
            "width": w,
            "height": h,
            "format": fmt,
            "scale": scale,
            "screenshot_path": str(path) if path else None,
            "image_b64": base64.b64encode(data).decode("ascii"),
        }

    if isinstance(monitor, str) and monitor.strip().lower() == "all":
        shots = [shot_one(m["index"]) for m in all_mons if m["index"] != 0]
        elapsed = int((time.perf_counter() - t0) * 1000)
        return {"ok": True, "monitor": "all", "shots": shots,
                "cursor": cursor, "elapsed_ms": elapsed}

    try:
        idx = int(monitor)
    except (TypeError, ValueError):
        return _bad_request(f"invalid monitor value: {monitor!r}")
    max_idx = len(all_mons) - 1
    if idx < 0 or idx > max_idx:
        return _bad_request(f"monitor index out of range: {idx} (0..{max_idx})")

    body = shot_one(idx)
    body["cursor"] = cursor
    body["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
    return body


class ExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="allow")  # 主仓多传的字段直接忽略，绝不 422

    action: str = Field(..., description="click|double_click|right_click|move|type|hotkey")
    x: Optional[int] = None
    y: Optional[int] = None
    text: Optional[str] = None
    monitor: int = 1
    settle_seconds: float = 0.5
    include_before_b64: bool = False
    fmt: str = "jpeg"
    quality: int = Field(default=JPEG_QUALITY, ge=10, le=95)
    scale: float = Field(default=0.5, ge=0.1, le=1.0)
    # 源图坐标换算：同时给出二者时，x/y 视为缩放源图（original_width=source_width）上的坐标；
    # 缺省时 x/y 为物理像素，行为与 v0.2 完全一致。
    source_width: Optional[int] = None
    source_height: Optional[int] = None
    # 录制提示：录制会话期间作为步骤的 hwnd（缺省用动作发生时的前台窗口）
    hwnd: Optional[int] = None


@router.post("/execute_action")
def execute_action(req: ExecuteRequest) -> Dict[str, Any]:
    # 环境门禁（v0.4 M2）：锁屏 / 安全桌面 / 桌面不可读时拒绝盲操作
    try:
        environment.assert_input_allowed()
    except environment.SystemNotReadable as exc:
        return JSONResponse(status_code=503, content={
            "ok": False, "error": "SYSTEM_NOT_READABLE", "detail": exc.detail,
            "environment": exc.snapshot})
    try:
        result = controller.run_action(req.model_dump())
    except controller.ActionError as exc:
        content: Dict[str, Any] = {"ok": False, "error": exc.error, "detail": exc.detail}
        content.update(exc.data)
        return JSONResponse(status_code=exc.status, content=content)
    # 录制会话：动作成功后自动追加步骤。不盲点由 controller 保证（前后审计截图
    # 已落盘）；锚点取自动作【前】截图（重放匹配的是点击前的样子）。
    if macro_recorder.active_task_id() is not None:
        try:
            _record_step(req, result)
        except Exception as exc:  # noqa: BLE001  录制失败不影响动作本身
            LOG.warning("record step failed: %s", exc)
    return result


def _record_step(req: ExecuteRequest, result: Dict[str, Any]) -> None:
    action = req.action.strip().lower()
    hwnd = req.hwnd or window_locator.foreground_hwnd()
    recorded_center = result.get("resolved_physical")
    target: Dict[str, Any] = {"type": "none", "image_b64": None,
                              "recorded_center": recorded_center}
    if action in ("click", "double_click", "right_click"):
        before_path = result.get("before_screenshot_path")
        if before_path and recorded_center:
            img = Image.open(before_path)
            b64, _center = macro_matcher.crop_around(
                img, int(recorded_center[0]), int(recorded_center[1]), 64)
            target = {"type": "anchor", "image_b64": b64,
                      "recorded_center": [int(recorded_center[0]),
                                          int(recorded_center[1])]}
    step = {"action": action, "hwnd": int(hwnd) if hwnd else None,
            "target": target, "text": req.text,
            "wait_after_ms": int(req.settle_seconds * 1000)}
    macro_recorder.add_step(step)
