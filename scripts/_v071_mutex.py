"""v0.7.1 互斥体探针（test_v071_security 专用辅助脚本）。

用法：
  python scripts/_v071_mutex.py hold    # 抢占互斥体并一直持有（打印 READY 后休眠）
  python scripts/_v071_mutex.py probe   # 尝试抢占：成功→exit 0(OK)，被占→exit 3(BUSY)

hold 模式模拟"另一个 Ghost 实例正在运行"，且不监听任何端口——
让 run.py 的拒绝可以归因到互斥体而不是端口门禁。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import single_instance  # noqa: E402


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "probe"
    if mode == "hold":
        # 持有者视角：抢不到就重试（探针进程会瞬时抢锁又释放，不能因此放弃）
        while not single_instance.acquire():
            time.sleep(0.2)
        print("READY", flush=True)
        while True:
            time.sleep(1)  # 持锁直到被父进程杀掉（内核随进程消亡释放互斥体）
    ok = single_instance.acquire()
    if ok:
        print("OK", flush=True)
        return 0
    print("BUSY", flush=True)
    return 3


if __name__ == "__main__":
    sys.exit(main())
