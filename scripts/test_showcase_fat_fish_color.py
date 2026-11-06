"""v0.17 开源彩色首图：线稿 + K-Means 分区 + 油漆桶分层填充（失败自动撤销/密集笔刷兜底）。

流程（全部真实物理鼠标，自建 mspaint + PID/hwnd 差集 + TOPMOST/前台防遮挡）：
  1. 线稿：`sketch_from_image` line 模式（line_epsilon_scale=0.0025，120 笔）。
  2. 分区：从画布线稿截图找闭合白色区域（连通域 + 距离变换取内部点），
     把每个区域反投影回原图，用 K-Means 主导色决定目标 Paint 调色板颜色。
  3. 填充：选油漆桶 + 1200 宽完整色板 → 逐色逐区点击填充；
     每次填充后局部截图校验：漏色/没填/颜色不对 → Ctrl+Z（失败再点工具栏撤销）；
     桶填失败的区在最后用画笔做密集扫描线填充（涂色书兜底）。
  4. 终稿：再跑一次线稿压盖，保证黑色描边干净；画布局部录屏合成
     `docs/assets/fat_fish_drawing_color.gif`（<3MB），最终帧 `docs/assets/fat_fish_final.png`。

跑法：
  .venv\\Scripts\\python.exe scripts\\test_showcase_fat_fish_color.py
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
except OSError:
    pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402
import win32process  # noqa: E402
import psutil  # noqa: E402
from PIL import Image  # noqa: E402

import test_showcase_fat_fish as base  # noqa: E402
from app.core import sketch, trajectory  # noqa: E402

WINDOW_POS = (400, 40)
WINDOW_SIZE = (1600, 1300)
COLOR_GIF = base.ASSETS / "fat_fish_drawing_color.gif"
COLOR_FINAL = base.ASSETS / "fat_fish_final.png"
LINE_FINAL = base.ASSETS / "fat_fish_final_line.png"
PREVIEW = base.ROOT / "data" / "_color_plan_preview.png"

# 1600x1050 Paint 窗口内完整 20 色色板的相对坐标（行1 y=137 / 行2 y=173）。
PALETTE_REL: Dict[str, Tuple[int, int]] = {
    "#000000": (838, 137),
    "#ED1C24": (946, 137),
    "#FF7F27": (982, 137),
    "#FFF200": (1018, 137),
    "#22B14C": (1054, 137),
    "#00A2E8": (1090, 137),
    "#3F48CC": (1126, 137),
    "#A349A4": (1162, 137),
    "#FFFFFF": (838, 173),
    "#C3C3C3": (874, 173),
    "#B97A57": (910, 173),
    "#FFAEC9": (946, 173),
    "#FFC90E": (982, 173),
    "#EFE4B0": (1018, 173),
    "#B5E61D": (1054, 173),
    "#99D9EA": (1090, 173),
    "#7092BE": (1126, 173),
    "#C8BFE7": (1162, 173),
}
PALETTE_RGB: Dict[str, Tuple[int, int, int]] = {
    k: (int(k[1:3], 16), int(k[3:5], 16), int(k[5:7], 16))
    for k in PALETTE_REL
}
BUCKET_REL = (450, 142)
BRUSH_REL = (590, 137)
UNDO_REL = (465, 60)

MIN_REGION_AREA = 120       # 画布内缩区像素
MAX_FILLS = 70
FACE_IMG_POINT = (232, 320)
EYE_IMG_POINTS = ((184, 305), (266, 305))
RESULTS: List[bool] = []


def say(msg: str) -> None:
    base.log(msg)


def check(ok: bool, name: str, detail: str = "") -> bool:
    RESULTS.append(bool(ok))
    say(("PASS " if ok else "FAIL ") + name
        + ((" | " + detail) if detail else ""))
    return bool(ok)


# ============================================================================
# 颜色规划
# ============================================================================

def palette_for_cluster(rgb: Tuple[int, int, int]) -> Optional[str]:
    b, g, r = int(rgb[2]), int(rgb[1]), int(rgb[0])
    hsv = cv2.cvtColor(np.uint8([[[b, g, r]]]), cv2.COLOR_BGR2HSV)[0, 0]
    h, s, v = int(hsv[0]), int(hsv[1]), int(hsv[2])
    if v < 55:
        return None
    if s < 12 and v > 205:
        return None
    if 95 <= h <= 140 and s >= 30:
        if v < 140:
            return "#3F48CC"   # 深蓝/藏青
        if v < 200:
            return "#7092BE"   # 中蓝
        return "#99D9EA"       # 浅蓝
    if 80 <= h < 95:
        return "#99D9EA"
    if 115 <= h <= 150 and s < 30:
        return "#C8BFE7"       # 淡紫阴影
    if (h < 20 or h > 160) and s >= 25:
        return "#FFAEC9"       # 粉/红
    if 10 <= h < 40 and s >= 20:
        return "#EFE4B0"       # 肤色/奶油
    return None


def image_fit(canvas_rect: List[int], iw: int, ih: int,
              margin: float = 0.05) -> Tuple[float, float, float]:
    cl, ct, cw, ch = (int(v) for v in canvas_rect)
    mx, my = int(cw * margin), int(ch * margin)
    aw, ah = max(4, cw - 2 * mx), max(4, ch - 2 * my)
    scale = min(aw / float(iw), ah / float(ih))
    ox = cl + mx + (aw - iw * scale) / 2.0
    oy = ct + my + (ah - ih * scale) / 2.0
    return scale, ox, oy


def build_label_map(face_path: Path, k: int = 10):
    im = np.array(Image.open(face_path).convert("RGB"))
    mx = im.max(2)
    mn = im.min(2)
    mask = ((mx - mn) > 12) | (mx < 235)
    pix = im[mask].astype(np.float32)
    cv2.setRNGSeed(0)
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 60, 0.5)
    _compact, labels, centers = cv2.kmeans(
        pix, k, None, crit, 6, cv2.KMEANS_PP_CENTERS)
    label_map = np.full(im.shape[:2], -1, np.int32)
    label_map[mask] = labels.ravel().astype(np.int32)
    centers_rgb = [(int(c[0]), int(c[1]), int(c[2])) for c in centers]
    return im, label_map, centers_rgb


def dominant_cluster(ix: int, iy: int, label_map: np.ndarray,
                     centers: List[Tuple[int, int, int]],
                     radius: int = 8) -> Tuple[int, int, int]:
    h, w = label_map.shape
    x0, x1 = max(0, ix - radius), min(w, ix + radius + 1)
    y0, y1 = max(0, iy - radius), min(h, iy + radius + 1)
    win = label_map[y0:y1, x0:x1]
    vals = win[win >= 0]
    if vals.size:
        idx = int(np.bincount(vals).argmax())
        if 0 <= idx < len(centers):
            return centers[idx]
    # 回退：用最近邻中心
    if 0 <= iy < h and 0 <= ix < w:
        pixel = np.array(label_map[iy, ix])
        if pixel >= 0:
            return centers[int(pixel)]
    return centers[0] if centers else (255, 255, 255)


def detect_regions(inner: Image.Image) -> List[Dict[str, Any]]:
    arr = np.array(inner.convert("RGB"))
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    line = (gray < 150).astype(np.uint8) * 255
    line = cv2.dilate(line, np.ones((2, 2), np.uint8), iterations=1)
    inv = 255 - line
    n, labels, stats, _ = cv2.connectedComponentsWithStats(inv, 8)
    h, w = gray.shape
    out: List[Dict[str, Any]] = []
    for i in range(1, n):
        x, y, bw, bh, area = (int(v) for v in stats[i])
        if area < MIN_REGION_AREA:
            continue
        if x <= 1 or y <= 1 or x + bw >= w - 1 or y + bh >= h - 1:
            continue  # 外圈背景 / 贴边
        mask = (labels == i).astype(np.uint8) * 255
        dist = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
        _mn, maxv, _ml, maxloc = cv2.minMaxLoc(dist)
        if maxv < 2.0:
            continue
        out.append({"mask": mask, "area": area,
                    "point": (int(maxloc[0]), int(maxloc[1])),
                    "bbox": (x, y, bw, bh)})
    out.sort(key=lambda r: -int(r["area"]))
    return out


def plan_colors(regions: List[Dict[str, Any]], face: Image.Image,
                label_map: np.ndarray, centers: List[Tuple[int, int, int]],
                inner_rect: List[int], fit: Tuple[float, float, float]
                ) -> List[Dict[str, Any]]:
    scale, ox, oy = fit
    ih, iw = face.shape[:2]
    plan: List[Dict[str, Any]] = []
    for reg in regions[:MAX_FILLS]:
        px, py = reg["point"]
        ix = int(round((inner_rect[0] + px - ox) / scale))
        iy = int(round((inner_rect[1] + py - oy) / scale))
        if not (0 <= ix < iw and 0 <= iy < ih):
            continue
        rgb = dominant_cluster(ix, iy, label_map, centers)
        color = palette_for_cluster(rgb)
        # 语义覆盖：脸部肤色、眼睛亮蓝
        if ((ix - FACE_IMG_POINT[0]) ** 2 + (iy - FACE_IMG_POINT[1]) ** 2) ** 0.5 < 48 \
                and reg["area"] > 1500:
            color = "#EFE4B0"
        for ex, ey in EYE_IMG_POINTS:
            if ((ix - ex) ** 2 + (iy - ey) ** 2) ** 0.5 < 32:
                if color in (None, "#7092BE", "#99D9EA", "#3F48CC"):
                    color = "#00A2E8"
                break
        if color is None:
            continue
        plan.append({"color": color, "point": (px, py), "area": reg["area"],
                     "mask": reg["mask"], "img_point": (ix, iy),
                     "cluster_rgb": rgb})
    return plan


def preview_plan(inner: Image.Image, plan: List[Dict[str, Any]]) -> None:
    arr = np.array(inner.convert("RGB"))
    for item in plan:
        rgb = np.array(PALETTE_RGB[item["color"]], dtype=np.uint8)
        arr[item["mask"] > 0] = rgb
    Image.fromarray(arr).save(PREVIEW)
    by_color: Dict[str, int] = {}
    for item in plan:
        by_color[item["color"]] = by_color.get(item["color"], 0) + 1
    say("color plan: %d regions, colors=%s"
        % (len(plan), json.dumps(by_color, ensure_ascii=False)))
    say("preview -> %s" % PREVIEW)


# ============================================================================
# Paint 操作
# ============================================================================

def click(hwnd: int, caps: Dict[str, Any], x: int, y: int,
          settle: float = 0.2) -> Dict[str, Any]:
    return base.post("/execute_action", {
        "action": "click", "x": int(x), "y": int(y), "hwnd": int(hwnd),
        "capabilities": caps, "settle_seconds": settle,
        "include_after_b64": False, "include_before_b64": False,
    }, timeout=60)


def select_swatch(hwnd: int, caps: Dict[str, Any], wr: List[int],
                  color: str) -> bool:
    rel = PALETTE_REL.get(color.upper())
    if rel is None:
        return False
    r = click(hwnd, caps, wr[0] + rel[0], wr[1] + rel[1], 0.25)
    return r.get("_status") == 200 and r.get("ok") is True


def select_bucket(hwnd: int, caps: Dict[str, Any], wr: List[int]) -> bool:
    r = click(hwnd, caps, wr[0] + BUCKET_REL[0], wr[1] + BUCKET_REL[1], 0.4)
    return r.get("_status") == 200 and r.get("ok") is True


def select_brush(hwnd: int, caps: Dict[str, Any], wr: List[int]) -> bool:
    r = click(hwnd, caps, wr[0] + BRUSH_REL[0], wr[1] + BRUSH_REL[1], 0.35)
    return r.get("_status") == 200 and r.get("ok") is True


def undo_fill(hwnd: int, caps: Dict[str, Any], wr: List[int],
              inner_rect: List[int], before: Image.Image) -> bool:
    def _diff(img: Image.Image) -> int:
        a = np.array(img.convert("RGB")).astype(np.int16)
        b = np.array(before.convert("RGB")).astype(np.int16)
        if a.shape != b.shape:
            return 10 ** 9
        return int((np.abs(a - b).max(2) > 20).sum())

    base.post("/execute_action", {
        "action": "hotkey", "text": "ctrl+z", "hwnd": int(hwnd),
        "capabilities": caps, "settle_seconds": 0.3,
        "include_after_b64": False, "include_before_b64": False,
    }, timeout=60)
    after = base.crop_canvas(*base.get_screenshot(), inner_rect)
    if _diff(after) < 80:
        return True
    # 工具栏撤销按钮兜底
    click(hwnd, caps, wr[0] + UNDO_REL[0], wr[1] + UNDO_REL[1], 0.35)
    after = base.crop_canvas(*base.get_screenshot(), inner_rect)
    return _diff(after) < 80


def fill_region(hwnd: int, caps: Dict[str, Any], inner_rect: List[int],
                item: Dict[str, Any]) -> Tuple[str, str, Image.Image]:
    """返回 (status, detail, before)：kept / leak / partial / wrong / nochange / refused。

    before 是**填充前**的画布内缩图，调用方必须以它为撤销基准（否则撤销后
    与"泄漏后的画面"比较，永远判定撤销失败）。
    """
    before = base.crop_canvas(*base.get_screenshot(), inner_rect)
    px, py = item["point"]
    r = click(hwnd, caps, inner_rect[0] + px, inner_rect[1] + py, 0.18)
    if r.get("_status") != 200 or not r.get("ok"):
        return "refused", "click refused: %s" % r.get("error"), before
    time.sleep(0.15)
    after = base.crop_canvas(*base.get_screenshot(), inner_rect)
    a = np.array(after.convert("RGB")).astype(np.int16)
    b = np.array(before.convert("RGB")).astype(np.int16)
    if a.shape != b.shape:
        return "refused", "canvas size changed", before
    changed = np.abs(a - b).max(2) > 20
    total = int(changed.sum())
    if total < 30:
        return "nochange", "nothing changed", before
    region = item["mask"] > 0
    region_d = cv2.dilate(region.astype(np.uint8),
                          np.ones((5, 5), np.uint8), iterations=1) > 0
    inside = int((changed & region_d).sum())
    outside = int((changed & ~region_d).sum())
    coverage = inside / float(max(1, region.sum()))
    if outside > max(300, int(0.05 * total)):
        return "leak", "outside=%d total=%d" % (outside, total), before
    if coverage < 0.50:
        return "partial", "coverage=%.2f total=%d" % (coverage, total), before
    target = np.array(PALETTE_RGB[item["color"]], dtype=np.int16)
    med = np.median(a[changed], axis=0)
    dist = float(np.linalg.norm(med - target))
    if dist > 95:
        return "wrong", "wrong color med=%s target=%s" % (
            [int(v) for v in med], list(target)), before
    return "kept", "changed=%d coverage=%.2f color_d=%.0f" % (
        total, coverage, dist), before


def scanline_subpaths(mask: np.ndarray, spacing: int = 4,
                      inset: int = 1) -> List[List[List[int]]]:
    m = (mask > 0).astype(np.uint8) * 255
    if inset > 0:
        m = cv2.erode(m, np.ones((3, 3), np.uint8), iterations=inset)
    h, w = m.shape
    subpaths: List[List[List[int]]] = []
    for y in range(0, h, spacing):
        row = m[y]
        xs = np.where(row > 0)[0]
        if xs.size < 4:
            continue
        # 连续 run
        split = np.where(np.diff(xs) > 1)[0]
        starts = np.concatenate([[0], split + 1])
        ends = np.concatenate([split, [xs.size - 1]])
        for s, e in zip(starts, ends):
            x0, x1 = int(xs[s]), int(xs[e])
            if x1 - x0 < 4:
                continue
            subpaths.append([[x0, y], [x1, y]])
    return subpaths


def dense_fill(hwnd: int, caps: Dict[str, Any], inner_rect: List[int],
               item: Dict[str, Any]) -> bool:
    subs = scanline_subpaths(item["mask"], spacing=2, inset=1)
    if not subs:
        return False
    # 先点色板（路由也会点，但先点保证 find_swatch 失败时当前色正确）
    # wr 由调用方在 select_brush 时已设置；这里用固定色板相对坐标。
    # 通过 base.get_screenshot 取窗口 rect 不便，调用方把 wr 放进 item。
    wr = item["wr"]
    select_swatch(hwnd, caps, wr, item["color"])
    screen_subs = [[[inner_rect[0] + p[0], inner_rect[1] + p[1]]
                    for p in seg] for seg in subs]
    for chunk in sketch.chunk_calls(screen_subs, max_segments=48,
                                    max_points=1800, ms_per_point=2.0,
                                    max_duration_ms=6000):
        r = base.post("/draw_trajectory", {
            "subpaths": chunk["segments"], "duration_ms": chunk["duration_ms"],
            "color": item["color"], "hwnd": int(hwnd),
            "capabilities": caps, "is_painting": True,
            "include_after_b64": False, "include_before_b64": False,
        }, timeout=120)
        if r.get("_status") != 200 or not r.get("ok"):
            say("dense fill failed: " + json.dumps(r, ensure_ascii=False)[:300])
            return False
    return True


# ============================================================================
# GIF / 最终图
# ============================================================================

def ink_mask(img: Image.Image) -> np.ndarray:
    a = np.array(img.convert("RGB"))
    return ((a.max(2) - a.min(2)) > 25) | (a.mean(2) < 200)


def build_color_gif(frames: List[Tuple[float, bytes]], bbox: Tuple[int, int, int, int],
                    out_path: Path) -> Dict[str, Any]:
    imgs = []
    for _ts, b in frames:
        im = Image.open(io.BytesIO(b)).convert("RGB")
        imgs.append(im.crop(bbox))
    if not imgs:
        raise RuntimeError("no frames")
    # 去掉开头纯白
    first = 0
    for i, im in enumerate(imgs):
        if int(ink_mask(im).sum()) > 40:
            first = max(0, i - 2)
            break
    imgs = imgs[first:]
    if len(imgs) > 140:
        idx = [round(i * (len(imgs) - 1) / 139) for i in range(140)]
        imgs = [imgs[i] for i in idx]
    attempts = [(640, 64, 140), (560, 48, 120), (480, 32, 100),
                (480, 28, 95), (460, 24, 90), (440, 24, 90),
                (420, 24, 90), (400, 20, 85), (380, 16, 80),
                (360, 16, 80)]
    for width, colors, max_frames in attempts:
        use = list(imgs)
        if len(use) > max_frames:
            idx = [round(i * (len(use) - 1) / (max_frames - 1))
                   for i in range(max_frames)]
            use = [use[i] for i in idx]
        use.append(use[-1].copy())
        durations = [80] * (len(use) - 1) + [1500]

        def _fit(im: Image.Image) -> Image.Image:
            if im.width == width:
                return im
            h = max(1, round(im.height * width / im.width))
            return im.resize((width, h), Image.LANCZOS)

        fitted = [_fit(im) for im in use]
        base_img = fitted[-1].quantize(colors=colors, method=Image.MEDIANCUT)
        pframes = [im.quantize(palette=base_img, dither=Image.NONE)
                   for im in fitted]
        out_path.parent.mkdir(parents=True, exist_ok=True)
        pframes[0].save(out_path, save_all=True, append_images=pframes[1:],
                        duration=durations, loop=0, optimize=True, disposal=2)
        size = out_path.stat().st_size
        say("gif attempt %dx%d/%d colors/%d frames -> %.2f MB"
            % (width, fitted[0].height, colors, len(pframes),
               size / 1048576))
        if size <= 3 * 1024 * 1024:
            return {"width": width, "height": fitted[0].height,
                    "colors": colors, "frames": len(pframes),
                    "bytes": size, "duration_ms": sum(durations)}
    raise RuntimeError("GIF still exceeds 3MB")


# ============================================================================
# main
# ============================================================================

def main() -> int:
    started = time.time()
    say("== fat fish COLOR showcase ==")
    if not check(not base.desktop_locked(), "desktop unlocked"):
        return 2
    if not check(base.port_free(), "port 8030 free"):
        return 2
    existing = base.mspaint_processes()
    if existing:
        say("existing mspaint detected: %s; close it first" % [p.pid for p in existing])
        return 2

    try:
        base.prepare_face()
        base.ORIGINAL_CURSOR = base.cursor_pos()
        if base.ORIGINAL_CURSOR and base.ORIGINAL_CURSOR[0] <= 2 \
                and base.ORIGINAL_CURSOR[1] <= 2:
            say("cursor in Fail-Safe corner; move it first")
            return 2

        log_offset = 0
        try:
            log_offset = (base.ROOT / "logs" / "ghost.log").stat().st_size
        except OSError:
            pass
        base.SERVICE = base.spawn_service()
        if not check(base.wait_healthy(), "Ghost service healthy"):
            return 1
        check(base.wait_ocr_warmed(log_offset), "OCR warmup settled")

        before_pids = {p.pid for p in base.mspaint_processes()}
        before_hwnds = set(base.paint_windows())
        paint = subprocess.Popen(
            ["mspaint.exe"], cwd=str(base.ROOT),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        hwnd = 0
        end = time.time() + 20
        while time.time() < end and not hwnd:
            for h in base.paint_windows():
                if h not in before_hwnds:
                    hwnd = int(h)
                    break
            time.sleep(0.3)
        after_pids = {p.pid for p in base.mspaint_processes()} - before_pids
        if hwnd:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            after_pids.add(int(pid))
        base.TRACKED_PIDS.update(after_pids)
        base.PAINT_HWND = hwnd or None
        if not check(bool(hwnd), "self-built mspaint",
                     "hwnd=%r pids=%s" % (hwnd, sorted(after_pids))):
            return 1

        win32gui.SetWindowPos(
            hwnd, win32con.HWND_TOPMOST, WINDOW_POS[0], WINDOW_POS[1],
            WINDOW_SIZE[0], WINDOW_SIZE[1],
            win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW)
        time.sleep(0.6)
        base.force_foreground(hwnd)
        wr = list(win32gui.GetWindowRect(hwnd))
        say("window rect=%s" % (wr,))
        caps = {"allowed_windows": ["mspaint.exe"]}

        cinfo = base.post("/draw/canvas_info",
                          {"hwnd": hwnd, "capabilities": caps}, timeout=60)
        canvas_rect = (cinfo.get("canvas") or {}).get("canvas_rect")
        if not (cinfo.get("ok") and canvas_rect):
            say("canvas_info failed: " + json.dumps(cinfo, ensure_ascii=False)[:400])
            return 1
        inner_rect = base.inner_canvas_rect(canvas_rect)
        say("canvas=%s inner=%s" % (canvas_rect, inner_rect))

        # 录屏从线稿开始（只录画布内缩区）
        recorder = base.CanvasRecorder(inner_rect, interval=0.14, max_width=1600)
        recorder.start()
        time.sleep(0.3)

        # 线稿：黑 + 画笔
        check(select_swatch(hwnd, caps, wr, "#000000"), "select black")
        check(select_brush(hwnd, caps, wr), "select brush")
        line_payload = {
            "image_path": str(base.FACE), "mode": "line", "hwnd": hwnd,
            "capabilities": caps, "max_strokes": 120,
            "max_total_points": 8000, "margin": 0.05,
            "line_epsilon_scale": 0.0025,
        }
        t0 = time.time()
        line_res = base.post("/draw/sketch_from_image", line_payload, timeout=900)
        say("line art %.1fs ok=%r strokes=%r points=%r"
            % (time.time() - t0, line_res.get("ok"),
               line_res.get("strokes_drawn"), line_res.get("points_drawn")))
        if line_res.get("_status") != 200 or not line_res.get("ok"):
            say("line art failed: " + json.dumps(line_res, ensure_ascii=False)[:500])
            return 1

        # 采集线稿画布 + 颜色规划
        base_img, origin = base.get_screenshot()
        line_inner = base.crop_canvas(base_img, origin, inner_rect)
        line_inner.save("data/_color_line_art.png")
        gray = np.array(line_inner.convert("L"))
        dark = int((gray < 100).sum())
        check(dark > 1500, "line art actually black", "dark_px=%d" % dark)

        face_img, label_map, centers = build_label_map(base.FACE)
        regions = detect_regions(line_inner)
        fit = image_fit(canvas_rect, face_img.shape[1], face_img.shape[0], 0.05)
        plan = plan_colors(regions, face_img, label_map, centers,
                           inner_rect, fit)
        preview_plan(line_inner, plan)
        check(len(plan) >= 5, "color plan has regions", "%d" % len(plan))
        if os.environ.get("FAT_FISH_PREVIEW_ONLY") == "1":
            say("preview-only stop (no fills)")
            return 0

        # 桶填
        check(select_bucket(hwnd, caps, wr), "select bucket")
        kept = 0
        failed: List[Dict[str, Any]] = []
        by_color: Dict[str, List[Dict[str, Any]]] = {}
        for item in plan:
            by_color.setdefault(item["color"], []).append(item)
        for color, items in sorted(by_color.items(),
                                   key=lambda kv: -sum(i["area"] for i in kv[1])):
            if not select_swatch(hwnd, caps, wr, color):
                say("swatch select failed: %s" % color)
                failed.extend(items)
                continue
            say("fill color %s x%d" % (color, len(items)))
            for item in items:
                status, detail, before_fill = fill_region(
                    hwnd, caps, inner_rect, item)
                if status == "kept":
                    kept += 1
                else:
                    ok_undo = True
                    if status in ("leak", "partial", "wrong"):
                        ok_undo = undo_fill(
                            hwnd, caps, wr, inner_rect, before_fill)
                    say("  region %s %s (%s); undo=%r"
                        % (item["img_point"], status, detail, ok_undo))
                    # 桶填失败/漏色/部分填充都交给密集笔刷兜底；
                    # nochange/refused 不按 Ctrl+Z（否则会误撤销上一块成功填充）。
                    failed.append(item)
        check(kept >= 5, "bucket fills kept", "kept=%d failed=%d"
              % (kept, len(failed)))

        # 密集笔刷兜底（涂色书）
        if failed:
            check(select_brush(hwnd, caps, wr), "select brush for fallback")
            for item in failed[:20]:
                item["wr"] = wr
                ok = dense_fill(hwnd, caps, inner_rect, item)
                say("  dense fallback %s area=%d -> %r"
                    % (item["color"], item["area"], ok))
            time.sleep(0.4)

        # 终稿线稿压盖：保证黑描边干净
        check(select_swatch(hwnd, caps, wr, "#000000"), "select black (touch-up)")
        check(select_brush(hwnd, caps, wr), "select brush (touch-up)")
        t0 = time.time()
        touch = base.post("/draw/sketch_from_image", line_payload, timeout=900)
        say("line touch-up %.1fs ok=%r strokes=%r"
            % (time.time() - t0, touch.get("ok"), touch.get("strokes_drawn")))
        time.sleep(1.4)
        recorder.stop()
        say("recorded %d frames (%d errors)"
            % (len(recorder.frames), recorder.errors))

        # 最终画布 + 紧致 bbox
        final_img, origin = base.get_screenshot()
        final_inner = base.crop_canvas(final_img, origin, inner_rect)
        mask = ink_mask(final_inner)
        ys, xs = np.where(mask)
        if xs.size:
            pad = 18
            x0 = max(0, int(xs.min()) - pad)
            y0 = max(0, int(ys.min()) - pad)
            x1 = min(final_inner.width, int(xs.max()) + pad)
            y1 = min(final_inner.height, int(ys.max()) + pad)
            bbox = (x0, y0, x1, y1)
        else:
            bbox = (0, 0, final_inner.width, final_inner.height)
        final_crop = final_inner.crop(bbox)
        final_crop.save(COLOR_FINAL)
        final_crop.save("data/_color_final_inner.png")
        check(int(ink_mask(final_crop).sum()) > 1000, "final color canvas has ink",
              "size=%dx%d bbox=%s" % (final_crop.width, final_crop.height, bbox))

        gif_info = build_color_gif(recorder.frames, bbox, COLOR_GIF)
        check(COLOR_GIF.stat().st_size <= 3 * 1024 * 1024, "color gif <= 3MB",
              "%.2f MB" % (COLOR_GIF.stat().st_size / 1048576))
        say("color gif: %s" % COLOR_GIF)
        say("color final: %s" % COLOR_FINAL)
        say("gif_info: " + json.dumps(gif_info, ensure_ascii=False))
        say("total %.1fs" % (time.time() - started))
        return 0
    finally:
        try:
            base.cleanup_paint()
        except Exception:  # noqa: BLE001
            pass
        if base.ORIGINAL_CURSOR:
            base.move_cursor(base.ORIGINAL_CURSOR[0], base.ORIGINAL_CURSOR[1])
        base.stop_proc(base.SERVICE, base.PORT)
        say("cleanup done; port_free=%r" % base.port_free())


if __name__ == "__main__":
    sys.exit(main())
