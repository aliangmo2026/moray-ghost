"""Moray-Ghost 技能代理入口：把平台请求转发到用户本机的 Ghost(8030)。

用法：
    set GHOST_LOCAL_URL=http://127.0.0.1:8030   （可选，默认即本机）
    set GHOST_TOKEN=<Ghost 启动日志里的 token>   （可选，本地 Ghost 未关鉴权时必填）
    python main.py                                （监听 0.0.0.0:8080）

行为：
- 收到的任意路径原样转发（GET/POST/DELETE 等），响应字节级透传。
- 鉴权头改写：Authorization: Bearer xxx 会改写为 X-Ghost-Token: xxx；
  若请求已带 X-Ghost-Token 则原样保留。配置了 GHOST_TOKEN 且请求未带
  任何凭据时，用环境变量补齐。
- 不在任何源码/示例中硬编码可用凭据；token 只从环境变量读取。
"""
from __future__ import annotations

import os
from typing import Optional
from urllib.parse import urlsplit

import requests
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

DEFAULT_LOCAL = "http://127.0.0.1:8030"
UPSTREAM_TIMEOUT = 30.0

app = FastAPI(title="Moray-Ghost Skill Proxy", version="0.15.0")


def _upstream_base() -> str:
    url = os.environ.get("GHOST_LOCAL_URL", DEFAULT_LOCAL).rstrip("/")
    scheme = urlsplit(url).scheme
    if scheme not in ("http", "https"):
        raise ValueError(f"GHOST_LOCAL_URL 必须是 http/https: {url!r}")
    return url


def _rewrite_auth(headers: dict) -> dict:
    """Bearer → X-Ghost-Token；都没有时用 GHOST_TOKEN 环境变量补齐。"""
    out = {k: v for k, v in headers.items()
           if k.lower() not in ("host", "content-length", "x-ghost-token",
                                "authorization", "connection")}
    auth = headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        out["X-Ghost-Token"] = auth[7:].strip()
    elif "x-ghost-token" in headers:
        out["X-Ghost-Token"] = headers["x-ghost-token"]
    elif os.environ.get("GHOST_TOKEN"):
        out["X-Ghost-Token"] = os.environ["GHOST_TOKEN"]
    return out


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(path: str, request: Request) -> Response:
    try:
        base = _upstream_base()
    except ValueError as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})
    body = await request.body()
    url = f"{base}/{path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    try:
        resp = requests.request(
            method=request.method,
            url=url,
            headers=_rewrite_auth(dict(request.headers)),
            data=body if body else None,
            timeout=UPSTREAM_TIMEOUT,
        )
    except requests.RequestException as exc:
        return JSONResponse(
            status_code=502,
            content={"error": f"upstream unreachable: {exc}",
                     "hint": "确认本机 Ghost 已启动且 GHOST_LOCAL_URL 正确"})
    skip = {"content-encoding", "transfer-encoding", "content-length",
            "connection"}
    return Response(content=resp.content, status_code=resp.status_code,
                    headers={k: v for k, v in resp.headers.items()
                             if k.lower() not in skip})


@app.get("/__proxy/health")
async def proxy_health() -> JSONResponse:
    """代理自身健康检查（不经过 Ghost）。"""
    return JSONResponse(content={
        "status": "ok",
        "upstream": _safe_upstream(),
    })


def _safe_upstream() -> Optional[str]:
    try:
        return _upstream_base()
    except ValueError:
        return None


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0",
                port=int(os.environ.get("PROXY_PORT", "8080")))
