"""v0.12 重型实战：Tk 表单 + 本地浏览器表单，全程录制 GIF/MP4 到 docs/assets。

这不是单元测试，而是"手和眼 + 安全门禁"的完整演示：
  A 原生 Tk 表单（记事本风格，纯本地）：
    GUI RAG 检索 → RAG 选号「用户名/备注/保存成功」→ 真实点击+键入
    → 保存按钮读回输入框，标题/状态栏回显 ghost_agent / gui-rag-ok
    → Ghost Guard 拦截「删除账户」PENDING_APPROVAL（零输入）
    → 人工审批令牌 → 真实点击（标题 DANGER-1）。
  B 本地浏览器表单（自建 Edge + 临时 profile + 本地 HTML，绝不碰微信/真实账户）：
    CDP fill 姓名/邮箱/备注 → 点击提交（页面标题 SUBMITTED）
    → Guard 拦截「删除记录」→ 审批后 DELETED
    → Guard 拦截「退出登录」→ 审批后 LOGOUT。
全程录制：原生 Tk 段 3fps 抓窗口区（走服务端单一 capture 线程 /screenshot，
不自开 mss）；浏览器段 headless Edge + CDP 页面截图（避免 Edge 同步弹窗遮挡）
→ 带阶段字幕合成 GIF + MP4 + 最终 PNG，写入 docs/assets/。

铁律：Tk 靶窗自建（stdout 握手绑定 hwnd）；浏览器使用独立临时 profile
（--headless=new），绝不复用用户 Edge 配置；危险按钮只改本地页面/标题，
不连接任何真实业务；清场只杀自己的进程树（按临时 profile 指纹精确匹配）。
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
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

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import psutil  # noqa: E402
import requests  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from app.core import config  # noqa: E402

TOKEN = config.ensure_token()
APPROVAL = "v12-human-approval-secret"
CAPS = {"allowed_windows": ["python.exe"]}
ASSETS = ROOT / "docs" / "assets"
RESULTS: list = []
PROCS: list = []
SERVER: dict = {"proc": None}
CDP_PORT = 9223

NATIVE_GIF = ASSETS / "gui_rag_form_guard.gif"
NATIVE_MP4 = ASSETS / "gui_rag_form_guard.mp4"
NATIVE_PNG = ASSETS / "gui_rag_form_final.png"
BROWSER_GIF = ASSETS / "browser_form_guard.gif"
BROWSER_MP4 = ASSETS / "browser_form_guard.mp4"
BROWSER_PNG = ASSETS / "browser_form_final.png"

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>moray-v12-browser-form</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; padding: 36px 48px; background: #12141a;
         color: #e8edf5; font-family: "Microsoft YaHei", "Segoe UI", sans-serif; }
  .card { max-width: 760px; margin: 0 auto; background: #1b1f2a;
          border: 1px solid #2d3546; border-radius: 16px; padding: 28px 32px;
          box-shadow: 0 16px 40px rgba(0,0,0,.35); }
  h2 { margin: 0 0 6px; font-size: 28px; color: #f4f7fb; }
  p.sub { margin: 0 0 24px; color: #8d99ad; font-size: 15px; }
  label { display: block; margin: 14px 0 6px; color: #b9c4d6; font-size: 16px; }
  input, textarea { width: 100%; padding: 11px 13px; font-size: 17px;
                    color: #f1f5fb; background: #11141b;
                    border: 1px solid #39445a; border-radius: 9px; outline: none; }
  input:focus, textarea:focus { border-color: #4c9aff; }
  textarea { height: 76px; resize: none; }
  .row { display: flex; gap: 12px; margin-top: 24px; flex-wrap: wrap; }
  button { font-size: 17px; padding: 11px 22px; border-radius: 9px; border: 0;
           cursor: pointer; background: #2f6feb; color: #fff; }
  button.ghost { background: #2a3142; color: #d6deeb; }
  button.danger { background: #7a2834; color: #ffd9dd; }
  #status { margin-top: 22px; padding: 13px 15px; border-radius: 9px;
            background: #11141b; border: 1px solid #2d3546;
            color: #9fe3b8; font-size: 16px; min-height: 22px; }
</style>
</head>
<body>
<div class="card">
  <h2>Ghost 本地开户表单</h2>
  <p class="sub">moray-ghost v0.12 · GUI RAG + Ghost Guard 演示（纯本地，不连接真实账户）</p>
  <label>姓名</label><input id="name" placeholder="请输入姓名">
  <label>邮箱</label><input id="email" placeholder="name@example.local">
  <label>备注</label><textarea id="note" placeholder="给 Ghost 留一句话"></textarea>
  <div class="row">
    <button id="submit">提交</button>
    <button id="clear" class="ghost">清空</button>
    <button id="delete" class="danger">删除记录</button>
    <button id="logout" class="danger">退出登录</button>
  </div>
  <div id="status">就绪</div>
</div>
<script>
  const $ = (id) => document.getElementById(id);
  const val = (id) => $(id).value;
  const status = (s) => { $('status').textContent = s; };
  $('submit').onclick = () => {
    status('已提交: ' + val('name') + ' / ' + val('email') + ' / ' + val('note'));
    document.title = 'SUBMITTED:' + val('name') + '/' + val('email');
  };
  $('clear').onclick = () => {
    $('name').value = ''; $('email').value = ''; $('note').value = '';
    status('已清空'); document.title = 'CLEARED';
  };
  $('delete').onclick = () => {
    status('记录已删除'); document.title = 'DELETED';
  };
  $('logout').onclick = () => {
    status('已退出登录'); document.title = 'LOGOUT';
  };
</script>
</body>
</html>
"""


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  | {detail}" if detail else ""), flush=True)
    return bool(ok)


