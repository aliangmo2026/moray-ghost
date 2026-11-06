"""图像自动复刻引擎（v0.16 工业级物理操控引擎 · 终极杀招）。

大模型只传一张图片路径，本模块纯本地完成：
  读图 → （line）Canny 边缘 → 轮廓 → Douglas-Peucker 极简化
       → （color）K-Means 颜色聚类量化 → 逐色域轮廓极简化
  → 等比适配进目标画布（contain + 边距）
  → 切块成 drag 调用计划（≤ 段数/点数/时长上限，全部走安全管道）。

零模型依赖（纯 OpenCV/numpy）；鼠标一个像素都不碰——执行在路由层
经 controller.run_action 全门禁完成。本模块只产出"计划"。
"""
from __future__ import annotations

import math
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

MAX_IMAGE_BYTES = 20 * 1024 * 1024
ALLOWED_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")
# K-Means 前把图压到该边长以内（量化不需要全分辨率）
QUANT_MAX_SIDE = 220
DEFAULT_KMEANS_K = 6
# 经典画图（mspaint）调色板 RGB——color 模式把聚类色就近映射到这些
# 真实存在的色板点（find_swatch 找得到的颜色才画得出来）
PALETTE_RGB = [
    ("black", (0, 0, 0)),
    ("red", (237, 28, 36)),
    ("orange", (255, 127, 39)),
    ("yellow", (255, 242, 0)),
    ("green", (34, 177, 76)),
    ("blue", (0, 162, 232)),
    ("purple", (163, 73, 164)),
    ("white", (255, 255, 255)),
]
# 线稿规划的细节参数
CANNY_LO_FRAC = 0.66       # 阈值自适应（中值法）
CANNY_HI_FRAC = 1.33
APPROX_EPS_PX = 1.6        # Douglas-Peucker 基础 epsilon（物理像素）
MIN_STROKE_LEN_PX = 14.0   # 短于此弧长的轮廓丢弃（噪声）
MIN_STROKE_AREA = 24       # 面积过小的轮廓丢弃


class SketchError(ValueError):
    """图片/参数非法——路由层映射 400。"""


# ---- 读图 -------------------------------------------------------------------

def load_bgr(path: str) -> Tuple["np.ndarray", Dict[str, Any]]:
    """读本地图片 → (BGR ndarray, meta)。路径/大小/扩展名校验。"""
    p = str(path or "").strip().strip('"')
    if not p:
        raise SketchError("image_path is empty")
    if not os.path.isfile(p):
        raise SketchError("image_path does not exist: " + repr(p))
    ext = os.path.splitext(p)[1].lower()
    if ext not in ALLOWED_EXTS:
        raise SketchError("unsupported image extension " + repr(ext)
                          + " (allowed: " + ", ".join(ALLOWED_EXTS) + ")")
    try:
        size = os.path.getsize(p)
    except OSError as exc:
        raise SketchError("cannot stat image: " + str(exc)) from exc
    if size <= 0:
        raise SketchError("image file is empty")
    if size > MAX_IMAGE_BYTES:
        raise SketchError("image too large: " + str(size)
                          + " bytes (max " + str(MAX_IMAGE_BYTES) + ")")
    data = np.fromfile(p, dtype=np.uint8)  # 中文路径安全（cv2.imread 不可靠）
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None or img.size == 0:
        raise SketchError("cannot decode image: " + repr(p))
    h, w = img.shape[:2]
    if w < 8 or h < 8:
        raise SketchError("image too small: " + str(w) + "x" + str(h))
    return img, {"path": p, "width": int(w), "height": int(h),
                 "bytes": int(size)}


# ---- 轨迹规划（图像像素坐标系） ----------------------------------------------

def _approx(contour: "np.ndarray", perimeter: float,
            eps_scale: float = 0.006) -> "np.ndarray":
    """Douglas-Peucker：epsilon = max(基础像素, 周长比例)。"""
    eps = max(APPROX_EPS_PX, eps_scale * perimeter)
    approx = cv2.approxPolyDP(contour, eps, True)
    if approx is None or len(approx) < 2:
        approx = contour
    return approx


