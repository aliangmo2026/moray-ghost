"""v0.8.0 MCP + VL 感知验收：Stdio 通信 / 控件解析 / 安全点击 / 门禁透传 / 路由分流。

测试纪律（README 红线）：GUI 靶子自建（_v08_target：按钮「保存成功」+ 全局按键
标题回显，stdout 握手 + 差集绑定 + 只认自己 hwnd 消失）；不触碰用户窗口。
全程零裸 HTTP：一切经 MCP 协议（本脚本即 MCP Client）。

场景（按执行顺序）：
  0 前提：端口空闲；Ghost 服务（v0.8 代码）自拉起；靶窗就位。
  1 协议面：initialize 成功，tools/list 恰为 4 个（schema 含全部入参）。
  2 感知：analyze_screen(window=靶窗) → 后端为真实本地视觉（auto→ocr/llama），
     控件表含「保存成功」，bbox 中心落在靶窗内。
  3 安全点击：click_element("保存成功") → 200 → 靶窗标题出现 -CLICKED
     （VL 文本坐标 → 安全管道 → 真实点击生效）。
  4 键入：type_text(hwnd=靶窗)（先强占前台）→ 标题回显 |T:v08ok
     （前台门禁 + 能力令牌 + 键盘黑名单服务端全生效）。
  5 任务：execute_task（wait+零位移 move 步骤）→ done，agent_id 透传，
     capabilities 由 MCP 启动环境注入（payload 改不宽）。
  6 门禁透传：
     a) 错误 capabilities 拉起的 MCP 实例 → 点击 → 403 CAPABILITY_DENIED；
     b) 错误 token 拉起 → analyze → 401 UNAUTHORIZED；
     c) URL 形式 window → 走 CDP 分支 → 503 BROWSER_NOT_CONNECTED
       （证明 web 走 CDP、原生走 VL 的路由分流生效）。
  7 清场：靶窗销毁、服务停止、端口释放。
"""
from __future__ import annotations

import ctypes
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except OSError:
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import anyio  # noqa: E402
import psutil  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from app.core import config, window_locator as wl  # noqa: E402

TOKEN = config.ensure_token()
CAPS_PY = '["python.exe"]'
AGENT = "mcp-v08-test"
RESULTS: list = []
PROCS: list = []
SERVER: dict = {"proc": None}


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  | {detail}" if detail else ""), flush=True)
    return ok


def port_free(port: int = 8030) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def spawn(args: list, env_extra: dict | None = None,
          drain: bool = False) -> subprocess.Popen:
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.Popen(args, cwd=str(ROOT), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env,
                            text=True, encoding="utf-8", errors="replace")
    PROCS.append(proc)
    if drain:
        def _drain():
            try:
                for _ in proc.stdout:
                    pass
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=_drain, daemon=True).start()
    return proc


def spawn_server() -> subprocess.Popen:
    return spawn([sys.executable, str(ROOT / "run.py")], drain=True)


def read_ready(proc: subprocess.Popen, timeout: float = 15.0) -> list:
    import queue
    q: queue.Queue = queue.Queue()

    def _reader():
        try:
            q.put(proc.stdout.readline())
        except Exception:  # noqa: BLE001
            q.put("")
    threading.Thread(target=_reader, daemon=True).start()
    try:
        line = q.get(timeout=timeout)
    except queue.Empty:
        return []
    parts = line.strip().split()
    if not parts or parts[0] != "READY":
        return []
    try:
        return [int(p) for p in parts[1:]]
    except ValueError:
        return []


def wait_healthy(timeout: float = 30.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = requests_get_health()
            if r == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.4)
    return False


def requests_get_health() -> int:
    import requests
    r = requests.get("http://127.0.0.1:8030/api/ghost/health", timeout=2)
    return r.status_code


def kill_by_port(port: int) -> None:
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.laddr and c.laddr.port == port and c.pid:
                psutil.Process(c.pid).kill()
                time.sleep(1)
    except Exception as exc:  # noqa: BLE001
        print("  (kill_by_port: " + type(exc).__name__ + ")", flush=True)


def stop_proc(proc: subprocess.Popen, port: int | None = 8030) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    if port and not port_free(port):
        kill_by_port(port)


