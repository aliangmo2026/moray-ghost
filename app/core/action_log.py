"""驾驶舱动作日志（v0.6 M3）+ 结构化审计落盘（v0.7.1）。

- native 动作由 controller.run_action 成功/失败/异常三路径入账；
- cdp 动作由 browser 路由入账（channel=cdp）；
- 冻结期被拒动作（503）同样入账——"它在干什么、为什么没成"全程可见；
- 与任务注册表同策略：内存态，进程重启即清；
- v0.7.1：每条入账同时写 data/ghost_audit.jsonl（JSON Lines，含身份三元组
  ——何时/谁/哪个 token 指纹），供安全审计永久追溯（磁盘态，重启保留）。
"""
from __future__ import annotations

import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import audit_sink
from .config import SCREENSHOT_DIR

_buf: "deque[Dict[str, Any]]" = deque(maxlen=200)
_lock = threading.Lock()


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def record(channel: str, action: str, detail: Optional[Dict[str, Any]],
           ok: bool, status: int, thumb: Optional[str] = None) -> None:
    """入账一条动作（内存环 + JSONL 落盘）。thumb 只存 basename（完整路径不出服务）。"""
    thumb_name = Path(thumb).name if thumb else None
    entry = {
        "ts": _now(),
        "channel": channel,
        "action": action,
        "detail": detail or {},
        "ok": bool(ok),
        "status": int(status),
        "thumb": thumb_name,
    }
    with _lock:
        _buf.appendleft(entry)
    audit_sink.write("action", channel=channel, action=action,
                     ok=bool(ok), status=int(status),
                     detail=entry["detail"], thumb=thumb_name)


def recent(limit: int = 50) -> List[Dict[str, Any]]:
    """最新动作在前。"""
    with _lock:
        return [dict(e) for e in list(_buf)[: max(1, min(int(limit), 200))]]


def resolve_thumb(name: str) -> Optional[Path]:
    """缩略图白名单：basename 不含分隔符/..，解析后必须仍在 SCREENSHOT_DIR 内。"""
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    try:
        p = (SCREENSHOT_DIR / name).resolve()
    except Exception:  # noqa: BLE001
        return None
    if p.parent != SCREENSHOT_DIR.resolve():
        return None
    if not p.is_file():
        return None
    return p
