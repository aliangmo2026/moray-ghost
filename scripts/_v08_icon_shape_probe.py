"""收窄到 Paint 形状组（21 图标）的编号网格实验。

真值矩形在形状组内约为第 4 个（行优先）。如果 2B 对 21 格网格能选 #4，
说明“先 CV 找密集图标簇，再让 VL 选号”的路线可行。
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from app.core.icon_grid import (  # noqa: E402
    build_contact_sheet, detect_icon_candidates)

MODEL = ROOT / "models" / "Qwen2-VL-2B-Instruct-Q4_K_M.gguf"
MMPROJ = ROOT / "models" / "mmproj-Qwen2-VL-2B-Instruct-f16.gguf"
SHOT = ROOT / "data" / "screenshots" / "_paint_window.png"
REGION = (655, 95, 935, 245)
TRUTH_CENTER = (788 - REGION[0], 137 - REGION[1])

HEAD = ("You are a GUI icon classifier. The image is a numbered grid of "
        "shape tool icons. Each tile has an orange header with its number. "
        "Which numbered tile shows the ")
TAIL = ('? Reply ONLY JSON: {"index": N, "confidence": 0.0-1.0} or '
        '{"found": false}. Do not guess.')


def ask(llm, sheet: Image.Image, target: str) -> dict:
    buf = io.BytesIO()
    sheet.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    t0 = time.perf_counter()
    resp = llm.create_chat_completion(
        messages=[{"role": "system", "content": HEAD + target + TAIL},
                  {"role": "user", "content": [
                      {"type": "image_url", "image_url": {
                          "url": "data:image/png;base64," + b64}},
                      {"type": "text",
                       "text": "Which number is " + target + "?"}]}],
        temperature=0.0, max_tokens=64,
        response_format={"type": "json_object"})
    el = time.perf_counter() - t0
    text = resp["choices"][0]["message"]["content"] or ""
    print("\n== target=%r %.2fs ==\n%s" % (target, el, text[:300]),
          flush=True)
    try:
        return json.loads(text)
    except ValueError:
        return {}


def main() -> int:
    from llama_cpp import Llama
    from llama_cpp.llama_chat_format import MTMDChatHandler

    full = Image.open(SHOT).convert("RGB")
    crop = full.crop(REGION)
    cands = detect_icon_candidates(crop, max_candidates=36)
    # 行优先：先按 y 分带，再按 x
    cands.sort(key=lambda c: (round(c["bbox"][1] / 24), c["bbox"][0]))
    print("candidates:", len(cands), flush=True)
    truth = None
    for i, c in enumerate(cands, 1):
        x, y, w, h = c["bbox"]
        print("  #%02d bbox=%s" % (i, c["bbox"]), flush=True)
        if x <= TRUTH_CENTER[0] < x + w and y <= TRUTH_CENTER[1] < y + h:
            truth = i
    print("TRUTH index:", truth, "truth center:", TRUTH_CENTER, flush=True)
    sheet, used = build_contact_sheet(crop, cands, tile_w=80, tile_h=88,
                                      cols=7)
    sheet.save(ROOT / "data" / "screenshots" / "_icon_shape_sheet.png")
    print("sheet:", sheet.size, "used:", len(used), flush=True)

    handler = MTMDChatHandler(clip_model_path=str(MMPROJ),
                              verbose=False, use_gpu=False)
    llm = Llama(model_path=str(MODEL), chat_handler=handler, n_ctx=4096,
                n_threads=max(4, (os.cpu_count() or 8) // 2), n_batch=256,
                n_gpu_layers=0, verbose=False)
    for target in ("直角矩形", "sharp rectangle", "矩形（不要圆角）"):
        ans = ask(llm, sheet, target)
        print("  ->", ans, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
