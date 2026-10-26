"""moray-ghost 官方 Python SDK（v0.5 平台化）——Agent 接入桌面操控底座的零门槛客户端。

依赖仅 ``requests``，纯 HTTP 客户端、不写本地文件。一个客户端对象按命名空间
组织全部能力::

    from sdk import GhostClient

    client = GhostClient(agent_id="my-agent")          # token 自动从 GHOST_TOKEN/.env 解析
    client.health.check()                              # 探活
    client.environment.check()                         # 桌面可读/锁屏/UAC 体检
    shot = client.action.screenshot(scale=0.5)         # 抓屏（附 image_bytes）
    client.action.click(1280, 720)                     # 点击（全量安全管道在服务端）
    view = client.task.run(steps=[{"kind": "wait", "ms": 500}])   # 建+启+轮询一步到位

多 Agent：构造时传 ``agent_id``（或环境变量 ``GHOST_AGENT_ID``），请求自动携带
``X-Agent-Id`` 头；跨 Agent 访问他人任务会抛 ``GhostForbiddenError``。

v0.7 能力令牌（最小权限，强制）：构造时声明 ``allowed_windows``（或直接传
``capabilities``），客户端会在所有会产生桌面输入的请求里自动携带；服务端核对
目标窗口进程是否在白名单内。未声明 → 默认 ``{"allowed_windows": []}``：
纯 wait 任务可用，任何输入动作/含输入步骤的任务会被 403/400 明确拒绝。

异常体系（不要写裸 requests 代码）::
    GhostError                 基类：status / error / detail / data
    ├─ GhostAuthError          401 token 缺失或错误
    ├─ GhostForbiddenError     403 跨 Agent 访问
    ├─ GhostRedlineError       403 v0.7 红线：HIGH_RISK_WINDOW / HIGH_RISK_INPUT /
    │                          CAPABILITY_DENIED（黑名单命中或目标不在白名单）
    ├─ GhostFrozenError        503 emergency stop 冻结中
    ├─ SystemNotReadableError  503 锁屏/安全桌面/抓屏失败
    └─ TaskDegradedError       任务 degraded 挂起（.task 为完整任务快照）
    TaskTimeoutError           run/wait 超时（任务仍在服务端跑，可继续 wait）
"""
from __future__ import annotations

import base64
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

DEFAULT_BASE_URL = "http://127.0.0.1:8030"
TERMINAL_STATES = ("done", "failed", "canceled")
# run()/wait() 的停下时机：终态，或 degraded（挂起等主仓处置的决策点）
RETURN_STATES = TERMINAL_STATES + ("degraded",)


class GhostError(Exception):
    """服务端返回的非 2xx 响应，或连接失败。"""

    def __init__(self, status: int, error: str, detail: str = "",
                 data: Optional[Dict[str, Any]] = None):
        super().__init__(f"[{status}] {error}: {detail}")
        self.status = status
        self.error = error
        self.detail = detail
        self.data = data or {}


class GhostAuthError(GhostError):
    def __init__(self, detail: str = "missing or invalid X-Ghost-Token"):
        super().__init__(401, "UNAUTHORIZED", detail)


class GhostForbiddenError(GhostError):
    pass


class GhostRedlineError(GhostError):
    """v0.7 红线拒绝：HIGH_RISK_WINDOW / HIGH_RISK_INPUT / CAPABILITY_DENIED。

    黑名单窗口/热键/文本，或目标窗口不在 capabilities.allowed_windows 白名单。
    """


class GhostFrozenError(GhostError):
    """Ghost 已被 emergency stop 冻结；POST /api/ghost/emergency_stop/release 解冻。"""


class SystemNotReadableError(GhostError):
    """锁屏 / UAC 安全桌面 / 抓屏失败：服务端拒绝盲操作。"""


class TaskDegradedError(GhostError):
    """任务在步骤边界挂起（fail_class 见 ``.task["fail"]``）；处置后 resume 重排队。"""

    def __init__(self, task: Dict[str, Any]):
        fail = task.get("fail") or {}
        super().__init__(503, "TASK_DEGRADED",
                         "task " + str(task.get("task_id")) + " degraded at step "
                         + str(fail.get("step_id")) + ": "
                         + str(fail.get("fail_class")) + " — " + str(fail.get("detail")))
        self.task = task


class TaskTimeoutError(GhostError):
    def __init__(self, task_id: str, timeout: float):
        super().__init__(504, "TASK_TIMEOUT",
                         "task " + repr(task_id) + " not settled within "
                         + str(timeout) + "s (still running server-side)")


