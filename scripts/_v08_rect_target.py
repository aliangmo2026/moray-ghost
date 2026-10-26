"""定向 grounding 探针：只问「矩形工具图标在哪」，比较不同裁剪/缩放的可信度。

真实矩形图标（全窗物理像素）约在 x 779-798 / y 129-145，中心 ~ (788,137)。
输出会把归一化 bbox 映射回全窗坐标并打印误差。
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
TRUTH = (779, 129, 798, 145)

SYSTEM = (
    'You are a GUI visual grounding model. Locate the Rectangle tool button '
    '(the hollow square icon in the shapes toolbar). Reply ONLY JSON: '
    '{"text":"Rectangle","bbox":[x1,y1,x2,y2]} where bbox is normalized '
    '0-1000 relative to the image (top-left to bottom-right).')


def infer(llm, crop: Image.Image, origin: tuple, scale: float,
          tag: str) -> None:
    img = crop.resize((round(crop.width * scale),
                       round(crop.height * scale)))
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
                       "Locate the Rectangle tool icon."}]}],
        temperature=0.0, max_tokens=128,
        response_format={"type": "json_object"})
    el = time.perf_counter() - t0
    text = resp["choices"][0]["message"]["content"] or ""
    print("\n== %s crop=%s -> img=%s origin=%s elapsed=%.2fs =="
          % (tag, crop.size, img.size, origin, el), flush=True)
    try:
        data = json.loads(text)
        bb = data.get("bbox") or []
        if len(bb) == 4:
            px = [origin[0] + bb[0] / 1000 * crop.width,
                  origin[1] + bb[1] / 1000 * crop.height,
                  origin[0] + bb[2] / 1000 * crop.width,
                  origin[1] + bb[3] / 1000 * crop.height]
            cx, cy = (px[0] + px[2]) / 2, (px[1] + px[3]) / 2
            ecx, ecy = cx - (TRUTH[0] + TRUTH[2]) / 2, \
                cy - (TRUTH[1] + TRUTH[3]) / 2
            print("  %s bbox=%s -> full=%s center=(%.0f,%.0f) "
                  "err=(%.0f,%.0f) size=(%.0f,%.0f)"
                  % (data.get("text"), bb, [round(v) for v in px], cx, cy,
                     ecx, ecy, px[2] - px[0], px[3] - px[1]), flush=True)
        else:
            print("  bad bbox:", text[:300], flush=True)
    except Exception as exc:  # noqa: BLE001
        print("  JSON FAIL:", type(exc).__name__, exc, text[:300], flush=True)


def main() -> int:
    from llama_cpp import Llama
    from llama_cpp.llama_chat_format import MTMDChatHandler

    full = Image.open(SHOT).convert("RGB")
    handler = MTMDChatHandler(clip_model_path=str(MMPROJ),
                              verbose=False, use_gpu=False)
    llm = Llama(model_path=str(MODEL), chat_handler=handler, n_ctx=4096,
                n_threads=max(4, (os.cpu_count() or 8) // 2), n_batch=256,
                n_gpu_layers=0, verbose=False)

    # A 宽形状组 320x175 → 2x
    infer(llm, full.crop((640, 85, 960, 260)), (640, 85), 2.0, "A_wide2x")
    # B 紧形状网格 270x135 → 2.37x
    infer(llm, full.crop((660, 110, 930, 245)), (660, 110), 2.37,
          "B_tight2.4x")
    # C 工具条 1/4 (512x320) → 1.25x
    infer(llm, full.crop((512, 0, 1024, 320)), (512, 0), 1.25, "C_tile1")
    # D 工具条 1/2 (1024x320) → 0.625x
    infer(llm, full.crop((512, 0, 1536, 320)), (512, 0), 0.625, "D_half")
    return 0


if __name__ == "__main__":
    sys.exit(main())
