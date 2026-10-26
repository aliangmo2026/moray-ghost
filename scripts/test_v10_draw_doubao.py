# -*- coding: utf-8 -*-
"""v0.10 豆包头像复现验收：方案一「灵魂线稿」+ 方案二「像素风」。

数据：data/doubao.png（主仓发来的 512x512 3D 虚拟人像）。

方案一 灵魂线稿：
  Canny 边缘 → 形态学膨胀连接断线 → 连通域过滤小噪点 → 取面积 Top-N 轮廓
  → 降采样 → 逐条 draw_trajectory（服务端平滑+真实鼠标事件）在画图里描边；
  像素差分验证 + VLM(describe) 看是否认得出人像。

方案二 像素风：
  PIL 压成 32x32 → 自适应量化 ≤12 色 → 画 33+33 条黑色网格线（铅笔双线
  防填充泄漏）→ 截屏检测色板全部实心色板点 → 量化色就近映射色板 → B=油漆桶
  → 按色分组逐格填充（色切换最少化；每格一次 execute_action click，安全
  管道全生效；中途抽样守卫防"网格缺口导致整画布淹没"）→ 20 格抽样比对
  真值颜色 + VLM(describe) 识图。

红线：只操作本次 Popen 的 mspaint（PID 差集绑定；启动前已有 Paint 直接 SKIP），
两方案各用独立 Paint 实例（相变清场），清场只杀自己的进程树。

跑法：.venv\\Scripts\\python.exe scripts\\test_v10_draw_doubao.py [line|pixel|both]
"""
from __future__ import annotations

import base64
import ctypes
import io
import json
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

ART = ROOT / "data" / "doubao.png"
OUT_LINE = ROOT / "data" / "screenshots" / "doubao_line_art.png"
OUT_PIXEL = ROOT / "data" / "screenshots" / "doubao_pixel_art.png"

