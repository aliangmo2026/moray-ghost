"""结构化审计落盘（v0.7.1 企业级可观测性）：data/ghost_audit.jsonl。

- 标准 JSON Lines：一行一个 JSON 对象，追加写、逐行 flush（进程崩溃不丢已写行）；
- 每条动作/急停/生命周期事件都带身份三元组——何时( ts )、谁(identity.client /
  agent_id)、用什么 Token(identity.token_fp 指纹)——满足大厂安全审计可追溯要求；
- 线程安全（单锁串行追加）；超限自动轮转（ghost_audit.1.jsonl）；
- 审计写入自身绝不允许打断业务：任何异常只降级为日志告警。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from . import authz, config

LOG = logging.getLogger("moray-ghost.audit")

_lock = threading.Lock()
ROTATE_BYTES = 10 * 1024 * 1024   # 单文件上限，超过轮转到 .1


def _rotate_if_needed(path: Path) -> None:
    try:
        if path.is_file() and path.stat().st_size >= ROTATE_BYTES:
            backup = path.with_name(path.name + ".1")
            if backup.exists():
                backup.unlink()
            path.rename(backup)
    except OSError as exc:
        LOG.warning("audit rotation failed (kept appending): %s", exc)


def write(event: str, **fields: Any) -> None:
    """追加一条审计记录。event 之外的字段由调用方按需补充。"""
    entry: Dict[str, Any] = {
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "event": str(event),
        "service": config.SERVICE,
        "version": config.VERSION,
        "env": config.ENV_MODE,
        "pid": os.getpid(),
        "thread": threading.current_thread().name,
    }
    identity = authz.current()
    if identity:
        entry["identity"] = {k: identity.get(k) for k in
                             ("source", "client", "agent_id",
                              "token_kind", "token_fp")}
    for key, value in fields.items():
        if key in entry:
            continue  # 骨架字段（ts/event/identity/...）不可被调用方覆盖
        entry[key] = value
    line = json.dumps(entry, ensure_ascii=False, default=str)
    with _lock:
        try:
            _rotate_if_needed(config.AUDIT_PATH)
            with open(config.AUDIT_PATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
        except OSError as exc:
            LOG.warning("audit write failed: %s", exc)


def tail(limit: int = 20) -> list:
    """读取最后 N 条（管理/自检用）。文件缺失返回空表。"""
    try:
        with open(config.AUDIT_PATH, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    out = []
    for raw in lines[-max(1, int(limit)):]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
    return out


def path_str() -> str:
    return str(config.AUDIT_PATH)
