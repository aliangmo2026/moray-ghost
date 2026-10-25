"""宏锚点匹配：OpenCV 模板匹配（任务 3，A 方案）。

坐标系铁律（全程物理像素，内部零缩放）：
- 裁剪：在 scale=1.0 的原始截图（app.core.screen_mss.grab 返回的 PIL Image）上裁，
  crop 的像素 = 屏幕物理像素。
- 匹配：拿 scale=1.0 的当前屏截图做 cv2.matchTemplate(TM_CCOEFF_NORMED)。
- 返回：center 是物理像素坐标，可直接喂给 /execute_action（不带 source_width）。
- 禁止用 scaled 图省内存；若将来要做该优化，必须同步记录 template_scale 并在
  匹配前归一化。v0.3 不做这个优化。

语义铁律：
- 调用方传入的 target_point / 锚点中心只用于"在哪裁"，不参与匹配结果计算；
  重放坐标一律来自匹配框中心 = (mx + crop_w//2, my + crop_h//2)。
- find() 只区分 matched / not_found；抓屏失败（capture_failed）由调用方在
  调 find() 之前单独捕获并上报——两者绝不能混为一谈（抓屏失败 ≠ 按钮消失）。
"""
from __future__ import annotations

import base64
import io
import time
from typing import Any, Dict, List, Optional, Tuple, Union

import cv2
import numpy as np
from PIL import Image

from . import window_locator

# 锚点纹理下限：std 低于此值的模板视为不可靠（近平坦模板在 TM_CCOEFF_NORMED 下
# 数值不稳定，实测会给出满分伪匹配且命中位置随噪声漂移 → 点击落点不可预测）。
# 真实 UI 锚点（按钮/文字/图标）std 普遍 ≥ 25。
TEXTURE_MIN = 12.0


