"""通用绘制轨迹资产（v0.9.1 → v0.16 工业级）：平滑 + 降采样 + 画板取色。

从 test_v09_draw_whale.py（纯鼠标鲸鱼）沉淀的服务端资产，供
draw_trajectory 复合动作（HTTP / SDK / MCP）复用。四件套：

1. chaikin()          —— 角点控制多边形 → 平滑曲线（切角迭代；
                         v0.16 支持 preserve_corners 保留指定尖角不切）。
2. prepare_trajectory()—— 平滑 + 等距降采样（保端点，≤2000 点，与
                         controller drag 的轨迹上限同源）。
   prepare_subpaths() —— v0.16 多段轨迹（subpaths）前处理：逐段平滑 +
                         降采样（段间由 controller drag 物理抬笔，绝不连线）。
3. find_swatch()      —— CV 在窗口色板区定位"实心纯色圆点"（HSV 域 + 
                         填充率 + 中心像素复核，排除选中态圆环）。

物理层铁律不在本模块：真实鼠标移动事件由 backend.move_to 的
mouse_event 路径提供（SetCursorPos 画不出轨迹，见 docs/底层物理控制.md）。
"""
from __future__ import annotations

import colorsys
from typing import Any, List, Optional, Sequence, Tuple

import cv2
import numpy as np

MAX_TRAJECTORY_POINTS = 2000  # 与 controller.MAX_TRAJECTORY_POINTS 同源


def _chaikin_open(pts: List[Tuple[float, float]], iterations: int
                  ) -> List[Tuple[float, float]]:
    """开放曲线 Chaikin（保留首尾端点）——迭代主体，供各链复用。"""
    for _ in range(iterations):
        out: List[Tuple[float, float]] = []
        n = len(pts)
        out.append(pts[0])
        for i in range(n - 1):
            p0 = pts[i]
            p1 = pts[i + 1]
            out.append((0.75 * p0[0] + 0.25 * p1[0],
                        0.75 * p0[1] + 0.25 * p1[1]))
            out.append((0.25 * p0[0] + 0.75 * p1[0],
                        0.25 * p0[1] + 0.75 * p1[1]))
        out.append(pts[-1])
        pts = out
    return pts


def normalize_preserve(preserve_corners: Optional[Sequence[Any]],
                       n: int) -> List[int]:
    """preserve_corners 归一化：只留 0..n-1 的 int，去重升序。

    索引语义 = 传入控制点列表的原始下标（负数/越界一律丢弃，绝不猜）。
    """
    if not preserve_corners:
        return []
    out = set()
    for idx in preserve_corners:
        if isinstance(idx, bool) or not isinstance(idx, int):
            continue
        if 0 <= idx < n:
            out.add(idx)
    return sorted(out)


def chaikin(points: List[Tuple[float, float]], iterations: int = 2,
            closed: bool = False,
            preserve_corners: Optional[Sequence[Any]] = None
            ) -> List[Tuple[float, float]]:
    """Chaikin 切角平滑：控制多边形 → 平滑曲线，迭代次数越多越圆润。

    closed=False 时保留原首尾端点（开放曲线）；closed=True 时首尾相接
    （注意：画图软件会把闭合笔迹并成闭合形状，默认开放，见
    docs/底层物理控制.md 第 3 节）。

    preserve_corners（v0.16）：控制点下标数组——这些尖角被原样保留
    （切链平滑：角点两侧各成独立链独立平滑，角点本身逐迭代原样输出）。
    典型用途：眼睛/星形尖角等绝不能被抹圆的几何特征。
    """
    pts = [(float(x), float(y)) for x, y in points]
    if len(pts) < 2:
        return pts
    iterations = max(0, int(iterations))
    pres = normalize_preserve(preserve_corners, len(pts))
    if not pres:
        if not closed:
            return _chaikin_open(pts, iterations)
        for _ in range(iterations):
            out = []
            n = len(pts)
            for i in range(n):
                p0 = pts[i]
                p1 = pts[(i + 1) % n]
                out.append((0.75 * p0[0] + 0.25 * p1[0],
                            0.75 * p0[1] + 0.25 * p1[1]))
                out.append((0.25 * p0[0] + 0.75 * p1[0],
                            0.25 * p0[1] + 0.75 * p1[1]))
            pts = out
        if len(pts) > 1:
            pts.append(pts[0])
        return pts

    # ---- 保留尖角：在角点处切链，每链独立平滑（端点=角点，天然不丢） ----
    if closed:
        ring = pts + [pts[0]]  # 环展开成开链（末点=首点）
        anchors = pres if pres else [0]
        if len(anchors) == 1:
            a = anchors[0]
            chains = [ring[a:] + ring[1:a + 1]]  # 整环从 a 回到 a
        else:
            chains = []
            for i, a in enumerate(anchors):
                b = anchors[(i + 1) % len(anchors)]
                if i == len(anchors) - 1:
                    chains.append(ring[a:] + ring[:b + 1])
                else:
                    chains.append(ring[a:b + 1])
    else:
        bounds = sorted(set([0] + pres + [len(pts) - 1]))
        chains = [pts[bounds[i]:bounds[i + 1] + 1]
                  for i in range(len(bounds) - 1)
                  if bounds[i + 1] > bounds[i]]
    out: List[Tuple[float, float]] = []
    for chain in chains:
        sm = _chaikin_open(chain, iterations)
        out.extend(sm if not out else sm[1:])  # 拼接去重（链端点=角点）
    return out


