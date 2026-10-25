"""moray-ghost v0.2 入口：token 中间件 + lifespan + 路由装配。

导入顺序红线：app.core.screen_mss 在 import 时即完成 DPI 感知设置，
必须发生在 import mss / import pyautogui 之前（screen_mss 与 backend
各自在模块头部保证该顺序，此处先 import screen_mss 以固定先后）。
"""
from __future__ import annotations

import logging
import logging.handlers
from contextlib import asynccontextmanager
from typing import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core import screen_mss  # 先于其他 app 模块：尽早设置 DPI
from app.core import auth, backend, config, macro_store
from app.api import environment_routes, macro_routes, routes, task_routes, window_routes

config.ensure_dirs()
config.ensure_token()


def _setup_logging() -> logging.Logger:
    logger = logging.getLogger("moray-ghost")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    logger.addHandler(stream)
    file_handler = logging.handlers.RotatingFileHandler(
        config.LOG_DIR / "ghost.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
    return logger


LOG = _setup_logging()


@asynccontextmanager
async def lifespan(_: FastAPI):
    macro_store.init_db()
    backend.release_all_modifiers()  # 启动时清一次可能残留的修饰键（进程曾中途被杀等）
    LOG.info("moray-ghost v%s listening on http://%s:%s (dpi_aware=%s)",
             config.VERSION, config.HOST, config.PORT, screen_mss.DPI_AWARE)
    LOG.info("fail_safe_enabled=true token_required=true intent_token=%s",
             config.INTENT_TOKEN_ENABLED)
    LOG.info("db=%s screenshots=%s", config.DB_PATH, config.SCREENSHOT_DIR)
    yield
    LOG.info("moray-ghost stopped")


app = FastAPI(title="moray-ghost", version=config.VERSION, lifespan=lifespan)

_OPEN_PATHS = {"/health", "/screenshot", "/docs", "/redoc", "/openapi.json", "/favicon.ico"}


def _is_protected(path: str) -> bool:
    """执行动作类接口强校验；/health 与 /screenshot 探活放行。"""
    return ((path == "/execute_action" or path.startswith("/macro")
             or path.startswith("/window") or path.startswith("/environment")
             or path.startswith("/task")) and path not in _OPEN_PATHS)


@app.middleware("http")
async def token_guard(request: Request, call_next: Callable[[Request], Awaitable]):
    if _is_protected(request.url.path):
        try:
            auth.verify(request.headers.get(auth.HEADER))
        except auth.Unauthorized as exc:
            LOG.warning("401 %s %s: %s", request.method, request.url.path, exc)
            return JSONResponse(status_code=401, content={
                "ok": False, "error": "UNAUTHORIZED", "detail": str(exc)})
    return await call_next(request)


app.include_router(routes.router)
app.include_router(macro_routes.router)
app.include_router(window_routes.router)
app.include_router(environment_routes.router)
app.include_router(task_routes.router)
