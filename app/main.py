"""moray-ghost v0.2 入口：token 中间件 + lifespan + 路由装配。

导入顺序红线：app.core.screen_mss 在 import 时即完成 DPI 感知设置，
必须发生在 import mss / import pyautogui 之前（screen_mss 与 backend
各自在模块头部保证该顺序，此处先 import screen_mss 以固定先后）。
"""
from __future__ import annotations

import logging
import logging.handlers
from contextlib import asynccontextmanager
from typing import Awaitable, Callable, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core import screen_mss  # 先于其他 app 模块：尽早设置 DPI
from app.core import auth, backend, config, macro_store, task_queue
from app.api import browser_routes, dashboard_routes, environment_routes
from app.api import macro_routes, routes, task_routes, window_routes

config.ensure_dirs()
config.ensure_token()


def _setup_logging() -> logging.Logger:
    global _FILE_HANDLER
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
    _FILE_HANDLER = file_handler
    return logger


_FILE_HANDLER: Optional[logging.Handler] = None


LOG = _setup_logging()


def _setup_access_log() -> None:
    """uvicorn.access → logs/access.log（独立轮转，不挤占应用日志）。

    HTTP 层全量留痕（来源地址/方法/路径/状态码）：对 /execute_action、/task、
    /macro 重放的每一次调用都可事后归因。必须在 lifespan 里挂 handler——
    uvicorn 的 dictConfig 在 run() 期执行，会重置 import 期挂上去的 handler。
    """
    handler = logging.handlers.RotatingFileHandler(
        config.LOG_DIR / "access.log", maxBytes=2_000_000, backupCount=2,
        encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    access = logging.getLogger("uvicorn.access")
    if handler not in access.handlers:
        access.addHandler(handler)


@asynccontextmanager
async def lifespan(_: FastAPI):
    macro_store.init_db()
    backend.release_all_modifiers()  # 启动时清一次可能残留的修饰键（进程曾中途被杀等）
    task_queue.start_dispatcher()    # v0.5：任务 FIFO 单工作线程（串行派发）
    _setup_access_log()
    LOG.info("moray-ghost v%s listening on http://%s:%s (dpi_aware=%s)",
             config.VERSION, config.HOST, config.PORT, screen_mss.DPI_AWARE)
    LOG.info("fail_safe_enabled=true token_required=true intent_token=%s",
             config.INTENT_TOKEN_ENABLED)
    LOG.info("db=%s screenshots=%s", config.DB_PATH, config.SCREENSHOT_DIR)
    yield
    task_queue.stop_dispatcher()
    LOG.info("moray-ghost stopped")


app = FastAPI(title="moray-ghost", version=config.VERSION, lifespan=lifespan)

_OPEN_PATHS = {"/health", "/screenshot", "/docs", "/redoc", "/openapi.json", "/favicon.ico"}


_GHOST_PREFIX = "/api/ghost"
# 探活/监控放行：health、screenshot（v0.2 起）与 dashboard 只读状态（v0.5）、
# 驾驶舱动作日志与缩略图（v0.6 M3，与 /screenshot 同级：本机只读、信息量相当）
_OPEN_PATHS = {_GHOST_PREFIX + "/health", _GHOST_PREFIX + "/screenshot",
               _GHOST_PREFIX + "/dashboard/state",
               _GHOST_PREFIX + "/actions/recent"}
_THUMB_PREFIX = _GHOST_PREFIX + "/actions/thumb/"


def _is_protected(path: str) -> bool:
    """ghost 全部接口走 /api/ghost 前缀；仅探活/只读面板数据放行。"""
    if not path.startswith(_GHOST_PREFIX + "/"):
        return False
    if path in _OPEN_PATHS or path.startswith(_THUMB_PREFIX):
        return False
    return True


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
app.include_router(browser_routes.router)
app.include_router(dashboard_routes.router)
app.include_router(dashboard_routes.api_router)
