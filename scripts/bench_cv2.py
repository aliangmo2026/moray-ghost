"""cv2 性能剖析（任务 3.5-a，只诊断不改生产代码）。

Q1  matchTemplate 随输入尺寸的缩放关系（线性 → ROI 优化有效；非线性 → 另有问题）
Q2  颜色空间转换各路径耗时（find() 现行 PIL 链 vs numpy 直通 vs mss BGRA 直出）
Q5  ROI 800x600 实测 vs 理论值
（Q3 由外部 pip show / 换 headless 后重跑本脚本对比；Q4 由代码走读回答）

用法：.venv\\Scripts\\python scripts\\bench_cv2.py
"""
from __future__ import annotations

import base64
import ctypes
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except OSError:
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import mss  # noqa: E402
import numpy as np  # noqa: E402

from app.core.macro_matcher import crop_around, find  # noqa: E402  生产代码（只调用不修改）
from app.core.screen_mss import grab  # noqa: E402


def bench(fn, reps: int = 7) -> tuple:
    times = []
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000)
    times.sort()
    return times[0], times[len(times) // 2], times[-1]


def ms_line(tag: str, t: tuple) -> None:
    print(f"{tag:<64} min={t[0]:7.1f}  med={t[1]:7.1f}  max={t[2]:7.1f} ms", flush=True)


def main() -> int:
    print(f"cv2 version: {cv2.__version__}", flush=True)
    img, _origin = grab(1)  # 生产路径抓真实屏幕（PIL RGB）

    # 有纹理的 64x64 模板（纯色区会被防线拒，回退候选点）
    tpl = None
    tpl_center = None
    for px, py in [(img.width // 2, img.height // 2),
                   (img.width // 3, img.height // 3),
                   (img.width * 2 // 3, img.height * 2 // 3)]:
        b64, center = crop_around(img, px, py, 64)
        arr = cv2.imdecode(np.frombuffer(base64.b64decode(b64), dtype=np.uint8),
                           cv2.IMREAD_COLOR)
        if float(arr.std()) > 2.0:
            tpl = arr
            tpl_center = center
            break
    if tpl is None:
        print("找不到有纹理的模板区域", flush=True)
        return 1
    print(f"template: 64x64 at {tpl_center} (std={float(tpl.std()):.1f})", flush=True)

    np_rgb = np.asarray(img)
    screen = np.ascontiguousarray(np_rgb)  # RGB 顺序；通道序不影响 matchTemplate 耗时
    H, W = screen.shape[:2]
    print(f"screen: {W}x{H}", flush=True)

    # ---------- Q1：matchTemplate vs 输入尺寸 ----------
    print("--- Q1: matchTemplate 缩放关系（同一 64x64 模板） ---", flush=True)
    half = cv2.resize(screen, (W // 2, H // 2), interpolation=cv2.INTER_AREA)
    quarter = cv2.resize(screen, (W // 4, H // 4), interpolation=cv2.INTER_AREA)
    t_full = bench(lambda: cv2.matchTemplate(screen, tpl, cv2.TM_CCOEFF_NORMED))
    t_half = bench(lambda: cv2.matchTemplate(half, tpl, cv2.TM_CCOEFF_NORMED))
    t_quarter = bench(lambda: cv2.matchTemplate(quarter, tpl, cv2.TM_CCOEFF_NORMED))
    ms_line("Q1 full  2560x1600", t_full)
    ms_line("Q1 half  1280x800 ", t_half)
    ms_line("Q1 quarter 640x400", t_quarter)
    if t_full[1] > 0:
        print(f"Q1 ratios: half/full={t_half[1] / t_full[1]:.3f} (理论 0.250)  "
              f"quarter/full={t_quarter[1] / t_full[1]:.3f} (理论 0.0625)", flush=True)

    # ---------- Q2：颜色空间转换 ----------
    print("--- Q2: 转换路径 ---", flush=True)
    ms_line("Q2a img.convert('RGB')        （PIL 拷贝）",
            bench(lambda: img.convert("RGB")))
    ms_line("Q2b np.asarray(img)           （零拷贝视图）",
            bench(lambda: np.asarray(img)))
    ms_line("Q2c ascontiguousarray(asarray) （一次整幅拷贝）",
            bench(lambda: np.ascontiguousarray(np.asarray(img))))
    ms_line("Q2d cvtColor(RGB2BGR)          （现 find() 链的最后一环）",
            bench(lambda: cv2.cvtColor(np.ascontiguousarray(np.asarray(img)),
                                       cv2.COLOR_RGB2BGR)))
    ms_line("Q2e 现行 find() 完整预处理链（convert+asarray+contig+cvtColor）",
            bench(lambda: cv2.cvtColor(
                np.ascontiguousarray(np.asarray(img.convert("RGB"))),
                cv2.COLOR_RGB2BGR)))
    ms_line("Q2f numpy 反转视图 [:,:,::-1]  （零拷贝）",
            bench(lambda: np.asarray(img)[:, :, ::-1]))
    ms_line("Q2g 反转视图+ascontiguousarray （BGR 一次拷贝）",
            bench(lambda: np.ascontiguousarray(np.asarray(img)[:, :, ::-1])))
    # mss BGRA 直出（零 PIL；BGR 已是 cv2 原生序，无需 cvtColor）
    sct = mss.mss()
    shot = sct.grab(sct.monitors[1])
    ms_line("Q2h np.asarray(mss shot)       （BGRA 零拷贝视图）",
            bench(lambda: np.asarray(shot)))
    ms_line("Q2i ascontiguousarray(bgra[:,:,:3]) （mss→BGR 一次拷贝）",
            bench(lambda: np.ascontiguousarray(np.asarray(shot)[:, :, :3])))
    bgra = np.asarray(shot)
    print(f"Q2 sanity: mss BGRA shape={bgra.shape} dtype={bgra.dtype}；"
          f"即 mss 直出 numpy 可行（无 grab(output=raw) 参数，"
          f"ScreenShot.__array_interface__ 即零拷贝通道）", flush=True)

    # ---------- Q4 说明（代码走读） ----------
    print("--- Q4: find() 用 1 次 matchTemplate、单尺度 [1.0]，207ms 即单次耗时 ---",
          flush=True)

    # ---------- Q5：ROI 800x600 ----------
    print("--- Q5: ROI 800x600 实测 ---", flush=True)
    rx = max(0, min(tpl_center[0] - 400, W - 800))
    ry = max(0, min(tpl_center[1] - 300, H - 600))
    roi = screen[ry:ry + 600, rx:rx + 800]
    t_roi = bench(lambda: cv2.matchTemplate(roi, tpl, cv2.TM_CCOEFF_NORMED))
    ms_line("Q5 matchTemplate ROI 800x600", t_roi)
    if t_full[1] > 0:
        theo = t_full[1] * (800 * 600) / (W * H)
        print(f"Q5 理论值（按面积比线性）= {theo:.1f}ms；实测/理论 = "
              f"{t_roi[1] / theo if theo else 0:.2f}", flush=True)

    # ---------- 生产 find() 基线（同进程对照） ----------
    tpl_b64 = base64.b64encode(cv2.imencode(".png", tpl)[1].tobytes()).decode("ascii")
    ms_line("REF 生产 find() 全流程（转换+匹配，现行代码）",
            bench(lambda: find(img, tpl_b64, 0.80)))

    return 0


if __name__ == "__main__":
    sys.exit(main())
