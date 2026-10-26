"""在真实 Paint 截图上验证“CV 粗筛 + 编号网格 + VL 选号”能否命中矩形图标。

真值矩形图标中心约 (788,137)（全窗物理像素）。输出候选总数、真值候选编号、
VL 对「矩形 / Rectangle / 打印机(负例)」的选号。
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
TRUTH = (788, 137)

SYSTEM_HEAD = (
    "You are a GUI icon classifier. The image is a numbered grid of UI "
    "icon candidates. Each tile has an orange header with its number. "
    "Which numbered tile shows the ")
SYSTEM_TAIL = (
    " tool/icon? Reply ONLY JSON: "
    '{"index": N, "confidence": 0.0-1.0} or {"found": false}. '
    "Do not guess; if no tile clearly shows it, return found=false.")


def ask(llm, sheet: Image.Image, target: str) -> dict:
    buf = io.BytesIO()
    sheet.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    t0 = time.perf_counter()
    resp = llm.create_chat_completion(
        messages=[{"role": "system",
                   "content": SYSTEM_HEAD + target + SYSTEM_TAIL},
                  {"role": "user", "content": [
                      {"type": "image_url", "image_url": {
                          "url": "data:image/png;base64," + b64}},
                      {"type": "text",
                       "text": "Which number is " + target + "?"}]}],
        temperature=0.0, max_tokens=64,
        response_format={"type": "json_object"})
    el = time.perf_counter() - t0
    text = resp["choices"][0]["message"]["content"] or ""
    print("\n== target=%r %.2fs ==" % (target, el), flush=True)
    print(text[:400], flush=True)
    try:
        return json.loads(text)
    except ValueError:
        return {}


def main() -> int:
    from llama_cpp import Llama
    from llama_cpp.llama_chat_format import MTMDChatHandler

    full = Image.open(SHOT).convert("RGB")
    cands = detect_icon_candidates(full, max_candidates=48)
    print("candidates:", len(cands), flush=True)
    truth_idx = None
    for c in cands:
        x, y, w, h = c["bbox"]
        if x <= TRUTH[0] < x + w and y <= TRUTH[1] < y + h:
            truth_idx = cands.index(c) + 1
            print("TRUTH candidate #%d bbox=%s" % (truth_idx, c["bbox"]),
                  flush=True)
    for i, c in enumerate(cands[:48], 1):
        print("  #%02d bbox=%s score=%s" % (i, c["bbox"], c.get("score")),
              flush=True)
    sheet, used = build_contact_sheet(full, cands)
    sheet.save(ROOT / "data" / "screenshots" / "_icon_sheet.png")
    print("sheet size:", sheet.size, "used:", len(used), flush=True)

    handler = MTMDChatHandler(clip_model_path=str(MMPROJ),
                              verbose=False, use_gpu=False)
    llm = Llama(model_path=str(MODEL), chat_handler=handler, n_ctx=4096,
                n_threads=max(4, (os.cpu_count() or 8) // 2), n_batch=256,
                n_gpu_layers=0, verbose=False)
    for target in ("矩形", "Rectangle", "打印机"):
        ans = ask(llm, sheet, target)
        idx = ans.get("index")
        if isinstance(idx, int) and 1 <= idx <= len(cands):
            print("  -> picked #%d bbox=%s confidence=%s"
                  % (idx, cands[idx - 1]["bbox"], ans.get("confidence")),
                  flush=True)
        else:
            print("  ->", ans, flush=True)
    print("truth index:", truth_idx, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