def _decode_png_b64(b64: str) -> np.ndarray:
    raw = base64.b64decode(b64)
    arr = np.frombuffer(raw, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("screenshot_crop is not a decodable PNG image")
    return img


def decode_anchor(crop_b64: str):
    """解码并校验 heal 传入的锚点图（公开助手）。

    非法 base64/PNG、零方差或低纹理（std < TEXTURE_MIN）抛 ValueError——
    这类模板会造成命中位置随噪声漂移的危险点击。
    返回 (np数组, std)。
    """
    tpl = _decode_png_b64(crop_b64)
    std = float(np.std(tpl))
    if std < TEXTURE_MIN:
        raise ValueError("template texture too low (std=" + format(std, ".1f")
                         + " < " + str(TEXTURE_MIN) + "); cannot be a macro anchor")
    return tpl, std


def crop_around(img: Image.Image, cx: int, cy: int,
                size: Union[int, List[int], Tuple[int, int]]) -> Tuple[str, List[int]]:
    """以 (cx, cy) 为中心裁 size×size（或 [w,h]），越界自动 clamp，不越界不报错。

    返回 (crop_b64, clamped_center)：clamped_center 是裁剪框实际中心的物理像素
    坐标（贴近屏幕边缘时会偏离请求的 cx/cy）。
    """
    img_w, img_h = img.width, img.height
    if isinstance(size, (list, tuple)):
        w, h = int(size[0]), int(size[1])
    else:
        w = h = int(size)
    w = max(4, min(w, img_w))
    h = max(4, min(h, img_h))
    left = int(cx) - w // 2
    top = int(cy) - h // 2
    left = max(0, min(left, img_w - w))
    top = max(0, min(top, img_h - h))
    crop = img.crop((left, top, left + w, top + h))
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii"), [left + w // 2, top + h // 2]


def find(img: Image.Image, crop_b64: str, threshold: float = 0.80,
         roi: Optional[List[int]] = None,
         hwnd: Optional[int] = None) -> Dict[str, Any]:
    """在整屏（scale=1.0，物理像素）上匹配模板，三态之二（capture_failed 归调用方）。

    坐标铁律：**无论是否传 roi/hwnd，返回的 center 都是【全屏物理像素坐标】**——
    ROI 只限定搜索范围，匹配结果始终加回 ROI 原点 (l, t)。

    - roi:  [l,t,r,b] 屏幕物理像素搜索区；越界部分会收敛到屏幕边界
      （窗口可能部分在屏外；负索引会让 numpy 切片回绕，必须 clamp）。
    - hwnd: 传入时**当次**调 window_locator.get_rect(hwnd) 取 rect 作为 roi——
      每次重拿、绝不缓存（用户会拖窗口，某些应用会自己改大小）。
      hwnd 无效/最小化/零尺寸 → status="window_not_found"（调用方映射 409）。
    - 转换只对搜索区做（先 PIL crop 再转 BGR），ROI 路径的转换开销 ~2ms。

    返回：
      {"status": "matched", "center": [x,y], "score": float, "bbox": [x,y,w,h],
       "roi_used": [l,t,r,b]|None, "find_ms": int}
      {"status": "not_found", "best_score": float, "roi_used": ..., "find_ms": ...}
      {"status": "window_not_found", "reason": str, "find_ms": ...}   （仅 hwnd 无效时）
    """
    t0 = time.perf_counter()
    tpl = _decode_png_b64(crop_b64)
    th, tw = tpl.shape[:2]
    # 退化/低纹理防线：std=0 在 TM_CCOEFF_NORMED 下数学上无定义（伪满分），
    # 低纹理（std < TEXTURE_MIN）命中位置随噪声漂移（实测"score=1.0 瞎点"）。
    # 两类模板都直接判 not_found——宁可降级也不盲点。
    std_t = float(np.std(tpl))
    if std_t < TEXTURE_MIN:
        return {"status": "not_found", "best_score": 0.0,
                "reason": ("flat template" if std_t < 1e-6
                           else "low texture template (std="
                                + format(std_t, ".1f") + ")"),
                "roi_used": None,
                "find_ms": int((time.perf_counter() - t0) * 1000)}

    used_roi: Optional[List[int]] = None
    if hwnd is not None:
        rect = window_locator.get_rect(int(hwnd))
        if rect is None:
            return {"status": "window_not_found",
                    "reason": "hwnd invalid / minimized / zero-size (rect fetched fresh)",
                    "roi_used": None,
                    "find_ms": int((time.perf_counter() - t0) * 1000)}
        used_roi = rect
    elif roi is not None:
        used_roi = [int(v) for v in roi]

    img_w, img_h = img.width, img.height
    ox = oy = 0
    if used_roi is not None:
        left, top, right, bottom = used_roi
        left = max(0, min(int(left), img_w))
        top = max(0, min(int(top), img_h))
        right = max(0, min(int(right), img_w))
        bottom = max(0, min(int(bottom), img_h))
        if right - left < tw or bottom - top < th:
            return {"status": "not_found", "best_score": 0.0,
                    "reason": "roi smaller than template",
                    "roi_used": [left, top, right, bottom],
                    "find_ms": int((time.perf_counter() - t0) * 1000)}
        # 只转换搜索区：先 PIL crop 再转 BGR，ROI 路径转换开销从 ~17ms 降到 ~2ms
        search_pil = img.crop((left, top, right, bottom))
        search = cv2.cvtColor(
            np.ascontiguousarray(np.asarray(search_pil)), cv2.COLOR_RGB2BGR)
        ox, oy = left, top
        used_roi = [left, top, right, bottom]
    else:
        search = cv2.cvtColor(
            np.ascontiguousarray(np.asarray(img.convert("RGB"))), cv2.COLOR_RGB2BGR)

    res = cv2.matchTemplate(search, tpl, cv2.TM_CCOEFF_NORMED)
    # 纯色模板方差为 0 时 TM_CCOEFF_NORMED 会产生 NaN，统一压成 0（视为完全不像）
    res = np.nan_to_num(res, nan=0.0, posinf=0.0, neginf=0.0)
    _minv, maxv, _minloc, maxloc = cv2.minMaxLoc(res)
    score = float(maxv)
    find_ms = int((time.perf_counter() - t0) * 1000)
    if score >= threshold:
        cx = maxloc[0] + tw // 2 + ox
        cy = maxloc[1] + th // 2 + oy
        return {"status": "matched", "center": [int(cx), int(cy)],
                "score": score,
                "bbox": [int(maxloc[0] + ox), int(maxloc[1] + oy), int(tw), int(th)],
                "roi_used": used_roi, "find_ms": find_ms}
    return {"status": "not_found", "best_score": score,
            "roi_used": used_roi, "find_ms": find_ms}