def hdr() -> dict:
    return {"X-Ghost-Token": TOKEN}


def port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def free_port(start: int = 9223, end: int = 9250) -> int:
    for p in range(start, end):
        if port_free(p):
            return p
    raise RuntimeError("no free CDP port in range")


def kill_by_port(port: int) -> None:
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.laddr and c.laddr.port == port and c.pid:
                psutil.Process(c.pid).kill()
                time.sleep(0.5)
    except Exception:  # noqa: BLE001
        pass


def spawn(args: list, env_extra: dict | None = None,
          drain: bool = True) -> subprocess.Popen:
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.Popen(args, cwd=str(ROOT), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env,
                            text=True, encoding="utf-8", errors="replace")
    PROCS.append(proc)
    if not drain:
        return proc

    def _drain() -> None:
        try:
            for _ in proc.stdout:
                pass
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=_drain, daemon=True).start()
    return proc


def start_server(cdp_port: int) -> subprocess.Popen:
    return spawn([sys.executable, str(ROOT / "run.py")],
                 {"GHOST_APPROVAL_TOKEN": APPROVAL,
                  "GHOST_BROWSER_CDP_URL":
                      "http://127.0.0.1:" + str(cdp_port)})


def wait_health(timeout: float = 35.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = requests.get(
                "http://127.0.0.1:8030/api/ghost/health", timeout=2)
            if r.status_code == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.35)
    return False


def stop_proc(proc: subprocess.Popen, port: int | None = None) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=6)
    if port and not port_free(port):
        kill_by_port(port)


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
    for _ in range(5):
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
        time.sleep(0.25)
        if win32gui.GetForegroundWindow() == hwnd:
            return True
    return False


def title_of(hwnd: int) -> str:
    try:
        return win32gui.GetWindowText(int(hwnd))
    except Exception:  # noqa: BLE001
        return ""


def wait_title(hwnd: int, pred, timeout: float = 6.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred(title_of(hwnd)):
            return True
        time.sleep(0.15)
    return pred(title_of(hwnd))


def cursor_pos() -> tuple:
    import ctypes.wintypes
    pt = ctypes.wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def restore_cursor(pos: tuple) -> None:
    try:
        ctypes.windll.user32.SetCursorPos(int(pos[0]), int(pos[1]))
    except Exception:  # noqa: BLE001
        pass


def window_rect(hwnd: int) -> tuple:
    r = win32gui.GetWindowRect(int(hwnd))
    return int(r[0]), int(r[1]), int(r[2] - r[0]), int(r[3] - r[1])


def kill_edge_profile(profile: Path, edge_pid: int,
                      hwnd: int, cdp_port: int,
                      timeout: float = 15.0) -> dict:
    """只杀本次临时 profile 的 Edge 进程树（绝不碰用户已有 Edge）。

    Edge 的窗口宿主进程不一定是 Popen 返回的 launcher PID（可能重新挂到
    另一个 msedge.exe），`taskkill /T /PID launcher` 会漏。以
    --user-data-dir=<本次唯一临时目录> 为指纹精确匹配命令行，再兜底
    taskkill /T；最后等到窗口消失 + CDP 端口释放。
    """
    pids: set = {int(edge_pid)}
    needles = {str(profile), str(profile).replace("\\", "/")}
    try:
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmd = " ".join(p.info.get("cmdline") or [])
            except (psutil.Error, TypeError):
                continue
            if any(n and n in cmd for n in needles):
                pids.add(int(p.info["pid"]))
    except Exception:  # noqa: BLE001
        pass
    for pid in sorted(pids, reverse=True):
        try:
            psutil.Process(pid).kill()
        except Exception:  # noqa: BLE001
            pass
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(edge_pid)],
                   capture_output=True)
    end = time.time() + timeout
    while time.time() < end:
        gone = (not hwnd) or (not win32gui.IsWindow(hwnd))
        if gone and port_free(cdp_port):
            break
        time.sleep(0.3)
    return {
        "pids": sorted(pids),
        "window_gone": (not hwnd) or (not win32gui.IsWindow(hwnd)),
        "port_free": port_free(cdp_port),
        "waited_s": round(max(0.0, timeout - (end - time.time())), 2),
    }


