# -*- coding: utf-8 -*-
"""v0.10/v0.11 豆包头像复现验收：方案一「灵魂线稿」+ 方案二「高精度像素画」。

数据：data/doubao.png（主仓发来的 512x512 3D 虚拟人像）。

方案一 灵魂线稿：
  Canny 边缘 → 形态学膨胀连接断线 → 连通域过滤小噪点 → 取面积 Top-N 轮廓
  → 降采样 → 逐条 draw_trajectory（服务端平滑+真实鼠标事件）在画布里描边。

方案二 高精度像素画（v0.11 重构，默认 64x64，可 128x128）：
  绝不逐格点击。PIL 压成 GRID_N×GRID_N → 自适应量化 → 背景语义白化 →
  色板检测（深色主题无关）→ 量化色 LAB 最近色板映射 → **同色连续像素合并成
  run-length 横向轨迹** → 画笔（非铅笔，粗细可控）逐 run 一次 drag 拖出整段
  （每条轨迹 is_painting=true 豁免频率熔断指纹计数——合法长时绘图绝不画到
  一半被安全门禁截断；冻结/归属/前台/Fail-Safe/硬超时等其余门禁一道不少）。
  笔宽闭环：画布左侧浮动滑杆点击设值 → 探针线实测宽度 → 不达标自动再调
  → Ctrl+Z 清探针 → run 端点按 T/2 内缩（圆头帽不越界）+ 多道覆盖补高。

全程录屏：后台线程 4fps 抓画布区（走服务端单一 capture 线程 /screenshot，
不自开 mss 实例）→ cv2 合成加速 MP4 + PIL 合成 GIF → docs/assets/。

红线：只操作本次 Popen 的 mspaint（PID 差集绑定；启动前已有 Paint 直接
SKIP），清场只杀自己的进程树。

跑法：.venv\\Scripts\\python.exe scripts\\test_v10_draw_doubao.py [line|pixel|both] [64|128]
"""
from __future__ import annotations

import base64
import ctypes
import io
import json
import math
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

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import psutil  # noqa: E402
import requests  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402
import win32process  # noqa: E402
from PIL import Image  # noqa: E402

from app.core import config  # noqa: E402

TOKEN = config.ensure_token()
MODE = (sys.argv[1] if len(sys.argv) > 1 else "both").strip().lower()
if MODE not in ("line", "pixel", "both"):
    MODE = "both"
try:
    GRID_N = int(sys.argv[2]) if len(sys.argv) > 2 else 64
except ValueError:
    GRID_N = 64
if GRID_N not in (32, 64, 128):
    GRID_N = 64
QUANT_COLORS = 16 if GRID_N <= 64 else 20

ART = ROOT / "data" / "doubao.png"
OUT_LINE = ROOT / "data" / "screenshots" / "doubao_line_art.png"
OUT_PIXEL = ROOT / "data" / "screenshots" / f"doubao_pixel_art_{GRID_N}.png"
ASSETS = ROOT / "docs" / "assets"
VID_PIXEL = ASSETS / ("doubao_drawing.mp4" if GRID_N == 64
                      else f"doubao_drawing_{GRID_N}.mp4")
GIF_PIXEL = ASSETS / ("doubao_drawing.gif" if GRID_N == 64
                      else f"doubao_drawing_{GRID_N}.gif")
VID_LINE = ASSETS / "doubao_line_drawing.mp4"

RESULTS: list = []
PROCS: list = []
MAX_STROKES = 30
MIN_CONTOUR_AREA = 60.0
MAX_STROKE_PTS = 90

# ---- 自建 Paint UI 标定（_v11_recon.py 读图标定，窗口相对分数，2048x1280） ----
FRAC_BRUSH_MAIN = (0.2930, 0.1352)   # 画笔库按钮上半（重选当前笔，不弹层）
FRAC_SIZE_TRACK_X = 0.0288           # 画布左侧浮动"粗细"滑杆轨道 x
FRAC_SIZE_TRACK_TOP = 0.2789         # 轨道顶端 = 最大
FRAC_SIZE_TRACK_BOT = 0.6078         # 轨道底端 = 最小（默认旋钮 ~80% 深）


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  | {detail}" if detail else ""), flush=True)
    return ok


def port_free(port: int = 8030) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def spawn(args: list, drain: bool = False) -> subprocess.Popen:
    proc = subprocess.Popen(
        args, cwd=str(ROOT), stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        errors="replace")
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


def wait_healthy(timeout: float = 30.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = requests.get("http://127.0.0.1:8030/api/ghost/health",
                             timeout=2)
            if r.status_code == 200:
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


def stop_proc(proc: subprocess.Popen | None, port: int | None = 8030) -> None:
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    if port and not port_free(port):
        kill_by_port(port)


def act(payload: dict, timeout: float = 90.0) -> dict:
    r = requests.post("http://127.0.0.1:8030/api/ghost/execute_action",
                      headers={"X-Ghost-Token": TOKEN}, json=payload,
                      timeout=timeout)
    try:
        body = r.json()
    except ValueError:
        body = {"ok": False, "error": "BAD_JSON", "detail": r.text[:300]}
    body.setdefault("ok", 200 <= r.status_code < 300)
    body["http_status"] = r.status_code
    return body


def draw(points, duration_ms: int, hwnd: int, caps: dict,
         smooth: int = 0, is_painting: bool = True) -> dict:
    """落笔一条轨迹。is_painting=True（默认）豁免频率熔断指纹计数。"""
    r = requests.post("http://127.0.0.1:8030/api/ghost/draw_trajectory",
                      headers={"X-Ghost-Token": TOKEN},
                      json={"points": points, "duration_ms": duration_ms,
                            "hwnd": hwnd, "smooth_iterations": smooth,
                            "is_painting": is_painting,
                            "settle_seconds": 0.03, "capabilities": caps},
                      timeout=90)
    try:
        body = r.json()
    except ValueError:
        body = {"ok": False, "error": "BAD_JSON", "detail": r.text[:300]}
    body.setdefault("ok", 200 <= r.status_code < 300)
    body["http_status"] = r.status_code
    return body


def safe_act(payload: dict, hwnd: int, tries: int = 3) -> dict:
    """用户实时操作会抢前台/遮挡 → 重试前 force_foreground（竞态安全循环）。"""
    out = {"ok": False, "error": "NOT_TRIED"}
    for i in range(tries):
        out = act(payload)
        if out.get("ok") and out.get("http_status") == 200:
            return out
        force_foreground(hwnd)
        time.sleep(0.35)
    return out


def hotkey(key: str, hwnd: int, caps: dict) -> dict:
    force_foreground(hwnd)
    return safe_act({"action": "hotkey", "text": key, "hwnd": hwnd,
                     "settle_seconds": 0.25, "capabilities": caps}, hwnd)


def shot_png() -> "np.ndarray":
    r = requests.get("http://127.0.0.1:8030/api/ghost/screenshot",
                     params={"monitor": 1, "fmt": "png", "scale": 1.0},
                     timeout=30)
    r.raise_for_status()
    img = Image.open(io.BytesIO(base64.b64decode(r.json()["image_b64"])))
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def vlm_describe(png_path: Path, question: str) -> dict:
    b64 = base64.b64encode(png_path.read_bytes()).decode("ascii")
    r = requests.post("http://127.0.0.1:8030/api/ghost/vl/describe",
                      headers={"X-Ghost-Token": TOKEN},
                      json={"question": question, "image_b64": b64,
                            "max_tokens": 400}, timeout=180)
    try:
        return r.json()
    except ValueError:
        return {"ok": False, "error": "BAD_JSON", "detail": r.text[:200]}


# ---- 窗口基建（自建 Paint，PID 差集绑定） -----------------------------------

def paint_pids() -> set:
    out = set()
    for p in psutil.process_iter(["pid", "name"]):
        if str(p.info.get("name") or "").lower() == "mspaint.exe":
            out.add(int(p.info["pid"]))
    return out


def kill_tree(proc: subprocess.Popen) -> None:
    try:
        root = psutil.Process(proc.pid)
        procs = root.children(recursive=True) + [root]
    except psutil.Error:
        procs = []
    for q in procs:
        try:
            q.kill()
        except psutil.Error:
            pass


def list_windows():
    out = []

    def _cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd) or ""
        if title:
            out.append((hwnd, title))
        return True
    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:  # noqa: BLE001
        pass
    return out