def find_by_title(stem: str, timeout: float = 6.0):
    end = time.time() + timeout
    while time.time() < end:
        for w in wl.list_windows():
            if stem in w["title"]:
                return w
        time.sleep(0.3)
    return None


def close_hwnd(hwnd: int) -> bool:
    if not hwnd or not win32gui.IsWindow(hwnd):
        return True
    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    for _ in range(25):
        time.sleep(0.2)
        if not win32gui.IsWindow(hwnd):
            return True
    return not win32gui.IsWindow(hwnd)


def force_foreground(hwnd: int) -> bool:
    for _ in range(4):
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
        except Exception:  # noqa: BLE001
            try:
                ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)
                ctypes.windll.user32.keybd_event(0x12, 0, 2, 0)
                win32gui.SetForegroundWindow(hwnd)
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.3)
        if win32gui.GetForegroundWindow() == hwnd:
            return True
    return False


def cursor_pos() -> tuple:
    import ctypes.wintypes
    pt = ctypes.wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    x, y = pt.x, pt.y
    corners = [(0, 0), (2559, 0), (0, 1599), (2559, 1599)]
    if any(abs(x - fx) <= 2 and abs(y - fy) <= 2 for fx, fy in corners):
        ctypes.windll.user32.SetCursorPos(min(max(x, 40), 2500),
                                          min(max(y, 40), 1500))
        return cursor_pos()
    return x, y


def tool_body(res) -> dict:
    """MCP call_tool 结果 → 结构化 dict（多形态兜底 + 原始形态 stderr 取证）。"""
    sc = getattr(res, "structured_content", None) or getattr(
        res, "structuredContent", None)
    if isinstance(sc, dict) and sc:
        if set(sc.keys()) == {"result"} and isinstance(sc.get("result"), dict):
            return sc["result"]
        if "ok" in sc or "error" in sc or "status" in sc:
            return sc
    texts = [getattr(c, "text", None)
             for c in (getattr(res, "content", None) or [])]
    joined = "\n".join(t for t in texts if t)
    if joined.strip():
        try:
            loaded = json.loads(joined)
            if isinstance(loaded, dict):
                return loaded
        except ValueError:
            pass
    print("RAW_TOOL_RESULT", file=sys.stderr)
    print(repr(res)[:800], file=sys.stderr)
    print("structured=", repr(sc)[:400], file=sys.stderr)
    print("text=", repr(joined)[:400], file=sys.stderr)
    return {"ok": False, "error": "MCP_EMPTY_RESULT",
            "detail": joined[:300],
            "isError": bool(getattr(res, "isError", False))}


def mcp_params(caps: str = CAPS_PY, token: str | None = TOKEN,
               agent: str | None = AGENT) -> StdioServerParameters:
    env = dict(os.environ)
    env["GHOST_CAPABILITIES"] = caps
    if token is not None:
        env["GHOST_TOKEN"] = token
    if agent is not None:
        env["GHOST_AGENT_ID"] = agent
    return StdioServerParameters(command=sys.executable,
                                 args=[str(ROOT / "mcp_server.py")], env=env)