RESULTS: list = []
PROCS: list = []
GRID_N = 32
QUANT_COLORS = 12
MAX_STROKES = 30
MIN_CONTOUR_AREA = 60.0
MAX_STROKE_PTS = 90


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
         smooth: int = 2) -> dict:
    r = requests.post("http://127.0.0.1:8030/api/ghost/draw_trajectory",
                      headers={"X-Ghost-Token": TOKEN},
                      json={"points": points, "duration_ms": duration_ms,
                            "hwnd": hwnd, "smooth_iterations": smooth,
                            "settle_seconds": 0.05, "capabilities": caps},
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
    return {"proc": paint, "hwnd": win["hwnd"], "rect": win["rect"],
            "title": win["title"]}


def close_own_paint(paint_obj, hwnd: int) -> bool:
    if paint_obj is not None:
        kill_tree(paint_obj["proc"])
        try:
            paint_obj["proc"].wait(timeout=8)
        except Exception:  # noqa: BLE001
            pass
    time.sleep(0.5)
    return not win32gui.IsWindow(hwnd)


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
    check("L9 清场：线稿画图窗口已销毁", close_own_paint(paint, hwnd))


# ---- 方案二：像素风 ---------------------------------------------------------

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
    """背景白化 → 32x32 + 自适应量化 ≤12 色（色位全留给人物）。"""
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


def phase_pixel() -> None:
    print("\n---- 方案二：像素风 ----", flush=True)
    pal, idx, usage = plan_pixels()
    active = [c for c in range(QUANT_COLORS) if usage[c] > 0]
    check("P1 32x32 量化 ≤12 色", len(active) <= QUANT_COLORS
          and idx.shape == (GRID_N, GRID_N),
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

    img0 = shot_png()
    canvas = detect_canvas(img0, win32gui.GetWindowRect(hwnd))
    check("P4 服务端像素检测画布", canvas is not None, f"canvas={canvas}")
    if not canvas:
        close_own_paint(paint, hwnd)
        return
    cx, cy, cw, ch = canvas
    side = int(min(cw, ch) * 0.9)
    box = (cx + (cw - side) // 2, cy + (ch - side) // 2, side)
    cell = side / float(GRID_N)

    keeper = FocusKeeper(hwnd)
    keeper.start()
    try:
        # ---- 网格：33+33 条黑线（铅笔双线防泄漏） ----
        hk = hotkey("p", hwnd, caps)
        check("P5 快捷键 P（铅笔）", hk.get("ok") is True
              and hk.get("http_status") == 200)
        grid_pts = []
        for i in range(GRID_N + 1):
            gx = int(round(box[0] + i * cell))
            grid_pts.append([[gx, box[1]], [gx, box[1] + side]])
            gy = int(round(box[1] + i * cell))
            grid_pts.append([[box[0], gy], [box[0] + side, gy]])
        t0 = time.time()
        ok_lines = 0
        for k, line in enumerate(grid_pts):
            off = [[p[0] + 1, p[1] + 1] for p in line]
            r1 = draw(line, 200, hwnd, caps, smooth=0)
            r2 = draw(off, 200, hwnd, caps, smooth=0)
            if (r1.get("ok") and r1.get("http_status") == 200
                    and r2.get("ok") and r2.get("http_status") == 200):
                ok_lines += 1
            if k % 16 == 15:
                print(f"  网格进度: {k+1}/{len(grid_pts)} "
                      f"({time.time()-t0:.0f}s)", flush=True)
        check("P6 网格 33+33 双线完成", ok_lines == len(grid_pts),
              f"{ok_lines}/{len(grid_pts)} ({time.time()-t0:.0f}s)")

        # ---- 色板检测 + 颜色映射 ----
        img_grid = shot_png()
        swatches = detect_swatches(img_grid, win32gui.GetWindowRect(hwnd))
        check("P7 色板实心点检测（≥6）", len(swatches) >= 6,
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

        # ---- 油漆桶（本版 B=填充桶，鲸鱼实测） + 逐格填充 ----
        hk = hotkey("b", hwnd, caps)
        check("P9 快捷键 B（油漆桶）", hk.get("ok") is True
              and hk.get("http_status") == 200)

        groups = {}
        for i in range(GRID_N):
            for j in range(GRID_N):
                groups.setdefault(int(idx[i, j]), []).append((i, j))
        order = sorted(groups, key=lambda c: -len(groups[c]))

        def cell_xy(ij):
            i, j = ij
            return (int(round(box[0] + (j + 0.5) * cell)),
                    int(round(box[1] + (i + 0.5) * cell)))

        def is_white(c):
            sw = swatch_of.get(c)
            if sw is None:
                return True
            return all(v > 232 for v in (sw[2][2], sw[2][1], sw[2][0]))

        todo = sum(len(groups[c]) for c in order if not is_white(c))
        print(f"  待填充格子: {todo}/{GRID_N*GRID_N} "
              f"（白格跳过 {GRID_N*GRID_N - todo}）", flush=True)
        t0 = time.time()
        filled, failed = 0, 0
        seq = 0
        for c in order:
            if is_white(c):
                continue
            sx, sy, _bgr = swatch_of[c]
            r = safe_act({"action": "click", "x": sx, "y": sy,
                          "hwnd": hwnd, "settle_seconds": 0.15,
                          "capabilities": caps}, hwnd)
            if not (r.get("ok") and r.get("http_status") == 200):
                failed += len(groups[c])
                continue
            for ij in groups[c]:
                px, py = cell_xy(ij)
                r = safe_act({"action": "click", "x": px, "y": py,
                              "hwnd": hwnd, "settle_seconds": 0.05,
                              "capabilities": caps}, hwnd)
                seq += 1
                if r.get("ok") and r.get("http_status") == 200:
                    filled += 1
                else:
                    failed += 1
                if seq % 64 == 0:
                    print(f"  填充进度: {seq} 次动作 "
                          f"({time.time()-t0:.0f}s)", flush=True)
                if seq == 32:  # 中途守卫：网格缺口会整幅淹没，早停别浪费时间
                    time.sleep(0.2)
                    probe = shot_png()
                    hits, tot = sample_cells(probe, canvas, box, pal, idx,
                                             swatch_of, n=6, tol=80)
                    if tot >= 3 and hits < max(1, tot // 3):
                        print("  [ABORT] 中途抽样命中率过低 "
                              f"({hits}/{tot})——疑似填充泄漏，停止防恶化",
                              flush=True)
                        check("P10 填充中途守卫（抽样颜色命中）", False,
                              f"hits={hits}/{tot}")
                        raise RuntimeError("pixel fill leak")
        dt = time.time() - t0
        check("P10 逐格填充完成", failed <= max(4, todo // 40),
              f"filled={filled} failed={failed} ({dt:.0f}s, "
              f"{dt / max(1, seq):.2f}s/动作)")

        time.sleep(0.6)
        img1 = shot_png()
        l, t = max(0, cx), max(0, cy)
        rr = min(img1.shape[1], cx + cw)
        bb = min(img1.shape[0], cy + ch)
        crop1 = img1[t:bb, l:rr]
        l0, t0c = max(0, cx), max(0, cy)
        base = img_grid[t0c:t0c + (bb - t0c), l0:l0 + (rr - l0)]
        diff = cv2.absdiff(base[:crop1.shape[0], :crop1.shape[1]],
                           crop1[:base.shape[0], :base.shape[1]])
        gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
        _, mask = cv2.threshold(gray, 14, 255, cv2.THRESH_BINARY)
        count = int(cv2.countNonZero(mask))
        check("P11 填充产生大量色块像素（≥30000px）", count >= 30000,
              f"changed={count}px")
        hits, tot = sample_cells(crop1, canvas, box, pal, idx,
                                 swatch_of, n=40, tol=70)
        check("P12 40 格抽样颜色比对（命中率 ≥60%）",
              tot >= 12 and hits >= 0.6 * tot, f"hits={hits}/{tot}")
        cv2.imwrite(str(OUT_PIXEL), crop1)
        print("  ART pixel: " + str(OUT_PIXEL), flush=True)
    except RuntimeError:
        pass
    finally:
        keeper.stop()
        try:
            cv2.imwrite(str(OUT_PIXEL), shot_png()[
                max(0, cy):cy + ch, max(0, cx):cx + cw])
        except Exception:  # noqa: BLE001
            pass
    check("P13 清场：像素画图窗口已销毁", close_own_paint(paint, hwnd))


# ---- VLM 识图反馈 -----------------------------------------------------------

PERSON_WORDS = ("人", "人物", "肖像", "头像", "女孩", "女生", "女人", "女士",
                "小姐", "女", "男", "脸", "portrait", "face", "person",
                "human", "girl", "boy", "woman", "man", "lady", "character",
                "avatar")
PARTIAL_WORDS = ("头发", "hair", "眼睛", "eyes", "五官", "表情", "卡通", "动漫",
                 "cartoon", "anime", "玩偶", "虚拟")
PIXEL_WORDS = ("像素", "pixel", "马赛克", "mosaic", "游戏", "game", "格",
               "方块")


import re

_COORD_ANS = re.compile(r"\(\s*\d+\s*,\s*\d+\s*\)")


def vlm_phase(tag: str, path: Path, extra_words=(),
              q2: str = "这幅画是什么风格的？用一短句描述。",
              context: str = "") -> None:
    q1 = context + "这张图里画了什么？"
    r = vlm_describe(path, q1)
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
    print(f"== moray-ghost v0.10 doubao portrait reproduction "
          f"(mode={MODE}) ==", flush=True)
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