def _contour_to_subpath(contour: "np.ndarray", closed: bool = True
                        ) -> List[List[int]]:
    pts = [[int(p[0][0]), int(p[0][1])] for p in contour]
    if closed and len(pts) >= 2 and pts[0] != pts[-1]:
        pts.append(pts[0])
    return pts


def line_subpaths(img_bgr: "np.ndarray",
                  max_strokes: int = 80
                  ) -> Tuple[List[List[List[int]]], Dict[str, Any]]:
    """线稿模式：Canny → 轮廓 → DP 极简 → 闭合轨迹（图像像素坐标）。"""
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    med = float(np.median(gray))
    lo = int(max(0, CANNY_LO_FRAC * med))
    hi = int(min(255, CANNY_HI_FRAC * med))
    if hi <= lo:
        hi = min(255, lo + 60)
    edges = cv2.Canny(gray, lo, hi)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST,
                                   cv2.CHAIN_APPROX_NONE)
    h, w = gray.shape[:2]
    img_area = float(w * h)
    scored: List[Tuple[float, "np.ndarray"]] = []
    for c in contours:
        area = abs(float(cv2.contourArea(c)))
        if area > 0.92 * img_area:      # 贴边大框（边缘检测的外框）丢弃
            continue
        peri = float(cv2.arcLength(c, True))
        if peri < MIN_STROKE_LEN_PX and area < MIN_STROKE_AREA:
            continue
        scored.append((max(peri, area * 0.1), c))
    scored.sort(key=lambda t: -t[0])
    subpaths: List[List[List[int]]] = []
    dropped = 0
    for score, c in scored[:max_strokes]:
        sp = _contour_to_subpath(_approx(c, max(score, 1.0)))
        if len(sp) >= 2:
            subpaths.append(sp)
        else:
            dropped += 1
    dropped += max(0, len(scored) - max_strokes)
    meta = {"canny_lo": lo, "canny_hi": hi,
            "contours": len(contours), "kept": len(subpaths),
            "dropped": dropped}
    return subpaths, meta


def nearest_palette_color(bgr: Sequence[float]) -> Tuple[str, float]:
    """BGR → (最近色板名, RGB 距离²)。white 也可能被选中（调用方决定跳过）。"""
    b, g, r = float(bgr[0]), float(bgr[1]), float(bgr[2])
    best_name, best_d = None, None
    for name, (pr, pg, pb) in PALETTE_RGB:
        d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
        if best_d is None or d < best_d:
            best_name, best_d = name, d
    return best_name, float(best_d)


