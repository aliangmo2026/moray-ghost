"""环境状态接口：GET /environment（鉴权由 app.main 的 token 中间件统一强校验）。"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter

from app.core import environment

router = APIRouter(prefix="/api/ghost")


@router.get("/environment")
def environment_state() -> Dict[str, Any]:
    return environment.snapshot()
