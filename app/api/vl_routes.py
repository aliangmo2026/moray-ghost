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
    target: Optional[str] = Field(
        default=None,
        description="定向 grounding：只定位标签包含该文本的控件（llama）")
    hybrid: bool = Field(
        default=False,
        description="CV 图标粗筛 + 编号网格 + VL 选号（建议 7B）")


class VLDescribeBody(BaseModel):
    model_config = ConfigDict(extra="allow")  # 主仓多传字段直接忽略

    question: str = Field(..., description="自由问题，如：这张图里画了什么？")
    image_b64: Optional[str] = Field(
        default=None,
        description="直接喂图（base64，主仓自己的截图/裁剪）；缺省则抓屏/裁窗口")
    monitor: int = Field(default=1, ge=0)
    window: Optional[str] = None
    hwnd: Optional[int] = None
    backend: str = Field(default="auto", description="仅 llama 支持 describe")
    max_tokens: int = Field(default=512, ge=32, le=2048)


@router.post("/vl/describe")
def vl_describe(body: VLDescribeBody) -> Dict[str, Any]:
    """v0.9.1：让本地 VL 模型"读"图——自由问答（不做控件解析）。"""
    try:
        return vl_driver.describe(monitor=body.monitor, window=body.window,
                                  hwnd=body.hwnd, backend=body.backend,
                                  question=body.question,
                                  image_b64=body.image_b64,
                                  max_tokens=body.max_tokens)
    except vl_driver.VLError as exc:
        content: Dict[str, Any] = {"ok": False, "error": exc.error,
                                   "detail": exc.detail}
        content.update(exc.data or {})
        return JSONResponse(status_code=exc.status, content=content)


@router.post("/vl/analyze")
def vl_analyze(body: VLAnalyzeBody) -> Dict[str, Any]:
    try:
        return vl_driver.analyze(monitor=body.monitor, window=body.window,
                                 hwnd=body.hwnd, backend=body.backend,
                                 target=body.target, hybrid=body.hybrid)
    except vl_driver.VLError as exc:
        content: Dict[str, Any] = {"ok": False, "error": exc.error,
                                   "detail": exc.detail}
        content.update(exc.data or {})
        return JSONResponse(status_code=exc.status, content=content)
