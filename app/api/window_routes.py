"""窗口枚举接口：GET /window/list（鉴权由 app.main 的 token 中间件统一强校验）。"""
from __future__ import annotations

import time
from typing import Any, Dict

from fastapi import APIRouter

from app.core import window_locator

router = APIRouter()


@router.get("/window/list")
def list_windows() -> Dict[str, Any]:
    t0 = time.perf_counter()
    windows = window_locator.list_windows()
    return {
        "ok": True,
        "windows": windows,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
    }
