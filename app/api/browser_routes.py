"""CDP 浏览器路由（v0.6 M2）：POST /browser/action、GET /browser/tabs。

安全管道对齐（防呆不绕过）：
- 冻结门禁：emergency stop 期间与 execute_action 同级 503 GHOST_FROZEN；
- 串行：与原生动作共用 controller.ACTION_LOCK（审计与状态一致性）；
- 审计：每次调用记 input_audit channel=cdp（与原生同一 grep 关键字）+ 动作日志；
- 会话边界：只操作 CDP 会话内已有 tab，ghost 不自动拉起浏览器。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.core import action_log, authz, browser_driver, task_queue
from app.core import controller

LOG = logging.getLogger("moray-ghost.browser")

router = APIRouter(prefix="/api/ghost")


class BrowserActionBody(BaseModel):
    model_config = ConfigDict(extra="allow")

    op: str = Field(..., pattern="^(click|dblclick|hover|fill|press|scroll|drag|tabs|close_tab)$")
    tab: Optional[Dict[str, Any]] = Field(default=None,
                                          description='{"url_contains": "..."} 约束操作的 tab')
    selector: Optional[Dict[str, Any]] = Field(
        default=None,
        description='{"by": "css|role|text|placeholder|label|testid", "value": "...", "nth": 0}')
    text: Optional[str] = Field(default=None, description="fill 的文本 / press 的按键名")
    dx: Optional[int] = None
    dy: Optional[int] = None
    points: Optional[List[List[float]]] = Field(default=None,
                                                 description="drag 轨迹（页面 CSS px）")


def _err(exc: browser_driver.BrowserError) -> JSONResponse:
    content: Dict[str, Any] = {"ok": False, "error": exc.error, "detail": exc.detail}
    content.update(exc.data)
    return JSONResponse(status_code=exc.status, content=content)


@router.get("/browser/tabs")
def list_tabs() -> Dict[str, Any]:
    """列出已打开的 tab（只读）。未连上调试端口 → 503 BROWSER_NOT_CONNECTED。"""
    try:
        return browser_driver.tabs()
    except browser_driver.BrowserError as exc:
        return _err(exc)


@router.post("/browser/action")
def browser_action(body: BrowserActionBody, request: Request) -> Dict[str, Any]:
    # v0.7.1 身份上下文 + 生产环境输入源过滤（CDP 通道与原生通道同一门禁）
    authz.bind_request(request)
    try:
        authz.assert_action_source()
    except authz.SourceDenied as exc:
        action_log.record("cdp", body.op, _detail_of(body), False, 403)
        return JSONResponse(status_code=403, content={
            "ok": False, "error": "SOURCE_DENIED", "detail": str(exc)})
    # 冻结门禁（与 execute_action 同级）；被拒也入账（驾驶舱全程可见）
    if task_queue.is_frozen():
        action_log.record("cdp", body.op, _detail_of(body), False, 503)
        return JSONResponse(status_code=503, content={
            "ok": False, "error": "GHOST_FROZEN",
            "detail": "ghost is frozen by emergency stop at "
                      + str(task_queue.frozen_at())})

    LOG.info("input_audit channel=cdp op=%s tab=%s selector=%s",
             body.op,
             (body.tab or {}).get("url_contains"),
             (body.selector or {}).get("value"))

    try:
        with controller.ACTION_LOCK:
            out = browser_driver.run_op(body.model_dump())
    except browser_driver.BrowserError as exc:
        action_log.record("cdp", body.op, _detail_of(body), False, exc.status)
        return _err(exc)
    out["elapsed_ms"] = out.get("elapsed_ms") or None
    action_log.record("cdp", body.op,
                      {"tab": out.get("tab_url"),
                       "selector": (body.selector or {}).get("value"),
                       "dx": body.dx, "dy": body.dy},
                      True, 200, thumb=out.get("after_screenshot_path"))
    return out


def _detail_of(body: BrowserActionBody) -> Dict[str, Any]:
    return {"tab": (body.tab or {}).get("url_contains"),
            "selector": (body.selector or {}).get("value"),
            "op": body.op}
