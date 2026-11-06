"""绘制路由（v0.16 工业级物理操控引擎）。

POST /api/ghost/draw_trajectory —— 高级几何绘制复合动作：
  - subpaths 多段轨迹：段间自动"抬笔→移动→落笔"，绝不连线（controller
    drag 原生支持，逐段过归属门禁 / Fail-Safe / 硬超时）。
  - primitives 几何原语：{"type":"circle",...} 等，本地展开成平滑点列
    （大模型只传语义，不传坐标）。
  - brush_width：OCR 驱动画图软件"大小"下拉调整笔宽（app/core/brush）。
  - preserve_corners：Chaikin 平滑保留指定尖角（眼睛/星角不被抹圆）。
  - canvas 感知：CV 定位画纸物理边界；dry_run 安全预演（零输入返回警告）。
  - 极简响应（v0.16 默认）：绝不返回全屏截图——只回
    {ok, bbox, points_count, elapsed_ms, ...} 元数据；需要看画面走
    GET /api/ghost/screenshot/thumb（256px 缩略图）。include_screenshot=true
    可显式要回旧版全量响应。

POST /api/ghost/draw/sketch_from_image —— 图像自动复刻引擎：
  传图片路径 + 目标窗口，Ghost 本地完成 Canny/K-Means/Douglas-Peucker
  规划并亲自落笔复刻（app/core/sketch），大模型连坐标都不用传。

POST /api/ghost/draw/canvas_info —— 画纸物理边界探测（只读）。

安全红线不变：冻结 / 环境门禁 / 能力令牌 / 窗口黑名单 / 落点归属 /
Fail-Safe / 硬超时 / 审计，全部经 controller.run_action 同一管道生效；
token 由 main 中间件统一校验。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import win32gui
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.core import (action_log, authz, brush as brush_mod, canvas,
                      controller, environment, geometry, safety_gate,
                      screen_mss, sketch as sketch_mod, task_queue,
                      trajectory, vl_driver)
from app.core.controller import ActionError

router = APIRouter(prefix="/api/ghost")

_SKETCH_TOTAL_POINT_BUDGET = 8000  # 单次 sketch 总点数预算（与 controller 同源）


class DrawTrajectoryBody(BaseModel):
    model_config = ConfigDict(extra="allow")  # 主仓多传字段直接忽略

    points: Optional[List[List[float]]] = Field(
        default=None,
        description="单段轨迹控制点 [[x,y],...]（物理像素，≥2 点；与 "
                    "subpaths 互斥）")
    subpaths: Optional[List[List[List[float]]]] = Field(
        default=None,
        description="多段轨迹 [[[x1,y1],[x2,y2]],[[x3,y3],...]]：每段画完自动"
                    "抬笔移到下一段起点再落笔，段间绝不连线（≤200 段，"
                    "合计 ≤8000 点）")
    primitives: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description='几何原语列表，如 {"type":"circle","center":[0.5,0.5],'
                    '"radius":0.2}；支持 circle/ellipse/arc/line/rect/'
                    'polygon/bezier——Ghost 本地展开成平滑点列')
    primitive_units: str = Field(
        default="frac",
        description="primitives 坐标单位：frac=相对画布/窗口归一化（默认，"
                    "长度按 min(w,h) 等比保证正圆）；px=相对画布左上角像素")
    preserve_corners: Optional[List[Any]] = Field(
        default=None,
        description="Chaikin 平滑保留的尖角下标（相对各段控制点）：[0,3] 对"
                    "每段生效；[[0],[2,5]] 逐段指定——防止眼睛/星角被抹圆")
    color: Optional[str] = Field(
        default=None,
        description="落笔前点击的色板颜色：名称或 #RRGGBB（缺省=不取色）")
    color_required: bool = Field(
        default=False,
        description="true=色板未找到时 404 拒绝；false（默认）=用当前色如实 fallback")
    brush_width: Optional[int] = Field(
        default=None, ge=1, le=100,
        description="落笔前把画图软件笔宽调到最接近的档位（mspaint 走 OCR "
                    "定位'大小'下拉；不支持的软件如实报错）")
    brush_required: bool = Field(
        default=False,
        description="true=笔宽调整失败时 409 拒绝；false（默认）=告警后用当前笔宽")
    hwnd: Optional[int] = Field(default=None, description="目标窗口 hwnd")
    window: Optional[str] = Field(
        default=None, description="窗口标题子串（与 hwnd 二选一；都缺省=前台）")
    monitor: int = Field(default=1, ge=0)
    duration_ms: int = Field(default=800, ge=100, le=10000,
                             description="全部段的总时长（段间按点数分配）")
    smooth_iterations: int = Field(
        default=2, ge=0, le=4,
        description="Chaikin 切角迭代次数（0=不平滑，2=推荐）")
    closed: bool = Field(
        default=False,
        description="true=每段闭合曲线（首尾相接；注意画图软件会并成闭合形状）")
    settle_seconds: float = Field(default=0.1, ge=0.0, le=5.0)
    palette_region: Optional[List[float]] = Field(
        default=None,
        description="色板搜索区（窗口相对分数 [x0,y0,x1,y1]，默认整宽×顶部 30%）")
    dry_run: bool = Field(
        default=False,
        description="true=安全预演：完整规划（平滑/原语/画布检查）但绝不碰"
                    "鼠标，返回 bbox/警告")
    include_screenshot: bool = Field(
        default=False,
        description="true=回传旧版全量响应（含 after_image_b64 全屏图，"
                    "~25k token）；默认 false 极简响应，看画面请用 "
                    "GET /screenshot/thumb")
    is_painting: bool = Field(
        default=False,
        description="长时绘图任务标识：豁免频率熔断的指纹计数（合法绘图的"
                    "离散坐标不会构成死循环）；冻结/归属/前台/Fail-Safe/"
                    "硬超时等其余安全门禁一道不少，审计照记")
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


# ---- 共用门禁前置 -----------------------------------------------------------

def _gates(action_name: str, body_monitor: int):
    """冻结 + 环境门禁（返回 JSONResponse 或 None）；拒绝也入账动作日志。"""
    if task_queue.is_frozen():
        action_log.record("native", action_name, {}, False, 503)
        return _err(503, "GHOST_FROZEN",
                    "ghost is frozen (" + str(task_queue.frozen_reason())
                    + ") at " + str(task_queue.frozen_at()),
                    frozen_reason=task_queue.frozen_reason())
    try:
        environment.assert_input_allowed()
    except environment.SystemNotReadable as exc:
        return _err(503, "SYSTEM_NOT_READABLE", exc.detail,
                    environment=exc.snapshot)
    return None


def _require_caps(raw_caps: Any, what: str):
    if raw_caps is None:
        return _err(400, "CAPABILITIES_REQUIRED",
                    what + " requires capabilities.allowed_windows "
                    "(最小权限原则)")
    try:
        caps = safety_gate.parse_capabilities(raw_caps)
    except ValueError as exc:
        return _err(400, "SAFETY_BLOCKED", str(exc))
    if not caps.get("allowed_windows"):
        return _err(400, "CAPABILITIES_REQUIRED",
                    "capabilities.allowed_windows must be non-empty for "
                    + what)
    return caps


def _resolve_native_window(window: Optional[str], hwnd_body: Optional[int]):
    """native-only 窗口解析 → (JSONResponse|None, hwnd, info)。"""
    try:
        channel, resolved = vl_driver.resolve_window(window, hwnd_body)
        if channel == "cdp":
            return _err(400, "VL_ERROR",
                        "draw endpoints are native-only; URL targets route "
                        "to the CDP channel"), None, None
        hwnd = int(resolved)
    except vl_driver.VLError as exc:
        return _err(exc.status, exc.error, exc.detail), None, None
    info = safety_gate.window_info(hwnd)
    if info is None:
        return _err(409, "WINDOW_NOT_FOUND",
                    "window hwnd " + str(hwnd) + " is gone"), None, None
    return None, hwnd, info


def _validate_float_points(raw: Any, what: str) -> Optional[str]:
    if not isinstance(raw, list) or len(raw) < 2:
        return what + " must be a list of >=2 [x,y] entries"
    for p in raw:
        if (not isinstance(p, (list, tuple)) or len(p) < 2
                or not isinstance(p[0], (int, float))
                or not isinstance(p[1], (int, float))):
            return what + " entries must be [x,y] numbers"
    return None


def _validate_preserve(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        return "preserve_corners must be a non-empty list"
    if all(isinstance(v, int) and not isinstance(v, bool) for v in raw):
        return None
    if all(isinstance(v, list) for v in raw):
        for seg in raw:
            if not all(isinstance(v, int) and not isinstance(v, bool)
                       for v in seg):
                return "preserve_corners nested entries must be int lists"
        return None
    return ("preserve_corners must be [int, ...] or [[int,...], ...] "
            "(got mixed/invalid types)")


# ---- /draw_trajectory -------------------------------------------------------

@router.post("/draw_trajectory")
def draw_trajectory(body: DrawTrajectoryBody,
                    request: Request = None) -> Any:  # type: ignore[assignment]
    import time

    t0 = time.perf_counter()
    authz.bind_request(request)  # 与 /execute_action 同款身份上下文

    gate = _gates("draw_trajectory", body.monitor)
    if gate is not None:
        return gate

    # ---- 纯参数校验 ----
    n_inputs = sum(1 for v in (body.points, body.subpaths, body.primitives)
                   if v)
    if n_inputs == 0:
        return _err(400, "SAFETY_BLOCKED",
                    "provide points, subpaths or primitives (at least one)")
    if body.points is not None and body.subpaths is not None:
        return _err(400, "SAFETY_BLOCKED",
                    "points and subpaths are mutually exclusive")
    if body.points is not None:
        bad = _validate_float_points(body.points, "points")
        if bad:
            return _err(400, "SAFETY_BLOCKED", bad)
    if body.subpaths is not None:
        if not isinstance(body.subpaths, list) or not body.subpaths:
            return _err(400, "SAFETY_BLOCKED",
                        "subpaths must be a non-empty list of point lists")
        for i, sp in enumerate(body.subpaths):
            bad = _validate_float_points(sp, "subpaths[" + str(i) + "]")
            if bad:
                return _err(400, "SAFETY_BLOCKED", bad)
    if body.primitives is not None:
        if not isinstance(body.primitives, list) or not all(
                isinstance(p, dict) for p in body.primitives):
            return _err(400, "SAFETY_BLOCKED",
                        "primitives must be a list of objects")
    bad = _validate_preserve(body.preserve_corners)
    if bad:
        return _err(400, "SAFETY_BLOCKED", bad)
    if body.primitive_units not in ("frac", "px"):
        return _err(400, "SAFETY_BLOCKED",
                    "primitive_units must be 'frac' or 'px'")
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
    caps_res = _require_caps(body.capabilities, "draw_trajectory")
    if isinstance(caps_res, JSONResponse):
        return caps_res
    caps = caps_res

    # ---- 目标窗口解析（title 子串语义与 /vl/analyze 一致）----
    gate, hwnd, info = _resolve_native_window(body.window, body.hwnd)
    if gate is not None:
        return gate
    try:
        rect = win32gui.GetWindowRect(hwnd)
    except Exception:  # noqa: BLE001  窗口在解析后消失
        return _err(409, "WINDOW_NOT_FOUND",
                    "window hwnd " + str(hwnd) + " vanished before draw")

    color_name = (body.color or "").strip() or None

    # ---- 画布感知（一次抓屏同时供取色与画布检测）----
    try:
        cinfo = canvas.detect_window_canvas(rect, body.monitor)
        img = cinfo.pop("img")
    except Exception as exc:  # noqa: BLE001
        return _err(503, "CAPTURE_FAILED",
                    "screen grab failed before draw: "
                    + type(exc).__name__ + ": " + str(exc))

    # ---- 构建段列表：points/subpaths + primitives（本地展开）----
    try:
        segments: List[List[List[float]]] = []
        points_in = 0
        if body.subpaths is not None:
            segments.extend(body.subpaths)
            points_in += sum(len(s) for s in body.subpaths)
        elif body.points is not None:
            segments.append(body.points)
            points_in += len(body.points)
        prim_metas: List[Dict[str, Any]] = []
        if body.primitives:
            frame = canvas_frame(cinfo, rect)
            prim_subs, prim_metas = geometry.primitives_to_subpaths(
                body.primitives, frame, body.primitive_units)
            segments.extend(prim_subs)
            points_in += sum(len(s) for s in prim_subs)
        prepared = trajectory.prepare_subpaths(
            segments, body.smooth_iterations, body.closed,
            body.preserve_corners)
    except (ValueError, geometry.PrimitiveError) as exc:
        return _err(400, "SAFETY_BLOCKED", str(exc))
    flat = [p for seg in prepared for p in seg]
    if len(flat) < 2:
        return _err(400, "SAFETY_BLOCKED",
                    "trajectory collapsed to <2 points after smoothing")
    if len(prepared) > controller.MAX_SUBPATHS:
        return _err(400, "SAFETY_BLOCKED",
                    "subpaths count " + str(len(prepared))
                    + " exceeds max " + str(controller.MAX_SUBPATHS))
    if len(flat) > controller.MAX_SUBPATH_TOTAL_POINTS:
        return _err(400, "SAFETY_BLOCKED",
                    "total points " + str(len(flat)) + " exceeds max "
                    + str(controller.MAX_SUBPATH_TOTAL_POINTS))
    xs = [p[0] for p in flat]
    ys = [p[1] for p in flat]
    bbox = [min(xs), min(ys), max(xs), max(ys)]

    # ---- 安全预演警告（dry_run 或正式绘制都给）----
    warnings = canvas.check_points(flat, monitor=body.monitor,
                                   window_rect=rect, canvas=cinfo)

    # ---- dry_run：规划照常，绝不碰鼠标/不取色/不调笔宽 ----
    if body.dry_run:
        return _minimal_response(
            ok=True, t0=t0, hwnd=hwnd, info=info, canvas_info=cinfo,
            stroke_meta=_stroke_meta(body, points_in, len(flat),
                                     len(prepared), prim_metas),
            bbox=bbox, warnings=warnings, color_info=None, brush_info=None,
            dry_run=True, drag_result=None)

    color_info: Dict[str, Any] = {"requested": color_name,
                                  "found": None, "clicked": False,
                                  "fallback": False}

    # ---- 第 1 段：取色（可选）----
    if color_name:
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
                    "settle_seconds": 0.2, "capabilities": caps,
                    "include_after_b64": False})
                color_info["clicked"] = bool(click_result.get("ok"))
            except ActionError as exc:
                return _action_error(exc)

    # ---- 第 2 段：笔宽（可选）----
    brush_info: Optional[Dict[str, Any]] = None
    if body.brush_width is not None:
        try:
            brush_info = brush_mod.set_brush_width(
                int(body.brush_width), hwnd, caps, body.monitor)
        except brush_mod.BrushError as exc:
            if body.brush_required:
                content: Dict[str, Any] = {
                    "ok": False, "error": brush_mod.BrushError.error,
                    "detail": exc.detail}
                content.update(exc.data or {})
                return JSONResponse(
                    status_code=getattr(exc, "status", 409), content=content)
            brush_info = {"requested": int(body.brush_width),
                          "adjusted": False, "reason": exc.detail,
                          "reason_code": (exc.data or {}).get("reason")}
        except ActionError as exc:
            if body.brush_required:
                return _action_error(exc)
            brush_info = {"requested": int(body.brush_width),
                          "adjusted": False, "reason": exc.detail}

    # ---- 第 3 段：落笔（drag 全安全管道，多段自动抬笔）----
    try:
        drag_result = controller.run_action({
            "action": "drag", "subpaths": prepared,
            "duration_ms": body.duration_ms, "hwnd": hwnd,
            "monitor": body.monitor, "is_painting": body.is_painting,
            "settle_seconds": body.settle_seconds, "capabilities": caps,
            "include_after_b64": bool(body.include_screenshot)})
    except ActionError as exc:
        return _action_error(exc)

    return _minimal_response(
        ok=True, t0=t0, hwnd=hwnd, info=info, canvas_info=cinfo,
        stroke_meta=_stroke_meta(body, points_in, len(flat), len(prepared),
                                 prim_metas),
        bbox=bbox, warnings=warnings, color_info=color_info,
        brush_info=brush_info, dry_run=False, drag_result=drag_result,
        include_screenshot=bool(body.include_screenshot))


def canvas_frame(canvas_info: Dict[str, Any],
                 window_rect: Any) -> tuple:
    """原语坐标系画框：优先检测到的画布，否则窗口矩形。"""
    cr = canvas_info.get("canvas_rect") if canvas_info else None
    if cr and cr[2] > 2 and cr[3] > 2:
        return (int(cr[0]), int(cr[1]), int(cr[2]), int(cr[3]))
    l, t, r, b = window_rect
    return (int(l), int(t), int(r - l), int(b - t))


def _stroke_meta(body: DrawTrajectoryBody, points_in: int, points_sent: int,
                 subpaths_count: int, prim_metas: List[Dict[str, Any]]
                 ) -> Dict[str, Any]:
    meta = {"points_in": points_in, "points_sent": points_sent,
            "subpaths_count": subpaths_count,
            "smooth_iterations": body.smooth_iterations,
            "closed": body.closed, "is_painting": body.is_painting,
            "duration_ms": body.duration_ms}
    if prim_metas:
        meta["primitives"] = prim_metas
    return meta


def _minimal_response(ok: bool, t0: float, hwnd: int,
                      info: Dict[str, Any], canvas_info: Dict[str, Any],
                      stroke_meta: Dict[str, Any], bbox: List[int],
                      warnings: List[Dict[str, Any]],
                      color_info: Optional[Dict[str, Any]],
                      brush_info: Optional[Dict[str, Any]], dry_run: bool,
                      drag_result: Optional[Dict[str, Any]],
                      include_screenshot: bool = False) -> Dict[str, Any]:
    """v0.16 极简响应：绝不携带全屏截图（~90KB/2.5 万 token 的灾难根因）。"""
    import time

    resp: Dict[str, Any] = {
        "ok": ok,
        "action": "draw_trajectory",
        "window": {"hwnd": hwnd, "title": info.get("title"),
                   "process": info.get("process")},
        "stroke": stroke_meta,
        "bbox": [int(v) for v in bbox],
        "points_count": stroke_meta.get("points_sent"),
        "canvas": {"source": canvas_info.get("source"),
                   "canvas_rect": canvas_info.get("canvas_rect")},
        "warnings": warnings,
        "dry_run": dry_run,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
    }
    if color_info is not None:
        resp["color"] = color_info
    if brush_info is not None:
        resp["brush"] = brush_info
    if not dry_run:
        # 看画面请走 GET /api/ghost/screenshot/thumb（256px 缩略图）
        resp["screenshot_omitted"] = not include_screenshot
        if include_screenshot and drag_result is not None:
            resp["result"] = drag_result  # 旧版全量响应（显式 opt-in）
    return resp


# ---- /draw/sketch_from_image ------------------------------------------------

class SketchFromImageBody(BaseModel):
    model_config = ConfigDict(extra="allow")

    image_path: str = Field(..., description="待复刻图片的本地路径")
    mode: str = Field(default="line", description="line=线稿（Canny 边缘）；"
                                                  "color=彩色（K-Means 量化分色）")
    hwnd: Optional[int] = None
    window: Optional[str] = None
    monitor: int = Field(default=1, ge=0)
    color: Optional[str] = Field(
        default=None, description="line 模式落笔前点击的色板颜色（缺省=当前色）")
    color_required: bool = Field(
        default=False,
        description="true=色板未找到（line 取色或 color 分组取色）时 404 拒绝；"
                    "false=color 模式找不到该色的色板点则跳过该色组并告警")
    k: int = Field(default=6, ge=2, le=12,
                   description="color 模式 K-Means 聚类数")
    max_strokes: int = Field(default=80, ge=1, le=300,
                             description="line 模式最多画的轮廓条数")
    max_strokes_per_color: int = Field(default=24, ge=1, le=100)
    margin: float = Field(default=0.06, ge=0.0, le=0.3,
                          description="画布内边距（占画布宽高比例）")
    max_total_points: int = Field(
        default=_SKETCH_TOTAL_POINT_BUDGET, ge=200,
        le=_SKETCH_TOTAL_POINT_BUDGET,
        description="总点数预算（超出的轮廓按序丢弃并告警）")
    palette_region: Optional[List[float]] = None
    dry_run: bool = Field(
        default=False,
        description="true=只返回复刻计划（轨迹统计/分块/警告），绝不落笔")
    capabilities: Optional[Dict[str, Any]] = None


@router.post("/draw/sketch_from_image")
def sketch_from_image(body: SketchFromImageBody,
                      request: Request = None) -> Any:  # type: ignore[assignment]
    import time

    t0 = time.perf_counter()
    authz.bind_request(request)

    gate = _gates("sketch_from_image", body.monitor)
    if gate is not None:
        return gate

    mode = str(body.mode or "").strip().lower()
    if mode not in ("line", "color"):
        return _err(400, "SAFETY_BLOCKED",
                    "mode must be 'line' or 'color', got " + repr(body.mode))
    caps_res = _require_caps(body.capabilities, "sketch_from_image")
    if isinstance(caps_res, JSONResponse):
        return caps_res
    caps = caps_res
    mon_count = len(screen_mss.monitors())
    if body.monitor < 1 or body.monitor > mon_count - 1:
        return _err(400, "SAFETY_BLOCKED",
                    "monitor out of range: " + str(body.monitor)
                    + " (1.." + str(mon_count - 1) + ")")
    if body.color is not None:
        try:
            trajectory.resolve_color(body.color)
        except ValueError as exc:
            return _err(400, "SAFETY_BLOCKED", str(exc))

    gate, hwnd, info = _resolve_native_window(body.window, body.hwnd)
    if gate is not None:
        return gate
    try:
        rect = win32gui.GetWindowRect(hwnd)
    except Exception:  # noqa: BLE001
        return _err(409, "WINDOW_NOT_FOUND",
                    "window hwnd " + str(hwnd) + " vanished before sketch")

    try:
        cinfo = canvas.detect_window_canvas(rect, body.monitor)
        img = cinfo.pop("img")
    except Exception as exc:  # noqa: BLE001
        return _err(503, "CAPTURE_FAILED",
                    "screen grab failed before sketch: "
                    + type(exc).__name__ + ": " + str(exc))
    canvas_rect = cinfo["canvas_rect"]
    if cinfo["source"] != "detected":
        return _err(422, "CANVAS_NOT_FOUND",
                    "no canvas detected in target window "
                    + repr(info.get("title")) + " — sketch needs a light "
                    "canvas region; open the drawing app first",
                    canvas=cinfo)

    try:
        img_bgr, img_meta = sketch_mod.load_bgr(body.image_path)
    except sketch_mod.SketchError as exc:
        return _err(400, "SAFETY_BLOCKED", str(exc))

    warnings: List[Dict[str, Any]] = []
    try:
        if mode == "line":
            subpaths, plan_meta = sketch_mod.line_subpaths(
                img_bgr, max_strokes=body.max_strokes)
            groups = [{"color": (body.color or "").strip().lower() or None,
                       "subpaths": subpaths}]
        else:
            groups_raw, plan_meta = sketch_mod.color_groups(
                img_bgr, k=body.k,
                max_strokes_per_color=body.max_strokes_per_color)
            groups = [{"color": g["color"], "subpaths": g["subpaths"]}
                      for g in groups_raw]
        if not groups or all(not g["subpaths"] for g in groups):
            return _err(422, "SKETCH_EMPTY",
                        "no drawable strokes extracted from image "
                        "(try another image or raise max_strokes)",
                        plan=plan_meta)
        # 画布适配 + 总点数预算
        budget = int(body.max_total_points)
        planned_groups: List[Dict[str, Any]] = []
        omitted = 0
        for g in groups:
            mapped, fit_meta = sketch_mod.fit_subpaths(
                g["subpaths"], img_meta["width"], img_meta["height"],
                canvas_rect, margin=body.margin)
            kept: List[List[List[int]]] = []
            for seg in mapped:
                if budget - len(seg) < 0:
                    omitted += 1
                    continue
                budget -= len(seg)
                kept.append(seg)
            if kept:
                planned_groups.append({"color": g["color"],
                                       "subpaths": kept,
                                       "fit": fit_meta})
        if omitted:
            warnings.append({"code": "STROKES_OMITTED", "count": omitted,
                             "message": str(omitted) + " stroke(s) omitted: "
                             "total points budget "
                             + str(body.max_total_points) + " exhausted"})
    except Exception as exc:  # noqa: BLE001
        return _err(500, "SKETCH_PLAN_FAILED",
                    "stroke planning failed: " + type(exc).__name__ + ": "
                    + str(exc))

    flat = [p for g in planned_groups for seg in g["subpaths"] for p in seg]
    points_total = len(flat)
    strokes_total = sum(len(g["subpaths"]) for g in planned_groups)
    xs = [p[0] for p in flat]
    ys = [p[1] for p in flat]
    bbox = [min(xs), min(ys), max(xs), max(ys)] if flat else list(canvas_rect)
    warnings.extend(canvas.check_points(flat, monitor=body.monitor,
                                        window_rect=rect, canvas=cinfo))

    if body.dry_run:
        return {
            "ok": True, "action": "sketch_from_image", "dry_run": True,
            "mode": mode, "image": img_meta,
            "strokes_planned": strokes_total,
            "points_total": points_total,
            "groups": [{"color": g["color"],
                        "strokes": len(g["subpaths"]),
                        "points": sum(len(s) for s in g["subpaths"])}
                       for g in planned_groups],
            "plan": plan_meta,
            "canvas": cinfo, "bbox": bbox, "warnings": warnings,
            "elapsed_ms": int((time.perf_counter() - t0) * 1000)}

    # ---- 执行：逐色组取色 → 分块 drag（全部走 run_action 全门禁）----
    region = body.palette_region
    strokes_drawn = 0
    points_drawn = 0
    chunks_executed = 0
    color_report: List[Dict[str, Any]] = []
    try:
        for g in planned_groups:
            color_name = g["color"]
            group_report: Dict[str, Any] = {"color": color_name,
                                            "strokes": len(g["subpaths"])}
            if color_name:
                try:
                    swatch = trajectory.find_swatch(
                        img, rect, color_name,
                        region=tuple(region) if region is not None
                        else (0.0, 0.0, 1.0, 0.30))
                except Exception as exc:  # noqa: BLE001
                    swatch = None
                    group_report["swatch_error"] = (type(exc).__name__ + ": "
                                                    + str(exc))[:200]
                if swatch is None:
                    if body.color_required:
                        return _err(404, "COLOR_SWATCH_NOT_FOUND",
                                    "no solid " + repr(color_name)
                                    + " swatch found in palette region of "
                                    + repr(info.get("title")))
                    if mode == "line":
                        # 线稿模式与 draw_trajectory 同语义：取色失败如实
                        # 告警后用当前色继续画（绝不因色板问题拒绝复刻）。
                        group_report["fallback"] = True
                        warnings.append({
                            "code": "COLOR_FALLBACK", "count": 1,
                            "sample": None,
                            "message": "swatch " + repr(color_name)
                            + " not found; drawing with current color"})
                    else:
                        group_report["skipped"] = True
                        group_report["reason"] = ("swatch not found for "
                                                  + repr(color_name))
                        warnings.append({
                            "code": "COLOR_GROUP_SKIPPED", "count": 1,
                            "sample": None,
                            "message": "color group " + repr(color_name)
                            + " skipped: no palette swatch found"})
                        color_report.append(group_report)
                        continue
                try:
                    controller.run_action({
                        "action": "click", "x": int(swatch[0]),
                        "y": int(swatch[1]), "hwnd": hwnd,
                        "monitor": body.monitor, "settle_seconds": 0.2,
                        "capabilities": caps, "include_after_b64": False})
                except ActionError as exc:
                    return _action_error(exc)
                group_report["swatch_clicked"] = True
            calls = sketch_mod.chunk_calls(g["subpaths"])
            group_report["chunks"] = len(calls)
            for call in calls:
                try:
                    controller.run_action({
                        "action": "drag", "subpaths": call["segments"],
                        "duration_ms": call["duration_ms"], "hwnd": hwnd,
                        "monitor": body.monitor, "is_painting": True,
                        "settle_seconds": 0.05, "capabilities": caps,
                        "include_after_b64": False})
                except ActionError as exc:
                    group_report["error"] = exc.error + ": " + exc.detail
                    color_report.append(group_report)
                    return _action_error(exc)
                chunks_executed += 1
                strokes_drawn += len(call["segments"])
                points_drawn += call["points"]
            color_report.append(group_report)
    except Exception as exc:  # noqa: BLE001
        return _err(500, "SKETCH_EXECUTE_FAILED",
                    "sketch execution failed: " + type(exc).__name__ + ": "
                    + str(exc))

    if strokes_drawn == 0:
        return _err(422, "SKETCH_NOT_DRAWN",
                    "planned " + str(strokes_total) + " stroke(s) but 0 were "
                    "drawn (all color groups skipped?)",
                    color_report=color_report, warnings=warnings)

    return {
        "ok": True, "action": "sketch_from_image", "dry_run": False,
        "mode": mode, "image": img_meta,
        "window": {"hwnd": hwnd, "title": info.get("title"),
                   "process": info.get("process")},
        "strokes_planned": strokes_total, "strokes_drawn": strokes_drawn,
        "points_planned": points_total, "points_drawn": points_drawn,
        "chunks_executed": chunks_executed,
        "color_groups": color_report, "plan": plan_meta,
        "canvas": {"source": cinfo.get("source"),
                   "canvas_rect": cinfo.get("canvas_rect")},
        "bbox": [int(v) for v in bbox],
        "screenshot_omitted": True,
        "warnings": warnings,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000)}


# ---- /draw/canvas_info ------------------------------------------------------

class CanvasInfoBody(BaseModel):
    model_config = ConfigDict(extra="allow")

    hwnd: Optional[int] = None
    window: Optional[str] = None
    monitor: int = Field(default=1, ge=0)


@router.post("/draw/canvas_info")
def draw_canvas_info(body: CanvasInfoBody,
                     request: Request = None) -> Any:  # type: ignore[assignment]
    """画纸物理边界探测（只读：抓屏 + CV，零输入、无需能力令牌）。"""
    import time

    t0 = time.perf_counter()
    authz.bind_request(request)
    mon_count = len(screen_mss.monitors())
    if body.monitor < 1 or body.monitor > mon_count - 1:
        return _err(400, "SAFETY_BLOCKED",
                    "monitor out of range: " + str(body.monitor)
                    + " (1.." + str(mon_count - 1) + ")")
    gate, hwnd, info = _resolve_native_window(body.window, body.hwnd)
    if gate is not None:
        return gate
    try:
        rect = win32gui.GetWindowRect(hwnd)
    except Exception:  # noqa: BLE001
        return _err(409, "WINDOW_NOT_FOUND",
                    "window hwnd " + str(hwnd) + " vanished")
    try:
        cinfo = canvas.detect_window_canvas(rect, body.monitor)
        cinfo.pop("img", None)  # PIL Image 不可 JSON 序列化，绝不进响应
    except Exception as exc:  # noqa: BLE001
        return _err(503, "CAPTURE_FAILED",
                    "screen grab failed during canvas probe: "
                    + type(exc).__name__ + ": " + str(exc))
    try:
        mon_rect = screen_mss.monitor_rect(body.monitor)
    except Exception:  # noqa: BLE001
        mon_rect = None
    return {"ok": True, "action": "canvas_info",
            "window": {"hwnd": hwnd, "title": info.get("title"),
                       "process": info.get("process")},
            "window_rect": [int(v) for v in rect],
            "monitor": body.monitor, "monitor_rect": mon_rect,
            "canvas": cinfo,
            "elapsed_ms": int((time.perf_counter() - t0) * 1000)}
