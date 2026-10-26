"""GPU 冒烟：cu128 轮子 + Qwen2-VL + mmproj，测 TTFT/总耗时/显存。

用法：.venv\\Scripts\\python.exe scripts\\_v08_gpu_smoke.py
前提：scripts/_v08_target_ground.py 已生成 data/screenshots/_target_crop.png
"""
from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

MODEL = ROOT / "models" / "Qwen2-VL-2B-Instruct-Q4_K_M.gguf"
MMPROJ = ROOT / "models" / "mmproj-Qwen2-VL-2B-Instruct-f16.gguf"
IMG = ROOT / "data" / "screenshots" / "_target_crop.png"

HEAD = ("You are a GUI visual grounding model. Locate the control whose "
        "visible label contains 保存成功. Return ONLY JSON: "
        '{"found":true,"text":"...","type":"button","bbox":[x1,y1,x2,y2]} '
        "normalized 0-1000, or {\"found\":false}. bbox must tightly enclose "
        "the control.")


def gpu_mem() -> str:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10)
        return out.stdout.strip()
    except Exception as exc:  # noqa: BLE001
        return "n/a " + type(exc).__name__


def add_cuda_dll_dirs() -> list:
    """把 pip 安装的 CUDA 12.8 runtime/cuBLAS DLL 目录加入搜索路径。"""
    dirs = []
    site = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    for sub in (Path("cuda_runtime") / "bin", Path("cublas") / "bin"):
        p = site / sub
        if p.is_dir():
            try:
                os.add_dll_directory(str(p))
            except OSError:
                pass
            os.environ["PATH"] = str(p) + os.pathsep + os.environ.get(
                "PATH", "")
            dirs.append(str(p))
    return dirs


def make_handler(MTMDChatHandler, mmproj: str, use_gpu: bool):
    try:
        return MTMDChatHandler(mmproj_path=mmproj, verbose=False,
                               use_gpu=use_gpu)
    except TypeError:
        return MTMDChatHandler(clip_model_path=mmproj, verbose=False,
                               use_gpu=use_gpu)


def main() -> int:
    print("cuda dll dirs:", add_cuda_dll_dirs(), flush=True)
    import llama_cpp
    from llama_cpp import Llama
    from llama_cpp.llama_chat_format import MTMDChatHandler

    print("llama_cpp:", llama_cpp.__version__, flush=True)
    print("gpu before model load:", llama_cpp.llama_supports_gpu_offload(),
          flush=True)
    print("gpu before:", gpu_mem(), flush=True)

    handler = make_handler(MTMDChatHandler, str(MMPROJ), True)
    t0 = time.perf_counter()
    llm = Llama(model_path=str(MODEL), chat_handler=handler, n_ctx=4096,
                n_threads=max(4, (os.cpu_count() or 8) // 2), n_batch=256,
                n_gpu_layers=-1, verbose=False)
    print("model loaded %.2fs" % (time.perf_counter() - t0), flush=True)
    print("gpu after load:", llama_cpp.llama_supports_gpu_offload(),
          gpu_mem(), flush=True)

    img = Image.open(IMG).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    t0 = time.perf_counter()
    ttft = None
    chunks = []
    stream = llm.create_chat_completion(
        messages=[{"role": "system", "content": HEAD},
                  {"role": "user", "content": [
                      {"type": "image_url", "image_url": {
                          "url": "data:image/png;base64," + b64}},
                      {"type": "text", "text": "Locate 保存成功."}]}],
        temperature=0.0, max_tokens=128,
        response_format={"type": "json_object"}, stream=True)
    for ch in stream:
        piece = ((ch.get("choices") or [{}])[0].get("delta")
                 or {}).get("content")
        if piece:
            if ttft is None:
                ttft = time.perf_counter() - t0
            chunks.append(piece)
    total = time.perf_counter() - t0
    text = "".join(chunks)
    print("TTFT=%.2fs total=%.2fs chars=%d"
          % (ttft or -1, total, len(text)), flush=True)
    print("content:", text[:500], flush=True)
    print("gpu after infer:", gpu_mem(), flush=True)
    # 真值中心（相对 _target_crop）：(711-420, 482-360) = (291,122)
    try:
        data = json.loads(text)
        bb = data.get("bbox")
        if isinstance(bb, list) and len(bb) == 4:
            w, h = img.size
            px = [bb[0] / 1000 * w, bb[1] / 1000 * h,
                  bb[2] / 1000 * w, bb[3] / 1000 * h]
            cx, cy = (px[0] + px[2]) / 2, (px[1] + px[3]) / 2
            print("center=(%.1f,%.1f) truth=(291,122) err=(%.1f,%.1f)"
                  % (cx, cy, cx - 291, cy - 122), flush=True)
    except Exception as exc:  # noqa: BLE001
        print("parse failed:", type(exc).__name__, exc, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