# ================= async 场景 =================
async def scenario(stem: str, hwnd: int, btn_c: tuple) -> None:
    base_title = "moray-v08-" + stem
    url = f"http://127.0.0.1:8030/api/ghost"  # 仅用于文档一致性（无直接请求）

    # --- 1 协议面 ---
    async with stdio_client(mcp_params()) as (r, w):
        async with ClientSession(r, w) as s:
            info = await s.initialize()
            si = getattr(info, "server_info", None)
            check("1 MCP initialize 成功（Stdio）",
                  si is not None and si.name == "moray-ghost",
                  f"server={getattr(si, 'name', None)}")
            tools = await s.list_tools()
            names = sorted(t.name for t in tools.tools)
            check("1 tools/list 恰为 5 个标准工具",
                  names == ["analyze_screen", "click_element",
                            "draw_trajectory", "execute_task",
                            "type_text"], str(names))
            schema_ok = True
            for t in tools.tools:
                sch = getattr(t, "input_schema", None) or {}
                need = {"analyze_screen": {"monitor", "window", "backend"},
                        "click_element": {"text", "window", "channel"},
                        "type_text": {"text", "hwnd"},
                        "draw_trajectory": {"points"},
                        "execute_task": {"task_payload"}}[t.name]
                if not need <= set(sch.get("properties", {})):
                    schema_ok = False
            check("1 各 tool schema 入参完整", schema_ok)

            # --- 2 感知（真实本地视觉后端） ---
            # 感知也必须先聚焦：ZCode 等 topmost 窗口可能盖住靶窗上半部，
            # 否则模型只能看到输入框（实测）。
            force_foreground(hwnd)
            an = tool_body(await s.call_tool("analyze_screen", {
                "window": base_title}))
            controls = an.get("controls") or []
            hit = next((c for c in controls
                        if str(c.get("text")) == "保存成功"), None)
            if hit is None:
                # 2B/GPU 偶发首帧漏控件（实测只回输入框）；重试一次。
                print("  (analyze retry: first pass missed 保存成功)",
                      flush=True)
                force_foreground(hwnd)
                an = tool_body(await s.call_tool("analyze_screen", {
                    "window": base_title}))
                controls = an.get("controls") or []
                hit = next((c for c in controls
                            if str(c.get("text")) == "保存成功"), None)
            texts = [str(c.get("text")) for c in controls]
            check("2 analyze_screen 成功且为真实视觉后端",
                  an.get("ok") is True and an.get("backend") in ("ocr", "llama"),
                  f"backend={an.get('backend')} count={an.get('count')}")
            check("2 控件表精准解析出按钮「保存成功」", hit is not None,
                  f"texts={json.dumps(texts, ensure_ascii=False)[:150]}")
            if hit:
                cx, cy = hit["center"]
                rect = win32gui.GetWindowRect(hwnd)
                inside = rect[0] <= cx < rect[2] and rect[1] <= cy < rect[3]
                check("2 bbox 中心落在靶窗内（物理像素坐标）", inside,
                      f"center={[cx, cy]} rect={rect}")

            # --- 3 VL 定位 + 安全点击 ---
            # CPU 真 VL 单次分析 ~20s，期间其他窗口可能抢占前台；点击前
            # 重新聚焦靶窗（门禁本身不改，仍校验落点归属）。
            check("3 靶窗强占前台（点击门禁前提）", force_foreground(hwnd))
            clk = tool_body(await s.call_tool("click_element", {
                "text": "保存成功", "window": base_title}))
            if clk.get("http_status") != 200:
                # 用户实时操作可能在动作瞬间抢前台/移鼠标 → 门禁 400；
                # 重新聚焦后重试一次（断言仍要求最终 200，门禁未放宽）。
                print("  (click retry: first=%s body=%s)"
                      % (clk.get("http_status"),
                         json.dumps(clk, ensure_ascii=False)[:300]),
                      flush=True)
                force_foreground(hwnd)
                clk = tool_body(await s.call_tool("click_element", {
                    "text": "保存成功", "window": base_title}))
            check("3 click_element（VL 文本坐标）→ 200",
                  clk.get("ok") is True and clk.get("http_status") == 200,
                  f"status={clk.get('http_status')} "
                  f"clicked={json.dumps(clk.get('clicked'), ensure_ascii=False)} "
                  f"body={json.dumps(clk, ensure_ascii=False)[:200]}")
            w2 = find_by_title(base_title + "-CLICKED", timeout=6)
            check("3 真实点击生效（靶窗标题 -CLICKED）", w2 is not None,
                  f"title={(w2 or {}).get('title')!r}")

            # --- 4 type_text（前台门禁 + 能力令牌） ---
            check("4 靶窗强占前台（前台门禁前提）", force_foreground(hwnd))
            ty = tool_body(await s.call_tool("type_text", {
                "text": "v08ok", "hwnd": hwnd}))
            check("4 type_text → 200（键盘黑名单/能力令牌服务端生效）",
                  ty.get("ok") is True and ty.get("http_status") == 200,
                  f"status={ty.get('http_status')}")
            w3 = find_by_title("|T:v08ok", timeout=6)
            w_now = find_by_title(base_title, timeout=0.1)
            check("4 键入文本真实到达（标题回显 |T:v08ok）",
                  w3 is not None,
                  f"echo={(w3 or {}).get('title')!r} "
                  f"now={(w_now or {}).get('title')!r} "
                  f"type_body={json.dumps(ty, ensure_ascii=False)[:200]}")

            # --- 5 execute_task（安全队列 + 环境白名单注入） ---
            cx0, cy0 = cursor_pos()
            out = tool_body(await s.call_tool("execute_task", {
                "task_payload": {
                    "goal": "v08-mcp-task",
                    "steps": [
                        {"kind": "wait", "ms": 200},
                        {"kind": "action", "payload": {
                            "action": "move", "x": cx0, "y": cy0,
                            "settle_seconds": 0}}],
                    "capabilities": {"allowed_windows": ["evil.exe"]}},
                "wait": True, "timeout_s": 60}))
            check("5 execute_task → done（capabilities 以环境为准，payload 改不宽）",
                  out.get("status") == "done"
                  and out.get("agent_id") == AGENT and out.get("waited") is True,
                  f"body={json.dumps(out, ensure_ascii=False)[:400]}")

    # --- 6a 错误白名单的 MCP 实例 → 点击被服务端能力令牌拦截 ---
    async with stdio_client(mcp_params(caps='["explorer.exe"]')) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            bad = tool_body(await s.call_tool("click_element", {
                "text": "保存成功", "window": base_title}))
            check("6a 错误 capabilities → 403 CAPABILITY_DENIED（服务端拦截）",
                  bad.get("error") == "CAPABILITY_DENIED"
                  or bad.get("http_status") == 403,
                  f"error={bad.get('error')} status={bad.get('http_status')}")

    # --- 6b 错误 token → 401 ---
    async with stdio_client(mcp_params(token="wrong-token-v08")) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            bad = tool_body(await s.call_tool("analyze_screen", {
                "window": base_title}))
            check("6b 错误 token → 401 UNAUTHORIZED",
                  bad.get("error") == "UNAUTHORIZED"
                  or bad.get("http_status") == 401,
                  f"error={bad.get('error')} status={bad.get('http_status')}")

    # --- 6c URL 形式 window → CDP 路由分支 ---
    async with stdio_client(mcp_params()) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            routed = tool_body(await s.call_tool("click_element", {
                "text": "任意控件", "window": "https://example.invalid/app"}))
            check("6c URL window → CDP 分支（503 BROWSER_NOT_CONNECTED）",
                  routed.get("error") == "BROWSER_NOT_CONNECTED",
                  f"error={routed.get('error')} "
                  f"status={routed.get('http_status')}")
    _ = url


