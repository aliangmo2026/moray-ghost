"""mss 截图——v0.15 阶段二起真身迁至 app/platforms/windows.py，本模块只做再导出。

保留本路径是为了零回归：所有 `from app.core import screen_mss` 的旧引用拿到
的就是平台模块里的同一个函数对象（单一模块状态——抓屏线程/健康统计/修剪
计数器不会因双模块各一份而分裂）。新代码请用 app.platforms.get_screen()。
"""
from __future__ import annotations

from app.platforms.windows import (  # noqa: F402  顺序红线由平台模块内部保证
    DPI_AWARE,
    DPI_MODE,
    b64_of,
    dpi_status,
    encode,
    grab,
    grab_health,
    monitor_at,
    monitor_rect,
    monitors,
    new_shot_path,
    save_audit,
    save_bytes,
    virtual_screen,
)

__all__ = [
    "DPI_AWARE", "DPI_MODE", "b64_of", "dpi_status", "encode", "grab",
    "grab_health", "monitor_at", "monitor_rect", "monitors", "new_shot_path",
    "save_audit", "save_bytes", "virtual_screen",
]
