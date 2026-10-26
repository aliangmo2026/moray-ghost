"""VL 感知路由（v0.8.0）：POST /api/ghost/vl/analyze —— 服务端跑 vl_driver。

为什么收口在服务端（而不是 MCP 进程自己看屏幕）：
- 抓屏必须走 screen_mss 的单一 capture 线程（BitBlt 单实例纪律）；
- VL 模型（rapidocr / llama GGUF）进程内只加载一次，不随每个 MCP 客户端复制；
- 与 /screenshot 同级的只读语义：冻结期照常可用（不产生任何输入）。
鉴权由 app.main 的 token 中间件统一处理（非开放路径 → 必须 X-Ghost-Token）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.core import vl_driver

router = APIRouter(prefix="/api/ghost")


class VLAnalyzeBody(BaseModel):
    model_config = ConfigDict(extra="allow")  # 主仓多传字段直接忽略

    monitor: int = Field(default=1, ge=0)
    window: Optional[str] = Field(
        default=None, description="窗口标题子串 / http(s) URL（→ CDP 分支拒绝）")
    hwnd: Optional[int] = None
    backend: str = Field(default="auto",
                         description="auto|native|ocr|llama|mock")


@router.post("/vl/analyze")
def vl_analyze(body: VLAnalyzeBody) -> Dict[str, Any]:
    try:
        return vl_driver.analyze(monitor=body.monitor, window=body.window,
                                 hwnd=body.hwnd, backend=body.backend)
    except vl_driver.VLError as exc:
        content: Dict[str, Any] = {"ok": False, "error": exc.error,
                                   "detail": exc.detail}
        content.update(exc.data or {})
        return JSONResponse(status_code=exc.status, content=content)