def main() -> int:
    print("== moray-ghost v0.8.0 MCP+VL acceptance ==", flush=True)
    try:
        check("0 前提：端口 8030 空闲", port_free())
        srv = spawn_server()
        SERVER["proc"] = srv
        check("0 前提：Ghost v0.8 服务启动", wait_healthy())
        if not srv or srv.poll() is not None:
            return 1
        stem = "t" + str(int(time.time() * 1000))[-8:]
        tgt = spawn([sys.executable, str(ROOT / "scripts" / "_v08_target.py"),
                     stem])
        ready = read_ready(tgt)
        check("0 靶窗就位（自建，stdout 握手）", len(ready) == 3,
              f"ready={ready}")
        if len(ready) != 3:
            return 1
        hwnd, bx, by = ready
        try:
            anyio.run(scenario, stem, hwnd, (bx, by))
        finally:
            check("7 靶窗销毁（只认自己 hwnd 消失）", close_hwnd(hwnd))
            stop_proc(tgt, port=None)
            stop_proc(srv)
            check("7 服务停止、端口释放", port_free())
    finally:
        for p in PROCS:
            try:
                stop_proc(p, port=None)
            except Exception:  # noqa: BLE001
                pass
    passed = sum(1 for x in RESULTS if x)
    print(f"\n== RESULT: {passed}/{len(RESULTS)} passed ==", flush=True)
    return len(RESULTS) - passed


if __name__ == "__main__":
    sys.exit(main())
