"""几何原语引擎（v0.16 工业级物理操控引擎）。

大模型只传语义参数（{"type":"circle","center":[0.5,0.5],"radius":0.2}），
本模块在本地把原语展开成几百个平滑坐标点——绝不让主仓模型算三角函数。

坐标系约定：
- units="frac"（默认）：center/x/y 等点坐标按目标画框 (l,t,w,h) 的宽高归一
  （[0,1] 相对画框左上角）；radius/rx/ry/rect 宽高等"长度量"按
  min(frame_w, frame_h) 等比缩放——保证圆在任何画框里都是正圆。
- units="px"：所有数值都是相对画框左上角的物理像素。

输出为"段"（subpath）点列：闭合原语首尾补端（首点复制到末尾），
controller drag 落笔后画回起点；点数自适应弧长（步距 ~2.5px，
上限 720 点），下游还有 ≤2000 点/段的等距降采样兜底。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Sequence, Tuple

Frame = Tuple[int, int, int, int]  # (left, top, width, height) 物理像素

MAX_PRIMITIVE_POINTS = 720
_STEP_PX = 2.5  # 相邻采样点目标间距（物理像素）


class PrimitiveError(ValueError):
    """原语参数非法——路由层映射为 400 SAFETY_BLOCKED。"""


def _num(spec: Dict[str, Any], key: str) -> float:
    v = spec.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise PrimitiveError(
            "primitive field " + repr(key) + " must be a number, got "
            + repr(v))
    return float(v)


def _point(spec: Dict[str, Any], key: str, frame: Frame, units: str
           ) -> Tuple[float, float]:
    v = spec.get(key)
    if (not isinstance(v, (list, tuple)) or len(v) < 2
            or any(isinstance(c, bool) or not isinstance(c, (int, float))
                   for c in v[:2])):
        raise PrimitiveError(
            "primitive field " + repr(key) + " must be [x,y] numbers, got "
            + repr(v))
    x, y = float(v[0]), float(v[1])
    l, t, w, h = frame
    if units == "frac":
        if not (-2.0 <= x <= 3.0 and -2.0 <= y <= 3.0):
            raise PrimitiveError(
                "frac coordinates must lie in [-2,3] (got " + repr(key)
                + "=" + repr(v) + "); use units='px' for pixel values")
        return l + x * w, t + y * h
    return l + x, t + y


def _length(v: float, frame: Frame, units: str, key: str) -> float:
    f = float(v)
    if units == "frac":
        if not (0.0 <= f <= 4.0):
            raise PrimitiveError(
                "frac length " + repr(key) + " must lie in [0,4] (got "
                + repr(f) + "); use units='px' for pixel values")
        return f * min(frame[2], frame[3])
    if f < 0:
        raise PrimitiveError("length " + repr(key) + " must be >= 0")
    return f


def _arc_points(cx: float, cy: float, r: float, a0: float, a1: float
                ) -> List[Tuple[float, float]]:
    sweep = abs(a1 - a0)
    n = max(8, min(MAX_PRIMITIVE_POINTS,
                   int(math.ceil(max(sweep, 1e-6) * r / _STEP_PX))))
    return [(cx + r * math.cos(a0 + (a1 - a0) * i / n),
             cy + r * math.sin(a0 + (a1 - a0) * i / n))
            for i in range(n + 1)]


def _bezier_points(ctrl: Sequence[Tuple[float, float]]) -> List[Tuple[float, float]]:
    if len(ctrl) == 3:      # 二次
        p0, p1, p2 = ctrl
        f = lambda t: (  # noqa: E731
            (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t ** 2 * p2[0],
            (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t ** 2 * p2[1])
    elif len(ctrl) == 4:    # 三次
        p0, p1, p2, p3 = ctrl
        f = lambda t: (  # noqa: E731
            (1 - t) ** 3 * p0[0] + 3 * (1 - t) ** 2 * t * p1[0]
            + 3 * (1 - t) * t ** 2 * p2[0] + t ** 3 * p3[0],
            (1 - t) ** 3 * p0[1] + 3 * (1 - t) ** 2 * t * p1[1]
            + 3 * (1 - t) * t ** 2 * p2[1] + t ** 3 * p3[1])
    else:
        raise PrimitiveError(
            "bezier needs p0..p2 (quadratic) or p0..p3 (cubic), got "
            + str(len(ctrl)) + " control points")
    # 控制多边形周长决定点数（贝塞尔弧长 ≤ 多边形周长）
    peri = sum(math.dist(ctrl[i], ctrl[i + 1]) for i in range(len(ctrl) - 1))
    n = max(16, min(MAX_PRIMITIVE_POINTS, int(math.ceil(peri / _STEP_PX))))
    return [f(i / n) for i in range(n + 1)]


def _close(pts: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    if pts and math.dist(pts[0], pts[-1]) > 0.25:
        pts.append(pts[0])
    return pts


def primitive_points(spec: Dict[str, Any], frame: Frame,
                     units: str = "frac") -> List[Tuple[float, float]]:
    """单个原语 spec → 物理像素点列。非法参数抛 PrimitiveError。"""
    if not isinstance(spec, dict):
        raise PrimitiveError("each primitive must be an object, got "
                             + repr(type(spec).__name__))
    ptype = str(spec.get("type", "")).strip().lower()
    if units not in ("frac", "px"):
        raise PrimitiveError("units must be 'frac' or 'px', got "
                             + repr(units))
    l, t, w, h = frame
    if w < 2 or h < 2:
        raise PrimitiveError("frame is degenerate: " + repr(frame))

    if ptype == "circle":
        cx, cy = _point(spec, "center", frame, units)
        r = _length(_num(spec, "radius"), frame, units, "radius")
        if r <= 0:
            raise PrimitiveError("radius must be > 0")
        n = max(24, min(MAX_PRIMITIVE_POINTS,
                        int(math.ceil(2 * math.pi * r / _STEP_PX))))
        return _close([(cx + r * math.cos(2 * math.pi * i / n),
                        cy + r * math.sin(2 * math.pi * i / n))
                       for i in range(n)])

    if ptype == "ellipse":
        cx, cy = _point(spec, "center", frame, units)
        rx = _length(_num(spec, "rx" if "rx" in spec else "radius"),
                     frame, units, "rx")
        ry = _length(_num(spec, "ry" if "ry" in spec else "radius"),
                     frame, units, "ry")
        rot = math.radians(_num(spec, "rotation_deg")) if "rotation_deg" in spec else 0.0
        if rx <= 0 or ry <= 0:
            raise PrimitiveError("rx/ry must be > 0")
        n = max(24, min(MAX_PRIMITIVE_POINTS,
                        int(math.ceil(math.pi * (rx + ry) / _STEP_PX))))
        cr, sr = math.cos(rot), math.sin(rot)
        return _close([(cx + rx * math.cos(2 * math.pi * i / n) * cr
                        - ry * math.sin(2 * math.pi * i / n) * sr,
                        cy + rx * math.cos(2 * math.pi * i / n) * sr
                        + ry * math.sin(2 * math.pi * i / n) * cr)
                       for i in range(n)])

    if ptype == "arc":
        cx, cy = _point(spec, "center", frame, units)
        r = _length(_num(spec, "radius"), frame, units, "radius")
        a0 = math.radians(_num(spec, "start_deg"))
        a1 = math.radians(_num(spec, "end_deg"))
        return _arc_points(cx, cy, r, a0, a1)

    if ptype == "line":
        x0, y0 = _point(spec, "from", frame, units)
        x1, y1 = _point(spec, "to", frame, units)
        return [(x0, y0), (x1, y1)]

    if ptype == "rect":
        x0, y0 = _point(spec, "top_left", frame, units)
        rw = _length(_num(spec, "width"), frame, units, "width")
        rh = _length(_num(spec, "height"), frame, units, "height")
        if rw <= 0 or rh <= 0:
            raise PrimitiveError("rect width/height must be > 0")
        return _close([(x0, y0), (x0 + rw, y0), (x0 + rw, y0 + rh),
                       (x0, y0 + rh)])

    if ptype == "polygon":
        v = spec.get("vertices")
        if (not isinstance(v, list) or len(v) < 3
                or not all(isinstance(p, (list, tuple)) and len(p) >= 2
                           for p in v)):
            raise PrimitiveError("polygon vertices must be >=3 [x,y] pairs")
        pts = [_point({"__v": p}, "__v", frame, units) for p in v]
        return _close(pts)

    if ptype in ("bezier", "curve"):
        ctrl: List[Tuple[float, float]] = []
        for key in ("p0", "p1", "p2", "p3"):
            if key in spec:
                ctrl.append(_point(spec, key, frame, units))
        if len(ctrl) < 3:
            raise PrimitiveError(
                "bezier needs p0,p1,p2 (quadratic) or p0..p3 (cubic)")
        return _bezier_points(ctrl)

    raise PrimitiveError(
        "unknown primitive type " + repr(spec.get("type"))
        + " (valid: circle, ellipse, arc, line, rect, polygon, bezier)")


def primitives_to_subpaths(specs: List[Dict[str, Any]], frame: Frame,
                           units: str = "frac"
                           ) -> Tuple[List[List[List[float]]], List[Dict[str, Any]]]:
    """原语列表 → 段列表（物理像素 float 点列）+ 每段元信息。

    点数上限守卫在调用方（draw_routes / controller）；这里只做纯几何。
    """
    subs: List[List[List[float]]] = []
    metas: List[Dict[str, Any]] = []
    for i, spec in enumerate(specs):
        pts = primitive_points(spec, frame, units)
        if len(pts) < 2:
            raise PrimitiveError("primitive " + str(i) + " collapsed")
        subs.append([[float(x), float(y)] for x, y in pts])
        ptype = str(spec.get("type", "?")).lower()
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        metas.append({"type": ptype, "points": len(pts),
                      "bbox": [int(min(xs)), int(min(ys)),
                               int(max(xs)), int(max(ys))]})
    return subs, metas
