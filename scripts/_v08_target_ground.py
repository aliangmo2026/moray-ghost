"""在真实靶窗截图上比较三种 bbox 提示词，目标：让点击中心落进「保存成功」按钮。

真实按钮中心（READY 实测）：(711, 482)。靶窗 rect=(420,360,1002,716)。
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
SHOT = ROOT / "data" / "screenshots" / "20261026_195911_137100_74b0_before.jpg"
RECT = (420, 360, 1002, 716)
TRUTH = (711, 482)

A = ("You are a GUI parser. Return ONLY JSON: {\"controls\":[{\"text\":"
     "\"label\",\"type\":\"button|input|text|icon\",\"bbox\":[x1,y1,x2,y2]}]}. "
     "bbox is normalized 0-1000 relative to the image. Include every visible "
     "control.")
B = ("You are a GUI visual grounding model. Locate the button labeled "
     "「保存成功」. Return ONLY JSON: {\"text\":\"保存成功\",\"type\":\"button\","
     "\"bbox\":[x1,y1,x2,y2]} normalized 0-1000. The bbox must tightly enclose "
     "the button (its border), not the surrounding panel.")
C = ("You are a GUI visual grounding model. Locate the text 「保存成功」. "
     "Return ONLY JSON: {\"text\":\"保存成功\",\"type\":\"text\","
     "\"bbox\":[x1,y1,x2,y2]} normalized 0-1000. The bbox must tightly enclose "
     "the four Chinese glyphs only, not the button border or panel.")


def run(llm, img: Image.Image, system: str, user: str, tag: str) -> None:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    t0 = time.perf_counter()
    resp = llm.create_chat_completion(
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": [
                      {"type": "image_url", "image_url": {
                          "url": "data:image/png;base64," + b64}},
                      {"type": "text", "text": user}]}],
        temperature=0.0, max_tokens=256,
        response_format={"type": "json_object"})
    el = time.perf_counter() - t0
    text = resp["choices"][0]["message"]["content"] or ""
    print("\n== %s %.2fs ==" % (tag, el), flush=True)
    print(text[:600], flush=True)
    try:
        data = json.loads(text)
        items = data.get("controls") or [data]
        w, h = img.size
        for it in items:
            bb = it.get("bbox")
            if isinstance(bb, list) and len(bb) == 4:
                px = [RECT[0] + bb[0] / 1000 * (RECT[2] - RECT[0]),
                      RECT[1] + bb[1] / 1000 * (RECT[3] - RECT[1]),
                      RECT[0] + bb[2] / 1000 * (RECT[2] - RECT[0]),
                      RECT[1] + bb[3] / 1000 * (RECT[3] - RECT[1])]
                cx, cy = (px[0] + px[2]) / 2, (px[1] + px[3]) / 2
                print("  ", it.get("text"), "full=", [round(v) for v in px],
                      "center=(%.0f,%.0f) err=(%.0f,%.0f)"
                      % (cx, cy, cx - TRUTH[0], cy - TRUTH[1]), flush=True)
    except Exception as exc:  # noqa: BLE001
        print("JSON FAIL", type(exc).__name__, exc, flush=True)


def main() -> int:
    from llama_cpp import Llama
    from llama_cpp.llama_chat_format import MTMDChatHandler

    full = Image.open(SHOT).convert("RGB")
    crop = full.crop(RECT)
    print("crop size", crop.size)
    crop.save(ROOT / "data" / "screenshots" / "_target_crop.png")
    img = crop.resize((640, round(crop.height * 640 / crop.width)))
    handler = MTMDChatHandler(clip_model_path=str(MMPROJ),
                              verbose=False, use_gpu=False)
    llm = Llama(model_path=str(MODEL), chat_handler=handler, n_ctx=4096,
                n_threads=max(4, (os.cpu_count() or 8) // 2), n_batch=256,
                n_gpu_layers=0, verbose=False)
    run(llm, img, A, "List all UI controls as JSON.", "A_generic")
    run(llm, img, B, "Locate the 保存成功 button.", "B_button_tight")
    run(llm, img, C, "Locate the 保存成功 text glyphs.", "C_text_tight")
    return 0


if __name__ == "__main__":
    sys.exit(main())
