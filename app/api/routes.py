"""契约 v0.2 路由：/health、/screenshot、/execute_action。

鉴权由 app.main 的 token 中间件统一处理（/health、/screenshot 探活放行）。
"""
from __future__ import annotations

import base64
import logging
import os
import time
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from app.core import backend, controller, macro_matcher, macro_recorder
from app.core import action_locks, action_log, authz, environment, screen_mss
from app.core import task_queue, virtual_input
from app.core import vl_driver, window_locator
from app.core.config import (DB_PATH, JPEG_QUALITY, MAX_CONCURRENT_TASKS,
                             SERVICE, VERSION, VIRTUAL_FALLBACK,
                             VIRTUAL_INPUT_ENABLED, VIRTUAL_POINTER_ENABLED,
                             VIRTUAL_VERIFY, VL_BACKEND, VL_MODEL_PATH,
                             VL_MMPROJ_PATH, VL_PROFILE)

LOG = logging.getLogger("moray-ghost.routes")

router = APIRouter(prefix="/api/ghost")


@router.get("/health")
async def health() -> Dict[str, Any]:
    """v0.7.2 关键路径异步化：async 处理器直接跑在事件循环上，绝不排队等
    线程池——即使所有动作线程被长任务占满，健康检查依然即时返回。

    v0.8.2：回显 VL 真视觉后端（配置/实际选中/模型/GPU offload 能力）。"""
    return {
        "status": "ok",
        "service": SERVICE,
        "version": VERSION,
        "fail_safe_enabled": True,
        "token_required": True,
        "backends": {"screenshot": "mss", "input": "pyautogui"},
        "vl_backend": vl_driver.describe_backend(),
        "vl": {
            "configured": VL_BACKEND,
            "profile": VL_PROFILE,
            "model": os.path.basename(VL_MODEL_PATH) or None,
            "mmproj": os.path.basename(VL_MMPROJ_PATH) or None,
            "gpu_offload": vl_driver.gpu_offload_supported(),
            "model_loaded": vl_driver.llama_loaded(),
            "hybrid": vl_driver.hybrid_enabled(),
        },
        "monitors": [m for m in screen_mss.monitors() if m["index"] != 0],
        "cursor": list(backend.position()),
        "db_path": str(DB_PATH),
        "frozen": task_queue.is_frozen(),
        "frozen_reason": task_queue.frozen_reason(),
        # v0.17：虚拟通道能力 + 细粒度锁调度观测
        "virtual_input": {**virtual_input.describe(),
                          "enabled": VIRTUAL_INPUT_ENABLED,
                          "fallback": VIRTUAL_FALLBACK,
                          "verify": VIRTUAL_VERIFY,
                          "pointer_enabled": VIRTUAL_POINTER_ENABLED},
        "action_locks": action_locks.snapshot(),
        "task_workers": MAX_CONCURRENT_TASKS,
        **vl_driver.guard_status(),
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


THUMB_SIDE_PX = 256  # v0.16 缩略图最长边：低成本"看一眼"，替代 draw 响应回传全屏图


@router.get("/screenshot/thumb")
def screenshot_thumb(
    monitor: int = Query(default=1, ge=0),
    fmt: str = Query(default="jpeg", pattern="^(png|jpeg)$"),
    quality: int = Query(default=60, ge=10, le=95),
) -> Dict[str, Any]:
    """v0.16 极简感知端点：最长边 256px 的超低分辨率缩略图（base64）。

    draw_trajectory / sketch_from_image 默认不再回传全屏截图（一次 ~90KB
    ≈ 2.5 万 token 的灾难根因）；模型需要"看一眼"画布效果时按需调本端点，
    单次 ~4-8KB。响应只含缩略图元数据，绝不附全尺寸图。
    """
    t0 = time.perf_counter()
    all_mons = screen_mss.monitors()
    if monitor < 0 or monitor > len(all_mons) - 1:
        return _bad_request(f"monitor index out of range: {monitor} "
                            f"(0..{len(all_mons) - 1})")
    img, origin = screen_mss.grab(monitor)
    side = max(img.width, img.height)
    scale = min(1.0, THUMB_SIDE_PX / float(side))
    data = screen_mss.encode(img, fmt, quality, scale)
    tw = max(1, round(img.width * scale))
    th = max(1, round(img.height * scale))
    return {
        "ok": True,
        "monitor": monitor,
        "monitor_origin": [origin["left"], origin["top"]],
        "original_width": img.width,
        "original_height": img.height,
        "width": tw,
        "height": th,
        "thumb_side_px": max(tw, th),
        "format": fmt,
        "bytes": len(data),
        "image_b64": base64.b64encode(data).decode("ascii"),
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
    }


class ExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="allow")  # 主仓多传的字段直接忽略，绝不 422

    action: str = Field(..., description="click|double_click|right_click|move|type|hotkey|drag|scroll")
    x: Optional[int] = None
    y: Optional[int] = None
    text: Optional[str] = None
    monitor: int = 1
    settle_seconds: float = 0.5
    include_before_b64: bool = False
    fmt: str = "jpeg"
    quality: int = Field(default=JPEG_QUALITY, ge=10, le=95)
    scale: float = Field(default=0.5, ge=0.1, le=1.0)
    # drag（v0.6 M1）：x2/y2 插值轨迹，或 points 显式轨迹（x/y 缺省取 points[0]）
    x2: Optional[int] = None
    y2: Optional[int] = None
    points: Optional[List[List[int]]] = Field(default=None,
                                              description="drag 轨迹点 [[x,y],...]，≤2000 点")
    points_t: Optional[List[int]] = Field(default=None,
                                          description="drag 轨迹毫秒时间戳（相对起点，非递减）")
    duration_ms: Optional[int] = Field(default=None, ge=100, le=10000)
    # scroll（v0.6 M1）：dy 滚动幅度（+上/-下），smooth 分片平滑
    dy: Optional[int] = None
    smooth: Optional[bool] = None
    # 源图坐标换算：同时给出二者时，x/y 视为缩放源图（original_width=source_width）上的坐标；
    # 缺省时 x/y 为物理像素，行为与 v0.2 完全一致。
    source_width: Optional[int] = None
    source_height: Optional[int] = None
    # 录制提示：录制会话期间作为步骤的 hwnd（缺省用动作发生时的前台窗口）
    hwnd: Optional[int] = None
    # v0.17 输入通道：physical（默认，真实鼠标键盘）| virtual（PostMessage，
    # 不移动物理鼠标、不抢前台；必须有 hwnd）。virtual 支持 click 系/type/
    # drag/scroll；move/hotkey 不支持（400 VIRTUAL_UNSUPPORTED）。
    channel: str = Field(default="physical", pattern="^(physical|virtual)$",
                         description="physical | virtual")
    # virtual 投递后无可观测变化时是否自动降级 physical（缺省读配置，默认 true）
    virtual_fallback: Optional[bool] = None
    # 是否做 128px 像素/WM_GETTEXT 可观测性校验（缺省 true；调试可关）
    verify: Optional[bool] = None


