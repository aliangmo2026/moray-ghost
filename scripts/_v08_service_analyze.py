"""对运行中的 Ghost 服务做一次真实 /vl/analyze（可指定目标窗口标题）。

用法：python _v08_service_analyze.py [title-substring]
默认识别自建靶窗 moray-v08-*。输出完整 JSON、timings、进程 RSS/GPU 显存。
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psutil  # noqa: E402
import requests  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402

from app.core import config, window_locator as wl  # noqa: E402

TOKEN = config.ensure_token()


def rss_gb() -> float:
    return psutil.Process().memory_info().rss / 1e9


def server_pid() -> int:
    for c in psutil.net_connections(kind="inet"):
        if c.laddr and c.laddr.port == 8030 and c.pid:
            return c.pid
    return 0


def main() -> int:
    title = sys.argv[1] if len(sys.argv) > 1 else ""
    tgt = None
    hwnd = 0
    try:
        if not title:
            stem = "a" + str(int(time.time() * 1000))[-8:]
            tgt = subprocess.Popen(
                [sys.executable, str(ROOT / "scripts" / "_v08_target.py"),
                 stem], stdout=subprocess.PIPE, text=True, encoding="utf-8")
            line = tgt.stdout.readline().strip()
            parts = line.split()
            if not parts or parts[0] != "READY":
                print("target not ready:", line)
                return 1
            hwnd = int(parts[1])
            title = "moray-v08-" + stem
            time.sleep(0.5)
        pid = server_pid()
        print("server_pid=", pid, "rss=%.2fGB" % rss_gb(), flush=True)
        t0 = time.perf_counter()
        r = requests.post("http://127.0.0.1:8030/api/ghost/vl/analyze",
                          headers={"X-Ghost-Token": TOKEN},
                          json={"window": title, "backend": "llama"},
                          timeout=180)
        dt = time.perf_counter() - t0
        print("http=%s wall=%.2fs" % (r.status_code, dt), flush=True)
        print(json.dumps(r.json(), ensure_ascii=False, indent=2), flush=True)
        if pid:
            try:
                p = psutil.Process(pid)
                print("server rss=%.2fGB" %
                      (p.memory_info().rss / 1e9), flush=True)
            except Exception as exc:  # noqa: BLE001
                print("rss read failed", exc, flush=True)
        if hwnd:
            print("hwnd=", hwnd, "rect=", win32gui.GetWindowRect(hwnd))
    finally:
        if hwnd and win32gui.IsWindow(hwnd):
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        if tgt:
            try:
                tgt.wait(timeout=5)
            except Exception:  # noqa: BLE001
                tgt.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
