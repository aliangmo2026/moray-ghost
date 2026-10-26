"""moray-ghost 启动入口：python run.py（也可用 start.ps1）。

v0.7.1 单实例门禁：抢命名互斥体 + 端口预检，任一被占直接报错退出——
本机永远只允许一个 Ghost 实例持有桌面输入。
"""
from __future__ import annotations

import sys

import uvicorn

from app.core import config, single_instance


def main() -> int:
    try:
        single_instance.assert_single_instance(config.HOST, config.PORT)
    except single_instance.InstanceAlreadyRunning as exc:
        print("FATAL: " + str(exc), file=sys.stderr)
        print("FATAL: refusing to start a second instance.", file=sys.stderr)
        return 1
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT,
                log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
