"""全局急停热键（v0.7.1 物理级绝对控制）：Ctrl+Alt+Q → 立刻进程级退出。

网页大红按钮点不到时的物理逃生口：`keyboard` 库注册系统级低级键盘钩子
（不抑制组合键、不影响其他程序），按下即：
1. 强制释放全部按键与鼠标键（进程死后 OS 不会替你补 key-up——按住态必须先清）；
2. 审计落盘 estop_hotkey 事件（同步写完再退，证据不丢）；
3. os._exit(1)——跳过一切 finally/atexit，绝对服从物理断电。

热键注册失败只告警不阻断服务（HTTP /emergency_stop 仍是主急停通道）。
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional

from . import audit_sink, config, janitor

LOG = logging.getLogger("moray-ghost.estop")

_lock = threading.Lock()
_hotkey: Optional[str] = None
_started = False


def _trigger() -> None:
    """热键回调（keyboard 钩子线程）：清场 → 审计 → 立刻退出，绝不返回。"""
    try:
        janitor.release_input("estop_hotkey")
    except Exception:  # noqa: BLE001  急停路径上任何异常都不能阻止退出
        pass
    try:
        audit_sink.write("estop_hotkey", hotkey=str(_hotkey),
                         detail="global estop hotkey pressed; "
                                "releasing input and exiting process")
    except Exception:  # noqa: BLE001
        pass
    os._exit(1)


def start(combo: Optional[str] = None) -> bool:
    """注册系统级急停热键。返回是否注册成功（失败不阻断服务）。"""
    global _started, _hotkey
    with _lock:
        if _started:
            return True
        combo = str(combo or config.ESTOP_HOTKEY).strip().lower()
        if not combo:
            LOG.warning("estop hotkey disabled (empty combo)")
            return False
        try:
            import keyboard
            # suppress=False：不吞组合键，其他程序对该热键的响应不受影响。
            keyboard.add_hotkey(combo, _trigger, suppress=False)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("estop hotkey registration failed (%s): %s", combo, exc)
            return False
        _hotkey = combo
        _started = True
        LOG.info("estop hotkey armed: %s (press to kill this process immediately)",
                 combo)
        return True


def stop() -> None:
    """注销热键（lifespan 收尾用；正常退出时才需要，急停退出靠进程消亡）。"""
    global _started
    with _lock:
        if not _started:
            return
        _started = False
        try:
            import keyboard
            keyboard.unhook_all()
        except Exception:  # noqa: BLE001
            pass
        LOG.info("estop hotkey disarmed")


def armed() -> bool:
    return _started


def combo() -> Optional[str]:
    return _hotkey
