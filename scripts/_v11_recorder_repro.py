# -*- coding: utf-8 -*-
"""v0.11 录屏循环取证：起服务 → CanvasRecorder 跑 6s → 打印帧数/错误 + 内联异常栈。"""
from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from test_v10_draw_doubao import (  # noqa: E402
    CanvasRecorder, port_free, spawn, stop_proc, wait_healthy)


def main() -> int:
    srv = None
    try:
        if not port_free():
            print("port busy", flush=True)
            return 1
        srv = spawn([sys.executable, "run.py"], drain=True)
        if not wait_healthy():
            print("service not healthy", flush=True)
            return 1
        rec = CanvasRecorder((510, 462, 1537, 908), fps=4.0)
        rec.start()
        time.sleep(6)
        n = rec.stop()
        print(f"frames={n} errors={rec.errors}", flush=True)
        if rec.first_exc:
            print("first_exc:\n" + rec.first_exc, flush=True)
        # 内联重放一次抓帧循环，打印真实异常
        import base64
        import cv2
        import numpy as np
        import requests
        try:
            r = requests.get(
                "http://127.0.0.1:8030/api/ghost/screenshot",
                params={"monitor": 1, "fmt": "jpeg", "scale": 1.0,
                        "quality": 80}, timeout=15)
            print("status", r.status_code, "len", len(r.content), flush=True)
            raw = base64.b64decode(r.json()["image_b64"])
            arr = cv2.imdecode(np.frombuffer(raw, np.uint8),
                               cv2.IMREAD_COLOR)
            print("arr", None if arr is None else arr.shape, flush=True)
            x, y, w, h = rec.crop
            crop = arr[max(0, y):y + h, max(0, x):x + w]
            print("crop", crop.shape, "size", crop.size, flush=True)
            ok, buf = cv2.imencode(".jpg", crop,
                                   [cv2.IMWRITE_JPEG_QUALITY, 82])
            print("encode ok", ok, "bytes", len(buf.tobytes()), flush=True)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    finally:
        if srv is not None:
            stop_proc(srv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
