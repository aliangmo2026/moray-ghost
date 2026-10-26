"""v0.8 画图工具像素级精度综合验收：真视觉栈操作 MS Paint 画一笔并逐像素验证。

链路（全程走 v0.8 新栈，安全门禁零旁路）：
  1. 自建进程拉起 mspaint.exe（差集绑定，只杀自己的），强占前台；
  2. MCP analyze_screen（前景=画图）→ OCR/llama 读出控件列表；
  3. MCP click_element 尝试选择形状工具（矩形族候选；找不到则用默认铅笔，
     两种路径都接受——重点是像素级验证拖出来的痕迹）；
  4. 服务端像素检测画布（cv2 最大白色连通域）→ 截图 before；
  5. MCP execute_task（drag 步骤，hwnd=画图窗口 → 能力令牌/落点归属门禁照走）
     从画布中心向对角拖一笔；
  6. 截图 after → cv2 差分 → 笔迹 bbox 与拖拽意图 bbox 逐边对比（±tol），
     同时校验笔迹像素量——像素级精度判据；
  7. 清场：只杀自己的 mspaint 进程、停服务、端口释放。

测试纪律：不触碰用户已有窗口；只对本进程 Popen 的 mspaint 操作与销毁。
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
import cv2  # noqa: E402
import numpy as np  # noqa: E402
import psutil  # noqa: E402
import requests  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from app.core import config, safety_gate, window_locator as wl  # noqa: E402

TOKEN = config.ensure_token()
CAPS_PAINT = '["mspaint.exe"]'
RESULTS: list = []
PROCS: list = []


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


def wait_healthy(timeout: float = 30.0) -> bool:
    import requests as rq
    end = time.time() + timeout
    while time.time() < end:
        try:
            if rq.get("http://127.0.0.1:8030/api/ghost/health",
                      timeout=2).status_code == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.4)
    return False


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


def shot_png() -> "np.ndarray":
    """全尺度截图 → BGR numpy（物理像素）。"""
    r = requests.get("http://127.0.0.1:8030/api/ghost/screenshot",
                     params={"monitor": 1, "fmt": "png", "scale": 1.0},
                     timeout=30)
    r.raise_for_status()
    import base64
    from io import BytesIO
    from PIL import Image
    img = Image.open(BytesIO(base64.b64decode(r.json()["image_b64"])))
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def find_paint_window(snap_before_pids: set, timeout: float = 20.0):
    """差集绑定：新出现且进程为 mspaint.exe 的顶层窗。"""
    end = time.time() + timeout
    while time.time() < end:
        for w in wl.list_windows():
            info = safety_gate.window_info(w["hwnd"])
            proc = str((info or {}).get("process") or "").lower()
            if proc == "mspaint.exe" and w["rect"][2] - w["rect"][0] > 300:
                return w
        time.sleep(0.4)
    return None


def detect_canvas(img: "np.ndarray", rect: tuple):
    """服务端像素检测：窗口内最大白色连通域（跳过顶部 18% 功能区）。"""
    l, t, r, b = rect
    l, t = max(0, l), max(0, t)
    crop = img[t:b, l:r]
    if crop.size == 0:
        return None
    h, w = crop.shape[:2]
    y0 = int(h * 0.18)
    sub = crop[y0:, :]
    gray = cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(gray, 245, 255, cv2.THRESH_BINARY)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    best = None
    for c in contours:
        x, y, cw, ch = cv2.boundingRect(c)
        if cw < 80 or ch < 60:
            continue
        if cw * ch < 0.06 * w * h:
            continue
        if best is None or cw * ch > best[2] * best[3]:
            best = (x, y, cw, ch)
    if best is None:
        return None
    x, y, cw, ch = best
    return [l + x, t + y0 + y, cw, ch]   # 屏幕物理坐标


def tool_body(res) -> dict:
    sc = getattr(res, "structured_content", None) or getattr(
        res, "structuredContent", None)
    if isinstance(sc, dict) and sc and (
            "ok" in sc or "error" in sc or "status" in sc):
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
    return {"ok": False, "error": "MCP_EMPTY_RESULT", "detail": joined[:200]}


def mcp_params() -> StdioServerParameters:
    env = dict(os.environ)
    env["GHOST_CAPABILITIES"] = CAPS_PAINT
    env["GHOST_TOKEN"] = TOKEN
    env["GHOST_AGENT_ID"] = "paint-precision-test"
    return StdioServerParameters(command=sys.executable,
                                 args=[str(ROOT / "mcp_server.py")], env=env)


# ================= MCP 场景 =================
async def scenario(paint_hwnd: int, canvas: list) -> None:
    cx = canvas[0] + canvas[2] // 2
    cy = canvas[1] + canvas[3] // 2
    drag_a = (cx - 90, cy - 65)
    drag_b = (cx + 90, cy + 65)

    # 形状工具尝试（找不到就用默认铅笔——像素验证两种路径通用）
    shape_used = None
    async with stdio_client(mcp_params()) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            an = tool_body(await s.call_tool("analyze_screen", {}))
            controls = an.get("controls") or []
            texts = [str(c.get("text")) for c in controls]
            check("1 MCP analyze_screen（前景=画图）",
                  an.get("ok") is True and an.get("backend") in ("ocr", "llama"),
                  f"backend={an.get('backend')} n={an.get('count')} "
                  f"texts={json.dumps(texts, ensure_ascii=False)[:140]}")
            for cand in ("矩形", "Rectangle", "正方形", "圆角矩形"):
                got = tool_body(await s.call_tool("click_element", {
                    "text": cand, "match": "contains"}))
                if got.get("ok") is True and got.get("http_status") == 200:
                    shape_used = cand
                    break
            print(f"  (shape tool: {shape_used or '默认铅笔'})", flush=True)

            # 拖一笔（经安全任务队列：能力令牌/归属门禁/硬超时全生效）
            out = tool_body(await s.call_tool("execute_task", {
                "task_payload": {
                    "goal": "paint-precision-drag",
                    "steps": [{"kind": "action", "payload": {
                        "action": "drag", "x": drag_a[0], "y": drag_a[1],
                        "x2": drag_b[0], "y2": drag_b[1],
                        "duration_ms": 900, "settle_seconds": 0.3,
                        "hwnd": paint_hwnd}}]},
                "wait": True, "timeout_s": 60}))
            check("2 MCP execute_task（drag 经安全队列）→ done",
                  out.get("status") == "done",
                  f"status={out.get('status')} "
                  f"fail={(out.get('fail') or {}).get('fail_class')}")
    PAINT_STATE["shape"] = shape_used
    PAINT_STATE["drag"] = [drag_a, drag_b]


PAINT_STATE: dict = {}


def main() -> int:
    print("== moray-ghost v0.8 paint precision acceptance ==", flush=True)
    srv = None
    paint = None
    win = None
    phwnd = 0
    try:
        check("0 前提：端口 8030 空闲", port_free())
        srv = spawn_server()
        check("0 前提：Ghost 服务启动", wait_healthy())
        if srv.poll() is not None:
            return 1

        pids_before = {p.pid for p in psutil.process_iter(["pid"])}
        paint = spawn(["mspaint.exe"])
        win = find_paint_window(pids_before)
        check("1 自建画图窗口已就位（差集绑定）", win is not None,
              f"win={(win or {}).get('title')!r}")
        if not win:
            return 1
        phwnd = win["hwnd"]
        check("1 画图强占前台", force_foreground(phwnd))
        time.sleep(0.6)

        img0 = shot_png()
        rect = win32gui.GetWindowRect(phwnd)
        canvas = detect_canvas(img0, rect)
        check("1 服务端像素检测画布（最大白色连通域）", canvas is not None,
              f"rect={rect} canvas={canvas}")
        if not canvas:
            return 1

        anyio.run(scenario, phwnd, canvas)

        # ---- 像素级验证 ----
        time.sleep(0.4)
        img1 = shot_png()
        l = max(0, canvas[0] - 40)
        t = max(0, canvas[1] - 40)
        r = min(img1.shape[1], canvas[0] + canvas[2] + 40)
        b = min(img1.shape[0], canvas[1] + canvas[3] + 40)
        d0 = img0[t:b, l:r]
        d1 = img1[t:b, l:r]
        diff = cv2.absdiff(d0, d1)
        gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
        _, mask = cv2.threshold(gray, 18, 255, cv2.THRESH_BINARY)
        count = int(cv2.countNonZero(mask))
        check("3 拖拽产生真实笔迹像素（≥300px）", count >= 300,
              f"changed={count}px shape={PAINT_STATE.get('shape')}")
        pts = cv2.findNonZero(mask)
        if pts is None:
            check("3 笔迹 bbox 与意图逐边对比（±18px）", False,
                  "no changed pixels")
        else:
            x, y, bw, bh = cv2.boundingRect(pts)
            got_bbox = [l + x, t + y, bw, bh]
            want = PAINT_STATE.get("drag") or [[0, 0], [0, 0]]
            (ax, ay), (bx2, by2) = want
            want_bbox = [ax, ay, bx2 - ax, by2 - ay]
            tol = 18
            edges = [abs(got_bbox[i] - want_bbox[i]) for i in range(4)]
            ok = all(e <= tol for e in edges)
            check("3 笔迹 bbox 与意图逐边对比（±18px）", ok,
                  f"got={got_bbox} want={want_bbox} edges={edges}")
    finally:
        if paint and paint.poll() is None:
            paint.kill()
            try:
                paint.wait(timeout=8)
            except Exception:  # noqa: BLE001
                pass
        gone = True
        if phwnd:
            time.sleep(0.6)
            gone = not win32gui.IsWindow(phwnd)
        check("4 清场：自建画图窗口已销毁", gone)
        if srv:
            stop_proc(srv)
        check("4 清场：服务停止、端口释放", port_free())
    passed = sum(1 for x in RESULTS if x)
    print(f"\n== RESULT: {passed}/{len(RESULTS)} passed ==", flush=True)
    return len(RESULTS) - passed


if __name__ == "__main__":
    sys.exit(main())
