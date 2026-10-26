"""v0.9 混合图标定位单测（无模型/GUI）：CV 粗筛 + 编号网格 + 开关矩阵。

跑法：.venv\\Scripts\\python.exe scripts\\test_v09_hybrid_icons.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw  # noqa: E402

from app.core import config, vl_driver  # noqa: E402
from app.core.icon_grid import (  # noqa: E402
    build_contact_sheet, detect_icon_candidates)

RESULTS: list = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  | {detail}" if detail else ""), flush=True)
    return ok


def make_icons() -> Image.Image:
    img = Image.new("RGB", (640, 480), (30, 30, 32))
    d = ImageDraw.Draw(img)
    # 直角矩形（目标）
    d.rectangle([100, 100, 142, 142], outline=(255, 255, 255), width=3)
    # 椭圆
    d.ellipse([200, 100, 242, 142], outline=(255, 255, 255), width=3)
    # 三角形
    d.polygon([(300, 142), (321, 100), (342, 142)],
              outline=(255, 255, 255), width=3)
    # 十字/加号
    d.line([400, 100, 400, 142], fill=(255, 255, 255), width=3)
    d.line([379, 121, 421, 121], fill=(255, 255, 255), width=3)
    return img


def main() -> int:
    img = make_icons()
    cands = detect_icon_candidates(img, max_candidates=24)
    check("CV 至少检出 3 个小图标", len(cands) >= 3, f"n={len(cands)}")
    truth = None
    for c in cands:
        x, y, w, h = c["bbox"]
        if x <= 121 < x + w and y <= 121 < y + h:
            truth = c
            break
    check("CV 检出包含矩形中心的候选", truth is not None,
          f"truth={truth and truth['bbox']}")

    sheet, used = build_contact_sheet(img, cands, tile_w=80, tile_h=88,
                                      cols=6)
    check("编号网格生成（尺寸/数量一致）",
          sheet.width > 0 and sheet.height > 0 and len(used) == len(cands),
          f"sheet={sheet.size} used={len(used)}")
    if truth is not None:
        idx = [c["bbox"] for c in used].index(truth["bbox"]) + 1
        check("真值候选在网格中有唯一编号",
              1 <= idx <= len(used), f"index={idx}")

    empty = Image.new("RGB", (320, 240), (30, 30, 32))
    check("纯色图无图标候选（不误报）",
          detect_icon_candidates(empty, max_candidates=8) == [])

    old_mode, old_model = config.VL_HYBRID, config.VL_MODEL_PATH
    try:
        config.VL_HYBRID = "auto"
        config.VL_MODEL_PATH = r"E:\models\Qwen2-VL-2B-Instruct-Q4_K_M.gguf"
        two_b = vl_driver.hybrid_enabled()
        config.VL_MODEL_PATH = r"E:\models\Qwen2-VL-7B-Instruct-Q4_K_M.gguf"
        seven_b = vl_driver.hybrid_enabled()
        config.VL_HYBRID = "0"
        off = vl_driver.hybrid_enabled()
        config.VL_HYBRID = "1"
        forced = vl_driver.hybrid_enabled()
        check("hybrid 开关矩阵（auto/2b=false, auto/7b=true, 0=false, 1=true）",
              two_b is False and seven_b is True and off is False
              and forced is True,
              f"2b={two_b} 7b={seven_b} off={off} forced={forced}")
    finally:
        config.VL_HYBRID, config.VL_MODEL_PATH = old_mode, old_model

    passed = sum(1 for x in RESULTS if x)
    print(f"\n== RESULT: {passed}/{len(RESULTS)} passed ==", flush=True)
    return len(RESULTS) - passed


if __name__ == "__main__":
    sys.exit(main())