def vl_elements(window: str) -> dict:
    r = requests.post(
        "http://127.0.0.1:8030/api/ghost/vl/elements",
        headers=hdr(), json={"window": window, "backend": "auto"},
        timeout=180)
    try:
        return r.json()
    except ValueError:
        return {"ok": False, "raw": r.text[:300]}


def vl_ground(query: str, window: str) -> dict:
    r = requests.post(
        "http://127.0.0.1:8030/api/ghost/vl/ground",
        headers=hdr(), json={"window": window, "query": query,
                             "backend": "auto", "strategy": "auto"},
        timeout=180)
    try:
        return r.json()
    except ValueError:
        return {"ok": False, "raw": r.text[:300]}


def ghost_action(payload: dict) -> dict:
    body = {"monitor": 1, "settle_seconds": 0.08, "fmt": "png",
            "quality": 70, "scale": 0.25, "capabilities": CAPS}
    body.update(payload)
    r = requests.post(
        "http://127.0.0.1:8030/api/ghost/execute_action",
        headers=hdr(), json=body, timeout=60)
    try:
        out = r.json()
    except ValueError:
        out = {"raw": r.text[:300]}
    out["http_status"] = r.status_code
    return out


def click_at(x: int, y: int, hwnd: int,
             approval: str | None = None) -> dict:
    payload = {"action": "click", "x": int(x), "y": int(y),
               "hwnd": int(hwnd)}
    if approval is not None:
        payload["approval_token"] = approval
    return ghost_action(payload)


def hotkey(combo: str, hwnd: int) -> dict:
    return ghost_action({"action": "hotkey", "text": combo,
                         "hwnd": int(hwnd)})


def type_text(text: str, x: int, y: int, hwnd: int) -> dict:
    return ghost_action({"action": "type", "text": text, "x": int(x),
                         "y": int(y), "hwnd": int(hwnd)})


def browser_action(payload: dict) -> dict:
    body = {"tab": {"url_contains": "target.html"}}
    body.update(payload)
    r = requests.post(
        "http://127.0.0.1:8030/api/ghost/browser/action",
        headers=hdr(), json=body, timeout=90)
    try:
        out = r.json()
    except ValueError:
        out = {"raw": r.text[:300]}
    out["http_status"] = r.status_code
    return out


def browser_tabs() -> list:
    r = requests.get(
        "http://127.0.0.1:8030/api/ghost/browser/tabs",
        headers=hdr(), timeout=30)
    try:
        return (r.json() or {}).get("tabs") or []
    except ValueError:
        return []


def tab_title_has(needle: str) -> bool:
    return any(needle in (t.get("title") or "") for t in browser_tabs())


