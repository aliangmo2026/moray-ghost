"""定向定位的诚实性测试：在真实 Paint 整窗 640 图上找「矩形」控件。

如果模型对不存在的文本仍返回 found:true + 随机 bbox，就不能把定向定位
无条件接入 click_element（会产生错误点击）。
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

TMPL = ('You are a GUI visual grounding model. Locate the control whose '
        'visible label contains {target!r}. Return ONLY JSON: '
        '{{"found":true,"text":"...","type":"button|input|text|icon",'
        '"bbox":[x1,y1,x2,y2]}} normalized 0-1000, or {{"found":false}} '
        'if no such control is visible. Do not guess.')


def infer(llm, img: Image.Image, target: str) -> None:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    t0 = time.perf_counter()
    resp = llm.create_chat_completion(
        messages=[{"role": "system", "content": TMPL.format(target=target)},
                  {"role": "user", "content": [
                      {"type": "image_url", "image_url": {
                          "url": "data:image/png;base64," + b64}},
                      {"type": "text", "text": "Locate " + target}]}],
        temperature=0.0, max_tokens=192,
        response_format={"type": "json_object"})
    el = time.perf_counter() - t0
    text = resp["choices"][0]["message"]["content"] or ""
    print("\n== target=%r %.2fs ==" % (target, el), flush=True)
    print(text[:600], flush=True)


def main() -> int:
    from llama_cpp import Llama
    from llama_cpp.llama_chat_format import MTMDChatHandler

    full = Image.open(SHOT).convert("RGB")
    img = full.resize((640, round(full.height * 640 / full.width)))
    handler = MTMDChatHandler(clip_model_path=str(MMPROJ),
                              verbose=False, use_gpu=False)
    llm = Llama(model_path=str(MODEL), chat_handler=handler, n_ctx=4096,
                n_threads=max(4, (os.cpu_count() or 8) // 2), n_batch=256,
                n_gpu_layers=0, verbose=False)
    infer(llm, img, "矩形")
    infer(llm, img, "Rectangle")
    infer(llm, img, "画笔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