def process_name(hwnd: int) -> str:
    try:
        _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        return str(psutil.Process(pid).name()).lower()
    except Exception:  # noqa: BLE001
        return ""


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
        time.sleep(0.25)
        if win32gui.GetForegroundWindow() == hwnd:
            return True
    return False


class FocusKeeper:
    """绘制期间保持 Paint 前台（归属门禁不放宽，焦点丢失只会让动作被正确拒绝）。"""

    def __init__(self, hwnd: int):
        self.hwnd = hwnd
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stop_event.is_set():
            try:
                if win32gui.GetForegroundWindow() != self.hwnd:
                    force_foreground(self.hwnd)
            except Exception:  # noqa: BLE001
                pass
            self.stop_event.wait(0.5)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=2)


def find_paint_window(before_pids: set, paint_proc: subprocess.Popen,
                      timeout: float = 20.0):
    allowed = {int(paint_proc.pid)}
    try:
        allowed |= {c.pid for c in psutil.Process(
            paint_proc.pid).children(recursive=True)}
    except psutil.Error:
        pass
    end = time.time() + timeout
    while time.time() < end:
        for hwnd, title in list_windows():
            if process_name(hwnd) != "mspaint.exe":
                continue
            try:
                _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:  # noqa: BLE001
                continue
            rect = win32gui.GetWindowRect(hwnd)
            if rect[2] - rect[0] <= 300:
                continue
            if int(pid) in allowed and int(pid) not in before_pids:
                return {"hwnd": hwnd, "title": title, "rect": rect}
        time.sleep(0.4)
    return None


def detect_canvas(img, rect):
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
    return [l + x, t + y0 + y, cw, ch]


def open_own_paint(phase: str):
    """自建一个 Paint 实例；已有 mspaint 直接返回 None（红线 SKIP）。"""
    existing = paint_pids()
    if existing:
        print("[SKIP] 检测到既有 mspaint.exe %s（%s 阶段）；"
              "红线只碰自建窗口。" % (sorted(existing), phase), flush=True)
        return None
    before = {p.pid for p in psutil.process_iter(["pid"])}
    paint = spawn(["mspaint.exe"])
    win = find_paint_window(before, paint)
    if win is None:
        kill_tree(paint)
        return None
    # 长时绘制期间用户全屏窗口可能压住测试窗 → TOPMOST 常驻（自建窗口）
    try:
        win32gui.SetWindowPos(win["hwnd"], win32con.HWND_TOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
    except Exception:  # noqa: BLE001
        pass
    return {"proc": paint, "hwnd": win["hwnd"], "rect": win["rect"],
            "title": win["title"], "before": before}


def close_own_paint(paint_obj, hwnd: int) -> bool:
    if paint_obj is not None:
        kill_tree(paint_obj["proc"])
        # 铁律清场：启动前差集之外的 mspaint 一律是我方产物——WinUI mspaint
        # 偶发 launcher/real 换壳，kill_tree 可能漏网（首轮实证孤儿 pid 存活）
        before = paint_obj.get("before") or set()
        for pid in paint_pids() - before:
            try:
                psutil.Process(pid).kill()
            except psutil.Error:
                pass
        try:
            paint_obj["proc"].wait(timeout=8)
        except Exception:  # noqa: BLE001
            pass
    time.sleep(0.6)
    left = set()
    if paint_obj is not None:
        left = paint_pids() - (paint_obj.get("before") or set())
    return (not win32gui.IsWindow(hwnd)) and not left


# ---- 全程录屏（后台线程抓画布区 → MP4/GIF） ---------------------------------

class CanvasRecorder:
    """4fps 抓画布区（走服务端 /screenshot 单一 capture 线程，不自开 mss）。

    帧以 JPEG bytes 入内存（~80KB/帧，上限 2600 帧 ≈ 10 分钟），结束后
    compose_video / compose_gif 合成加速回放。
    """

    def __init__(self, crop_box, fps: float = 4.0, max_frames: int = 2600):
        self.crop = crop_box  # (x, y, w, h) 物理像素
        self.interval = 1.0 / max(0.5, fps)
        self.max_frames = int(max_frames)
        self.frames: list = []
        self.errors = 0
        self.first_exc: str = ""
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def _run(self):
        while not self.stop_event.is_set():
            t0 = time.time()
            try:
                r = requests.get(
                    "http://127.0.0.1:8030/api/ghost/screenshot",
                    params={"monitor": 1, "fmt": "jpeg", "scale": 1.0,
                            "quality": 80}, timeout=15)
                if r.status_code == 200:
                    raw = base64.b64decode(r.json()["image_b64"])
                    arr = cv2.imdecode(np.frombuffer(raw, np.uint8),
                                       cv2.IMREAD_COLOR)
                    if arr is not None:
                        x, y, w, h = self.crop
                        crop = arr[max(0, y):y + h, max(0, x):x + w]
                        if crop.size:
                            ok_e, buf = cv2.imencode(
                                ".jpg", crop,
                                [cv2.IMWRITE_JPEG_QUALITY, 82])
                            if ok_e:
                                self.frames.append(buf.tobytes())
                            else:
                                self.errors += 1
                else:
                    self.errors += 1
            except Exception as exc:  # noqa: BLE001
                self.errors += 1
                if not self.first_exc:
                    import traceback as _tb
                    self.first_exc = _tb.format_exc(limit=4)
                    print("  [recorder] first error: "
                          + repr(exc), flush=True)
            if len(self.frames) >= self.max_frames:
                break
            self.stop_event.wait(max(0.0, self.interval - (time.time() - t0)))

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self) -> int:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=8)
        return len(self.frames)


