"""CV 图标候选检测 + 编号图标网格（混合定位的“粗筛”层）。

设计目标：把全窗视觉搜索压缩成“N 个方形小图标”的多选题。
- 双极性 Otsu（浅色图标/深色图标都覆盖）+ 形态学闭运算合并笔画；
- 过滤尺寸/长宽比/密度，按方形度与尺寸排序，最多 max_candidates 个；
- 生成带橙色编号头的 contact sheet，供 VL 只做“几号是目标”的分类。

坐标约定：detect 返回的 bbox 是输入 PIL 图内的像素坐标（x,y,w,h）。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Tuple

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# 开源标准（v0.7.1 E-1）：源码里不写盘符绝对路径；Windows 目录从环境读取。
_WINDIR = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or ""
_FONT_CANDIDATES = tuple(
    os.path.join(_WINDIR, "Fonts", name)
    for name in ("segoeui.ttf", "arial.ttf")
)


def _font(size: int):
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _components(gray: np.ndarray, polarity: int,
                offset_x: int = 0, offset_y: int = 0) -> List[Dict[str, Any]]:
    """单极性连通域 → 候选框。polarity=1 取亮物（深色主题），-1 取暗物。"""
    g = gray if polarity > 0 else 255 - gray
    try:
        _, th = cv2.threshold(g, 0, 255,
                              cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    except cv2.error:
        return []
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, kernel, iterations=2)
    n, _labels, stats, _cents = cv2.connectedComponentsWithStats(th, 8)
    h_img, w_img = gray.shape[:2]
    out: List[Dict[str, Any]] = []
    for i in range(1, n):
        x, y, w, h, area = [int(v) for v in stats[i]]
        if w < 10 or h < 10 or w > 80 or h > 80:
            continue
        if area < 15:
            continue
        if w > 0.20 * w_img or h > 0.20 * h_img:
            continue
        aspect = w / float(h)
        if aspect < 0.35 or aspect > 2.8:
            continue
        density = area / float(max(1, w * h))
        if density < 0.02 or density > 0.98:
            continue
        out.append({
            "bbox": [x + offset_x, y + offset_y, w, h],
            "area": area,
            "aspect": aspect,
            "density": density,
            "polarity": polarity,
        })
    return out


def _iou(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x1, y1 = max(ax, bx), max(ay, by)
    x2, y2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if x2 <= x1 or y2 <= y1:
        return 0.0
    inter = (x2 - x1) * (y2 - y1)
    union = aw * ah + bw * bh - inter
    return inter / float(max(1, union))


def _rank(c: Dict[str, Any]) -> float:
    """方形、14–56px、密度适中优先；文字碎片的分数通常更低。"""
    w, h = c["bbox"][2], c["bbox"][3]
    side = max(w, h)
    square = 1.0 - min(1.0, abs(c["aspect"] - 1.0))
    if 16 <= side <= 56:
        size = 1.0
    elif side < 16:
        size = 0.5
    else:
        size = max(0.2, 1.0 - (side - 56) / 80.0)
    density = 1.0 if 0.12 <= c["density"] <= 0.75 else 0.4
    return square * size * density


def detect_icon_candidates(img: Image.Image,
                           max_candidates: int = 48) -> List[Dict[str, Any]]:
    """返回按“像图标程度”排序的候选框（已去重、已扩成方形）。"""
    rgb = np.array(img.convert("RGB"))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    raw = _components(gray, 1) + _components(gray, -1)
    raw.sort(key=_rank, reverse=True)
    kept: List[Dict[str, Any]] = []
    for c in raw:
        x, y, w, h = c["bbox"]
        cand = (x, y, w, h)
        if any(_iou(cand, tuple(k["bbox"])) > 0.55 for k in kept):
            continue
        # 扩成正方形并留 4px 边距（图标笔画不贴边）
        side = max(w, h) + 8
        cx, cy = x + w / 2.0, y + h / 2.0
        x1 = int(round(cx - side / 2.0))
        y1 = int(round(cy - side / 2.0))
        x1 = max(0, min(img.width - side, x1))
        y1 = max(0, min(img.height - side, y1))
        c = dict(c)
        c["bbox"] = [x1, y1, int(side), int(side)]
        c["score"] = round(_rank(c), 4)
        kept.append(c)
        if len(kept) >= max_candidates:
            break
    return kept


def build_contact_sheet(img: Image.Image, candidates: List[Dict[str, Any]],
                        tile_w: int = 80, tile_h: int = 88,
                        cols: int = 8, gap: int = 4
                        ) -> Tuple[Image.Image, List[Dict[str, Any]]]:
    """生成带编号的 contact sheet；返回 (sheet, 实际放入的候选列表)。"""
    rows = max(1, (len(candidates) + cols - 1) // cols)
    sheet = Image.new(
        "RGB", (cols * (tile_w + gap) + gap, rows * (tile_h + gap) + gap),
        (28, 28, 30))
    d = ImageDraw.Draw(sheet)
    header = 18
    number_font = _font(13)
    used: List[Dict[str, Any]] = []
    for i, c in enumerate(candidates):
        col, row = i % cols, i // cols
        x0 = gap + col * (tile_w + gap)
        y0 = gap + row * (tile_h + gap)
        bx, by, bw, bh = c["bbox"]
        tile = img.crop((bx, by, bx + bw, by + bh)).resize(
            (tile_w, tile_h - header), Image.LANCZOS)
        d.rectangle([x0, y0, x0 + tile_w - 1, y0 + header - 1],
                    fill=(255, 176, 0))
        d.text((x0 + 4, y0 + 1), str(i + 1), fill=(0, 0, 0),
               font=number_font)
        sheet.paste(tile, (x0, y0 + header))
        d.rectangle([x0, y0, x0 + tile_w - 1, y0 + tile_h - 1],
                    outline=(120, 120, 120), width=1)
        used.append(dict(c, index=i + 1))
    return sheet, used
