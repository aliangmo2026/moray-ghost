"""moray-ghost 启动入口：python run.py（也可用 start.ps1）。

v0.7.1 单实例门禁：抢命名互斥体 + 端口预检，任一被占直接报错退出——
本机永远只允许一个 Ghost 实例持有桌面输入。
"""
from __future__ import annotations

import os
import sys

import uvicorn

from app.core import config, single_instance


class _NonBlockingStream:
    """stdout/stderr 非阻塞包装（v0.7.2）：管道/控制台写满时丢弃该条输出，

    绝不阻塞写入方。实证（2026-10-26 栈转储取证）：父进程把服务 stdout 接进
    4KB 匿名管道又不读取时，日志写满管道 → 持 logging 锁的线程卡死在 flush →
    全体请求线程级联卡在 LOG.info → uvicorn 访问日志冻结事件循环 → 整机假死。
    本包装从 FD 层根除该死锁类：宁可丢日志，不可丢服务。
    """

    def __init__(self, stream):
        self._s = stream
        try:
            os.set_blocking(stream.fileno(), False)
        except Exception:  # noqa: BLE001  控制台句柄等不支持时保持原样
            pass

    def write(self, data):
        try:
            return self._s.write(data)
        except (BlockingIOError, OSError):
            return 0  # 缓冲满/管道断：丢弃本条，服务必须活着

    def flush(self):
        try:
            self._s.flush()
        except (BlockingIOError, OSError):
            pass

    def isatty(self):
        try:
            return self._s.isatty()
        except Exception:  # noqa: BLE001
            return False

    def fileno(self):
        return self._s.fileno()


def _install_nonblocking_console() -> None:
    try:
        sys.stdout = _NonBlockingStream(sys.stdout)
        sys.stderr = _NonBlockingStream(sys.stderr)
    except Exception:  # noqa: BLE001  包装失败不阻断启动
        pass


def main() -> int:
    try:
        single_instance.assert_single_instance(config.HOST, config.PORT)
    except single_instance.InstanceAlreadyRunning as exc:
        print("FATAL: " + str(exc), file=sys.stderr)
        print("FATAL: refusing to start a second instance.", file=sys.stderr)
        return 1
    _install_nonblocking_console()
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT,
                log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