def _decode(jb: bytes):
    return cv2.imdecode(np.frombuffer(jb, np.uint8), cv2.IMREAD_COLOR)


def compose_video(frames_jpeg: list, path: Path, fps_out: int = 12,
                  max_side: int = 640) -> bool:
    """JPEG 帧序列 → 加速回放 MP4（12fps 输出 = 4fps 采集的 3 倍速）。"""
    if len(frames_jpeg) < 8:
        return False
    first = _decode(frames_jpeg[0])
    if first is None:
        return False
    h, w = first.shape[:2]
    sc = max_side / float(max(h, w))
    if sc < 1:
        w, h = max(2, int(w * sc) // 2 * 2), max(2, int(h * sc) // 2 * 2)
    path.parent.mkdir(parents=True, exist_ok=True)
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"),
                         fps_out, (w, h))
    if not vw.isOpened():
        return False
    for jb in frames_jpeg:
        f = _decode(jb)
        if f is None:
            continue
        if f.shape[0] != h or f.shape[1] != w:
            f = cv2.resize(f, (w, h))
        vw.write(f)
    vw.release()
    return path.is_file() and path.stat().st_size > 4096


def compose_gif(frames_jpeg: list, path: Path, every: int = 2,
                max_w: int = 380, colors: int = 96, dur_ms: int = 80) -> bool:
    """隔帧抽样 → 限宽限色 GIF（控文件大小）。"""
    imgs = []
    for jb in frames_jpeg[::max(1, every)]:
        f = _decode(jb)
        if f is None:
            continue
        im = Image.fromarray(cv2.cvtColor(f, cv2.COLOR_BGR2RGB))
        if im.width > max_w:
            im = im.resize((max_w, int(im.height * max_w / im.width)))
        imgs.append(im.convert("P", palette=Image.ADAPTIVE, colors=colors))
    if len(imgs) < 6:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    imgs[0].save(path, save_all=True, append_images=imgs[1:],
                 duration=dur_ms, loop=0, optimize=True)
    return path.is_file() and path.stat().st_size > 2048


# ---- 方案一：灵魂线稿 -------------------------------------------------------

def load_doubao_bgr() -> "np.ndarray":
    im = Image.open(ART)
    if im.mode in ("RGBA", "LA", "P"):
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        im = bg
    else:
        im = im.convert("RGB")
    return cv2.cvtColor(np.array(im), cv2.COLOR_RGB2BGR)


def extract_line_strokes(img_bgr):
    """Canny + 膨胀 + 连通域过滤 → Top-N 轮廓（归一化 [0..1] 坐标点列）。"""
    h, w = img_bgr.shape[:2]
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 55, 160)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=2)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST,
                                   cv2.CHAIN_APPROX_NONE)
    kept = []
    for c in contours:
        area = cv2.contourArea(c)
        if area < MIN_CONTOUR_AREA:
            continue  # 滤掉小噪点
        kept.append((area, c))
    kept.sort(key=lambda t: t[0], reverse=True)
    strokes = []
    for area, c in kept[:MAX_STROKES]:
        pts = c.reshape(-1, 2).astype(float)
        if len(pts) > 240:  # 先粗降采样控制点数
            step = len(pts) / 240.0
            pts = pts[[round(i * step) for i in range(240)]]
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c.astype(np.float32),
                                  max(1.5, peri * 0.002), True)
        pts = approx.reshape(-1, 2).astype(float)
        if len(pts) < 2:
            continue
        if len(pts) > MAX_STROKE_PTS:
            step = len(pts) / float(MAX_STROKE_PTS)
            pts = pts[[round(i * step) for i in range(MAX_STROKE_PTS)]]
        norm = [[round(float(x) / w, 4), round(float(y) / h, 4)]
                for x, y in pts]
        strokes.append(norm)
    return strokes, len(kept)


def map_to_canvas(norm_pts, box):
    x0, y0, s = box
    return [[int(round(x0 + nx * s)), int(round(y0 + ny * s))]
            for nx, ny in norm_pts]


