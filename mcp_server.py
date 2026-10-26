"""moray-ghost MCP Server（v0.8.0 · Stdio）：把 Ghost 接进任意 AI Agent。

启动（由 Agent 侧拉起，stdio JSON-RPC）：
    python mcp_server.py
身份环境（启动时注入，**tool 参数永远无法覆盖白名单**）：
    GHOST_TOKEN        静态 token（env > .env > 首启自动生成，与 SDK 同语义）
    GHOST_AGENT_ID     可选，X-Agent-Id 队列归属
    GHOST_CAPABILITIES JSON 数组，如 ["notepad.exe"] —— 所有输入类调用的
                       capabilities.allowed_windows（per-call 不可改宽）

铁律：
- stdout 只承载 MCP 协议帧——本进程任何代码绝不 print 到 stdout（诊断一律 stderr）；
- 所有动作经 HTTP + X-Ghost-Token 回到 Ghost 服务端安全管道（令牌 / 能力令牌 /
  黑名单 / 熔断 / 硬超时 / 冻结 / 审计 100% 生效），MCP 层只做协议翻译与路由；
- 凭据只从环境读取，不落盘、不打印、不进错误详情。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.core import config, vl_driver  # noqa: E402

log = logging.getLogger("ghost-mcp")
if not log.handlers:
    _h = logging.StreamHandler(sys.stderr)  # 诊断只走 stderr，绝不碰协议通道
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

BASE = "http://127.0.0.1:8030/api/ghost"
TOKEN = config.ensure_token()
AGENT_ID = os.getenv("GHOST_AGENT_ID", "").strip() or None

_raw_caps = os.getenv("GHOST_CAPABILITIES", "").strip()
try:
    CAPABILITIES = json.loads(_raw_caps) if _raw_caps else []
except ValueError:
    log.warning("GHOST_CAPABILITIES is not valid JSON; using []")
    CAPABILITIES = []
if not isinstance(CAPABILITIES, list):
    CAPABILITIES = []

HEADERS: Dict[str, str] = {"X-Ghost-Token": TOKEN}
if AGENT_ID:
    HEADERS["X-Agent-Id"] = AGENT_ID


def _caps_field() -> Dict[str, Any]:
    """输入类调用的能力白名单：只来自启动环境，调用方无法改宽。"""
    if CAPABILITIES:
        return {"capabilities": {"allowed_windows": list(CAPABILITIES)}}
    return {}


def _post(path: str, payload: Dict[str, Any],
          timeout: float = 60.0) -> Dict[str, Any]:
    try:
        r = requests.post(f"http://127.0.0.1:8030/api/ghost{path}",
                          headers=HEADERS, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        return {"ok": False, "error": "GHOST_UNREACHABLE",
                "detail": type(exc).__name__ + ": " + str(exc)}
    return _body(r)


def _get(path: str, timeout: float = 30.0) -> Dict[str, Any]:
    try:
        r = requests.get(f"http://127.0.0.1:8030/api/ghost{path}",
                         headers=HEADERS, timeout=timeout)
    except requests.RequestException as exc:
        return {"ok": False, "error": "GHOST_UNREACHABLE",
                "detail": type(exc).__name__ + ": " + str(exc)}
    return _body(r)


def _body(r: "requests.Response") -> Dict[str, Any]:
    try:
        body = r.json()
    except ValueError:
        return {"ok": False, "error": "BAD_RESPONSE",
                "detail": r.text[:200], "http_status": r.status_code}
    if isinstance(body, dict):
        body.setdefault("ok", 200 <= r.status_code < 300)
        body["http_status"] = r.status_code
        return body
    return {"ok": False, "error": "BAD_RESPONSE",
            "detail": "non-object JSON", "http_status": r.status_code}


# ---- MCP Server ------------------------------------------------------------

from mcp.server.mcpserver import MCPServer  # noqa: E402

mcp = MCPServer(
    name="moray-ghost",
    instructions=(
        "moray-ghost desktop automation gateway. analyze_screen reads native "
        "window controls; click_element routes web targets to CDP and native "
        "targets to VL+safe click; type_text types into a window (focus it "
        "first via click_element); execute_task runs a queued task. "
        "Identity/capabilities come from the spawn environment "
        "(GHOST_TOKEN / GHOST_AGENT_ID / GHOST_CAPABILITIES) and are enforced "
        "server-side; security denials are returned as structured "
        "{ok:false, error, detail} payloads."))


@mcp.tool()
def analyze_screen(monitor: int = 1, window: Optional[str] = None,
                   backend: str = "auto") -> Dict[str, Any]:
    """感知一个原生窗口的控件列表（文本 / 类型 / 物理像素 bbox / 中心）。

    window 省略 → 前台窗口；给标题子串 → 唯一定位（0/多命中会如实报错）。
    backend: auto|native|ocr|llama|mock（服务端执行，本进程不跑模型）。
    """
    body = _post("/vl/analyze", {"monitor": monitor, "window": window,
                                 "backend": backend})
    if body.get("error") == "WINDOW_NOT_FOUND":
        body["detail"] = "target window not found: " + repr(window)
    return body


@mcp.tool()
def click_element(text: str, window: Optional[str] = None,
                  channel: str = "auto", match: str = "exact",
                  backend: str = "auto",
                  settle_seconds: float = 0.3) -> Dict[str, Any]:
    """按可见文本点击元素。

    路由：channel=auto 时，URL 或 Edge/Chrome 窗口 → CDP（by=text 选择器）；
    原生窗口 → VL 解析文本坐标 → 走安全管道点击（hwnd + 能力白名单）。
    多命中绝不猜（409 VL_AMBIGUOUS）；未命中 404 VL_ELEMENT_NOT_FOUND。
    """
    ch = channel if channel in ("cdp", "native") \
        else vl_driver.resolve_channel(window)
    if ch == "cdp":
        payload: Dict[str, Any] = {"op": "click",
                                   "selector": {"by": "text", "value": text}}
        if window and vl_driver._looks_like_url(str(window)):
            payload["tab"] = {"url_contains": str(window)}
        return _post("/browser/action", payload)

    an = _post("/vl/analyze", {"window": window, "backend": backend})
    if not an.get("ok"):
        return an
    try:
        control = vl_driver.find_control(an.get("controls") or [], text,
                                         match=match)
    except vl_driver.VLError as exc:
        return {"ok": False, "error": exc.error, "detail": exc.detail,
                "window": an.get("window"), "backend": an.get("backend")}
    cx, cy = control["center"]
    out = _post("/execute_action", {
        "action": "click", "x": cx, "y": cy,
        "hwnd": (an.get("window") or {}).get("hwnd"),
        "settle_seconds": settle_seconds, **_caps_field()})
    out["clicked"] = control
    out["backend"] = an.get("backend")
    return out


@mcp.tool()
def type_text(text: str, hwnd: Optional[int] = None,
              x: Optional[int] = None, y: Optional[int] = None,
              settle_seconds: float = 0.3) -> Dict[str, Any]:
    """向窗口键入文本（键盘黑名单 / 能力令牌 / 前台门禁全部服务端生效）。

    带 hwnd 时目标必须处于前台（否则 409 WINDOW_NOT_FOREGROUND——先
    click_element 聚焦）。x/y 省略 → 键入前后光标零位移（读取当前光标）。
    """
    if x is None or y is None:
        health = _get("/health")
        if not health.get("ok"):
            return health
        cursor = health.get("cursor") or [0, 0]
        x = int(cursor[0]) if x is None else int(x)
        y = int(cursor[1]) if y is None else int(y)
    return _post("/execute_action", {
        "action": "type", "text": text, "x": x, "y": y, "hwnd": hwnd,
        "settle_seconds": settle_seconds, **_caps_field()})


@mcp.tool()
def execute_task(task_payload: Dict[str, Any], wait: bool = True,
                 timeout_s: int = 90) -> Dict[str, Any]:
    """经安全任务队列执行一个任务（steps/goal/task_id…；201 → start → 轮询）。

    capabilities 恒由启动环境注入（task_payload 里的同名字段会被覆盖）——
    白名单在 MCP 模式下与 SDK 模式同样不可越权。wait=True 时轮询到终态
    （done/degraded/canceled）或超时（超时返回 task_id 供继续查询）。
    """
    payload = dict(task_payload or {})
    if CAPABILITIES:
        payload["capabilities"] = {"allowed_windows": list(CAPABILITIES)}
    created = _post("/task", payload, timeout=30)
    if not created.get("ok"):
        return created
    tid = created.get("task_id")
    started = _post(f"/task/{tid}/start", {}, timeout=30)
    if not started.get("ok"):
        started["task_id"] = tid
        return started
    if not wait:
        return {"ok": True, "task_id": tid, "status": started.get("status"),
                "queue_position": started.get("queue_position"), "waited": False}
    deadline = time.time() + max(1, int(timeout_s))
    view: Dict[str, Any] = {"ok": True, "task_id": tid}
    while time.time() < deadline:
        view = _get(f"/task/{tid}")
        if view.get("status") in ("done", "degraded", "canceled", "failed"):
            view["waited"] = True
            return view
        time.sleep(0.4)
    view.setdefault("ok", False)
    view.setdefault("error", "MCP_TASK_WAIT_TIMEOUT")
    view["detail"] = "task still " + str(view.get("status")) + \
        " after " + str(timeout_s) + "s; poll GET /task/" + str(tid)
    return view


def main() -> int:
    if not TOKEN:
        log.error("no GHOST_TOKEN available (env/.env) - refusing anonymous "
                  "MCP gateway")
        return 1
    log.info("moray-ghost MCP server up: agent=%s capabilities=%s "
             "vl_backend=%s (stdio)",
             AGENT_ID or "-", json.dumps(CAPABILITIES, ensure_ascii=False),
             config.VL_BACKEND)
    mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
