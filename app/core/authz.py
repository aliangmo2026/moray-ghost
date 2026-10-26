"""调用来源与身份上下文（v0.7.1）：生产环境输入源过滤 + 审计身份归因。

ctx 由三处服务端代码显式写入（用户 payload 永远碰不到它）：
- token 中间件校验通过后写入 request.state.ghost_ctx（身份：来源地址/token 指纹/agent）；
- 动作路由在处理请求时把它落到本线程 ContextVar（source=http_api）；
- 任务调度线程在派发前写入任务准入时捕获的快照（source=task_queue）。

GHOST_ENV=production 时，controller/browser 的一切动作入口都要求 ctx.source
来自上述合法调用链——进程内裸调 run_action 等旁路一律 403 SOURCE_DENIED。
开发环境（默认 dev）不启用该过滤，旧测试与本地联调行为不变。
"""
from __future__ import annotations

import contextvars
import hashlib
from typing import Any, Dict, Optional

_ctx: "contextvars.ContextVar[Optional[Dict[str, Any]]]" = contextvars.ContextVar(
    "ghost_call_ctx", default=None)

# 生产环境认可的动作来源：带 Token 的 HTTP 调用链本身，或由它派发的任务队列。
ALLOWED_SOURCES = ("http_api", "task_queue")


class SourceDenied(Exception):
    """生产环境：动作不在合法调用链上（controller层转成 403 SOURCE_DENIED）。"""


def token_fingerprint(token: Optional[str]) -> Optional[str]:
    """token 的不可逆指纹（sha256 前 12 位 hex）。

    审计要求"查到用什么 Token 触发"——落指纹可归因比对，绝不落 token 原文
    （源码/示例/日志中出现可用凭据是红线）。
    """
    if not token:
        return None
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()[:12]


def set_ctx(ctx: Optional[Dict[str, Any]]) -> None:
    _ctx.set(dict(ctx) if ctx else None)


def bind_request(request: Any) -> None:
    """把 FastAPI Request 上的身份上下文落到本线程。

    request 为 None（进程内直调路由函数等旁路）→ 清空上下文：
    生产环境因此拒绝该动作（默认拒绝），开发环境保持旧行为。
    显式覆盖（含 None）：绝不让同一线程上一请求的残留身份串进本次动作。
    """
    state = getattr(request, "state", None)
    set_ctx(getattr(state, "ghost_ctx", None))


def current() -> Dict[str, Any]:
    return _ctx.get() or {}


def snapshot() -> Dict[str, Any]:
    """当前上下文快照（任务准入时捕获，派发时回放）。"""
    return dict(_ctx.get() or {})


def assert_action_source() -> None:
    """生产环境输入源过滤：非合法调用链 → SourceDenied（零副作用，先于一切校验）。"""
    from . import config
    if not config.IS_PRODUCTION:
        return
    source = (_ctx.get() or {}).get("source")
    if source not in ALLOWED_SOURCES:
        raise SourceDenied(
            "production mode: action not originating from an authenticated "
            "HTTP request chain (source=" + repr(source)
            + "; allowed=" + repr(ALLOWED_SOURCES) + ")")