def downsample(points: List[Tuple[float, float]],
               max_points: int = MAX_TRAJECTORY_POINTS
               ) -> List[Tuple[float, float]]:
    """等距降采样，首尾端点必保留（与 controller drag 的上限策略一致）。"""
    pts = [(float(x), float(y)) for x, y in points]
    if len(pts) <= max_points:
        return pts
    keep = int(max_points)
    step = (len(pts) - 1) / (keep - 1)
    return [pts[round(i * step)] for i in range(keep)]


def prepare_trajectory(points: List[List[float]],
                       smooth_iterations: int = 2, closed: bool = False,
                       max_points: int = MAX_TRAJECTORY_POINTS,
                       preserve_corners: Optional[Sequence[Any]] = None
                       ) -> List[List[int]]:
    """draw_trajectory 的轨迹前处理：平滑 → 闭合补端 → 降采样 → 整数化。"""
    pts = [(float(x), float(y)) for x, y in points]
    if closed:
        if len(pts) >= 2 and pts[0] != pts[-1]:
            pts = pts + [pts[0]]
    smoothed = chaikin(pts, smooth_iterations, closed=False,
                       preserve_corners=preserve_corners)
    final = downsample(smoothed, max_points)
    return [[int(round(x)), int(round(y))] for x, y in final]


def _per_segment_preserve(preserve_corners: Optional[Sequence[Any]],
                          count: int) -> List[Optional[Sequence[Any]]]:
    """preserve_corners 的多段语义：扁平数组 → 每段同款；嵌套数组 → 逐段。

    嵌套长度不足的段没有保留角（None）；嵌套超长 → ValueError（绝不静默截断）。
    """
    if not preserve_corners:
        return [None] * count
    if all(isinstance(v, int) and not isinstance(v, bool)
           for v in preserve_corners):
        return [preserve_corners] * count
    if all(isinstance(v, (list, tuple)) for v in preserve_corners):
        if len(preserve_corners) > count:
            raise ValueError(
                "preserve_corners nested list is longer than subpaths ("
                + str(len(preserve_corners)) + " > " + str(count) + ")")
        return list(preserve_corners) + [None] * (count - len(preserve_corners))
    raise ValueError("preserve_corners must be [int, ...] (每个下标相对各段自身"
                     "控制点) 或 [[int,...], ...] (逐段)")