def phase_line(srv_alive_check) -> None:
    print("\n---- 方案一：灵魂线稿 ----", flush=True)
    img = load_doubao_bgr()
    strokes, kept_n = extract_line_strokes(img)
    total_pts = sum(len(s) for s in strokes)
    check("L1 轮廓提取（膨胀 Canny，滤小噪点后保留）",
          len(strokes) >= 8 and total_pts >= 120,
          f"strokes={len(strokes)} kept={kept_n} pts={total_pts}")

    paint = open_own_paint("line")
    ok = paint is not None
    check("L2 自建画图窗口（PID 差集绑定）", ok,
          f"title={(paint or {}).get('title')!r}")
    if not ok:
        return
    hwnd = paint["hwnd"]
    caps = {"allowed_windows": ["mspaint.exe"]}
    check("L3 画图强占前台", force_foreground(hwnd))
    time.sleep(0.6)

    img0 = shot_png()
    canvas = detect_canvas(img0, win32gui.GetWindowRect(hwnd))
    check("L4 服务端像素检测画布", canvas is not None, f"canvas={canvas}")
    if not canvas:
        close_own_paint(paint, hwnd)
        return
    cx, cy, cw, ch = canvas
    side = int(min(cw, ch) * 0.88)
    box = (cx + (cw - side) // 2, cy + (ch - side) // 2, side)

    rec = CanvasRecorder((max(0, cx), max(0, cy), cw, ch), fps=4.0)
    rec.start()
    keeper = FocusKeeper(hwnd)
    keeper.start()
    try:
        hk = hotkey("p", hwnd, caps)
        check("L5 快捷键 P（铅笔）", hk.get("ok") is True
              and hk.get("http_status") == 200)

        base = time.time()
        done = 0
        for i, s in enumerate(strokes):
            dur = int(min(1200, max(300, len(s) * 9)))
            r = draw(map_to_canvas(s, box), dur, hwnd, caps, smooth=2)
            if r.get("ok") and r.get("http_status") == 200:
                done += 1
            elif i % 5 == 0:
                force_foreground(hwnd)
        print(f"  描边进度: {done}/{len(strokes)} 条 "
              f"({time.time()-base:.0f}s)", flush=True)
        check("L6 全部轮廓描边 200", done == len(strokes),
              f"{done}/{len(strokes)}")

        time.sleep(0.4)
        img1 = shot_png()
        l, t = max(0, cx), max(0, cy)
        rr = min(img1.shape[1], cx + cw)
        bb = min(img1.shape[0], cy + ch)
        diff = cv2.absdiff(img0[t:bb, l:rr], img1[t:bb, l:rr])
        gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
        _, mask = cv2.threshold(gray, 14, 255, cv2.THRESH_BINARY)
        count = int(cv2.countNonZero(mask))
        check("L7 画布产生线稿笔迹（≥4000px）", count >= 4000,
              f"changed={count}px")
        pts = cv2.findNonZero(mask)
        if pts is not None:
            x, y, bw, bh = cv2.boundingRect(pts)
            inside = (x >= 0 and y >= 0 and x + bw <= cw
                      and y + bh <= ch)
            check("L8 笔迹 bbox 在画布内", inside,
                  f"bbox=[{x},{y},{bw},{bh}] canvas={cw}x{ch}")
        else:
            check("L8 笔迹 bbox 在画布内", False, "no pixels")
        cv2.imwrite(str(OUT_LINE), img1[t:bb, l:rr])
        print("  ART line: " + str(OUT_LINE), flush=True)
    finally:
        keeper.stop()
        n_frames = rec.stop()
        ok_v = compose_video(rec.frames, VID_LINE)
        check("L9 全程录屏合成线稿 MP4", ok_v and n_frames >= 15,
              f"frames={n_frames} errors={rec.errors} -> {VID_LINE.name}")
    check("L10 清场：线稿画图窗口已销毁", close_own_paint(paint, hwnd))


# ---- 方案二：高精度像素画（run-length 合并拖拽） -----------------------------

def split_background(img_bgr):
    """颜色语义抠底：豆包背景是"偏蓝且亮"（b>r+12），皮肤 r>b、头发/毛衣
    深色——天然可分。只保留触边的大连通域（防误伤画面内偶发偏蓝区域）。"""
    out = img_bgr.copy()
    b = out[..., 0].astype(int)
    g = out[..., 1].astype(int)
    r = out[..., 2].astype(int)
    bg = (b > r + 12) & (b > 180) & (g > 140)
    bg = cv2.morphologyEx(bg.astype(np.uint8), cv2.MORPH_CLOSE,
                          np.ones((5, 5), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(bg, 8)
    h, w = bg.shape
    keep = np.zeros((h, w), bool)
    for i in range(1, n):
        x, y, cw, ch, area = stats[i]
        touches = x == 0 or y == 0 or x + cw >= w or y + ch >= h
        if touches and area > 0.02 * h * w:
            keep |= labels == i
    out[keep] = (255, 255, 255)
    return out, int(keep.sum())


def plan_pixels():
    """背景白化 → GRID_N×GRID_N + 自适应量化 ≤QUANT_COLORS 色。"""
    im = Image.open(ART)
    if im.mode != "RGB":
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        im = bg
    img = cv2.cvtColor(np.array(im), cv2.COLOR_RGB2BGR)
    img, bg_px = split_background(img)
    # 近白归一：防自适应量化把色位浪费在白色变体上（眼高光/抗锯齿边）
    near_white = np.abs(img.astype(int) - 255).sum(axis=2) < 24
    img[near_white] = (255, 255, 255)
    print(f"  背景白化: {bg_px}px ({100.0 * bg_px / img.shape[0] / img.shape[1]:.0f}%)",
          flush=True)
    im = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    small = im.resize((GRID_N, GRID_N), Image.LANCZOS)
    q = small.convert("P", palette=Image.ADAPTIVE,
                      colors=QUANT_COLORS)
    idx = np.array(q)
    pal = np.array(q.getpalette()[:QUANT_COLORS * 3],
                   dtype=int).reshape(-1, 3)
    usage = np.bincount(idx.ravel(), minlength=QUANT_COLORS)
    return pal, idx, usage


def detect_swatches(img, rect, region=(0.565, 0.055, 0.765, 0.21)):
    """色板区找全部实心圆形色板点（任意颜色/主题无关）。

    深色主题 Paint 实测（2026-10-27）：功能区底色近黑，不能假设浅色背景——
    用区域中位数作背景模型。形状过滤 + 中心 5x5 纯色复核；空心环
    （选中态/自定义色空槽）靠填充率排除。
    """
    l, t, r, b = rect
    w, h = r - l, b - t
    x0 = l + int(w * region[0])
    x1 = l + int(w * region[2])
    y0 = t + int(h * region[1])
    y1 = t + int(h * region[3])
    crop = img[max(0, y0):y1, max(0, x0):x1].copy()
    if crop.size == 0:
        return []
    bg = np.median(crop.reshape(-1, 3), axis=0)
    dist = np.abs(crop.astype(int) - bg).sum(axis=2)
    mask = (dist > 90).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    found = []
    for c in contours:
        x, y, cw, ch = cv2.boundingRect(c)
        area = cv2.contourArea(c)
        if not (10 <= cw <= 42 and 10 <= ch <= 42):
            continue
        if not (0.55 <= cw / float(ch) <= 1.9):
            continue
        if area / float(max(1, cw * ch)) < 0.45:
            continue
        cx, cy = x0 + x + cw // 2, max(0, y0) + y + ch // 2
        patch = img[cy - 2:cy + 3, cx - 2:cx + 3]
        if patch.shape[:2] != (5, 5):
            continue
        med = np.median(patch.reshape(-1, 3), axis=0)
        if np.abs(patch.reshape(-1, 3) - med).std() > 20:
            continue
        found.append((int(cx), int(cy), [int(v) for v in med]))
    dedup = []
    for item in sorted(found, key=lambda t: (t[1], t[0])):
        if all(abs(item[0] - d[0]) + abs(item[1] - d[1]) > 12
               for d in dedup):
            dedup.append(item)
    return dedup


def _lab(bgr):
    return cv2.cvtColor(np.uint8([[[int(bgr[0]), int(bgr[1]), int(bgr[2])]]]),
                        cv2.COLOR_BGR2LAB)[0, 0].astype(float)


def nearest_swatch(rgb, swatches, allow_light=True):
    """量化色 RGB → 最近色板点（LAB 感知距离）。

    allow_light=False（非近白格）时排除"亮而无色度"的候选（白/浅灰，
    lum>170 且 chroma<40）：浅肤色距白/浅灰比距三文鱼色更近，不排除的话
    脸会被涂成灰色/跳填——实测踩坑（第一版豆包像素画脸是灰的）。
    """
    target = _lab((rgb[2], rgb[1], rgb[0]))
    best, bd = None, 1e18
    for sx, sy, bgr in swatches:
        lum = 0.299 * bgr[2] + 0.587 * bgr[1] + 0.114 * bgr[0]
        chroma = max(bgr) - min(bgr)
        if not allow_light and lum > 170 and chroma < 40:
            continue
        d = float(((_lab(bgr) - target) ** 2).sum())
        if d < bd:
            best, bd = (sx, sy, bgr), d
    return best, bd


def sample_cells(img, canvas, box, pal, idx, swatch_of, n=40, tol=70):
    """画布截图上抽样比对格子颜色与量化真值（经色板映射后的颜色）。"""
    x0, y0, s = box
    cell = s / float(GRID_N)
    rng = np.random.RandomState(7)
    hits, total = 0, 0
    for _ in range(n):
        i, j = int(rng.randint(GRID_N)), int(rng.randint(GRID_N))
        c = int(idx[i, j])
        sw = swatch_of.get(c)
        if sw is None:
            continue
        rgb = [int(sw[2][2 - k]) for k in range(3)]  # swatch BGR→RGB
        if all(v > 232 for v in rgb):
            continue  # 跳过填白格
        px = int(x0 + (j + 0.5) * cell)
        py = int(y0 + (i + 0.5) * cell)
        l, t = max(0, canvas[0]), max(0, canvas[1])
        x, y = px - l, py - t
        if not (0 <= y < img.shape[0] and 0 <= x < img.shape[1]):
            continue
        got = img[y, x].astype(int)
        want = np.array([rgb[2], rgb[1], rgb[0]])  # BGR
        total += 1
        if int(np.abs(got - want).max()) <= tol:
            hits += 1
    return hits, total


def _is_white_swatch(sw) -> bool:
    bgr = sw[2]
    return all(v > 232 for v in (bgr[2], bgr[1], bgr[0]))


def plan_runs(idx, swatch_of):
    """同色连续像素合并成横向 run：{(sw_x, sw_y): (swatch, [(i, j0, j1)...])}。

    白格（画布本色）天然跳过；相邻格同色板即同 run。行内蛇形排序由
    paint_groups 负责（减少空走）。
    """
    groups: dict = {}
    for i in range(GRID_N):
        j = 0
        while j < GRID_N:
            c = int(idx[i, j])
            sw = swatch_of.get(c)
            if sw is None or _is_white_swatch(sw):
                j += 1
                continue
            key = (sw[0], sw[1])
            j0 = j
            while j < GRID_N:
                c2 = int(idx[i, j])
                sw2 = swatch_of.get(c2)
                if (sw2 is None or _is_white_swatch(sw2)
                        or (sw2[0], sw2[1]) != key):
                    break
                j += 1
            groups.setdefault(key, (sw, []))[1].append((i, j0, j - 1))
    return groups


def run_passes(run, cell, box, stroke_w, passes):
    """一条 run → N 道平行横线（多道补高）；端点内缩 cap 在 cell/2-1
    （圆头帽不越界；内缩上限防御——首轮 T 假大 458px 曾把全部 run
    塌缩成圆点）。"""
    i, j0, j1 = run
    x0, y0, _s = box
    yc = y0 + (i + 0.5) * cell
    inset = min(stroke_w / 2.0, cell / 2.0 - 1.0)
    xl = x0 + j0 * cell + inset
    xr = x0 + (j1 + 1) * cell - inset
    if xr < xl:
        xm = (xl + xr) / 2.0
        xl = xr = xm
    gap = cell / float(passes)
    out = []
    for k in range(passes):
        dy = (k + 0.5) * gap - cell / 2.0
        yy = int(round(yc + dy))
        out.append([[int(round(xl)), yy], [int(round(xr)), yy]])
    return out


def park_cursor(hwnd: int, rect, caps: dict) -> None:
    """把光标停到功能区（画布外）：WinUI Paint 的笔刷光标预览圆与调粗细
    后的画布中心尺寸指示圆都是**应用内渲染**，会被 BitBlt 截屏捕获——
    测量前必须让光标离画布（预览即消），否则 diff bbox 被伪影撑大
    （实测 53px 真笔宽被撑成 792px 假值）。move 只移动不点击，安全。"""
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    safe_act({"action": "move",
              "x": rect[0] + int(w * 0.45),
              "y": rect[1] + int(h * 0.11),
              "hwnd": hwnd, "settle_seconds": 0.1,
              "capabilities": caps}, hwnd, tries=2)


def set_brush_size(cell, hwnd, caps, rect, canvas):
    """浮动滑杆点击设粗细 + 探针线实测闭环。返回实测笔宽 T（px），失败 None。

    本版 Paint 画笔粗细没有功能区下拉（≡控件是形状线宽、无选中形状时
    禁用），只有画布左侧浮动滑杆（_v11_recon 标定：轨道 x=59，
    y=0.2789~0.6078 窗口分数，顶端最大；范围未知 → 对 f 二分收敛）。
    探针放画布左缘留白带内的竖线（bbox 宽 = 笔宽），测量序列三步防伪影：
    滑杆点击 → park_cursor 消掉中心指示圆 → 基线 → 探针 → park 消掉
    光标预览圆 → 实拍 → Ctrl+Z → 复核画布干净（不净即中止）。
    """
    cx, cy, cw, ch = canvas
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    sx = rect[0] + int(w * FRAC_SIZE_TRACK_X)
    y_top = rect[1] + int(h * FRAC_SIZE_TRACK_TOP)
    y_bot = rect[1] + int(h * FRAC_SIZE_TRACK_BOT)
    px = cx + 30           # 画布左缘留白带（艺术框外 ~330px）
    py1, py2 = cy + 120, cy + 220
    lo = max(5.0, cell * 0.5)
    hi = cell * 1.15
    f = 0.5
    best = 0
    for it in range(6):
        knob_y = int(y_top + f * (y_bot - y_top))
        r = safe_act({"action": "click", "x": sx, "y": knob_y,
                      "hwnd": hwnd, "settle_seconds": 0.15,
                      "capabilities": caps}, hwnd)
        if not (r.get("ok") and r.get("http_status") == 200):
            print(f"  [size] 滑杆点击失败 iter={it}: {r.get('error')}",
                  flush=True)
            continue
        park_cursor(hwnd, rect, caps)   # 消掉调粗细后的中心指示圆
        time.sleep(0.45)
        base = shot_png()
        rd = draw([[px, py1], [px, py2]], 250, hwnd, caps, smooth=0)
        park_cursor(hwnd, rect, caps)   # 消掉光标笔刷预览圆
        time.sleep(0.3)
        now = shot_png()
        l0, t0 = max(0, cx), max(0, cy)
        r1 = min(now.shape[1], cx + cw)
        b1 = min(now.shape[0], cy + ch)
        diff = cv2.absdiff(now[t0:b1, l0:r1], base[t0:b1, l0:r1])
        gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
        _, mask = cv2.threshold(gray, 20, 255, cv2.THRESH_BINARY)
        pts = cv2.findNonZero(mask)
        t_w = 0
        if pts is not None:
            _bx, _by, bw, bh = cv2.boundingRect(pts)
            # 竖直探针：笔宽 ≈ bbox 宽（拖拽异常无竖向延展则记 0）
            t_w = int(bw) if bh >= 40 else 0
        hotkey("ctrl+z", hwnd, caps)
        time.sleep(0.35)
        clean = shot_png()
        diff2 = cv2.absdiff(clean[t0:b1, l0:r1], base[t0:b1, l0:r1])
        g2 = cv2.cvtColor(diff2, cv2.COLOR_BGR2GRAY)
        _, m2 = cv2.threshold(g2, 20, 255, cv2.THRESH_BINARY)
        residue = int(cv2.countNonZero(m2))
        print(f"  [size] iter={it} f={f:.3f} T={t_w}px "
              f"(target {lo:.0f}-{hi:.0f}) residue={residue}", flush=True)
        if rd.get("http_status") != 200:
            force_foreground(hwnd)
        best = t_w
        if residue >= 300:
            print("  [size] 探针撤销后画布不净——中止，绝不带污作画", flush=True)
            return None
        if lo <= t_w <= hi:
            return t_w
        if t_w <= 0:
            f = min(1.0, f + 0.25)          # 没线 → 往大调
        elif t_w > hi:
            f = (f + 1.0) / 2.0             # 太粗 → 二分向小
        else:
            f = f / 2.0 if f > 0.02 else 0.01  # 偏细 → 二分向大
    print(f"  [size] 闭环未精确收敛，兜底使用实测 T={best}px", flush=True)
    return best if 4 <= best <= cell * 1.6 else None


def phase_pixel() -> None:
    print(f"\n---- 方案二：高精度像素画 {GRID_N}x{GRID_N} ----", flush=True)
    pal, idx, usage = plan_pixels()
    active = [c for c in range(QUANT_COLORS) if usage[c] > 0]
    check(f"P1 {GRID_N}x{GRID_N} 量化 ≤{QUANT_COLORS} 色",
          len(active) <= QUANT_COLORS and idx.shape == (GRID_N, GRID_N),
          f"colors={len(active)} usage={[int(usage[c]) for c in active]}")

    paint = open_own_paint("pixel")
    ok = paint is not None
    check("P2 自建画图窗口（PID 差集绑定）", ok,
          f"title={(paint or {}).get('title')!r}")
    if not ok:
        return
    hwnd = paint["hwnd"]
    caps = {"allowed_windows": ["mspaint.exe"]}
    check("P3 画图强占前台", force_foreground(hwnd))
    time.sleep(0.6)
    rect = win32gui.GetWindowRect(hwnd)

    img0 = shot_png()
    canvas = detect_canvas(img0, rect)
    check("P4 服务端像素检测画布", canvas is not None, f"canvas={canvas}")
    if not canvas:
        close_own_paint(paint, hwnd)
        return
    cx, cy, cw, ch = canvas
    side = int(min(cw, ch) * 0.9)
    box = (cx + (cw - side) // 2, cy + (ch - side) // 2, side)
    cell = side / float(GRID_N)
    print(f"  艺术框 box={box} cell={cell:.2f}px", flush=True)

    rec = CanvasRecorder((max(0, cx), max(0, cy), cw, ch), fps=4.0,
                         max_frames=2600 if GRID_N <= 64 else 8000)
    rec.start()
    keeper = FocusKeeper(hwnd)
    keeper.start()
    try:
        # ---- 工具：画笔（默认激活；显式重选防状态漂移） ----
        r = safe_act({"action": "click",
                      "x": rect[0] + int((rect[2] - rect[0]) * FRAC_BRUSH_MAIN[0]),
                      "y": rect[1] + int((rect[3] - rect[1]) * FRAC_BRUSH_MAIN[1]),
                      "hwnd": hwnd, "settle_seconds": 0.2,
                      "capabilities": caps}, hwnd)
        check("P5 选中画笔工具（画笔库主按钮）", r.get("http_status") == 200)

        # ---- 粗细：滑杆+探针闭环 ----
        stroke_w = set_brush_size(cell, hwnd, caps, rect, canvas)
        size_ok = (stroke_w is not None
                   and 5 <= stroke_w <= cell * 1.6)
        check("P6 笔宽闭环（滑杆+探针，5≤T≤cell×1.6）", size_ok,
              f"T={stroke_w}px cell={cell:.1f}px")
        if not size_ok:
            return
        # 尺寸环可能扰动画布几何 → 重检画布，漂移即重设艺术框
        img_c2 = shot_png()
        canvas2 = detect_canvas(img_c2, win32gui.GetWindowRect(hwnd))
        if canvas2 and (abs(canvas2[0] - cx) > 24 or abs(canvas2[1] - cy) > 24
                        or abs(canvas2[2] - cw) > 24
                        or abs(canvas2[3] - ch) > 24):
            print(f"  [canvas] 几何漂移 {canvas} -> {canvas2}，重设艺术框",
                  flush=True)
            cx, cy, cw, ch = canvas2
            side = int(min(cw, ch) * 0.9)
            box = (cx + (cw - side) // 2, cy + (ch - side) // 2, side)
            cell = side / float(GRID_N)
        passes = 1 if stroke_w >= cell else int(math.ceil(cell / stroke_w))
        print(f"  每 run {passes} 道覆盖（T={stroke_w}px, cell={cell:.1f}px）",
              flush=True)

        # ---- 色板检测 + 颜色映射 ----
        img_grid = shot_png()
        swatches = detect_swatches(img_grid, rect)
        check("P7 色板实心点检测（≥8）", len(swatches) >= 8,
              f"n={len(swatches)} sample={[(s[0], s[1], s[2]) for s in swatches[:4]]}")
        swatch_of, dist_of = {}, {}
        for c in active:
            rgb = [int(v) for v in pal[c]]
            allow_light = all(v > 232 for v in rgb)
            sw, d = nearest_swatch(rgb, swatches, allow_light=allow_light)
            if sw is not None:
                swatch_of[c] = sw
                dist_of[c] = int(d ** 0.5)
        print("  颜色映射: " + json.dumps(
            {str(c): {"rgb": [int(v) for v in pal[c]],
                      "dist": dist_of.get(c),
                      "swatch": swatch_of[c][2] if c in swatch_of else None}
             for c in active}, ensure_ascii=False), flush=True)
        check("P8 量化色→色板映射（全部有着落）",
              len(swatch_of) == len(active),
              f"{len(swatch_of)}/{len(active)}")
        if len(swatch_of) != len(active):
            return

        # ---- run-length 计划 ----
        groups = plan_runs(idx, swatch_of)
        total_runs = sum(len(v[1]) for v in groups.values())
        non_white_cells = sum((j1 - j0 + 1) for v in groups.values()
                              for (_i, j0, j1) in v[1])
        order = sorted(groups.items(), key=lambda kv: -len(kv[1][1]))
        print(f"  run 计划: {total_runs} 条 run / {len(groups)} 组颜色 / "
              f"非白格 {non_white_cells}", flush=True)
        check("P9 run 合并计划合理（防逐格点击）",
              max(30, GRID_N // 2) <= total_runs
              <= GRID_N * GRID_N // 3
              and non_white_cells >= 0.05 * GRID_N * GRID_N,
              f"runs={total_runs} (vs {GRID_N*GRID_N} 格逐点), "
              f"压缩率 {GRID_N*GRID_N / max(1, total_runs):.1f}x")

        t0 = time.time()
        drawn, failed = 0, 0

        def paint_group(sw, runs):
            nonlocal drawn, failed
            sx, sy, _bgr = sw
            r = safe_act({"action": "click", "x": sx, "y": sy,
                          "hwnd": hwnd, "settle_seconds": 0.12,
                          "capabilities": caps}, hwnd)
            if not (r.get("ok") and r.get("http_status") == 200):
                failed += len(runs) * passes
                return 0
            n0 = drawn
            rows: dict = {}
            for run in runs:
                rows.setdefault(run[0], []).append(run)
            seq = 0
            for i in sorted(rows):
                line_runs = rows[i]
                if i % 2 == 1:
                    line_runs = line_runs[::-1]  # 蛇形减少空走
                for run in line_runs:
                    pts_list = run_passes(run, cell, box, stroke_w, passes)
                    for pts in pts_list:
                        span = abs(pts[1][0] - pts[0][0])
                        dur = int(min(700, max(100, span * 1.8)))
                        rr = draw(pts, dur, hwnd, caps, smooth=0)
                        seq += 1
                        if rr.get("ok") and rr.get("http_status") == 200:
                            drawn += 1
                        else:
                            force_foreground(hwnd)
                            rr = draw(pts, dur, hwnd, caps, smooth=0)
                            if rr.get("ok") and rr.get("http_status") == 200:
                                drawn += 1
                            else:
                                failed += 1
            return drawn - n0

        first_key, (first_sw, first_runs) = order[0]
        n_first = paint_group(first_sw, first_runs)
        # ---- 首组守卫：细线灾难（滑杆失效画成线框）在此早停 ----
        park_cursor(hwnd, rect, caps)  # 光标预览圆是应用内渲染，先停笔离画布
        time.sleep(0.35)
        probe = shot_png()
        l0, t0c = max(0, cx), max(0, cy)
        r1 = min(probe.shape[1], cx + cw)
        b1 = min(probe.shape[0], cy + ch)
        crop = probe[t0c:b1, l0:r1]
        diff = cv2.absdiff(crop, img0[t0c:b1, l0:r1])
        g = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
        _, m = cv2.threshold(g, 14, 255, cv2.THRESH_BINARY)
        changed = int(cv2.countNonZero(m))
        first_cells = sum((j1 - j0 + 1) for (_i, j0, j1) in first_runs)
        expect = first_cells * cell * cell
        ratio = changed / max(1.0, expect)
        print(f"  首组 {first_key}: runs={len(first_runs)} changed={changed}px "
              f"expect≈{expect:.0f} ratio={ratio:.2f}", flush=True)
        check("P10 首组填充覆盖率（防细线线框灾难）", ratio >= 0.45,
              f"ratio={ratio:.2f}")
        if ratio < 0.45:
            return  # 工具异常，早停防恶化

        for key, (sw, runs) in order[1:]:
            paint_group(sw, runs)
        dt = time.time() - t0
        total_strokes = drawn + failed
        check("P11 全部 run 拖拽完成（失败 ≤2%）",
              failed <= max(2, int(0.02 * total_strokes)),
              f"drawn={drawn} failed={failed} ({dt:.0f}s, "
              f"{dt / max(1, total_strokes):.2f}s/轨迹, "
              f"{total_strokes} 动作 vs {GRID_N*GRID_N} 逐格)")

        park_cursor(hwnd, rect, caps)
        time.sleep(0.5)
        img1 = shot_png()
        l0, t0c = max(0, cx), max(0, cy)
        r1 = min(img1.shape[1], cx + cw)
        b1 = min(img1.shape[0], cy + ch)
        crop1 = img1[t0c:b1, l0:r1]
        base = img0[t0c:b1, l0:r1]
        diff = cv2.absdiff(base[:crop1.shape[0], :crop1.shape[1]],
                           crop1[:base.shape[0], :base.shape[1]])
        gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
        _, mask = cv2.threshold(gray, 14, 255, cv2.THRESH_BINARY)
        count = int(cv2.countNonZero(mask))
        check("P12 成品产生大量色块像素（≥非白面积 50%）",
              count >= 0.5 * non_white_cells * cell * cell,
              f"changed={count}px expect≈{non_white_cells * cell * cell:.0f}")
        hits, tot = sample_cells(crop1, canvas, box, pal, idx,
                                 swatch_of, n=60, tol=70)
        check("P13 60 格抽样颜色比对（命中率 ≥60%）",
              tot >= 20 and hits >= 0.6 * tot, f"hits={hits}/{tot}")
        # 成品裁艺术框（画布顶缘探针带已撤销，双保险）
        ax, ay, asize = box
        art = img1[ay:ay + asize, ax:ax + asize]
        cv2.imwrite(str(OUT_PIXEL), art)
        print("  ART pixel: " + str(OUT_PIXEL), flush=True)

        # ---- is_painting 熔断豁免双向实证（同点连发，点选已填同色格心） ----
        proof_pt = None
        for i in range(GRID_N):
            for j in range(GRID_N):
                c = int(idx[i, j])
                sw = swatch_of.get(c)
                if sw is not None and not _is_white_swatch(sw):
                    proof_pt = [int(box[0] + (j + 0.5) * cell),
                                int(box[1] + (i + 0.5) * cell)]
                    break
            if proof_pt:
                break
        if proof_pt:
            ok_n = 0
            for _ in range(55):
                rr = draw([proof_pt, [proof_pt[0] + 1, proof_pt[1]]],
                          100, hwnd, caps, smooth=0)
                if rr.get("http_status") == 200:
                    ok_n += 1
                else:
                    break
            check("P16 is_painting=True 同点55连发零熔断（长绘图不被截断）",
                  ok_n == 55, f"200×{ok_n}/55")
            n429 = 0
            for _ in range(56):
                rr = draw([proof_pt, [proof_pt[0] + 1, proof_pt[1]]],
                          100, hwnd, caps, smooth=0, is_painting=False)
                if rr.get("http_status") == 429:
                    n429 += 1
            check("P17 is_painting=False 第51次触发 429 熔断（防线未削弱）",
                  n429 >= 1, f"429×{n429}/56")
            rr = requests.post(
                "http://127.0.0.1:8030/api/ghost/emergency_stop/release",
                headers={"X-Ghost-Token": TOKEN}, timeout=10)
            check("P18 release 解冻复原", rr.status_code == 200,
                  f"http={rr.status_code}")
    except RuntimeError:
        pass
    finally:
        keeper.stop()
        n_frames = rec.stop()
        ok_v = compose_video(rec.frames, VID_PIXEL)
        ok_g = compose_gif(rec.frames, GIF_PIXEL,
                           every=2 if GRID_N <= 64 else 6)
        check("P14 全程录屏合成像素画 MP4+GIF",
              ok_v and n_frames >= 30,
              f"frames={n_frames} errors={rec.errors} "
              f"mp4={ok_v} gif={ok_g} -> {VID_PIXEL.name}")
        try:
            park_cursor(hwnd, rect, caps)
            time.sleep(0.3)
            _ax = box[0]
            _ay = box[1]
            _as = box[2]
            cv2.imwrite(str(OUT_PIXEL), shot_png()[max(0, _ay):_ay + _as,
                                                    max(0, _ax):_ax + _as])
        except Exception:  # noqa: BLE001
            pass
        # 清场必须在 finally：任何早退路径（尺寸环/覆盖率守卫 return）
        # 都不得泄漏自建 Paint（首轮/三轮实证泄漏过两个孤儿进程）
        check("P15 清场：像素画图窗口已销毁", close_own_paint(paint, hwnd))


# ---- VLM 识图反馈 -----------------------------------------------------------

PERSON_WORDS = ("人", "人物", "肖像", "头像", "女孩", "女生", "女人", "女士",
                "小姐", "女", "男", "脸", "portrait", "face", "person",
                "human", "girl", "boy", "woman", "man", "lady", "character",
                "avatar")
PARTIAL_WORDS = ("头发", "hair", "眼睛", "eyes", "五官", "表情", "卡通", "动漫",
                 "cartoon", "anime", "玩偶", "虚拟")
PIXEL_WORDS = ("像素", "pixel", "马赛克", "mosaic", "游戏", "game", "格",
               "方块")

import re  # noqa: E402

_COORD_ANS = re.compile(r"\(\s*\d+\s*,\s*\d+\s*\)")


def vlm_phase(tag: str, path: Path, extra_words=(),
              q1: str = "这张图里画了什么？",
              q2: str = "这幅画是什么风格的？用一短句描述。",
              context: str = "") -> None:
    q = context + q1
    r = vlm_describe(path, q)
    d1 = str(r.get("description") or "")
    if _COORD_ANS.search(d1):
        # 2B 把色块网格当软件截图会输出 grounding 坐标——补语境重试一次
        r = vlm_describe(path, context +
                         "这是用户在画图软件里手绘的一幅画，不是软件截图。"
                         "请描述画的内容：画的是什么？")
        d1 = str(r.get("description") or "")
    print(f"  [{tag}] Q1 原文: {d1!r}", flush=True)
    print(f"  [{tag}] Q1 计时: {(r.get('timings') or {})}", flush=True)
    low = d1.lower()
    words = PERSON_WORDS + extra_words
    if any(w.lower() in low for w in words):
        check(f"V[{tag}] VLM 认出人像/主题", True, d1[:90])
    elif any(w.lower() in low for w in PARTIAL_WORDS):
        check(f"V[{tag}] VLM 认出人像/主题（部分）", True,
              "partial: " + d1[:90])
    else:
        check(f"V[{tag}] VLM 认出人像/主题", False, d1[:120])
    r2 = vlm_describe(path, context + q2)
    d2 = str(r2.get("description") or "")
    print(f"  [{tag}] Q2 原文: {d2!r}", flush=True)


def main() -> int:
    print(f"== moray-ghost doubao portrait reproduction "
          f"(mode={MODE}, grid={GRID_N}) ==", flush=True)
    srv = None
    try:
        check("0 前提：豆包原图存在", ART.is_file(), str(ART))
        if not ART.is_file():
            return 1
        check("0 前提：端口 8030 空闲", port_free())
        srv = spawn([sys.executable, "run.py"], drain=True)
        check("0 前提：Ghost 服务启动", wait_healthy())
        if srv.poll() is not None:
            return 1
        if MODE in ("line", "both"):
            phase_line(None)
            if OUT_LINE.is_file():
                vlm_phase("LINE", OUT_LINE, extra_words=(),
                          context="这是一幅手绘的简笔画作品。")
        if MODE in ("pixel", "both"):
            phase_pixel()
            if OUT_PIXEL.is_file():
                vlm_phase("PIXEL", OUT_PIXEL, extra_words=PIXEL_WORDS,
                          q1="这张图里画的是谁？",
                          q2="图里主要有哪些颜色？请列出两三种。",
                          context="这是一幅手绘的像素画作品。")
    finally:
        if srv is not None:
            stop_proc(srv)
        check("9 清场：服务停止、端口释放", port_free())
    passed = sum(1 for x in RESULTS if x)
    print(f"\n== RESULT: {passed}/{len(RESULTS)} passed ==", flush=True)
    return len(RESULTS) - passed


if __name__ == "__main__":
    sys.exit(main())
