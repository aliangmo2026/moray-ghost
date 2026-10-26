"""在真实 MS Paint 截图上做分块 A/B：整窗 / 工具条 / 形状组 / 横向切片。

只读取已保存的 _paint_window.png，不启动任何窗口。输出每块耗时与识别结果，
用于决定 VL 驱动在 CPU 上可接受的分块策略。
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

SYSTEM = (
    "You are a GUI parser. Return ONLY JSON: {\"controls\":[{\"text\":"
    "\"label or icon tool name\",\"type\":\"button|input|text|icon|menu|"
    "link\",\"bbox\":[x1,y1,x2,y2]}]}. bbox is normalized 0-1000 relative "
    "to the image (top-left to bottom-right). Name icon-only toolbar "
    "buttons by their tool (Rectangle, Pencil, Eraser, Fill, Oval, Text). "
    "Include every visible control.")


def infer(llm, img: Image.Image, tag: str) -> None:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    t0 = time.perf_counter()
    resp = llm.create_chat_completion(
        messages=[{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": [
                      {"type": "image_url", "image_url": {
                          "url": "data:image/png;base64," + b64}},
                      {"type": "text", "text":
                       "List all UI controls in this image as JSON."}]}],
        temperature=0.0, max_tokens=768,
        response_format={"type": "json_object"})
    el = time.perf_counter() - t0
    text = resp["choices"][0]["message"]["content"] or ""
    print("\n== %s size=%s elapsed=%.2fs ==" % (tag, img.size, el),
          flush=True)
    try:
        data = json.loads(text)
        ctrls = data.get("controls") or []
        print("controls=%d" % len(ctrls), flush=True)
        w, h = img.size
        for c in ctrls:
            bb = c.get("bbox")
            if isinstance(bb, list) and len(bb) == 4:
                px = [round(bb[0] / 1000 * w), round(bb[1] / 1000 * h),
                      round(bb[2] / 1000 * w), round(bb[3] / 1000 * h)]
                print("  ", repr(c.get("text")), c.get("type"),
                      "norm=", bb, "px=", px, flush=True)
    except Exception as exc:  # noqa: BLE001
        print("JSON FAIL:", type(exc).__name__, exc, flush=True)
        print(text[:800], flush=True)


def main() -> int:
    from llama_cpp import Llama
    from llama_cpp.llama_chat_format import MTMDChatHandler

    full = Image.open(SHOT).convert("RGB")
    handler = MTMDChatHandler(clip_model_path=str(MMPROJ),
                              verbose=False, use_gpu=False)
    llm = Llama(model_path=str(MODEL), chat_handler=handler, n_ctx=4096,
                n_threads=max(4, (os.cpu_count() or 8) // 2), n_batch=256,
                n_gpu_layers=0, verbose=False)

    small = full.resize((640, round(full.height * 640 / full.width)))
    infer(llm, small, "full640")

    toolbar = full.crop((0, 0, full.width, 320))
    infer(llm, toolbar.resize((640, 100)), "toolbar640x100")

    # 形状组（手工从截图量取，仅探针用）：x 640-960, y 85-260
    shape = full.crop((640, 85, 960, 260))
    infer(llm, shape.resize((640, 350)), "shapes640x350")

    # 工具条四等分，每块 512x320 → 640x400
    for i in range(4):
        tile = toolbar.crop((i * 512, 0, (i + 1) * 512, 320))
        infer(llm, tile.resize((640, 400)), "tile%d" % i)
    return 0


if __name__ == "__main__":
    sys.exit(main())