def prepare_subpaths(subpaths: List[List[List[float]]],
                     smooth_iterations: int = 2, closed: bool = False,
                     preserve_corners: Optional[Sequence[Any]] = None,
                     max_points: int = MAX_TRAJECTORY_POINTS
                     ) -> List[List[List[int]]]:
    """多段轨迹前处理（v0.16）：逐段 平滑→闭合补端→降采样→整数化。

    段间衔接由 controller drag 物理抬笔完成（mouse_up → 移动 → mouse_down），
    本模块只负责把每段各自准备好；任何一段平滑后塌缩到 <2 点即报错。
    """
    per_pres = _per_segment_preserve(preserve_corners, len(subpaths))
    out: List[List[List[int]]] = []
    for seg, pres in zip(subpaths, per_pres):
        prepared = prepare_trajectory(seg, smooth_iterations, closed,
                                      max_points, pres)
        if len(prepared) < 2:
            raise ValueError(
                "a subpath collapsed to <2 points after smoothing")
        out.append(prepared)
    return out


# ---- 画板取色：HSV 域 + 中心像素复核 ---------------------------------------
# 每个命名颜色 = (inRange 下界, inRange 上界, 中心像素 BGR 判定函数)。
# 判定函数排除"选中态圆环"（环是彩色的但中心仍是黑/白）。

def _center_blue(b: int, g: int, r: int) -> bool:
    return b > 140 and b > r + 30 and b > g + 10


def _center_red(b: int, g: int, r: int) -> bool:
    return r > 130 and r > b + 40 and r > g + 40


def _center_green(b: int, g: int, r: int) -> bool:
    return g > 110 and g > r + 15 and g > b + 15


def _center_yellow(b: int, g: int, r: int) -> bool:
    return r > 150 and g > 130 and b < r - 50 and b < g - 40


def _center_orange(b: int, g: int, r: int) -> bool:
    return r > 150 and 50 < g < r - 40 and b < r - 80


def _center_purple(b: int, g: int, r: int) -> bool:
    return b > 110 and r > 70 and b > g + 40 and r > g + 10


def _center_black(b: int, g: int, r: int) -> bool:
    return max(b, g, r) < 90


def _center_white(b: int, g: int, r: int) -> bool:
    return min(b, g, r) > 215


_NAMED_COLORS = {
    "blue":   ((95, 120, 80), (135, 255, 255), _center_blue),
    "red":    ((0, 120, 80), (8, 255, 255), _center_red),
    "red2":   ((172, 120, 80), (180, 255, 255), _center_red),
    "green":  ((35, 100, 60), (85, 255, 255), _center_green),
    "yellow": ((20, 120, 120), (32, 255, 255), _center_yellow),
    "orange": ((10, 120, 120), (22, 255, 255), _center_orange),
    "purple": ((125, 80, 60), (160, 255, 255), _center_purple),
    "black":  ((0, 0, 0), (180, 160, 70), _center_black),
    "white":  ((0, 0, 215), (180, 40, 255), _center_white),
}


def _hex_to_ranges(hexstr: str) -> Tuple[Tuple[int, int, int],
                                         Tuple[int, int, int], object]:
    """#RRGGBB → HSV inRange 域（色相 ±14 环形容差）+ 中心像素复核。"""
    s = hexstr.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        raise ValueError("hex color must be #RRGGBB or #RGB: " + repr(hexstr))
    try:
        rn, gn, bn = (int(s[0:2], 16) / 255.0, int(s[2:4], 16) / 255.0,
                      int(s[4:6], 16) / 255.0)
    except ValueError as exc:
        raise ValueError("bad hex color: " + repr(hexstr)) from exc
    h, lv, sv = colorsys.rgb_to_hsv(rn, gn, bn)
    hue = h * 179.0  # OpenCV H ∈ 0..179
    s8 = sv * 255.0
    v8 = lv * 255.0
    tol = 14
    lo_h = max(0, hue - tol)
    hi_h = min(179, hue + tol)
    if lo_h <= 0 or hi_h >= 179:  # 跨红端环形展开
        ranges = [((0, int(s8 * 0.55), int(v8 * 0.5)),
                   (hi_h, 255, 255)),
                  ((lo_h, int(s8 * 0.55), int(v8 * 0.5)),
                   (179, 255, 255))]
    else:
        ranges = [((lo_h, int(s8 * 0.55), int(v8 * 0.5)),
                   (hi_h, 255, 255))]
    tr, tg, tb = int(rn * 255), int(gn * 255), int(bn * 255)

    def _center(b: int, g: int, r: int, _t=(tb, tg, tr)) -> bool:
        return (abs(b - _t[0]) <= 80 and abs(g - _t[1]) <= 80
                and abs(r - _t[2]) <= 80)

    return ranges[0][0], ranges[0][1], _center  # 多域场景取主域（环形红极少用于色板）


