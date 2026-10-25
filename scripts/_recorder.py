"""帧序列录像器（无 ffmpeg 时的实况录像替代）：按 fps 周期抓全屏存 jpg。

用法（后台运行）：
    .venv\\Scripts\\python scripts/_recorder.py --out data/recordings/<run> --seconds 420
结束自动生成 manifest.txt（帧文件 + 时间戳清单）。用后目录保留作证据。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    try:
        ctypes_windll = __import__("ctypes").windll
        ctypes_windll.shcore.SetProcessDpiAwareness(2)
    except OSError:
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.screen_mss import grab  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--fps", type=float, default=0.5)
    ap.add_argument("--seconds", type=float, default=420.0)
    args = ap.parse_args()

    out = (ROOT / args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    manifest = out / "manifest.txt"
    print(f"recording -> {out} @ {args.fps}fps for {args.seconds}s", flush=True)
    period = 1.0 / max(args.fps, 0.1)
    t_end = time.time() + args.seconds
    n = 0
    with manifest.open("w", encoding="utf-8") as mf:
        mf.write("# ts_epoch frame_file\n")
        while time.time() < t_end:
            t0 = time.time()
            try:
                img, _origin = grab(1)
                name = f"frame_{n:05d}_{int(t0 * 1000)}.jpg"
                img.save(out / name, format="JPEG", quality=70)
                mf.write(f"{t0:.3f} {name}\n")
                mf.flush()
                n += 1
            except Exception as exc:  # noqa: BLE001  抓屏失败也记录（本身就是证据）
                mf.write(f"{t0:.3f} GRAB_FAILED {type(exc).__name__}: {exc}\n")
                mf.flush()
            remain = period - (time.time() - t0)
            if remain > 0:
                time.sleep(remain)
    print(f"recording done: {n} frames", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