def color_groups(img_bgr: "np.ndarray", k: int = DEFAULT_KMEANS_K,
                 max_strokes_per_color: int = 24
                 ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """彩色模式：K-Means 量化 → 逐色域外轮廓 → DP 极简 → 按色分组。

    每组 = {"color": 色板名, "bgr": 量化中心, "subpaths": [...]}。
    就近映射到经典画图色板（白域跳过——白纸画白不可见，调用方告警）。
    """
    h, w = img_bgr.shape[:2]
    scale = min(1.0, QUANT_MAX_SIDE / float(max(w, h)))
    small = cv2.resize(img_bgr, (max(8, int(w * scale)),
                                 max(8, int(h * scale))),
                       interpolation=cv2.INTER_AREA) if scale < 1.0 else img_bgr
    sh, sw = small.shape[:2]
    pixels = small.reshape(-1, 3).astype(np.float32)
    k = max(2, min(12, int(k)))
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 12, 1.0)
    _ret, labels, centers = cv2.kmeans(pixels, k, None, criteria, 3,
                                       cv2.KMEANS_PP_CENTERS)
    labels = labels.reshape(sh, sw)
    groups: List[Dict[str, Any]] = []
    skipped_white = 0
    for ci in range(k):
        center = centers[ci]
        name, _dist = nearest_palette_color(center)
        v = float(max(center))            # max(B,G,R) ≈ 亮度
        s = float(max(center) - min(center))  # 粗略饱和度
        if name == "white" or (v > 230 and s < 40):
            skipped_white += 1            # 白/近白域：白纸上不可见，跳过
            continue
        mask = (labels == ci).astype(np.uint8) * 255
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_NONE)
        subs: List[List[List[int]]] = []
        scored = []
        for c in contours:
            area = abs(float(cv2.contourArea(c)))
            peri = float(cv2.arcLength(c, True))
            if area < MIN_STROKE_AREA and peri < MIN_STROKE_LEN_PX:
                continue
            scored.append((max(peri, area * 0.1), c))
        scored.sort(key=lambda t: -t[0])
        for score, c in scored[:max_strokes_per_color]:
            sp = _contour_to_subpath(_approx(c, max(score, 1.0)))
            if len(sp) >= 2:
                subs.append(sp)
        if subs:
            groups.append({"color": name,
                           "bgr": [float(center[0]), float(center[1]),
                                   float(center[2])],
                           "subpaths": subs})
    groups.sort(key=lambda g: -sum(len(s) for s in g["subpaths"]))
    meta = {"k": k, "quant_size": [sw, sh], "skipped_white_domains":
            skipped_white, "groups": len(groups)}
    return groups, meta


# ---- 画布适配与执行切块 ------------------------------------------------------

def fit_subpaths(subpaths: List[List[List[int]]], src_w: int, src_h: int,
                 canvas_rect: Sequence[int], margin: float = 0.06
                 ) -> Tuple[List[List[List[int]]], Dict[str, Any]]:
    """图像像素坐标 → 画布物理坐标：等比 contain + 居中 + 边距。"""
    cl, ct, cw, ch = (int(canvas_rect[0]), int(canvas_rect[1]),
                      int(canvas_rect[2]), int(canvas_rect[3]))
    mx, my = int(cw * margin), int(ch * margin)
    avail_w, avail_h = max(4, cw - 2 * mx), max(4, ch - 2 * my)
    scale = min(avail_w / float(src_w), avail_h / float(src_h))
    off_x = cl + mx + (avail_w - src_w * scale) / 2.0
    off_y = ct + my + (avail_h - src_h * scale) / 2.0
    mapped = [[[int(round(off_x + p[0] * scale)),
                int(round(off_y + p[1] * scale))] for p in seg]
              for seg in subpaths]
    meta = {"scale": round(float(scale), 5),
            "origin": [int(round(off_x)), int(round(off_y))],
            "margin_px": [mx, my]}
    return mapped, meta


def chunk_calls(subpaths: List[List[List[int]]],
                max_segments: int = 48, max_points: int = 1800,
                ms_per_point: float = 3.5, max_duration_ms: int = 9000
                ) -> List[Dict[str, Any]]:
    """把段列表切块成 drag 调用计划（每块 ≤ 段数/点数/时长上限）。

    controller 单次 drag 上限：200 段 / 8000 点 / duration_ms≤10000；
    这里更保守（48 段 / 1800 点 / ≤9s），给门禁和抓屏留足余量。
    duration_ms 按点数给节奏（≥400ms 起步）。
    """
    calls: List[Dict[str, Any]] = []
    cur: List[List[List[int]]] = []
    cur_pts = 0

    def _flush() -> None:
        nonlocal cur, cur_pts
        if not cur:
            return
        pts = sum(len(s) for s in cur)
        duration = int(min(max_duration_ms,
                           max(400, round(pts * ms_per_point))))
        calls.append({"segments": cur, "points": pts,
                      "duration_ms": duration})
        cur, cur_pts = [], 0

    for seg in subpaths:
        if cur and (len(cur) >= max_segments
                    or cur_pts + len(seg) > max_points):
            _flush()
        cur.append(seg)
        cur_pts += len(seg)
    _flush()
    return calls
