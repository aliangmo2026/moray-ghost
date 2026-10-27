"""v0.14 MCP Resources 协议测试：stdio JSON-RPC 真实握手 + 资源读取。

验证客户端 AI「不调工具就能看到环境」的完整链路：
  1. initialize 握手（mcp 2.3.0，protocolVersion 2026-07-28）→
     serverInfo/capabilities.resources 声明；
  2. resources/list 同时含 screen://current（image/jpeg）与
     ui://elements（application/json）；
  3. resources/read screen://current → blob base64 → JPEG 魔数 +
     解码尺寸 == 主屏物理分辨率（与 analyze/bbox 同一坐标系的证明）；
  4. resources/read ui://elements → 元素表 JSON：先自建带文字的 Tk 靶窗
     置前台，读回的表必须含 ≥1 个带 id/bbox 的元素，且 bbox 落在屏内；
  5. tools/list 回归（Resources 与 Tools 共存，v0.12 工具一个不少）；
  6. 未知资源 uri → JSON-RPC error（错误路径如实报，不静默返回空）。

铁律：mcp_server 的 stdout 只承载协议帧——本测试只按行读 JSON-RPC，
诊断走 stderr；靶窗自建；ghost 服务与 MCP 进程全部杀进程树。
"""
from __future__ import annotations

import base64
import ctypes
import io
import json
import queue
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

if sys.platform == "win32":
    ROOT = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(ROOT))

import tkinter as tk  # noqa: E402

import psutil  # noqa: E402
import requests  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402

from PIL import Image  # noqa: E402

from app.core import config, screen_mss  # noqa: E402

TOKEN = config.ensure_token()
BASE = "http://127.0.0.1:8030/api/ghost"
PROTOCOL = "2026-07-28"

RESULTS: list = []
PROCS: list = []  # (Popen, kind)


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  | {detail}" if detail else ""), flush=True)
    return bool(ok)


def port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def kill_by_port(port: int) -> None:
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.laddr and c.laddr.port == port and c.pid:
                psutil.Process(c.pid).kill()
                time.sleep(0.5)
    except Exception:  # noqa: BLE001
        pass


def kill_tree(proc: subprocess.Popen) -> None:
    try:
        parent = psutil.Process(proc.pid)
        tree = parent.children(recursive=True) + [parent]
    except psutil.Error:
        tree = []
    for p in tree:
        try:
            p.terminate()
        except psutil.Error:
            pass
    _gone, alive = psutil.wait_procs(tree, timeout=6)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass


def wait_health(timeout: float = 40.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = requests.get(f"{BASE}/health", timeout=2)
            if r.status_code == 200 and r.json().get("status") == "ok":
                return r.json()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.35)
    return {}


# ---------------- stdio JSON-RPC 客户端 ----------------

class McpClient:
    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self.inbox: "queue.Queue[dict]" = queue.Queue()
        self._id = 0
        self.stderr_tail: list = []
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self.inbox.put(json.loads(line))
                except ValueError:
                    self.stderr_tail.append("BAD-FRAME:" + line[:120])
        except Exception:  # noqa: BLE001
            pass

    def _read_stderr(self) -> None:
        try:
            for line in self.proc.stderr:
                self.stderr_tail.append(line.rstrip()[:200])
                if len(self.stderr_tail) > 40:
                    del self.stderr_tail[:-40]
        except Exception:  # noqa: BLE001
            pass

    def send(self, payload: dict) -> None:
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict,
                timeout: float = 120.0) -> dict:
        self._id += 1
        rid = self._id
        self.send({"jsonrpc": "2.0", "id": rid, "method": method,
                   "params": params})
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = self.inbox.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                break
            if msg.get("id") == rid:
                return msg
            # 通知/其他 id（本测试单路串行，跳过）
        raise TimeoutError(f"no response for {method} id={rid} "
                           f"(stderr tail={self.stderr_tail[-3:]})")

    def notify(self, method: str, params: dict | None = None) -> None:
        self.send({"jsonrpc": "2.0", "method": method,
                   "params": params or {}})


# ---------------- 靶窗 ----------------

