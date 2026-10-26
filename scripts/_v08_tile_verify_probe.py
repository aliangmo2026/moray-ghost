"""单图标 yes/no 复核探针：2B 能否区分直角矩形 vs 圆角矩形？

候选来自 Paint 形状组：
  sharp   = #4 (775,124,29,29)
  rounded = #5 (808,124,29,29)
  ellipse = #3 (743,125,27,27)
  para    = #6 (842,125,27,27)
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

MODEL = ROOT / "models" / "Qwen2-VL-2B-Instruct-Q4_K_M.gguf"
MMPROJ = ROOT / "models" / "mmproj-Qwen2-VL-2B-Instruct-f16.gguf"
SHOT = ROOT / "data" / "screenshots" / "_paint_window.png"

TILES = {
    "sharp": (775, 124, 29, 29),
    "rounded": (808, 124, 29, 29),
    "ellipse": (743, 125, 27, 27),
    "para": (842, 125, 27, 27),
}

HEAD = ("You are a GUI icon inspector. Look at this single icon tile. "
        "Does it show a ")
TAIL = ('? Reply ONLY JSON: {"match": true|false, "confidence": 0.0-1.0}. '
        "Be strict: a rounded rectangle is NOT a sharp-cornered rectangle.")


def ask(llm, tile: Image.Image, kind: str) -> dict:
    buf = io.BytesIO()
    tile.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    t0 = time.perf_counter()
    resp = llm.create_chat_completion(
        messages=[{"role": "system", "content": HEAD + kind + TAIL},
                  {"role": "user", "content": [
                      {"type": "image_url", "image_url": {
                          "url": "data:image/png;base64," + b64}},
                      {"type": "text", "text": "Inspect this icon."}]}],
        temperature=0.0, max_tokens=48,
        response_format={"type": "json_object"})
    el = time.perf_counter() - t0
    text = resp["choices"][0]["message"]["content"] or ""
    print("  %-8s -> %s  (%.2fs)" % (kind, text.strip()[:160], el),
          flush=True)
    try:
        return json.loads(text)
    except ValueError:
        return {}


def main() -> int:
    from llama_cpp import Llama
    from llama_cpp.llama_chat_format import MTMDChatHandler

    full = Image.open(SHOT).convert("RGB")
    tiles = {}
    for name, (x, y, w, h) in TILES.items():
        pad = 8
        crop = full.crop((max(0, x - pad), max(0, y - pad),
                          min(full.width, x + w + pad),
                          min(full.height, y + h + pad)))
        tiles[name] = crop.resize((160, 160), Image.LANCZOS)
        tiles[name].save(ROOT / "data" / "screenshots" /
                         ("_tile_%s.png" % name))

    handler = MTMDChatHandler(clip_model_path=str(MMPROJ),
                              verbose=False, use_gpu=False)
    llm = Llama(model_path=str(MODEL), chat_handler=handler, n_ctx=4096,
                n_threads=max(4, (os.cpu_count() or 8) // 2), n_batch=256,
                n_gpu_layers=0, verbose=False)
    for name in ("sharp", "rounded", "ellipse", "para"):
        print("\n== tile %s ==" % name, flush=True)
        ask(llm, tiles[name], "sharp-cornered rectangle (直角矩形)")
        ask(llm, tiles[name], "rounded rectangle (圆角矩形)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