def resolve_color(color: str) -> Tuple[List[Tuple[Tuple[int, int, int],
                                                 Tuple[int, int, int]]],
                                       object]:
    """颜色名 / #hex → ([(lo, hi), ...] HSV 域列表, 中心像素判定函数)。"""
    name = str(color).strip().lower()
    if name.startswith("#"):
        lo, hi, checker = _hex_to_ranges(name)
        return [(lo, hi)], checker
    aliases = {"darkblue": "blue", "lightblue": "blue", "skyblue": "blue",
               "cyan": "blue", "darkred": "red", "lightred": "red",
               "darkgreen": "green", "lightgreen": "green",
               "gray": "black", "grey": "black",
               "darkyellow": "yellow", "gold": "yellow",
               "violet": "purple", "magenta": "purple"}
    key = aliases.get(name, name)
    if key == "red":
        return [(_NAMED_COLORS["red"][0], _NAMED_COLORS["red"][1]),
                (_NAMED_COLORS["red2"][0], _NAMED_COLORS["red2"][1])], \
            _NAMED_COLORS["red"][2]
    if key not in _NAMED_COLORS:
        raise ValueError("unknown color " + repr(color)
                         + " (named: " + ", ".join(sorted(
                             k for k in _NAMED_COLORS if not k.endswith("2")))
                         + " or #RRGGBB)")
    lo, hi, checker = _NAMED_COLORS[key]
    return [(lo, hi)], checker


def find_swatch(img, rect: Tuple[int, int, int, int], color: str,
                region: Tuple[float, float, float, float] = (0.0, 0.0, 1.0,
                                                             0.30)
                ) -> Optional[Tuple[int, int, int]]:
    """在窗口 rect 的 region（相对分数 x0,y0,x1,y1）内找"实心纯色色板点"。

    img 是全屏 BGR 截图（numpy），rect 是窗口 (l,t,r,b) 屏幕物理像素。
    返回 (x, y, area)（屏幕坐标）或 None。规则与鲸鱼画图实测一致：
    面积/长宽比/填充率过滤 + 中心像素必须真是目标色（排除选中态圆环），
    取最靠上者（同高取面积大者）。
    """
    if img is None:
        return None
    if not isinstance(img, np.ndarray):
        # screen_mss.grab 返回 PIL Image；统一转 numpy BGR（与 cv2 同序）
        img = cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2BGR)
    if img.size == 0:
        return None
    l, t, r, b = rect
    w, h = r - l, b - t
    if w < 200 or h < 200:
        return None
    fx0, fy0, fx1, fy1 = region
    x0 = l + int(w * float(fx0))
    x1 = l + int(w * float(fx1))
    y0 = t + int(h * float(fy0))
    y1 = t + int(h * float(fy1))
    x0, y0 = max(0, x0), max(0, y0)
    crop = img[y0:y1, x0:x1]
    if crop.size == 0:
        return None
    ranges, checker = resolve_color(color)
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
    for lo, hi in ranges:
        mask |= cv2.inRange(hsv, np.array(lo, dtype=np.uint8),
                            np.array(hi, dtype=np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    best = None
    for c in contours:
        area = cv2.contourArea(c)
        if area < 60 or area > 3000:
            continue
        x, y, cw, ch = cv2.boundingRect(c)
        if cw < 10 or ch < 10 or not (0.6 <= cw / float(ch) <= 1.7):
            continue
        if area / float(max(1, cw * ch)) < 0.35:
            continue  # 排除空心环（选中态）
        cx = x0 + x + cw // 2
        cy = y0 + y + ch // 2
        cb, cg, cr = [int(v) for v in img[cy, cx]]
        if not checker(cb, cg, cr):
            continue
        if best is None or cy < best[1] or (cy == best[1] and area > best[2]):
            best = (cx, cy, area)
    return best
