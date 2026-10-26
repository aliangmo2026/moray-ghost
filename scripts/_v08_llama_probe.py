"""v0.8.1 Qwen2-VL + mmproj 运行时探针：验证 mtmd 视觉链路可加载、可推理。

只做单次合成图推理（矩形图标无文字 + 一个「保存」按钮），输出 TTFT/总耗时/
token 数/内存/显存，供驱动挂载参数定标。不触碰用户窗口。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw  # noqa: E402

MODEL = ROOT / "models" / "Qwen2-VL-2B-Instruct-Q4_K_M.gguf"
MMPROJ = ROOT / "models" / "mmproj-Qwen2-VL-2B-Instruct-f16.gguf"
OUT = ROOT / "data" / "screenshots" / "_probe_vl.png"

SYSTEM = (
    "You are a GUI parser. List every visible UI control in the screenshot. "
    'Reply with ONLY a JSON array: [{"text": "...", "type": '
    '"button|input|text|menu|image|checkbox|link", "bbox": [x, y, w, h]}] '
    "where bbox is in pixels of the given image. No prose.")


def make_image() -> Image.Image:
    img = Image.new("RGB", (640, 480), "white")
    d = ImageDraw.Draw(img)
    # 无文字的矩形图标（模拟画图工具条里的 Rectangle 按钮）
    d.rectangle([40, 40, 100, 100], outline="black", width=3)
    # 有文字的按钮
    d.rectangle([180, 40, 300, 100], outline="black", fill="#e8e8e8", width=2)
    d.text((205, 60), "Save", fill="black")
    # 一段文字
    d.text((40, 160), "Canvas", fill="black")
    return img


def gpu_mem() -> str:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10)
        return out.stdout.strip()
    except Exception as exc:  # noqa: BLE001
        return "n/a (" + type(exc).__name__ + ")"


def main() -> int:
    from llama_cpp import Llama
    from llama_cpp.llama_chat_format import MTMDChatHandler

    OUT.parent.mkdir(parents=True, exist_ok=True)
    make_image().save(OUT, format="PNG")
    mode = (sys.argv[1] if len(sys.argv) > 1 else "mtmd").lower()

    print("mode=" + mode, flush=True)
    print("gpu before:", gpu_mem(), flush=True)
    t0 = time.perf_counter()
    if mode == "mtmd":
        handler = MTMDChatHandler(clip_model_path=str(MMPROJ),
                                  verbose=False, use_gpu=False)
    else:
        from llama_cpp.llama_chat_format import Qwen25VLChatHandler
        handler = Qwen25VLChatHandler(clip_model_path=str(MMPROJ),
                                      verbose=False)
    print("handler ready %.1fs" % (time.perf_counter() - t0), flush=True)

    t0 = time.perf_counter()
    llm = Llama(model_path=str(MODEL), chat_handler=handler,
                n_ctx=4096, n_threads=max(4, (os.cpu_count() or 8) // 2),
                n_batch=256, n_gpu_layers=0, verbose=False)
    print("model loaded %.1fs" % (time.perf_counter() - t0), flush=True)
    print("gpu after load:", gpu_mem(), flush=True)
    print("meta chat_template:", bool(
        llm.metadata.get("tokenizer.chat_template")), flush=True)

    import base64
    b64 = base64.b64encode(OUT.read_bytes()).decode("ascii")
    t0 = time.perf_counter()
    ttft = None
    chunks = []
    stream = llm.create_chat_completion(
        messages=[{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": [
                      {"type": "image_url", "image_url": {
                          "url": "data:image/png;base64," + b64}},
                      {"type": "text", "text":
                       "Parse the UI controls as JSON array."}]}],
        temperature=0.0, max_tokens=512, stream=True)
    for ch in stream:
        delta = ch["choices"][0].get("delta") or {}
        piece = delta.get("content")
        if piece:
            if ttft is None:
                ttft = time.perf_counter() - t0
            chunks.append(piece)
    total = time.perf_counter() - t0
    text = "".join(chunks)
    print("TTFT=%.3fs total=%.3fs chars=%d" % (ttft or -1, total, len(text)),
          flush=True)
    print("content:", text[:2000], flush=True)
    print("gpu after infer:", gpu_mem(), flush=True)
    try:
        m, e = text.find("["), text.rfind("]")
        print("parsed_items=%d" % len(json.loads(text[m:e + 1])), flush=True)
    except Exception as exc:  # noqa: BLE001
        print("parse failed:", type(exc).__name__, exc, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
