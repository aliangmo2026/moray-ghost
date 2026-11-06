"""画布感知与安全预演（v0.16 工业级物理操控引擎）。

两件事：
1. canvas_info：结合窗口位置 + 截图 CV，定位"画纸"的物理边界——
   在窗口截图里找最大的高亮（近白）连通区作为画布（画图/Paint 类
   白纸应用命中率高）；找不到时如实降级为窗口内缩框（source=fallback），
   绝不编造。
2. check_points（dry_run 引擎）：给定轨迹点做一次"如果真画会怎样"的
   预演——越出显示器 / 撞 Fail-Safe 角落 / 越出窗口 / 越出画布 /
   压到工具栏带，全部给出结构化警告，鼠标一个像素都不动。

只读感知模块：绝不产生任何输入，绝不依赖 controller。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
import win32gui

from . import screen_mss

LOG = logging.getLogger("moray-ghost.canvas")

FAIL_SAFE_GUARD_PX = 2      # 与 controller.CORNER_GUARD_PX 同源
WHITE_THRESHOLD = 235       # 灰度 ≥ 此值视作"纸"
MIN_CANVAS_AREA_FRAC = 0.08  # 画布至少占窗口面积比例
TOP_SKIP_FRAC = 0.10        # 顶部工具栏跳过带（检测起点）


def _failsafe_points() -> List[Tuple[int, int]]:
    from . import backend
    pts = getattr(backend.pyautogui, "FAILSAFE_POINTS", [(0, 0)])
    return [(int(p[0]), int(p[1])) for p in pts]


def _in_corner(pt: Sequence[float], tol: int = FAIL_SAFE_GUARD_PX) -> bool:
    x, y = float(pt[0]), float(pt[1])
    return any(abs(x - fx) <= tol and abs(y - fy) <= tol
               for fx, fy in _failsafe_points())


def detect_canvas_bgr(img_bgr: "np.ndarray", window_rect: Tuple[int, int, int, int],
                      white_threshold: int = WHITE_THRESHOLD,
                      min_area_frac: float = MIN_CANVAS_AREA_FRAC
                      ) -> Optional[Dict[str, Any]]:
    """在窗口截图里找"画纸"：最大的高亮连通区的外接矩形。

    img_bgr：全屏 BGR ndarray（screen_mss.grab 转 numpy 后）；
    window_rect：(l,t,r,b) 物理像素。返回
    {"rect": [l,t,w,h]（物理像素）, "area_frac": 占窗口比例} 或 None。
    """
    if img_bgr is None:
        return None
    if not isinstance(img_bgr, np.ndarray):
        img_bgr = cv2.cvtColor(
            np.asarray(img_bgr.convert("RGB")), cv2.COLOR_RGB2BGR)
    l, t, r, b = window_rect
    l, t = max(0, int(l)), max(0, int(t))
    r = min(img_bgr.shape[1], int(r))
    b = min(img_bgr.shape[0], int(b))
    if r - l < 120 or b - t < 100:
        return None
    crop = img_bgr[t:b, l:r]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(gray, white_threshold, 255, cv2.THRESH_BINARY)
    # 顶栏（工具带）先跳过：白纸不会出现在窗口最顶上
    y0 = int(crop.shape[0] * TOP_SKIP_FRAC)
    mask[:y0, :] = 0
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    win_area = float((r - l) * (b - t))
    best = None
    for c in contours:
        x, y, cw, ch = cv2.boundingRect(c)
        area = float(cw * ch)
        if area < min_area_frac * win_area:
            continue
        if best is None or area > best[2] * best[3]:
            best = (x, y, cw, ch)
    if best is None:
        return None
    x, y, cw, ch = best
    return {"rect": [l + x, t + y, cw, ch],
            "area_frac": round(float(cw * ch) / max(1.0, win_area), 4)}


def _fallback_canvas(window_rect: Tuple[int, int, int, int]) -> List[int]:
    """探测失败的内缩兜底框：顶 14%（工具栏）+ 底 8% + 左右各 3%。"""
    l, t, r, b = window_rect
    w, h = r - l, b - t
    return [int(l + w * 0.03), int(t + h * 0.14),
            int(w * 0.94), int(h * 0.78)]


def detect_window_canvas(window_rect: Tuple[int, int, int, int],
                         monitor: int = 1) -> Dict[str, Any]:
    """窗口画布探测总入口：抓屏（可复用）→ CV 定位；失败如实降级并标注 source。

    返回 {"canvas_rect": [l,t,w,h], "source": "detected"|"fallback",
          "area_frac": float|None, "toolbar_band": [l,t,w,h]|None,
          "img": 本次抓屏（PIL Image，供同一请求内的取色复用）}。
    调用方对 JSON 响应前应 pop 掉 img（不可序列化）。
    """
    detected: Optional[Dict[str, Any]] = None
    img = None
    try:
        img, _origin = screen_mss.grab(monitor)
        detected = detect_canvas_bgr(img, window_rect)
    except Exception as exc:  # noqa: BLE001  抓屏失败 → 兜底框（如实标注）
        LOG.warning("canvas detection grab failed: %s", exc)
    if detected is not None:
        cl, ct, cw, ch = detected["rect"]
        l, t = window_rect[0], window_rect[1]
        toolbar = [l, t, window_rect[2] - l, max(0, ct - t)]
        return {"canvas_rect": [cl, ct, cw, ch],
                "source": "detected",
                "area_frac": detected.get("area_frac"),
                "toolbar_band": toolbar if toolbar[3] > 2 else None,
                "img": img}
    fb = _fallback_canvas(window_rect)
    return {"canvas_rect": fb, "source": "fallback", "area_frac": None,
            "toolbar_band": None, "img": img}


def canvas_info(window_rect: Tuple[int, int, int, int], monitor: int = 1
                ) -> Dict[str, Any]:
    """detect_window_canvas 的 JSON 安全版（剥离 img）。"""
    info = detect_window_canvas(window_rect, monitor)
    info.pop("img", None)
    return info


def _monitor_rect(monitor: int) -> Optional[Dict[str, int]]:
    try:
        mons = screen_mss.monitors()
        if monitor < 1 or monitor > len(mons) - 1:
            return None
        return screen_mss.monitor_rect(monitor)
    except Exception:  # noqa: BLE001
        return None


def check_points(points: Sequence[Sequence[float]],
                 monitor: int = 1,
                 window_rect: Optional[Tuple[int, int, int, int]] = None,
                 canvas: Optional[Dict[str, Any]] = None,
                 warn_outside_window: bool = True
                 ) -> List[Dict[str, Any]]:
    """dry_run 预演：给定点列 → 结构化警告列表（不产生任何输入）。

    矩形格式约定（与生产者一致，混用必错）：
      monitor/内部比较用 mss 的 {left,top,width,height}；
      window_rect = win32 (l,t,r,b)；canvas["canvas_rect"] = [l,t,w,h]。

    警告 code：
      OUT_OF_MONITOR   越出显示器（真画必被 controller 400 拒绝）
      FAIL_SAFE_CORNER 距 Fail-Safe 角落 ≤2px（真画必被 400 拒绝）
      OUT_OF_WINDOW    越出目标窗口矩形（可能画到别的应用上）
      OUT_OF_CANVAS    越出画布矩形（画到工具栏/边框上）
      TOOLBAR_OVERLAP  落进画布上方的工具栏条带
    """
    warnings: List[Dict[str, Any]] = []

    def _add(code: str, samples: List[List[int]]) -> None:
        if not samples:
            return
        warnings.append({
            "code": code, "count": len(samples),
            "sample": samples[0],
            "message": _MESSAGES[code].format(count=len(samples),
                                              sample=samples[0])})

    mon = _monitor_rect(monitor)
    out_mon: List[List[int]] = []
    corners: List[List[int]] = []
    out_win: List[List[int]] = []
    out_canvas: List[List[int]] = []
    toolbar_hits: List[List[int]] = []
    for p in points[:20000]:
        x, y = int(round(float(p[0]))), int(round(float(p[1])))
        if mon is not None:
            inside = (mon["left"] <= x < mon["left"] + mon["width"]
                      and mon["top"] <= y < mon["top"] + mon["height"])
            if not inside:
                out_mon.append([x, y])
                continue
        if _in_corner((x, y)):
            corners.append([x, y])
        if warn_outside_window and window_rect is not None:
            l, t, r, b = window_rect
            if not (l <= x < r and t <= y < b):
                out_win.append([x, y])
        if canvas is not None:
            cl, ct, cw, ch = canvas["canvas_rect"]
            if not (cl <= x < cl + cw and ct <= y < ct + ch):
                out_canvas.append([x, y])
            band = canvas.get("toolbar_band")
            if band is not None:
                bl, bt, bw, bh = band
                if bh > 0 and bl <= x < bl + bw and bt <= y < bt + bh:
                    toolbar_hits.append([x, y])
    _add("OUT_OF_MONITOR", out_mon)
    _add("FAIL_SAFE_CORNER", corners)
    _add("OUT_OF_WINDOW", out_win)
    _add("OUT_OF_CANVAS", out_canvas)
    _add("TOOLBAR_OVERLAP", toolbar_hits)
    return warnings


_MESSAGES = {
    "OUT_OF_MONITOR": "{count} point(s) outside monitor {sample} — a real "
                      "stroke would be rejected (400 SAFETY_BLOCKED)",
    "FAIL_SAFE_CORNER": "{count} point(s) within Fail-Safe corner guard of "
                        "{sample} — a real stroke would be rejected",
    "OUT_OF_WINDOW": "{count} point(s) outside target window, e.g. {sample} "
                     "— stroke would land on another application",
    "OUT_OF_CANVAS": "{count} point(s) outside the canvas rect, e.g. "
                     "{sample} — stroke would paint on chrome/toolbars",
    "TOOLBAR_OVERLAP": "{count} point(s) inside the toolbar band, e.g. "
                       "{sample} — stroke would click toolbar controls",
}


def window_rect_of(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    """窗口物理矩形 (l,t,r,b)；窗口消失返回 None。"""
    try:
        return win32gui.GetWindowRect(int(hwnd))
    except Exception:  # noqa: BLE001
        return None