class GhostConnectionError(GhostError):
    def __init__(self, base_url: str, detail: str):
        super().__init__(0, "CONNECTION_ERROR",
                         "cannot reach ghost at " + base_url + ": " + detail)


def _resolve_token(token: Optional[str]) -> Optional[str]:
    """token 解析顺序：显式参数 > GHOST_TOKEN 环境变量 > 当前目录 .env。"""
    if token:
        return token.strip()
    env = os.getenv("GHOST_TOKEN", "").strip()
    if env:
        return env
    dot_env = Path.cwd() / ".env"
    if dot_env.is_file():
        for line in dot_env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("GHOST_TOKEN="):
                return line.split("=", 1)[1].strip()
    return None


class _Namespace:
    def __init__(self, client: "GhostClient"):
        self._client = client


class HealthAPI(_Namespace):
    def check(self) -> Dict[str, Any]:
        """探活：版本、显示器列表、光标位置（免 token）。"""
        return self._client.request("GET", "/health")


class EnvironmentAPI(_Namespace):
    def check(self) -> Dict[str, Any]:
        """桌面体检：desktop_readable / lock_screen / uac_secure_desktop / input_allowed。"""
        return self._client.request("GET", "/environment")


class ActionAPI(_Namespace):
    """单步桌面动作。全部走服务端统一安全管道（串行锁 + Fail-Safe + 前后审计截图 +
    v0.7 红线门禁）。自动携带客户端 capabilities（最小权限令牌）。"""

    def click(self, x: int, y: int, monitor: int = 1,
              settle_seconds: float = 0.5, **kw: Any) -> Dict[str, Any]:
        kw.setdefault("capabilities", self._client.capabilities)
        return self._client.request("POST", "/execute_action", {
            "action": "click", "x": int(x), "y": int(y),
            "monitor": monitor, "settle_seconds": settle_seconds, **kw})

    def double_click(self, x: int, y: int, **kw: Any) -> Dict[str, Any]:
        kw.setdefault("capabilities", self._client.capabilities)
        return self._client.request("POST", "/execute_action", {
            "action": "double_click", "x": int(x), "y": int(y), **kw})

    def right_click(self, x: int, y: int, **kw: Any) -> Dict[str, Any]:
        kw.setdefault("capabilities", self._client.capabilities)
        return self._client.request("POST", "/execute_action", {
            "action": "right_click", "x": int(x), "y": int(y), **kw})

    def move(self, x: int, y: int, **kw: Any) -> Dict[str, Any]:
        kw.setdefault("capabilities", self._client.capabilities)
        return self._client.request("POST", "/execute_action", {
            "action": "move", "x": int(x), "y": int(y), **kw})

    def type(self, text: str, x: Optional[int] = None, y: Optional[int] = None,
             hwnd: Optional[int] = None, **kw: Any) -> Dict[str, Any]:
        """输入文本；给 hwnd 时服务端强制前台门禁（绝不打进别的应用）。"""
        body: Dict[str, Any] = {"action": "type", "text": text}
        if x is not None:
            body["x"] = int(x)
        if y is not None:
            body["y"] = int(y)
        if hwnd is not None:
            body["hwnd"] = int(hwnd)
        kw.setdefault("capabilities", self._client.capabilities)
        return self._client.request("POST", "/execute_action", {**body, **kw})

    def hotkey(self, combo: str, **kw: Any) -> Dict[str, Any]:
        kw.setdefault("capabilities", self._client.capabilities)
        return self._client.request("POST", "/execute_action", {
            "action": "hotkey", "text": combo, **kw})

    def screenshot(self, monitor: int = 1, scale: float = 1.0,
                   fmt: str = "jpeg", quality: int = 70) -> Dict[str, Any]:
        """抓屏。返回体额外附 ``image_bytes``（已解码），落盘/喂视觉模型由调用方决定。"""
        out = self._client.request("GET", "/screenshot", params={
            "monitor": monitor, "scale": scale, "fmt": fmt, "quality": quality})
        if out.get("image_b64"):
            out["image_bytes"] = base64.b64decode(out["image_b64"])
        return out


