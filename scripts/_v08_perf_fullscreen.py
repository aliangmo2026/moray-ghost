"""全屏（最大化 ZCode 窗口 2560x1600）一次真 VL 解析的性能/内存实测。

- 冷启动（含首次 mmproj 挂载）与热启动各一次；
- 采样服务进程树 RSS / 系统可用内存 / nvidia-smi 显存；
- 输出 timings（TTFT、总耗时、输入缩放）、峰值内存、GPU 占用、服务存活。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psutil  # noqa: E402
import requests  # noqa: E402

from app.core import config  # noqa: E402

TOKEN = config.ensure_token()
HWND = 787738  # 最大化 ZCode 窗口（2560x1600 物理像素）
TARGET = sys.argv[1] if len(sys.argv) > 1 else None


def gpu() -> str:
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=memory.used,memory.free,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10)
        return out.stdout.strip()
    except Exception as exc:  # noqa: BLE001
        return "n/a " + type(exc).__name__


def gpu_apps() -> str:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "(none)"
    except Exception as exc:  # noqa: BLE001
        return "n/a " + type(exc).__name__


def server_pid() -> int:
    for c in psutil.net_connections(kind="inet"):
        if c.laddr and c.laddr.port == 8030 and c.pid:
            return c.pid
    return 0


def tree_rss(pid: int) -> float:
    try:
        p = psutil.Process(pid)
        procs = [p] + p.children(recursive=True)
        total = 0
        for q in procs:
            try:
                total += q.memory_info().rss
            except psutil.Error:
                pass
        return total / 1e9
    except psutil.Error:
        return -1.0


def wait_health(timeout: float = 40.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = requests.get(
                "http://127.0.0.1:8030/api/ghost/health", timeout=2)
            if r.status_code == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.3)
    return False


def analyze_once(tag: str) -> dict:
    result: dict = {}

    def _post():
        t0 = time.perf_counter()
        payload = {"hwnd": HWND, "backend": "llama"}
        if TARGET:
            payload["target"] = TARGET
        r = requests.post(
            "http://127.0.0.1:8030/api/ghost/vl/analyze",
            headers={"X-Ghost-Token": TOKEN},
            json=payload, timeout=300)
        result["wall_s"] = time.perf_counter() - t0
        result["status"] = r.status_code
        try:
            result["body"] = r.json()
        except ValueError:
            result["body"] = {"raw": r.text[:500]}

    th = threading.Thread(target=_post)
    pid = server_pid()
    peak_rss = 0.0
    peak_vram = 0
    min_avail = 1e9
    th.start()
    while th.is_alive():
        rss = tree_rss(pid)
        peak_rss = max(peak_rss, rss)
        try:
            min_avail = min(min_avail,
                            psutil.virtual_memory().available / 1e9)
        except Exception:  # noqa: BLE001
            pass
        try:
            used = int(gpu().split(",")[0])
            peak_vram = max(peak_vram, used)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    th.join()
    body = result.get("body") or {}
    tm = body.get("timings") or {}
    print("\n== %s ==" % tag, flush=True)
    print("http=%s wall=%.2fs analysis_ms=%s count=%s"
          % (result.get("status"), result.get("wall_s", -1),
             body.get("elapsed_ms"), body.get("count")), flush=True)
    print("timings:", json.dumps(tm, ensure_ascii=False), flush=True)
    print("controls:", json.dumps(body.get("controls") or [],
                                  ensure_ascii=False)[:800], flush=True)
    print("peak service RSS=%.2fGB  min system available=%.2fGB  "
          "peak GPU used=%dMiB" % (peak_rss, min_avail, peak_vram),
          flush=True)
    return body


def main() -> int:
    print("gpu before:", gpu(), flush=True)
    print("gpu compute apps before:", gpu_apps(), flush=True)
    srv = subprocess.Popen([sys.executable, str(ROOT / "run.py")],
                           cwd=str(ROOT), stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True,
                           encoding="utf-8", errors="replace")
    try:
        def _drain():
            try:
                for _ in srv.stdout:
                    pass
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=_drain, daemon=True).start()
        if not wait_health():
            print("service failed to start", flush=True)
            return 1
        print("service up pid=", server_pid(), flush=True)
        print("gpu after service start:", gpu(), flush=True)
        analyze_once("cold fullscreen 2560x1600")
        analyze_once("warm fullscreen 2560x1600")
        print("\ngpu after:", gpu(), flush=True)
        print("gpu compute apps after:", gpu_apps(), flush=True)
        print("service alive:", srv.poll() is None, flush=True)
    finally:
        if srv.poll() is None:
            srv.terminate()
            try:
                srv.wait(timeout=8)
            except subprocess.TimeoutExpired:
                srv.kill()
                srv.wait(timeout=8)
    return 0


if __name__ == "__main__":
    sys.exit(main())
