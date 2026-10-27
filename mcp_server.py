"""moray-ghost MCP Server（v0.12 · Stdio）：把 Ghost 接进任意 AI Agent。

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
from typing import Any, Dict, List, Optional

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
        "window controls; click_element uses GUI RAG (OCR+CV numbered elements "
        "-> VLM target_id, no coordinate generation) and routes web targets to "
        "CDP; dangerous labels return PENDING_APPROVAL and can only be approved "
        "by a human console (agents never hold the approval token); type_text "
        "types into a window (focus it first via click_element); "
        "draw_trajectory draws a smooth mouse stroke (optionally picking a "
        "palette color first); execute_task runs a queued task. "
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
                                 "backend": backend}, timeout=180)
    if body.get("error") == "WINDOW_NOT_FOUND":
        body["detail"] = "target window not found: " + repr(window)
    return body


@mcp.tool()
def click_element(text: str, window: Optional[str] = None,
                  channel: str = "auto", match: str = "exact",
                  backend: str = "auto",
                  settle_seconds: float = 0.3) -> Dict[str, Any]:
    """按可见文本点击元素。

    v0.12 优先 GUI RAG：检索编号元素 →（文本精确命中或 VLM target_id）→
    服务端返回真实 bbox → 安全管道点击。危险标签在 MCP 层直接
    PENDING_APPROVAL（agent 不持有审批令牌）。
    路由：channel=auto 时，URL 或 Edge/Chrome 窗口 → CDP（by=text 选择器）；
    原生窗口 → VL/GUI RAG。多命中绝不猜（409 VL_AMBIGUOUS）；未命中 404。
    """
    ch = channel if channel in ("cdp", "native") \
        else vl_driver.resolve_channel(window)
    if ch == "cdp":
        payload: Dict[str, Any] = {"op": "click",
                                   "selector": {"by": "text", "value": text}}
        if window and vl_driver._looks_like_url(str(window)):
            payload["tab"] = {"url_contains": str(window)}
        return _post("/browser/action", payload)

    # v0.12 GUI RAG 优先：OCR+CV 检索编号元素 → VLM 只回 target_id → 真实 bbox。
    # 文本目标通常零模型命中（method=text_exact）；模糊目标由 VLM 选号。
    # 未命中/后端不可用时回落到既有 analyze/target/hybrid 路径，行为不变。
    rag = _post("/vl/ground", {"window": window, "backend": backend,
                               "query": text, "strategy": "auto",
                               "match": match}, timeout=180)
    if rag.get("ok"):
        control = rag.get("element") or {}
        if rag.get("requires_approval"):
            # Agent 层无权自批：审批令牌只应存在于人类控制台，绝不从
            # MCP 启动环境读取/透传。
            return {"ok": False, "error": "PENDING_APPROVAL",
                    "detail": "Ghost Guard: target element "
                              + repr(control.get("text"))
                              + " requires human approval",
                    "http_status": 409,
                    "requires_approval": True,
                    "target_id": rag.get("target_id"),
                    "element": control,
                    "target_guard": rag.get("target_guard"),
                    "guard": rag.get("guard"),
                    "backend": rag.get("backend")}
        center = control.get("center")
        if not (isinstance(center, (list, tuple)) and len(center) == 2):
            return {"ok": False, "error": "VL_BAD_ELEMENT",
                    "detail": "GUI RAG returned an element without a valid "
                              "center: " + str(control)[:300],
                    "backend": rag.get("backend")}
        out = _post("/execute_action", {
            "action": "click", "x": int(center[0]), "y": int(center[1]),
            "hwnd": (rag.get("window") or {}).get("hwnd"),
            "settle_seconds": settle_seconds, **_caps_field()})
        out["clicked"] = control
        out["backend"] = rag.get("backend")
        out["target_id"] = rag.get("target_id")
        out["method"] = rag.get("method")
        return out
    if rag.get("error") not in ("VL_ELEMENT_NOT_FOUND",
                               "VL_BACKEND_UNAVAILABLE", "VL_AMBIGUOUS"):
        return rag

    an = _post("/vl/analyze", {"window": window, "backend": backend},
               timeout=180)
    if not an.get("ok"):
        return an
    try:
        control = vl_driver.find_control(an.get("controls") or [], text,
                                         match=match)
    except vl_driver.VLElementNotFound as exc:
        # 通用控件表未命中（2B 模型通常只列 1 条）→ 定向 grounding 二次定位。
        # 服务端会校验标签包含目标 + bbox 面积/长宽比合理，拒绝整图/1px 幻觉。
        loc = _post("/vl/analyze", {"window": window, "backend": backend,
                                    "target": text}, timeout=180)
        hybrid_detail = None
        if (not loc.get("ok")
                and loc.get("error") == "VL_ELEMENT_NOT_FOUND"
                and vl_driver.hybrid_enabled()):
            # 7B 档：CV 粗筛小图标 → 编号网格 → VL 选号。
            hyp = _post("/vl/analyze", {"window": window, "backend": backend,
                                        "target": text, "hybrid": True},
                        timeout=300)
            if hyp.get("ok"):
                loc = hyp
            elif hyp.get("error") != "VL_ELEMENT_NOT_FOUND":
                return hyp
            else:
                hybrid_detail = hyp.get("detail")
        if not loc.get("ok"):
            if loc.get("error") == "VL_ELEMENT_NOT_FOUND":
                detail = exc.detail
                if hybrid_detail:
                    detail += "; hybrid: " + str(hybrid_detail)
                return {"ok": False, "error": exc.error, "detail": detail,
                        "window": an.get("window"),
                        "backend": an.get("backend")}
            return loc
        try:
            control = vl_driver.find_control(loc.get("controls") or [], text,
                                             match=match)
        except vl_driver.VLError as exc2:
            return {"ok": False, "error": exc2.error,
                    "detail": exc2.detail, "window": loc.get("window"),
                    "backend": loc.get("backend")}
        an = loc
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


@mcp.tool()
def draw_trajectory(points: List[List[float]], color: Optional[str] = None,
                    hwnd: Optional[int] = None, window: Optional[str] = None,
                    duration_ms: int = 800, smooth_iterations: int = 2,
                    closed: bool = False,
                    color_required: bool = False) -> Dict[str, Any]:
    """画一条平滑鼠标轨迹（纯鼠标绘制底层：Chaikin 平滑 + 降采样 + 真实鼠标事件）。

    points 为物理像素 [[x,y],...]（≥2 点，服务端平滑降采样到 ≤2000 点）。
    color（可选）先在目标窗口色板区找实心纯色色板点并点击（名称或 #RRGGBB；
    找不到且 color_required=true → 404，否则用当前色 fallback）。落笔走
    drag 的全套安全管道（归属门禁/Fail-Safe/硬超时/审计）。closed=true 会
    画闭合曲线（注意：画图软件会把闭合笔迹并成闭合形状）。
    """
    payload: Dict[str, Any] = {
        "points": points, "duration_ms": int(duration_ms),
        "smooth_iterations": int(smooth_iterations), "closed": bool(closed),
        "color_required": bool(color_required)}
    if color:
        payload["color"] = str(color)
    if hwnd is not None:
        payload["hwnd"] = int(hwnd)
    if window:
        payload["window"] = str(window)
    return _post("/draw_trajectory", payload, timeout=120)


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
