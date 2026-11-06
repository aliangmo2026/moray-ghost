"""笔宽调整（v0.16 工业级物理操控引擎）。

brush_width 的语义契约：Ghost 替大模型把"目标画图软件的笔刷粗细"
调到位——策略按目标进程分派，所有点击/按键都走 controller.run_action
全门禁（能力令牌 / 黑名单 / 归属校验），绝不绕道。

内置策略：
  mspaint.exe  OCR 定位工具栏"大小/Size"控件 → 点击展开下拉 → OCR
               读出各档位（如 1/3/5/8 px）→ 点击最接近请求值的档位。
               Win11 画图没有笔宽快捷键，这是唯一可靠的 GUI 通道；
               每一步的 OCR 证据都记进返回值，失败时如实上抛。

诚实降级：任何一步失败 → BrushError（路由层按 brush_required 决定
409 拒绝还是降级告警继续画），绝不假装"调好了"。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from . import controller, safety_gate, screen_mss, vl_driver

LOG = logging.getLogger("moray-ghost.brush")

MAX_BRUSH_WIDTH = 100
MIN_BRUSH_WIDTH = 1

_SIZE_LABELS = ("大小", "size", "尺寸")
_PX_ITEM_RE = re.compile(r"^\s*(\d{1,3})\s*(?:px|像素|pixel)?\s*$", re.I)


class BrushError(Exception):
    status = 409
    error = "BRUSH_ADJUST_FAILED"

    def __init__(self, detail: str, **data: Any):
        super().__init__(detail)
        self.detail = detail
        self.data = data


def _ocr_items(img, origin: Dict[str, int],
               rect: Tuple[int, int, int, int]) -> List[Dict[str, Any]]:
    """对窗口矩形区域 OCR → [{text, bbox(物理屏幕), center, score}]。"""
    ox, oy = int(origin["left"]), int(origin["top"])
    l, t, r, b = rect
    left = max(0, l - ox)
    top = max(0, t - oy)
    right = min(img.width, r - ox)
    bottom = min(img.height, b - oy)
    if right - left < 8 or bottom - top < 8:
        return []
    crop = img.crop((left, top, right, bottom))
    items = vl_driver._ocr_read(crop)
    out: List[Dict[str, Any]] = []
    for it in items:
        bx, by, bw, bh = it["bbox"]
        out.append({"text": it["text"], "score": it.get("score", 0.0),
                    "bbox": [bx + left + ox, by + top + oy, bw, bh],
                    "center": [bx + left + ox + bw // 2,
                               by + top + oy + bh // 2]})
    return out


def _find_size_control(items: List[Dict[str, Any]],
                       rect: Tuple[int, int, int, int]
                       ) -> Optional[Dict[str, Any]]:
    """在 OCR 结果里定位"大小/Size"控件：文本前缀命中 + 位于窗口上带。"""
    l, t, r, b = rect
    win_w, win_h = max(1, r - l), max(1, b - t)
    for it in items:
        text = str(it.get("text", "")).strip()
        low = text.lower()
        if not any(low == lab or low.startswith(lab)
                   for lab in _SIZE_LABELS):
            continue
        cx, cy = it["center"]
        # 必须在工具栏带（上部 45%）且不是超宽文本（避免把整行说明当控件）
        if cy > t + win_h * 0.45:
            continue
        if it["bbox"][2] > win_w * 0.5:
            continue
        return it
    return None


def _nearest_item(items: List[Dict[str, Any]], width: int
                  ) -> Optional[Tuple[Dict[str, Any], int]]:
    """下拉档位 OCR → 最近 px 档位（同距取小）。"""
    best: Optional[Tuple[Dict[str, Any], int]] = None
    for it in items:
        m = _PX_ITEM_RE.match(str(it.get("text", "")))
        if not m:
            continue
        val = int(m.group(1))
        if val < 1 or val > 999:
            continue
        if best is None or abs(val - width) < abs(best[1] - width) \
                or (abs(val - width) == abs(best[1] - width) and val < best[1]):
            best = (it, val)
    return best


def set_brush_width(width: int, hwnd: int,
                    caps: Dict[str, Any], monitor: int = 1
                    ) -> Dict[str, Any]:
    """把目标画图软件笔宽调到最接近 width 的档位。返回调整证据。

    mspaint.exe 按界面代际分派：
      1) 经典版（Win10/旧 Win11，工具栏有「大小/Size」文字下拉）：OCR 定位
         → 点最近 px 档，exact=True。
      2) 2025 图标栏版：工具栏零文字、笔宽=左缘无读数竖滑条（数值跨启动
         持久化、轨道无刻度）——**没有可靠途径把"像素宽度"落到滑条位置**，
         如实拒绝（reason=mspaint_2025_no_numeric_size_control）；
         brush_required=false 时调用方可以带着当前笔宽继续画（降级告警），
         绝不假装"调好了"。
    抛 BrushError = 调整失败（reason 写明卡在哪一步、看到了什么）。
    """
    width = int(width)
    if not (MIN_BRUSH_WIDTH <= width <= MAX_BRUSH_WIDTH):
        raise BrushError(
            "brush_width must be within "
            + str(MIN_BRUSH_WIDTH) + ".." + str(MAX_BRUSH_WIDTH)
            + ", got " + str(width))
    info = safety_gate.window_info(int(hwnd))
    proc = str((info or {}).get("process") or "").lower()
    if proc == "mspaint.exe":
        return _mspaint_size(width, int(hwnd), caps, monitor, info or {})
    raise BrushError(
        "no brush_width strategy for process " + repr(proc)
        + " (built-in: mspaint.exe); draw without brush_width or add a "
        "strategy", process=proc)


def _mspaint_size(width: int, hwnd: int, caps: Dict[str, Any],
                  monitor: int, info: Dict[str, Any]) -> Dict[str, Any]:
    import win32gui
    steps: List[Dict[str, Any]] = []
    try:
        rect = win32gui.GetWindowRect(hwnd)
    except Exception as exc:  # noqa: BLE001
        raise BrushError("target window vanished: " + type(exc).__name__) from exc

    def _grab():
        return screen_mss.grab(monitor)

    # 1) OCR 工具栏找"大小/Size"控件
    try:
        img, origin = _grab()
        items = _ocr_items(img, origin, rect)
        control = _find_size_control(items, rect)
    except vl_driver.VLBackendUnavailable as exc:
        raise BrushError("OCR engine unavailable: " + str(exc.detail),
                         reason="ocr_unavailable") from exc
    except Exception as exc:  # noqa: BLE001
        raise BrushError("size control search failed: "
                         + type(exc).__name__ + ": " + str(exc)) from exc
    if control is None:
        texts = [it["text"] for it in items[:24]]
        raise BrushError(
            "no 'Size/大小' control found in mspaint toolbar (OCR saw: "
            + repr(texts) + "); 2025 icon-toolbar builds keep brush width "
            "in an unlabeled left-edge slider with no numeric readout, so "
            "exact px targeting is not supported there",
            reason="mspaint_2025_no_numeric_size_control"
            if any("画笔" in t or "工具" in t for t in texts)
            else "size_control_not_found",
            ocr_texts=texts)
    steps.append({"step": "size_control_found", "text": control["text"],
                  "center": control["center"]})

    # 2) 点击展开下拉（控件在主窗口内 → 带 hwnd 走归属门禁）
    try:
        controller.run_action({
            "action": "click", "x": int(control["center"][0]),
            "y": int(control["center"][1]), "hwnd": hwnd,
            "monitor": monitor, "settle_seconds": 0.5,
            "capabilities": caps, "include_after_b64": False})
    except controller.ActionError as exc:
        raise BrushError("clicking Size control refused: " + exc.error
                         + ": " + exc.detail, reason="control_click_denied",
                         steps=steps) from exc
    steps.append({"step": "dropdown_opened"})

    # 3) OCR 读档位（下拉可能溢出窗口底边 → 抓窗口 + 底部扩展 260px）
    import win32gui as _w
    l, t, r, b = rect
    try:
        mon = screen_mss.monitor_rect(monitor)
        bottom = min(b + 260, (mon or {}).get("top", 0)
                     + (mon or {}).get("height", b))
        img2, origin2 = _grab()
        items2 = _ocr_items(img2, origin2, (l, t, r, int(bottom)))
    except Exception as exc:  # noqa: BLE001
        _esc(caps)
        raise BrushError("size dropdown OCR failed: "
                         + type(exc).__name__ + ": " + str(exc),
                         reason="dropdown_ocr_failed", steps=steps) from exc
    pick = _nearest_item(items2, width)
    if pick is None:
        _esc(caps)
        texts = [it["text"] for it in items2[:24]]
        raise BrushError(
            "no px size options found in dropdown (OCR saw: " + repr(texts)
            + ")", reason="size_items_not_found", ocr_texts=texts,
            steps=steps)
    item, val = pick
    steps.append({"step": "size_item_found", "text": item["text"],
                  "value_px": val, "center": item["center"]})

    # 4) 点档位。下拉是 mspaint 的弹层（root ≠ 主 hwnd）：不带 hwnd 点击，
    #    锁内按落点根窗口（弹层进程仍是 mspaint.exe）过黑名单+白名单。
    try:
        controller.run_action({
            "action": "click", "x": int(item["center"][0]),
            "y": int(item["center"][1]), "monitor": monitor,
            "settle_seconds": 0.3, "capabilities": caps,
            "include_after_b64": False})
    except controller.ActionError as exc:
        _esc(caps)
        raise BrushError("clicking size item refused: " + exc.error + ": "
                         + exc.detail, reason="item_click_denied",
                         steps=steps) from exc
    return {"requested": width, "adjusted": True, "selected_px": val,
            "method": "mspaint_size_ocr", "process": "mspaint.exe",
            "exact": val == width, "steps": steps}


def _esc(caps: Dict[str, Any]) -> None:
    """兜底收尾：关掉可能悬着的下拉（esc 低危，仍走 run_action 门禁）。"""
    try:
        controller.run_action({"action": "hotkey", "text": "esc",
                               "settle_seconds": 0.1, "capabilities": caps,
                               "include_after_b64": False})
    except Exception as exc:  # noqa: BLE001
        LOG.warning("brush esc cleanup failed: %s", exc)