def force_foreground(hwnd: int) -> bool:
    """抢前台：Windows 会拒绝非前台进程的 SetForegroundWindow →
    Alt 空按片一次前台授权再设（v0.12/v0.14 测试通用手法），并校验结果。"""
    for _ in range(6):
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
        except Exception:  # noqa: BLE001
            pass
        if win32gui.GetForegroundWindow() == hwnd:
            return True
        try:
            ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)   # Alt down
            ctypes.windll.user32.keybd_event(0x12, 0, 2, 0)   # Alt up
            win32gui.SetForegroundWindow(hwnd)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.2)
        if win32gui.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.2)
    return win32gui.GetForegroundWindow() == hwnd


def spawn_text_target(title: str) -> tuple:
    """带多个文字标签的 Tk 窗口（保证 OCR/元素表必然命中 ≥1）。"""
    box: dict = {}

    def _run() -> None:
        root = tk.Tk()
        box["root"] = root
        root.title(title)
        root.geometry("520x300+300+240")
        tk.Label(root, text="Save File Now",
                 font=("Arial", 22, "bold")).pack(pady=30)
        tk.Label(root, text="Cancel Operation",
                 font=("Arial", 18)).pack(pady=10)
        root.update()
        frame = root.frame()
        box["hwnd"] = int(frame, 16) if isinstance(frame, str) else int(frame)
        root.mainloop()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    for _ in range(100):
        if box.get("hwnd"):
            break
        time.sleep(0.05)
    if not box.get("hwnd"):
        raise RuntimeError("text target did not come up")
    hwnd = box["hwnd"]
    if not force_foreground(hwnd):
        raise RuntimeError("could not foreground text target hwnd="
                           + str(hwnd))
    time.sleep(0.8)
    return box, hwnd


