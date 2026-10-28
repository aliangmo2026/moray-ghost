"""异常线程清道夫（v0.7.1）：失败/取消/崩溃/急停后的强制清场。

职责（统一入口 sweep()）：
1. 强制释放全部修饰键与鼠标键——任何异常路径都不把"按住"状态泄漏给系统；
2. 残留队列核对（冻结清队由 task_queue.freeze 负责，这里只做核对留痕）；
3. 线程盘点：与启动基线比对，除常驻 worker 与异步线程池外出现的任何
   杂散线程都写进审计，可事后追查"线程失控"；
4. 全程绝不抛异常（清道夫自己崩了才是事故）。
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional, Set

from . import audit_sink

LOG = logging.getLogger("moray-ghost.janitor")

# 按需启动的常驻 worker 前缀（晚于基线创建属正常，不判杂散）。
# "pynput." = v0.15 阶段三技能录制器的全局监听线程（仅录制会话期间存活）。
# "moray-ghost-skill" = 技能录制帧采集线程（与钩子线程解耦，仅会话期间存活）。
LONG_LIVED_WORKERS = ("moray-ghost-capture", "task-dispatcher",
                      "moray-ghost-browser", "pynput.", "moray-ghost-skill",
                      "moray-ghost-ocr-warm")
# anyio/asyncio 线程池前缀（按请求动态创建，属正常）。
_POOL_PREFIXES = ("AnyIO", "asyncio_")

_baseline: Set[str] = set()


def release_input(reason: str) -> Dict[str, Any]:
    """强制释放所有按键与鼠标键（幂等；对未按下的键是无害空操作）。"""
    from . import backend  # 延迟 import：backend 依赖 pyautogui，保持模块轻量
    out: Dict[str, Any] = {"reason": reason,
                           "modifiers_released": False,
                           "pointer_released": False}
    try:
        backend.release_all_modifiers()
        out["modifiers_released"] = True
    except Exception as exc:  # noqa: BLE001  清道夫绝不抛异常
        LOG.warning("release_all_modifiers failed (%s): %s", reason, exc)
    try:
        backend.release_pointer_buttons()
        out["pointer_released"] = True
    except Exception as exc:  # noqa: BLE001
        LOG.warning("release_pointer_buttons failed (%s): %s", reason, exc)
    return out


def live_threads() -> List[str]:
    return sorted(t.name for t in threading.enumerate())


def establish_baseline() -> List[str]:
    """启动完成后固化线程基线（lifespan 里调用一次）。"""
    global _baseline
    _baseline = set(live_threads())
    return sorted(_baseline)


def strays() -> List[str]:
    """基线之外的杂散线程（清道夫视角的"失控线程"名单）。"""
    out = []
    for name in live_threads():
        if name in _baseline:
            continue
        if name.startswith(LONG_LIVED_WORKERS) or name.startswith(_POOL_PREFIXES):
            continue
        out.append(name)
    return out


def sweep(reason: str, task_id: Optional[str] = None,
          extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """任务失败/取消/崩溃/急停的统一清场：释放输入 + 线程盘点 + 审计留痕。"""
    released = release_input(reason)
    report: Dict[str, Any] = {"reason": reason,
                              "input": released,
                              "threads": live_threads(),
                              "stray_threads": strays()}
    if task_id:
        report["task_id"] = task_id
    if extra:
        report.update(extra)
    try:
        audit_sink.write("sweep", **report)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("sweep audit write failed: %s", exc)
    if report["stray_threads"]:
        LOG.warning("stray threads after sweep(%s): %s", reason,
                    report["stray_threads"])
    else:
        LOG.info("sweep(%s): input released, no stray threads", reason)
    return report
