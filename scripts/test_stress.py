"""截图稳定性压测（诊断脚本，只测不改生产代码）。

铁律：
- 必须走生产路径：from app.core.screen_mss import grab —— 测的就是服务在跑的代码，
  绝不自己 new mss 实例。
- 单线程串行循环（生产即串行），每次截完立刻丢弃，不落盘。
- 句柄分两个指标看：psutil 总句柄 + GetGuiResources 的 GDI/User 句柄。

用法：.venv\\Scripts\\python scripts\\test_stress.py [--n 2000] [--monitor 1]
"""
from __future__ import annotations

import argparse
import ctypes
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psutil

from app.core.screen_mss import grab  # noqa: E402  生产路径

kernel32 = ctypes.windll.kernel32
user32 = ctypes.windll.user32
PSEUDO_CURRENT_PROCESS = ctypes.c_void_p(-1)  # GetCurrentProcess() 伪句柄

GR_GDIOBJECTS = 0
GR_USEROBJECTS = 1


def gui_count(flag: int) -> int:
    return int(user32.GetGuiResources(PSEUDO_CURRENT_PROCESS, flag))


def pct(sorted_vals: list, p: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, int(round(p / 100 * (len(sorted_vals) - 1)))))
    return float(sorted_vals[idx])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2000, help="总抓屏次数")
    ap.add_argument("--monitor", type=int, default=1)
    args = ap.parse_args()
    n = args.n
    proc = psutil.Process()
    print(f"pid={proc.pid} n={n} monitor={args.monitor} path=app.core.screen_mss.grab", flush=True)

    h0_total = proc.num_handles()
    h0_gdi = gui_count(GR_GDIOBJECTS)
    h0_user = gui_count(GR_USEROBJECTS)

    results = []  # (i, ok, ms, total, gdi, user, exc)
    t_start = time.perf_counter()
    for i in range(n):
        t0 = time.perf_counter()
        exc = None
        try:
            img, _origin = grab(args.monitor)
            del img
        except Exception as e:  # noqa: BLE001  诊断脚本：任何失败都原样记录
            exc = e
        ms = (time.perf_counter() - t0) * 1000.0
        results.append((i, exc is None, ms, proc.num_handles(),
                        gui_count(GR_GDIOBJECTS), gui_count(GR_USEROBJECTS), exc))
    elapsed = time.perf_counter() - t_start

    # 分段输出（每 200 次一段）
    block = 200
    block_handles = []
    for start in range(0, n, block):
        seg = results[start:start + block]
        oks = [r for r in seg if r[1]]
        lat = sorted(r[2] for r in oks)
        last = seg[-1]
        block_handles.append((last[3], last[4], last[5]))
        end_i = start + len(seg) - 1
        print(f"[{start}-{end_i}] ok={len(oks)}  fail={len(seg) - len(oks)}   "
              f"p50={pct(lat, 50):.0f}ms  p99={pct(lat, 99):.0f}ms  "
              f"handles: total={last[3]} gdi={last[4]} user={last[5]}", flush=True)
        for r in seg:
            if not r[1]:
                e = r[6]
                print(f"          └─ FAIL @ {r[0]}: {type(e).__name__}: {str(e)[:140]}"
                      f" | repr={repr(e)[:140]}", flush=True)

    # 汇总
    ok_count = sum(1 for r in results if r[1])
    fail_count = n - ok_count
    fail_rate = fail_count / n * 100
    lat_all = sorted(r[2] for r in results if r[1])
    types: Counter = Counter()
    for r in results:
        if not r[1]:
            e = r[6]
            types[f"{type(e).__name__}: {str(e)[:70]}"] += 1
    total_delta = results[-1][3] - h0_total
    gdi_delta = results[-1][4] - h0_gdi
    user_delta = results[-1][5] - h0_user
    trend_total = block_handles[-1][0] - block_handles[0][0]
    trend_gdi = block_handles[-1][1] - block_handles[0][1]
    trend_user = block_handles[-1][2] - block_handles[0][2]

    fail_idx = [r[0] for r in results if not r[1]]
    clusters = []
    for i in fail_idx:
        if clusters and i == clusters[-1][1] + 1:
            clusters[-1][1] = i
        else:
            clusters.append([i, i])
    cluster_txt = ", ".join(f"{a}-{b}" if a != b else str(a) for a, b in clusters)

    # gdi 是否按段单调增长
    gdi_mono = all(b[1] >= a[1] for a, b in zip(block_handles, block_handles[1:]))

    print(flush=True)
    print(f"总次数: {n}   失败: {fail_count}   失败率: {fail_rate:.2f}%   总耗时: {elapsed:.1f}s")
    dist_txt = "; ".join(f"{k} = {v}" for k, v in types.items()) if types else "(无)"
    print(f"失败类型分布: {dist_txt}")
    print(f"句柄趋势(首末段Δ): total Δ={trend_total:+d}  gdi Δ={trend_gdi:+d}  user Δ={trend_user:+d}")
    print(f"句柄趋势(全程Δ):   total Δ={total_delta:+d}  gdi Δ={gdi_delta:+d}  user Δ={user_delta:+d}")
    print(f"p50/p95/p99: {pct(lat_all, 50):.0f} / {pct(lat_all, 95):.0f} / {pct(lat_all, 99):.0f} ms")
    print(f"失败是否聚集: {cluster_txt if cluster_txt else '(无失败)'}")

    # 泄漏判定用稳态差值（首段末 vs 末段末）：全程Δ 含进程冷启动的一次性开销
    # （首次建 mss 实例、import 等），会顶高基线造成误判；稳态下降/持平即无泄漏。
    handles_stable = trend_gdi <= 5 and trend_total <= 20
    if fail_rate <= 1.0 and handles_stable:
        print("[结论] 稳定，单线程方案有效")
    if gdi_delta > 20 and gdi_mono:
        print("[结论] GDI 句柄泄漏，单线程是缓解不是根治，v0.3 并发前必须换方案")
    if fail_rate > 1.0 and handles_stable:
        print('[结论] 非泄漏型故障（桌面不可读/系统干扰），需要加"桌面可读性探测"')
    if clusters:
        print("[结论] 存在时间窗口故障，怀疑锁屏/UAC/远控/overlay")
    return 0


if __name__ == "__main__":
    sys.exit(main())