@router.post("/execute_action")
def execute_action(req: ExecuteRequest,
                   request: Request = None) -> Dict[str, Any]:  # type: ignore[assignment]
    # v0.7.1 身份上下文：token 中间件校验通过的身份落到本线程——
    # 生产环境输入源过滤（authz）与 JSONL 审计归因都读它。
    # 显式覆盖（含 None）：绝不让同一线程上一请求的残留身份串进本次动作。
    # request 可缺省：进程内直调（无 HTTP 身份）→ ctx=None → 生产环境被
    # SOURCE_DENIED 拒绝（旁路默认拒绝），开发环境保持旧行为。
    authz.bind_request(request)
    # 冻结门禁（v0.5）：emergency stop 期间拒绝一切桌面输入（被拒也入账，驾驶舱可见）
    if task_queue.is_frozen():
        action_log.record("native", req.action,
                          {"x": req.x, "y": req.y, "monitor": req.monitor}, False, 503)
        return JSONResponse(status_code=503, content={
            "ok": False, "error": "GHOST_FROZEN",
            "detail": "ghost is frozen (" + str(task_queue.frozen_reason())
                      + ") at " + str(task_queue.frozen_at()),
            "frozen_reason": task_queue.frozen_reason()})
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
        # 动作日志入账在 controller.run_action 单点完成（含进锁前校验），此处只回包
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
    # drag/scroll（v0.6 M1）：原样记录执行轨迹（点位+毫秒节奏）与滚轮参数，
    # 重放按轨迹复现而不是只记起终点。
    if action == "drag":
        step["points"] = result.get("trajectory")
        step["points_t"] = result.get("trajectory_t")
    if action == "scroll" and isinstance(result.get("scroll"), dict):
        step["dy"] = result["scroll"].get("dy")
        step["smooth"] = bool(result["scroll"].get("smooth"))
    macro_recorder.add_step(step)