def wait_tab_title(needle: str, timeout: float = 8.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if tab_title_has(needle):
            return True
        time.sleep(0.3)
    return tab_title_has(needle)


def by_label(elements: list, label: str) -> dict | None:
    for e in elements:
        if label in (e.get("text"), e.get("label")):
            return e
    return None


# ---- 录屏（3fps 窗口区；阶段字幕由合成器绘制） -----------------------------
class ScreenRecorder:
    def __init__(self, crop: tuple, fps: float = 3.0,
                 max_frames: int = 360):
        self.crop = crop
        self.interval = 1.0 / max(1.0, fps)
        self.max_frames = int(max_frames)
        self.frames: list = []       # [(jpeg_bytes, epoch_ts)]
        self.phases: list = []       # [(epoch_ts, caption)]
        self.errors = 0
        self.first_exc = ""
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def phase(self, caption: str) -> None:
        self.phases.append((time.time(), str(caption)))
        print("  [phase] " + str(caption), flush=True)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            t0 = time.time()
            try:
                r = requests.get(
                    "http://127.0.0.1:8030/api/ghost/screenshot",
                    params={"monitor": 1, "fmt": "jpeg", "scale": 1.0,
                            "quality": 80},
                    headers=hdr(), timeout=15)
                if r.status_code == 200:
                    raw = base64.b64decode(r.json()["image_b64"])
                    arr = cv2.imdecode(np.frombuffer(raw, np.uint8),
                                       cv2.IMREAD_COLOR)
                    if arr is not None:
                        x, y, w, h = self.crop
                        crop = arr[max(0, y):y + h, max(0, x):x + w]
                        if crop.size:
                            ok, buf = cv2.imencode(
                                ".jpg", crop,
                                [cv2.IMWRITE_JPEG_QUALITY, 84])
                            if ok:
                                self.frames.append((buf.tobytes(), t0))
                            else:
                                self.errors += 1
                else:
                    self.errors += 1
            except Exception as exc:  # noqa: BLE001
                self.errors += 1
                if not self.first_exc:
                    import traceback
                    self.first_exc = traceback.format_exc(limit=3)
                    print("  [recorder] first error: " + repr(exc),
                          flush=True)
            if len(self.frames) >= self.max_frames:
                break
            self.stop_event.wait(
                max(0.0, self.interval - (time.time() - t0)))

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self) -> int:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=10)
        return len(self.frames)


class PageRecorder:
    """CDP 页面级录像：只用 Playwright page.screenshot，不抓桌面。

    浏览器自身可能弹同步/首次运行对话框；页面截图只包含真实网页内容，
    填表/提交/Guard 拦截的证据不会被浏览器 UI 遮住。
    """

    def __init__(self) -> None:
        self.frames: list = []     # [(png_bytes, epoch_ts)]
        self.phases: list = []     # [(epoch_ts, caption)]
        self.errors = 0

    def phase(self, caption: str) -> None:
        self.phases.append((time.time(), str(caption)))
        print("  [phase] " + str(caption), flush=True)

    def add_b64(self, b64: str) -> None:
        if not b64:
            self.errors += 1
            return
        try:
            raw = base64.b64decode(b64)
        except Exception:  # noqa: BLE001
            self.errors += 1
            return
        self.frames.append((raw, time.time()))

    def add_response(self, resp: dict) -> None:
        for key in ("before_image_b64", "after_image_b64"):
            if resp.get(key):
                self.add_b64(resp[key])

    def stop(self) -> int:
        return len(self.frames)


