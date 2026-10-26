"""Qwen2-VL 坐标语义/提示词 A/B 探针（同一进程只加载一次模型）。

验证点：
- Qwen2-VL 原生 bbox 是否为 0-1000 归一化 [x1,y1,x2,y2]；
- 无文字矩形图标能否被识别并给出工具名；
- response_format=json_object 是否稳定产出合法 JSON；
- 不同输入分辨率下的视觉编码耗时。
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

from PIL import Image, ImageDraw  # noqa: E402

MODEL = ROOT / "models" / "Qwen2-VL-2B-Instruct-Q4_K_M.gguf"
MMPROJ = ROOT / "models" / "mmproj-Qwen2-VL-2B-Instruct-f16.gguf"

SYSTEM = (
    "You are a GUI parser. Return ONLY a JSON object: "
    '{"controls":[{"text":"label or icon tool name","type":'
    '"button|input|text|icon|menu|link","bbox":[x1,y1,x2,y2]}]}. '
    "bbox is normalized to 0-1000 relative to the image: x1,y1 is the "
    "top-left corner and x2,y2 the bottom-right corner. Include icon-only "
    "toolbar buttons and put their tool name (e.g. Rectangle, Pencil, "
    "Eraser) in text.")


def make_image(w: int, h: int) -> Image.Image:
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    sx, sy = w / 640.0, h / 480.0
    def B(x1, y1, x2, y2):
        return [int(x1 * sx), int(y1 * sy), int(x2 * sx), int(y2 * sy)]
    # 工具条：一排 6 个无文字图标（铅笔/橡皮/填充/矩形/椭圆/文字）
    for i in range(6):
        x1, y1, x2, y2 = B(20 + i * 50, 20, 60 + i * 50, 60)
        d.rectangle([x1, y1, x2, y2], outline="black", width=2)
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        if i == 3:   # 矩形图标
            d.rectangle([x1 + 8, y1 + 8, x2 - 8, y2 - 8], outline="black",
                        width=2)
        elif i == 0:  # 铅笔
            d.line([cx - 8, cy + 8, cx + 8, cy - 8], fill="black", width=3)
        elif i == 1:  # 橡皮
            d.rectangle([x1 + 8, y1 + 15, x2 - 8, y2 - 8], outline="black")
        elif i == 2:  # 填充
            d.ellipse([cx - 10, cy - 10, cx + 10, cy + 10], outline="black")
        elif i == 4:  # 椭圆
            d.ellipse([x1 + 8, y1 + 10, x2 - 8, y2 - 10], outline="black")
        else:         # 文字
            d.text((cx - 6, cy - 6), "A", fill="black")
    # Save 按钮
    a = B(180, 90, 300, 150)
    d.rectangle(a, outline="black", fill="#e8e8e8", width=2)
    d.text((a[0] + 25, a[1] + 18), "Save", fill="black")
    d.text(B(40, 220, 200, 250)[:2], "Canvas", fill="black")
    return img


def run(llm, img: Image.Image, tag: str) -> None:
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
                       "Parse the UI controls as JSON."}]}],
        temperature=0.0, max_tokens=512,
        response_format={"type": "json_object"})
    el = time.perf_counter() - t0
    text = resp["choices"][0]["message"]["content"] or ""
    print("\n== %s  size=%s  elapsed=%.2fs ==" % (tag, img.size, el),
          flush=True)
    print(text[:1500], flush=True)
    try:
        data = json.loads(text)
        ctrls = data.get("controls") or []
        print("JSON OK, controls=%d" % len(ctrls), flush=True)
        for c in ctrls[:12]:
            bb = c.get("bbox")
            if isinstance(bb, list) and len(bb) == 4:
                w, h = img.size
                px = [round(bb[0] / 1000 * w), round(bb[1] / 1000 * h),
                      round(bb[2] / 1000 * w), round(bb[3] / 1000 * h)]
                print("  ", c.get("text"), c.get("type"), "norm=", bb,
                      "px=", px, flush=True)
    except Exception as exc:  # noqa: BLE001
        print("JSON FAIL:", type(exc).__name__, exc, flush=True)
    usage = resp.get("usage") or {}
    print("usage:", usage, flush=True)


def main() -> int:
    from llama_cpp import Llama
    from llama_cpp.llama_chat_format import MTMDChatHandler

    handler = MTMDChatHandler(clip_model_path=str(MMPROJ),
                              verbose=False, use_gpu=False)
    llm = Llama(model_path=str(MODEL), chat_handler=handler, n_ctx=4096,
                n_threads=max(4, (os.cpu_count() or 8) // 2), n_batch=256,
                n_gpu_layers=0, verbose=False)
    run(llm, make_image(640, 480), "base640")
    run(llm, make_image(1280, 960), "base1280")
    return 0


if __name__ == "__main__":
    sys.exit(main())