def main() -> int:
    print("v0.14 MCP Resources 协议测试")
    ghost = None
    mcp_proc = None
    try:
        if not port_free(8030):
            kill_by_port(8030)
            time.sleep(1.0)
        ghost = subprocess.Popen(
            [sys.executable, str(ROOT / "run.py")], cwd=str(ROOT),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace")
        PROCS.append(ghost)
        threading.Thread(target=lambda: [None for _ in ghost.stdout],
                         daemon=True).start()
        health = wait_health()
        check("S1 ghost 服务启动", health.get("status") == "ok",
              str(health.get("status")))

        # MCP 进程：stdout = 协议专用通道（绝不允许任何 print 污染）
        env = dict(**__import__("os").environ)
        env["GHOST_TOKEN"] = TOKEN
        mcp_proc = subprocess.Popen(
            [sys.executable, str(ROOT / "mcp_server.py")], cwd=str(ROOT),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", env=env)
        PROCS.append(mcp_proc)
        client = McpClient(mcp_proc)

        # 1. initialize 握手
        resp = client.request("initialize", {
            "protocolVersion": PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "v14-resources-test", "version": "1.0"}})
        result = resp.get("result") or {}
        caps = result.get("capabilities") or {}
        check("R1 initialize 握手 + serverInfo",
              result.get("serverInfo", {}).get("name") == "moray-ghost",
              f"proto={result.get('protocolVersion')} "
              f"server={result.get('serverInfo')}")
        check("R2 capabilities 声明 resources",
              "resources" in caps,
              f"caps={sorted(caps.keys())}")
        client.notify("notifications/initialized")

        # 2. resources/list
        resp = client.request("resources/list", {})
        resources = (resp.get("result") or {}).get("resources") or []
        by_uri = {r.get("uri"): r for r in resources}
        check("R3 resources/list 含 screen://current + ui://elements",
              "screen://current" in by_uri and "ui://elements" in by_uri,
              f"uris={sorted(by_uri.keys())}")
        check("R4 资源 mimeType 正确",
              by_uri.get("screen://current", {}).get("mimeType")
              == "image/jpeg"
              and by_uri.get("ui://elements", {}).get("mimeType")
              == "application/json",
              f"screen={by_uri.get('screen://current', {}).get('mimeType')} "
              f"ui={by_uri.get('ui://elements', {}).get('mimeType')}")

        # 3. 读 screen://current（先放一个靶窗置前台，截图非空有内容）
        box, hwnd = spawn_text_target("v14-mcp-shot-target")
        resp = client.request("resources/read",
                              {"uri": "screen://current"}, timeout=60)
        if "error" in resp:
            check("R5 read screen://current → JPEG blob", False,
                  json.dumps(resp["error"], ensure_ascii=False)[:200])
        else:
            contents = (resp.get("result") or {}).get("contents") or []
            ok = False
            detail = f"contents={len(contents)}"
            if len(contents) == 1:
                blob = contents[0].get("blob") or ""
                raw = base64.b64decode(blob)
                img = Image.open(io.BytesIO(raw))
                mon1 = [m for m in screen_mss.monitors()
                        if m["index"] == 1][0]
                ok = (raw[:3] == b"\xff\xd8\xff"
                      and contents[0].get("mimeType") == "image/jpeg"
                      and img.size == (mon1["width"], mon1["height"])
                      and len(raw) > 20_000)
                detail = (f"magic={raw[:3].hex()} size={img.size} "
                          f"expect={(mon1['width'], mon1['height'])} "
                          f"bytes={len(raw)}")
            check("R5 read screen://current → JPEG blob（物理全分辨率）",
                  ok, detail)

        # 4. 读 ui://elements（靶窗置前台 → 元素表必含其文字）
        fg_ok = force_foreground(hwnd)
        time.sleep(1.0)  # 让 RAG 检索缓存（默认 TTL 2s）过期后再读
        resp = client.request("resources/read",
                              {"uri": "ui://elements"}, timeout=180)
        if "error" in resp:
            check("R6 read ui://elements → 元素表 JSON", False,
                  json.dumps(resp["error"], ensure_ascii=False)[:200])
        else:
            contents = (resp.get("result") or {}).get("contents") or []
            ok = len(contents) == 1
            detail = f"contents={len(contents)}"
            if ok:
                text = contents[0].get("text") or ""
                try:
                    data = json.loads(text)
                except ValueError:
                    ok, data = False, {}
                    detail = "text is not JSON: " + text[:120]
                if ok:
                    elements = data.get("elements") or []
                    mon1 = [m for m in screen_mss.monitors()
                            if m["index"] == 1][0]
                    in_screen = all(
                        isinstance(e.get("bbox"), list)
                        and len(e["bbox"]) == 4
                        and mon1["left"] <= e["bbox"][0]
                        and e["bbox"][0] + e["bbox"][2]
                        <= mon1["left"] + mon1["width"]
                        for e in elements)
                    labels = [str(e.get("text") or "") for e in elements]
                    hit = any("Save" in t or "Cancel" in t for t in labels)
                    ok = (data.get("ok") is not False and len(elements) >= 1
                          and in_screen and hit and fg_ok)
                    detail = (f"elements={len(elements)} "
                              f"in_screen={in_screen} fg={fg_ok} "
                              f"target_hit={hit} labels={labels[:6]}")
            check("R6 read ui://elements → 元素表 JSON（≥1 且靶窗命中）",
                  ok, detail)

        # 5. tools/list 回归（Resources 与 Tools 共存）
        resp = client.request("tools/list", {})
        tools = (resp.get("result") or {}).get("tools") or []
        names = sorted(t.get("name") for t in tools)
        need = {"analyze_screen", "click_element", "type_text",
                "execute_task", "draw_trajectory"}
        check("R7 tools/list 回归（v0.12 五工具俱在）",
              need.issubset(set(names)), f"tools={names}")

        # 6. 未知资源 → JSON-RPC error（错误路径不静默）
        resp = client.request("resources/read",
                              {"uri": "ghost://does-not-exist"}, timeout=30)
        check("R8 未知资源 uri → JSON-RPC error",
              "error" in resp,
              json.dumps(resp.get("error") or resp.get("result"),
                         ensure_ascii=False)[:160])
    finally:
        for p in (mcp_proc, ghost):
            if p is not None:
                kill_tree(p)

    total = len(RESULTS)
    passed = sum(1 for r in RESULTS if r)
    print(f"\n===== v14 MCP Resources：{passed}/{total} PASS =====")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