class TaskAPI(_Namespace):
    """任务生命周期 + 自动轮询。步骤 schema 见 README：macro / action / wait / require_vision。"""

    def create(self, steps: List[Dict[str, Any]], goal: Optional[str] = None,
               task_id: Optional[str] = None,
               capabilities: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._client.request("POST", "/task", {
            "goal": goal, "task_id": task_id, "steps": steps,
            "capabilities": capabilities if capabilities is not None
            else self._client.capabilities})

    def start(self, task_id: str) -> Dict[str, Any]:
        """入队（202）。返回体含 status=queued 与 queue_position。"""
        return self._client.request("POST", "/task/" + task_id + "/start", {})

    def status(self, task_id: str) -> Dict[str, Any]:
        """完整任务视图（含 step_states / queue_position / fail）。"""
        return self._client.request("GET", "/task/" + task_id)

    def resume(self, task_id: str) -> Dict[str, Any]:
        """degraded 挂起后处置完毕，重新排队继续（202）。"""
        return self._client.request("POST", "/task/" + task_id + "/resume", {})

    def cancel(self, task_id: str) -> Dict[str, Any]:
        return self._client.request("POST", "/task/" + task_id + "/cancel", {})

    def list(self, limit: int = 50) -> Dict[str, Any]:
        """本人（agent_id 视角）的任务列表。"""
        return self._client.request("GET", "/tasks", params={"limit": limit})

    def wait(self, task_id: str, timeout: float = 180.0,
             poll_interval: Optional[float] = None) -> Dict[str, Any]:
        """轮询直至终态或 degraded 挂起（决策点），返回最终任务视图。"""
        deadline = time.time() + timeout
        interval = poll_interval or self._client.poll_interval
        last: Dict[str, Any] = {}
        while time.time() < deadline:
            last = self.status(task_id)
            if last.get("status") in RETURN_STATES:
                return last
            time.sleep(interval)
        raise TaskTimeoutError(task_id, timeout)

    def run(self, steps: List[Dict[str, Any]], goal: Optional[str] = None,
            task_id: Optional[str] = None, timeout: float = 180.0,
            poll_interval: Optional[float] = None,
            on_degraded: str = "raise") -> Dict[str, Any]:
        """一步到位：create → start → wait。

        degraded 时 on_degraded="raise"（默认）抛 TaskDegradedError（.task 带快照，
        处置后调 resume() + wait() 继续）；on_degraded="return" 直接返回挂起时视图。
        """
        created = self.create(steps, goal=goal, task_id=task_id)
        tid = created["task_id"]
        self.start(tid)
        final = self.wait(tid, timeout=timeout, poll_interval=poll_interval)
        if final.get("status") == "degraded" and on_degraded == "raise":
            raise TaskDegradedError(final)
        return final


class SystemAPI(_Namespace):
    """emergency stop（冻结/解冻）。多 Agent 底座的最后一道闸。"""

    def emergency_stop(self) -> Dict[str, Any]:
        """清空队列 + 冻结：所有桌面输入接口立即 503。"""
        return self._client.request("POST", "/emergency_stop", {})

    def release(self) -> Dict[str, Any]:
        return self._client.request("POST", "/emergency_stop/release", {})


class WindowAPI(_Namespace):
    def list(self) -> Dict[str, Any]:
        """枚举可见顶层窗口（hwnd/title/rect/is_foreground），供 Agent 做目标发现。"""
        return self._client.request("GET", "/window/list")


class MacroAPI(_Namespace):
    """宏录制 / 安全重放 / 视觉反哺 Heal。"""

    def list(self) -> Dict[str, Any]:
        return self._client.request("GET", "/macro")

    def get(self, macro_id: str) -> Dict[str, Any]:
        return self._client.request("GET", "/macro/" + macro_id)

    def record_start(self, task_id: str) -> Dict[str, Any]:
        """开启录制会话：此后每个成功 execute_action 自动追加步骤。"""
        return self._client.request("POST", "/macro/record/start",
                                    {"task_id": task_id})

    def record_stop(self, task_id: str, name: Optional[str] = None,
                    app_signature: Optional[str] = None) -> Dict[str, Any]:
        """结束录制并写库，返回 macro_id。"""
        return self._client.request("POST", "/macro/record/stop", {
            "task_id": task_id, "name": name, "app_signature": app_signature})

    def replay(self, macro_id: str, dry_run: bool = False,
               hwnd: Optional[int] = None, task_id: Optional[str] = None,
               allow_fallback: bool = False,
               capabilities: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """锚点驱动安全重放；409 类失败（MACRO_NOT_FOUND 等）抛 GhostError。
        真实重放（dry_run=False）强制携带 capabilities（v0.7 最小权限）。"""
        return self._client.request("POST", "/macro/" + macro_id + "/replay", {
            "dry_run": dry_run, "hwnd": hwnd, "task_id": task_id,
            "allow_fallback": allow_fallback,
            "capabilities": capabilities if capabilities is not None
            else self._client.capabilities})

    def heal(self, macro_id: str, step_id: int, mode: str, base_version: int,
             vision: Dict[str, Any], task_id: Optional[str] = None,
             reason: Optional[str] = None) -> Dict[str, Any]:
        """视觉反哺：mode=relocate（给新中心即用即裁）| reanchor（直接给新锚点图）。"""
        body: Dict[str, Any] = {"step_id": step_id, "mode": mode,
                                "base_version": base_version, "vision": vision}
        if task_id:
            body["task_id"] = task_id
        if reason:
            body["reason"] = reason
        return self._client.request("POST", "/macro/" + macro_id + "/heal", body)

    def probe(self, screenshot_crop: str, match_threshold: float = 0.80,
              monitor: int = 1, window_hwnd: Optional[int] = None) -> Dict[str, Any]:
        """锚点匹配查询（只读，不执行动作）。四态：matched/not_found/
        capture_failed(503)/window_not_found(409)。"""
        return self._client.request("POST", "/macro/probe", {
            "screenshot_crop": screenshot_crop,
            "match_threshold": match_threshold,
            "monitor": monitor, "window_hwnd": window_hwnd})


class GhostClient:
    """moray-ghost 客户端。线程安全（requests.Session 本身线程安全）。"""

    def __init__(self, base_url: str = DEFAULT_BASE_URL,
                 token: Optional[str] = None,
                 agent_id: Optional[str] = None,
                 timeout: float = 30.0,
                 poll_interval: float = 0.4,
                 allowed_windows: Optional[List[str]] = None,
                 capabilities: Optional[Dict[str, Any]] = None):
        if not (base_url.startswith("http://") or base_url.startswith("https://")):
            raise ValueError("base_url must start with http:// or https://")
        self.base_url = base_url.rstrip("/")
        self.agent_id = (agent_id or os.getenv("GHOST_AGENT_ID", "").strip()) or None
        self.timeout = timeout
        self.poll_interval = poll_interval
        # v0.7 能力令牌（最小权限）：显式 capabilities > allowed_windows 便捷参数 >
        # 默认空名单（纯 wait 任务可用；任何输入动作会被服务端 403 明确拒绝）。
        if capabilities is not None:
            if not isinstance(capabilities, dict):
                raise ValueError("capabilities must be a dict")
            self.capabilities: Dict[str, Any] = dict(capabilities)
            self.capabilities.setdefault("allowed_windows", [])
        else:
            self.capabilities = {
                "allowed_windows": [w.strip().lower()
                                    for w in (allowed_windows or [])
                                    if isinstance(w, str) and w.strip()]}
        self._session = requests.Session()
        self._session.headers["X-Ghost-Token"] = _resolve_token(token) or ""
        if self.agent_id:
            self._session.headers["X-Agent-Id"] = self.agent_id

        self.health = HealthAPI(self)
        self.environment = EnvironmentAPI(self)
        self.action = ActionAPI(self)
        self.task = TaskAPI(self)
        self.system = SystemAPI(self)
        self.window = WindowAPI(self)
        self.macro = MacroAPI(self)

    # ---------- 底层 ----------

    def request(self, method: str, path: str, json_body: Any = None,
                params: Optional[Dict[str, Any]] = None,
                timeout: Optional[float] = None) -> Dict[str, Any]:
        """发请求并解析 JSON；非 2xx 抛 GhostError 子类。"""
        url = self.base_url + "/api/ghost" + path
        try:
            resp = self._session.request(
                method, url, json=json_body, params=params,
                timeout=timeout or self.timeout)
        except requests.RequestException as exc:
            raise GhostConnectionError(self.base_url, str(exc)) from exc
        if 200 <= resp.status_code < 300:
            try:
                return resp.json()
            except ValueError:
                return {"ok": True, "raw": resp.text}
        raise self._to_error(resp)

    @staticmethod
    def _to_error(resp: requests.Response) -> GhostError:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        error = str(body.get("error") or "HTTP_" + str(resp.status_code))
        detail = str(body.get("detail") or resp.text[:200])
        if resp.status_code == 401:
            return GhostAuthError(detail)
        if resp.status_code == 403 and error == "AGENT_FORBIDDEN":
            return GhostForbiddenError(403, error, detail)
        if resp.status_code == 403 and error in (
                "HIGH_RISK_WINDOW", "HIGH_RISK_INPUT", "CAPABILITY_DENIED"):
            return GhostRedlineError(resp.status_code, error, detail, data=body)
        if error == "GHOST_FROZEN":
            return GhostFrozenError(resp.status_code, error, detail)
        if error == "SYSTEM_NOT_READABLE":
            return SystemNotReadableError(resp.status_code, error, detail,
                                          data=body.get("environment"))
        return GhostError(resp.status_code, error, detail, data={
            k: v for k, v in body.items() if k not in ("ok", "error", "detail")})