def _font(size: int):
    for path in (r"C:\Windows\Fonts\msyh.ttc",
                 r"C:\Windows\Fonts\segoeui.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _caption_at(phases: list, ts: float) -> str:
    label = "v0.12 · GUI RAG + Ghost Guard"
    for t, text in phases:
        if t <= ts:
            label = text
    return label


def compose_media(rec, gif_path: Path, mp4_path: Path,
                  png_path: Path, max_w: int = 620,
                  colors: int = 96, frame_ms: int = 140) -> dict:
    """带阶段字幕合成 GIF/MP4 + 最终 PNG；返回帧数/尺寸证据。"""
    if not rec.frames:
        return {"frames": 0}
    font = _font(17)
    small = _font(13)
    images: list = []
    for jb, ts in rec.frames:
        arr = cv2.imdecode(np.frombuffer(jb, np.uint8), cv2.IMREAD_COLOR)
        if arr is None:
            continue
        im = Image.fromarray(cv2.cvtColor(arr, cv2.COLOR_BGR2RGB))
        if im.width > max_w:
            im = im.resize((max_w, int(im.height * max_w / im.width)),
                           Image.LANCZOS)
        bar_h = 48
        canvas = Image.new("RGB", (im.width, im.height + bar_h),
                           (16, 18, 23))
        canvas.paste(im, (0, bar_h))
        draw = ImageDraw.Draw(canvas)
        draw.rectangle([0, 0, 6, bar_h], fill=(0, 170, 90))
        caption = _caption_at(rec.phases, ts)
        draw.text((16, 8), "moray-ghost · " + caption,
                  fill=(238, 242, 248), font=font)
        draw.text((16, bar_h - 20), "v0.12 · GUI RAG + Ghost Guard · 本地演示",
                  fill=(120, 132, 150), font=small)
        images.append(canvas)
    if not images:
        return {"frames": 0}
    gif_path.parent.mkdir(parents=True, exist_ok=True)
    pal = [im.convert("P", palette=Image.ADAPTIVE, colors=colors)
           for im in images]
    pal[0].save(gif_path, save_all=True, append_images=pal[1:],
                duration=frame_ms, loop=0, optimize=True, disposal=2)
    images[-1].save(png_path, format="PNG")
    h, w = images[0].height, images[0].width
    vw = cv2.VideoWriter(str(mp4_path),
                         cv2.VideoWriter_fourcc(*"mp4v"), 12.0, (w, h))
    if vw.isOpened():
        for im in images:
            vw.write(cv2.cvtColor(np.array(im.convert("RGB")),
                                  cv2.COLOR_RGB2BGR))
        vw.release()
    return {
        "frames": len(images),
        "errors": rec.errors,
        "gif": gif_path.is_file() and gif_path.stat().st_size,
        "mp4": mp4_path.is_file() and mp4_path.stat().st_size,
        "png": png_path.is_file() and png_path.stat().st_size,
        "size": [w, h],
    }


# ============================== A: Tk 表单 =================================
def native_form_demo(hwnd: int, base_title: str) -> dict:
    print("\n== A Tk 表单实战（GUI RAG + Guard + GIF） ==", flush=True)
    force_foreground(hwnd)
    time.sleep(0.4)
    ext = vl_elements(base_title)
    els = ext.get("elements") or []
    check("A1 GUI RAG 检索 Tk 表单元素", ext.get("ok") is True
          and len(els) >= 13, f"count={ext.get('count')}")
    check("A2 危险元素已被 Guard 预标注",
          len((ext.get("guard") or {}).get("dangerous_ids") or []) == 3,
          str((ext.get("guard") or {}).get("dangerous_ids")))

    crop = window_rect(hwnd)
    rec = ScreenRecorder(crop, fps=3.0, max_frames=360)
    rec.start()
    time.sleep(0.5)
    rec.phase("RAG 检索：OCR+CV 编号元素")
    time.sleep(1.0)
    try:
        rec.phase("RAG 选号：用户名")
        g_user = vl_ground("用户名", base_title)
        ux, uy = g_user["element"]["center"]
        force_foreground(hwnd)
        r1 = click_at(ux, uy, hwnd)
        check("A3 RAG 命中「用户名」并安全点击", r1.get("http_status") == 200,
              f"id={g_user.get('target_id')} method={g_user.get('method')} "
              f"status={r1.get('http_status')}")
        time.sleep(0.3)
        rec.phase("键入 ghost_agent（Ctrl+A 全选后覆盖）")
        hotkey("ctrl+a", hwnd)
        hotkey("delete", hwnd)
        r2 = type_text("ghost_agent", ux, uy, hwnd)
        time.sleep(0.8)

        rec.phase("RAG 选号：备注")
        g_note = vl_ground("备注", base_title)
        nx, ny = g_note["element"]["center"]
        force_foreground(hwnd)
        r3 = click_at(nx, ny, hwnd)
        time.sleep(0.3)
        rec.phase("键入 gui-rag-ok")
        hotkey("ctrl+a", hwnd)
        hotkey("delete", hwnd)
        r4 = type_text("gui-rag-ok", nx, ny, hwnd)
        time.sleep(0.8)
        check("A4 RAG 命中「备注」并完成键入",
              r3.get("http_status") == 200 and r4.get("http_status") == 200,
              f"id={g_note.get('target_id')} "
              f"status={r4.get('http_status')}")

        rec.phase("RAG 选号：保存成功")
        g_save = vl_ground("保存成功", base_title)
        sx, sy = g_save["element"]["center"]
        force_foreground(hwnd)
        r5 = click_at(sx, sy, hwnd)
        saved = wait_title(
            hwnd, lambda t: "-BTN:保存成功" in t
            and "user=ghost_agent" in t and "note=gui-rag-ok" in t)
        check("A5 保存按钮读回输入框（标题回显 user/note）", saved,
              title_of(hwnd))
        rec.phase("保存成功：标题回显 ghost_agent / gui-rag-ok")
        time.sleep(1.0)

        g_del = vl_ground("删除账户", base_title)
        dx, dy = g_del["element"]["center"]
        before = title_of(hwnd)
        rec.phase("Ghost Guard：删除账户 → PENDING_APPROVAL")
        force_foreground(hwnd)
        blocked = click_at(dx, dy, hwnd)
        hits = (blocked.get("hits")
                or (blocked.get("visual_guard") or {}).get("hits") or [])
        check("A6 删除账户 → 409 PENDING_APPROVAL（零输入）",
              blocked.get("http_status") == 409
              and blocked.get("error") == "PENDING_APPROVAL"
              and title_of(hwnd) == before,
              f"hits={[h.get('token') for h in hits]}")
        time.sleep(1.2)

        rec.phase("人工审批令牌 → 真实点击删除账户")
        force_foreground(hwnd)
        approved = click_at(dx, dy, hwnd, approval=APPROVAL)
        ok_click = wait_title(
            hwnd, lambda t: t.endswith("-BTN:DANGER-1"))
        check("A7 审批后真实点击生效（DANGER-1）",
              approved.get("http_status") == 200 and ok_click,
              f"status={approved.get('http_status')} "
              f"title={title_of(hwnd)!r}")
        time.sleep(1.2)
        final_title = title_of(hwnd)
        check("A8 最终状态仍可读（未冻结/未关窗）",
              bool(final_title) and ("DANGER-1" in final_title),
              final_title)
        _ = (r2, r5)
    finally:
        n = rec.stop()
        media = compose_media(rec, NATIVE_GIF, NATIVE_MP4, NATIVE_PNG,
                              max_w=620)
        check("A9 原生实战 GIF/MP4/PNG 合成",
              n >= 15 and media.get("frames", 0) >= 15
              and (media.get("gif") or 0) > 20000
              and (media.get("mp4") or 0) > 4096
              and (media.get("png") or 0) > 10000,
              f"frames={n} media={media} gif={NATIVE_GIF.name}")
        if rec.first_exc:
            print(rec.first_exc, flush=True)

    return {"elements": els, "media": media}


# ============================ B: 浏览器表单 ================================
def browser_form_demo(cdp_port: int, edge_path: str,
                      tmp_dir: Path) -> dict:
    print("\n== B 浏览器表单实战（CDP + Guard + GIF） ==", flush=True)
    page_file = tmp_dir / "target.html"
    page_file.write_text(PAGE_HTML, encoding="utf-8")
    profile = tmp_dir / "profile"
    profile.mkdir(exist_ok=True)
    edge = subprocess.Popen(
        [edge_path, "--headless=new",
         "--remote-debugging-port=" + str(cdp_port),
         "--user-data-dir=" + str(profile), "--no-first-run",
         "--no-default-browser-check", "--disable-session-crashed-bubble",
         "--disable-sync", "--disable-background-networking",
         "--disable-features=msEdgeSync,msEdgeIdentityFeature,"
         "SigninInterception,msEdgeSignInRing",
         "--window-size=1040,780", str(page_file)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    PROCS.append(edge)
    print(f"  (edge pid={edge.pid} cdp={cdp_port})", flush=True)

    ready = False
    end = time.time() + 25
    while time.time() < end:
        tabs = browser_tabs()
        if any("target.html" in (t.get("url") or "") for t in tabs):
            ready = True
            break
        time.sleep(0.5)
    check("B1 CDP 接入本地浏览器靶页", ready,
          f"tabs={[(t.get('title'), (t.get('url') or '')[-20:]) for t in browser_tabs()][:3]}")
    if not ready:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(edge.pid)],
                       capture_output=True)
        return {}

    shot0 = browser_action({"op": "screenshot"})
    check("B2 CDP 页面截图就绪（录屏不依赖桌面）",
          shot0.get("http_status") == 200 and bool(shot0.get("image_b64")),
          f"status={shot0.get('http_status')} "
          f"b64={len(shot0.get('image_b64') or '')}")
    rec = PageRecorder()
    rec.phase("本地浏览器表单已打开")
    rec.add_b64(shot0.get("image_b64") or "")
    try:
        tab = {"url_contains": "target.html"}
        rec.phase("CDP 填写姓名")
        f1 = browser_action({"op": "fill", "tab": tab,
                             "selector": {"by": "css", "value": "#name"},
                             "text": "张三"})
        rec.add_response(f1)
        time.sleep(0.35)
        rec.phase("CDP 填写邮箱")
        f2 = browser_action({"op": "fill", "tab": tab,
                             "selector": {"by": "css", "value": "#email"},
                             "text": "ghost@example.local"})
        rec.add_response(f2)
        time.sleep(0.35)
        rec.phase("CDP 填写备注")
        f3 = browser_action({"op": "fill", "tab": tab,
                             "selector": {"by": "css", "value": "#note"},
                             "text": "GUI RAG + Ghost Guard 本地演示"})
        rec.add_response(f3)
        check("B3 CDP 三字段填表成功",
              all(x.get("http_status") == 200 for x in (f1, f2, f3)),
              f"status={[x.get('http_status') for x in (f1, f2, f3)]}")
        time.sleep(0.4)
        rec.phase("三字段已填写")
        rec.add_b64((browser_action({"op": "screenshot"})
                     .get("image_b64") or ""))

        rec.phase("点击 提交 → SUBMITTED")
        sub = browser_action({"op": "click", "tab": tab,
                              "selector": {"by": "css", "value": "#submit"}})
        rec.add_response(sub)
        submitted = wait_tab_title("SUBMITTED:张三/ghost@example.local")
        check("B4 提交真实生效（页面 title SUBMITTED）",
              sub.get("http_status") == 200 and submitted,
              f"status={sub.get('http_status')} "
              f"tabs={[t.get('title') for t in browser_tabs()]}")
        time.sleep(0.6)
        rec.phase("提交成功：页面状态 SUBMITTED")
        rec.add_b64((browser_action({"op": "screenshot"})
                     .get("image_b64") or ""))

        rec.phase("Ghost Guard：删除记录 → PENDING_APPROVAL")
        dele = browser_action({"op": "click", "tab": tab,
                               "selector": {"by": "css", "value": "#delete"}})
        hits = (dele.get("hits")
                or (dele.get("visual_guard") or {}).get("hits") or [])
        check("B5 删除记录 → 409 PENDING_APPROVAL",
              dele.get("http_status") == 409
              and dele.get("error") == "PENDING_APPROVAL"
              and (dele.get("visual_guard") or {}).get("source") == "cdp_dom",
              f"hits={[h.get('token') for h in hits]} "
              f"tab={[t.get('title') for t in browser_tabs()]}")
        check("B6 拦截后页面零副作用（title 仍 SUBMITTED）",
              tab_title_has("SUBMITTED:") and not tab_title_has("DELETED"),
              str([t.get("title") for t in browser_tabs()]))
        rec.phase("拦截生效：页面保持 SUBMITTED")
        rec.add_b64((browser_action({"op": "screenshot"})
                     .get("image_b64") or ""))
        time.sleep(0.7)

        rec.phase("人工审批令牌 → DELETED")
        dele2 = browser_action({"op": "click", "tab": tab,
                                "selector": {"by": "css",
                                             "value": "#delete"},
                                "approval_token": APPROVAL})
        rec.add_response(dele2)
        deleted = wait_tab_title("DELETED")
        check("B7 审批后删除真实生效（DELETED）",
              dele2.get("http_status") == 200 and deleted,
              f"status={dele2.get('http_status')} "
              f"guard={(dele2.get('visual_guard') or {}).get('verdict')}")
        time.sleep(0.6)
        rec.phase("删除生效：页面状态 DELETED")
        rec.add_b64((browser_action({"op": "screenshot"})
                     .get("image_b64") or ""))

        rec.phase("Ghost Guard：退出登录 → PENDING_APPROVAL")
        out1 = browser_action({"op": "click", "tab": tab,
                               "selector": {"by": "css",
                                            "value": "#logout"}})
        check("B8 退出登录 → 409 PENDING_APPROVAL",
              out1.get("http_status") == 409
              and out1.get("error") == "PENDING_APPROVAL",
              f"hits={[h.get('token') for h in (out1.get('hits') or [])]}")
        rec.phase("拦截生效：页面保持 DELETED")
        rec.add_b64((browser_action({"op": "screenshot"})
                     .get("image_b64") or ""))
        time.sleep(0.7)

        rec.phase("人工审批令牌 → LOGOUT")
        out2 = browser_action({"op": "click", "tab": tab,
                               "selector": {"by": "css",
                                            "value": "#logout"},
                               "approval_token": APPROVAL})
        rec.add_response(out2)
        logout = wait_tab_title("LOGOUT")
        check("B9 审批后退出登录真实生效（LOGOUT）",
              out2.get("http_status") == 200 and logout,
              f"status={out2.get('http_status')} "
              f"guard={(out2.get('visual_guard') or {}).get('verdict')}")
        time.sleep(0.5)
        rec.phase("退出登录生效：页面状态 LOGOUT")
        rec.add_b64((browser_action({"op": "screenshot"})
                     .get("image_b64") or ""))
    finally:
        n = rec.stop()
        media = compose_media(rec, BROWSER_GIF, BROWSER_MP4, BROWSER_PNG,
                              max_w=660)
        check("B10 浏览器实战 GIF/MP4/PNG 合成",
              n >= 10 and media.get("frames", 0) >= 10
              and (media.get("gif") or 0) > 20000
              and (media.get("mp4") or 0) > 4096
              and (media.get("png") or 0) > 10000,
              f"frames={n} media={media} gif={BROWSER_GIF.name}")
        cleanup = kill_edge_profile(profile, edge.pid, 0, cdp_port)
        check("B11 Edge 进程树销毁（临时 profile 指纹）",
              bool(cleanup["pids"]) and cleanup["port_free"],
              json.dumps(cleanup, ensure_ascii=False))
        check("B12 CDP 调试端口释放", cleanup["port_free"], str(cdp_port))
    return {"media": media}


def main() -> int:
    print("== moray-ghost v0.12 heavy GUI demo (Tk + browser) ==",
          flush=True)
    ASSETS.mkdir(parents=True, exist_ok=True)
    cursor0 = cursor_pos()
    hwnd = 0
    tmp_dir = Path(tempfile.mkdtemp(prefix="moray_v12_heavy_"))
    global CDP_PORT
    try:
        check("0.1 端口 8030 空闲", port_free(8030))
        edge_path = next((p for p in EDGE_CANDIDATES if Path(p).is_file()), "")
        check("0.2 本机 Edge 已定位", bool(edge_path), edge_path)
        CDP_PORT = free_port()
        srv = start_server(CDP_PORT)
        SERVER["proc"] = srv
        check("0.3 Ghost v0.12 服务启动（审批令牌 + CDP 端口回环）",
              wait_health(), f"cdp={CDP_PORT}")
        if srv.poll() is not None:
            return 1

        stem = "t" + str(int(time.time() * 1000))[-8:]
        base_title = "moray-v12-" + stem
        tgt = spawn([sys.executable,
                     str(ROOT / "scripts" / "_v12_form_target.py"), stem],
                    drain=False)
        line = tgt.stdout.readline().strip()
        payload = json.loads(line[len("READY "):]) if line.startswith(
            "READY ") else {}
        hwnd = int(payload.get("hwnd") or 0)
        check("0.4 Tk 表单靶窗就位（自建，10 控件）",
              hwnd > 0 and len(payload.get("controls") or []) == 10,
              f"hwnd={hwnd}")
        if not hwnd:
            return 1
        force_foreground(hwnd)
        native_form_demo(hwnd, base_title)
        check("A10 Tk 靶窗销毁（只认自己 hwnd 消失）", close_hwnd(hwnd))
        hwnd = 0
        stop_proc(tgt)
        time.sleep(0.5)

        if edge_path:
            browser_form_demo(CDP_PORT, edge_path, tmp_dir)
        else:
            check("B0 本机 Edge 缺失，浏览器实战跳过", False,
                  "install Microsoft Edge")
    finally:
        if hwnd:
            close_hwnd(hwnd)
        for p in PROCS:
            try:
                stop_proc(p, port=None)
            except Exception:  # noqa: BLE001
                pass
        for port in (8030, CDP_PORT):
            if not port_free(port):
                kill_by_port(port)
        check("C1 服务/CDP 端口全部释放",
              port_free(8030) and port_free(CDP_PORT),
              f"8030={port_free(8030)} cdp={CDP_PORT}:{port_free(CDP_PORT)}")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        check("C2 临时浏览器 profile 清理",
              not tmp_dir.exists(), str(tmp_dir))
        restore_cursor(cursor0)
        print("  鼠标已归位", cursor0, flush=True)
    passed = sum(1 for x in RESULTS if x)
    print(f"\n== RESULT: {passed}/{len(RESULTS)} passed ==", flush=True)
    print(f"  原生 GIF: {NATIVE_GIF}", flush=True)
    print(f"  浏览器 GIF: {BROWSER_GIF}", flush=True)
    return len(RESULTS) - passed


if __name__ == "__main__":
    sys.exit(main())
