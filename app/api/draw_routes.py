"""绘制路由（v0.9.1）：POST /api/ghost/draw_trajectory —— 平滑轨迹 + 画板取色。

复合动作（两段都走 controller.run_action 同一安全管道，分别审计入账）：
  1. 取色（可选）：CV 在目标窗口色板区定位"实心纯色色板点"→ 安全点击
     （找不到 → color_required=true 则 404 拒绝，否则如实标注 fallback）。
  2. 落笔：Chaikin 平滑 + 等距降采样（≤2000 点）→ 复用 drag 的
     起点+终点归属门禁 / 逐点 Fail-Safe / 硬超时 / finally 强制释放。

冻结门禁与环境门禁在本路由前置（与 /execute_action 同级）——复合动作
绝不绕过任何一道防线；token 由 main 中间件统一校验。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import win32gui
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.core import (action_log, authz, controller, environment, safety_gate,
                      screen_mss, task_queue, trajectory, vl_driver)
from app.core.controller import ActionError

router = APIRouter(prefix="/api/ghost")


class DrawTrajectoryBody(BaseModel):
    model_config = ConfigDict(extra="allow")  # 主仓多传字段直接忽略

    points: List[List[float]] = Field(
        ..., description="轨迹控制点 [[x,y],...]（物理像素，≥2 点）")
    color: Optional[str] = Field(
        default=None,
        description="落笔前点击的色板颜色：名称或 #RRGGBB（缺省=不取色）")
    color_required: bool = Field(
        default=False,
        description="true=色板未找到时 404 拒绝；false（默认）=用当前色如实 fallback")
    hwnd: Optional[int] = Field(default=None, description="目标窗口 hwnd")
    window: Optional[str] = Field(
        default=None, description="窗口标题子串（与 hwnd 二选一；都缺省=前台）")
    monitor: int = Field(default=1, ge=0)
    duration_ms: int = Field(default=800, ge=100, le=10000)
    smooth_iterations: int = Field(
        default=2, ge=0, le=4,
        description="Chaikin 切角迭代次数（0=不平滑，2=推荐）")
    closed: bool = Field(
        default=False,
        description="true=闭合曲线（首尾相接；注意画图软件会并成闭合形状）")
    settle_seconds: float = Field(default=0.1, ge=0.0, le=5.0)
    palette_region: Optional[List[float]] = Field(
        default=None,
        description="色板搜索区（窗口相对分数 [x0,y0,x1,y1]，默认整宽×顶部 30%）")
    capabilities: Optional[Dict[str, Any]] = None


def _err(status: int, error: str, detail: str, **extra: Any):
    content: Dict[str, Any] = {"ok": False, "error": error, "detail": detail}
    content.update(extra)
    return JSONResponse(status_code=status, content=content)


def _action_error(exc: ActionError) -> JSONResponse:
    content: Dict[str, Any] = {"ok": False, "error": exc.error,
                               "detail": exc.detail}
    content.update(exc.data or {})
    return JSONResponse(status_code=exc.status, content=content)


@router.post("/draw_trajectory")
def draw_trajectory(body: DrawTrajectoryBody,
                    request: Request = None) -> Dict[str, Any]:  # type: ignore[assignment]
    import time

    t0 = time.perf_counter()
    authz.bind_request(request)  # 与 /execute_action 同款身份上下文

    # ---- 冻结门禁（v0.5）：复合动作同样绝不输入 ----
    if task_queue.is_frozen():
        action_log.record("native", "draw_trajectory", {}, False, 503)
        return _err(503, "GHOST_FROZEN",
                    "ghost is frozen (" + str(task_queue.frozen_reason())
                    + ") at " + str(task_queue.frozen_at()),
                    frozen_reason=task_queue.frozen_reason())
    # ---- 环境门禁（v0.4 M2）：锁屏/安全桌面拒绝盲绘 ----
    try:
        environment.assert_input_allowed()
    except environment.SystemNotReadable as exc:
        return _err(503, "SYSTEM_NOT_READABLE", exc.detail,
                    environment=exc.snapshot)

    # ---- 纯参数校验 ----
    raw_points = body.points
    if not isinstance(raw_points, list) or len(raw_points) < 2:
        return _err(400, "SAFETY_BLOCKED",
                    "points must be a list of >=2 [x,y] entries")
    for p in raw_points:
        if (not isinstance(p, (list, tuple)) or len(p) < 2
                or not isinstance(p[0], (int, float))
                or not isinstance(p[1], (int, float))):
            return _err(400, "SAFETY_BLOCKED",
                        "points entries must be [x,y] numbers")
    if body.color is not None:
        try:
            trajectory.resolve_color(body.color)
        except ValueError as exc:
            return _err(400, "SAFETY_BLOCKED", str(exc))
    region = body.palette_region
    if region is not None:
        if (not isinstance(region, list) or len(region) != 4
                or not all(isinstance(v, (int, float)) and 0.0 <= v <= 1.0
                           for v in region)
                or region[0] >= region[2] or region[1] >= region[3]):
            return _err(400, "SAFETY_BLOCKED",
                        "palette_region must be [x0,y0,x1,y1] fractions "
                        "within 0..1 with x0<x1 and y0<y1")
    mon_count = len(screen_mss.monitors())
    if body.monitor < 1 or body.monitor > mon_count - 1:
        return _err(400, "SAFETY_BLOCKED",
                    "monitor out of range: " + str(body.monitor)
                    + " (1.." + str(mon_count - 1) + ")")

    # ---- 能力令牌（最小权限）：绘制必然产生桌面输入，必须声明 ----
    if body.capabilities is None:
        return _err(400, "CAPABILITIES_REQUIRED",
                    "draw_trajectory requires capabilities.allowed_windows "
                    "(最小权限原则)")
    try:
        caps = safety_gate.parse_capabilities(body.capabilities)
    except ValueError as exc:
        return _err(400, "SAFETY_BLOCKED", str(exc))
    if not caps.get("allowed_windows"):
        return _err(400, "CAPABILITIES_REQUIRED",
                    "capabilities.allowed_windows must be non-empty for "
                    "draw_trajectory")

    # ---- 目标窗口解析（title 子串语义与 /vl/analyze 一致）----
    try:
        channel, resolved = vl_driver.resolve_window(body.window, body.hwnd)
        if channel == "cdp":
            return _err(400, "VL_ERROR",
                        "draw_trajectory is native-only; URL targets route "
                        "to the CDP channel")
        hwnd = int(resolved)
    except vl_driver.VLError as exc:
        return _err(exc.status, exc.error, exc.detail)
    info = safety_gate.window_info(hwnd)
    if info is None:
        return _err(409, "WINDOW_NOT_FOUND",
                    "window hwnd " + str(hwnd) + " is gone")
    try:
        rect = win32gui.GetWindowRect(hwnd)
    except Exception:  # noqa: BLE001  窗口在解析后消失
        return _err(409, "WINDOW_NOT_FOUND",
                    "window hwnd " + str(hwnd) + " vanished before draw")

    color_name = (body.color or "").strip() or None
    color_info: Dict[str, Any] = {"requested": color_name,
                                  "found": None, "clicked": False,
                                  "fallback": False}

    # ---- 第 1 段：取色（可选）----
    if color_name:
        try:
            img, _origin = screen_mss.grab(body.monitor)
        except Exception as exc:  # noqa: BLE001
            return _err(503, "CAPTURE_FAILED",
                        "screen grab failed before color pick: "
                        + type(exc).__name__ + ": " + str(exc))
        try:
            swatch = trajectory.find_swatch(
                img, rect, color_name,
                region=tuple(region) if region is not None
                else (0.0, 0.0, 1.0, 0.30))
        except Exception as exc:  # noqa: BLE001
            return _err(500, "SWATCH_SEARCH_FAILED",
                        "palette search failed: " + type(exc).__name__
                        + ": " + str(exc))
        if swatch is None:
            if body.color_required:
                return _err(404, "COLOR_SWATCH_NOT_FOUND",
                            "no solid " + repr(color_name) + " swatch found "
                            "in palette region of window "
                            + repr(info.get("title")))
            color_info["found"] = False
            color_info["fallback"] = True
        else:
            sx, sy, area = swatch
            color_info["found"] = True
            color_info["center"] = [int(sx), int(sy)]
            color_info["area_px"] = int(area)
            try:
                click_result = controller.run_action({
                    "action": "click", "x": int(sx), "y": int(sy),
                    "hwnd": hwnd, "monitor": body.monitor,
                    "settle_seconds": 0.2, "capabilities": caps})
                color_info["clicked"] = bool(click_result.get("ok"))
            except ActionError as exc:
                return _action_error(exc)

    # ---- 第 2 段：平滑 + 降采样 + 落笔（drag 全安全管道）----
    prepared = trajectory.prepare_trajectory(
        raw_points, body.smooth_iterations, body.closed)
    if len(prepared) < 2:
        return _err(400, "SAFETY_BLOCKED",
                    "trajectory collapsed to <2 points after smoothing")
    try:
        drag_result = controller.run_action({
            "action": "drag", "points": prepared,
            "duration_ms": body.duration_ms, "hwnd": hwnd,
            "monitor": body.monitor,
            "settle_seconds": body.settle_seconds, "capabilities": caps})
    except ActionError as exc:
        return _action_error(exc)

    return {
        "ok": True,
        "action": "draw_trajectory",
        "window": {"hwnd": hwnd, "title": info.get("title"),
                   "process": info.get("process")},
        "color": color_info,
        "stroke": {"points_in": len(raw_points),
                   "points_sent": len(prepared),
                   "smooth_iterations": body.smooth_iterations,
                   "closed": body.closed,
                   "duration_ms": body.duration_ms},
        "result": drag_result,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
    }
