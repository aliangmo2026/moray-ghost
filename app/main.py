"""moray-ghost v0.2 入口：token 中间件 + lifespan + 路由装配。

导入顺序红线：app.core.screen_mss 在 import 时即完成 DPI 感知设置，
必须发生在 import mss / import pyautogui 之前（screen_mss 与 backend
各自在模块头部保证该顺序，此处先 import screen_mss 以固定先后）。
"""
from __future__ import annotations

import logging
import logging.handlers
import os
from contextlib import asynccontextmanager
from typing import Awaitable, Callable, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core import screen_mss  # 先于其他 app 模块：尽早设置 DPI
from app.core import (auth, authz, audit_sink, backend, config, estop,
                      janitor, macro_store, single_instance, task_queue)
from app.api import browser_routes, dashboard_routes, draw_routes
from app.api import environment_routes, macro_routes, routes, task_routes
from app.api import vl_routes, window_routes

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
_STACK_FH = None  # v0.7.2 栈转储文件句柄（GHOST_STACKDUMP_INTERVAL 门控）


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


def _spawn_watchdog() -> Optional[int]:
    """v0.7.2 独立看门狗：监控本进程 PID，被强杀后由它物理释放修饰键。

    与 Ghost 无共享句柄、DETACHED 独立进程——任务管理器连我们一起杀也杀不到它。
    拉起失败只告警不阻断服务（HTTP/热键急停仍是主通道）。
    """
    if not config.WATCHDOG_ENABLED:
        return None
    try:
        import os
        import subprocess
        import sys
        script = str(config.BASE_DIR / "scripts" / "ghost_watchdog.py")
        try:
            import psutil
            ctime = str(psutil.Process().create_time())
        except Exception:  # noqa: BLE001
            ctime = "0"
        proc = subprocess.Popen(
            [sys.executable, script, "--pid", str(os.getpid()),
             "--ctime", ctime],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        global _WATCHDOG_PROC
        _WATCHDOG_PROC = proc  # 持有引用，防 Popen 析构警告
        LOG.info("watchdog spawned: pid=%s watching pid=%s",
                 proc.pid, os.getpid())
        return proc.pid
    except Exception as exc:  # noqa: BLE001
        LOG.warning("watchdog spawn failed (non-fatal): %s", exc)
        return None


_WATCHDOG_PROC = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    # v0.7.1 单实例门禁（防御层）：run.py 已在进程入口抢占互斥体；此处兜住
    # `uvicorn app.main:app` 直接启动的旁路——互斥体被占即启动失败。
    if not single_instance.acquire():
        raise RuntimeError(
            "another moray-ghost instance is already running "
            "(single-instance mutex " + single_instance.MUTEX_NAME + " held)")
    _spawn_watchdog()          # v0.7.2 越早越好：进程强杀后的物理自救通道
    # v0.7.2 挂死取证（env 门控，运维可开）：周期性全进程线程栈转储到
    # logs/stacks.log——任何"卡死"都能事后定位到确切调用栈。
    dump_iv = float(os.getenv("GHOST_STACKDUMP_INTERVAL", "0") or 0)
    if dump_iv > 0:
        import faulthandler
        global _STACK_FH
        _STACK_FH = open(config.LOG_DIR / "stacks.log", "a",
                         encoding="utf-8", errors="replace")
        faulthandler.dump_traceback_later(dump_iv, repeat=True,
                                          file=_STACK_FH)
        LOG.info("stackdump enabled: every %ss -> logs/stacks.log", dump_iv)
    macro_store.init_db()
    backend.release_all_modifiers()  # 启动时清一次可能残留的修饰键（进程曾中途被杀等）
    task_queue.start_dispatcher()    # v0.5：任务 FIFO 单工作线程（串行派发）
    _setup_access_log()
    estop.start()                    # v0.7.1 全局急停热键（Ctrl+Alt+Q → 进程退出）
    janitor.establish_baseline()     # v0.7.1 线程基线（清道夫据此识别杂散线程）
    audit_sink.write("service_start", host=config.HOST, port=config.PORT,
                     mode="production" if config.IS_PRODUCTION else "development",
                     estop_hotkey=estop.combo())
    LOG.info("moray-ghost v%s listening on http://%s:%s (dpi_aware=%s env=%s)",
             config.VERSION, config.HOST, config.PORT, screen_mss.DPI_AWARE,
             config.ENV_MODE)
    LOG.info("fail_safe_enabled=true token_required=true intent_token=%s "
             "estop_hotkey=%s", config.INTENT_TOKEN_ENABLED, estop.combo())
    LOG.info("db=%s screenshots=%s audit=%s", config.DB_PATH,
             config.SCREENSHOT_DIR, audit_sink.path_str())
    yield
    estop.stop()
    task_queue.stop_dispatcher()
    janitor.release_input("shutdown")  # 退出前绝不把按键状态泄漏给系统
    audit_sink.write("service_stop")
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
            info = auth.verify(request.headers.get(auth.HEADER))
        except auth.Unauthorized as exc:
            LOG.warning("401 %s %s: %s", request.method, request.url.path, exc)
            return JSONResponse(status_code=401, content={
                "ok": False, "error": "UNAUTHORIZED", "detail": str(exc)})
        # v0.7.1 审计身份：token 校验通过才把身份上下文交给下游路由——
        # 动作路由据此写 authz ctx（生产环境输入源过滤 + JSONL 审计归因）。
        client = request.client
        request.state.ghost_ctx = {
            "source": "http_api",
            "client": (f"{client.host}:{client.port}" if client else None),
            "token_kind": info.get("kind"),
            "token_fp": authz.token_fingerprint(request.headers.get(auth.HEADER)),
            "agent_id": request.headers.get("X-Agent-Id"),
        }
    return await call_next(request)


app.include_router(routes.router)
app.include_router(macro_routes.router)
app.include_router(window_routes.router)
app.include_router(environment_routes.router)
app.include_router(task_routes.router)
app.include_router(browser_routes.router)
app.include_router(dashboard_routes.router)
app.include_router(dashboard_routes.api_router)
app.include_router(vl_routes.router)   # v0.8.0 /vl/analyze（需 token，只读）
app.include_router(draw_routes.router)  # v0.9.1 /draw_trajectory（复合绘制动作）
