"""v0.9 纯鼠标鲸鱼绘制验收（完全不使用 VLM）。

链路：
  1. 自建 mspaint.exe（启动前若已有 Paint 直接 SKIP；PID 差集绑定，只杀自己的进程树）；
  2. 通过 /execute_action 的 hotkey 发 P（铅笔）；本版 B 实测是填充桶
     （会把整张画布填黑），所以主体也用 P 双线加粗，避免误填充；
  3. CV 在顶部色板里找蓝色实心圆点并点击（找不到则用默认黑）；
  4. 用 Chaikin 平滑后的几十个坐标点，分多笔 drag（安全队列/归属门禁/硬超时全生效）
     画出鲸鱼：身体+尾鳍、眼睛、微笑、喷水、气泡、爱心、腹部线、胸鳍、海浪；
  5. 画布像素差分验证 + 保存整窗/画布截图到 data/screenshots/whale_*.png；
  6. 清场：只杀本次 Popen 的进程树，停服务。

跑法：.venv\\Scripts\\python.exe scripts\\test_v09_draw_whale.py
"""
from __future__ import annotations

import base64
import ctypes
import io
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
CAPS = {"allowed_windows": ["mspaint.exe"]}
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


def spawn(args: list, drain: bool = False) -> subprocess.Popen:
    proc = subprocess.Popen(
        args, cwd=r"E:\moray-ghost", stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        errors="replace")
    PROCS.append(proc)
    if drain:
        log = open(r"E:\moray-ghost\logs\whale_service.log", "w",
                   encoding="utf-8")

        def _drain():
            try:
                for line in proc.stdout:
                    log.write(line)
                    log.flush()
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=_drain, daemon=True).start()
    return proc


def spawn_server() -> subprocess.Popen:
    return spawn([sys.executable, "run.py"], drain=True)


def wait_healthy(timeout: float = 30.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = requests.get(
                "http://127.0.0.1:8030/api/ghost/health", timeout=2)
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
            info = process_name(hwnd)
            if info != "mspaint.exe":
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
    """绘制期间保持 Paint 前台（窗口覆盖会让安全门禁 400，门禁本身不改）。"""

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


def act(payload: dict, timeout: float = 120.0) -> dict:
    r = requests.post(
        "http://127.0.0.1:8030/api/ghost/execute_action",
        headers={"X-Ghost-Token": TOKEN}, json=payload, timeout=timeout)
    try:
        body = r.json()
    except ValueError:
        body = {"ok": False, "error": "BAD_JSON",
                "detail": r.text[:300], "http_status": r.status_code}
    if isinstance(body, dict):
        body.setdefault("ok", 200 <= r.status_code < 300)
        body["http_status"] = r.status_code
    if r.status_code >= 400:
        print("  ACTION HTTP %s: %s" %
              (r.status_code, json.dumps(body, ensure_ascii=False)[:500]),
              flush=True)
    return body


def hotkey(key: str, hwnd: int) -> dict:
    force_foreground(hwnd)
    return act({"action": "hotkey", "text": key, "hwnd": hwnd,
                "settle_seconds": 0.2, "capabilities": CAPS})


def drag(points, duration_ms: int, hwnd: int) -> dict:
    return act({"action": "drag", "points": points,
                "duration_ms": max(100, min(10000, int(duration_ms))),
                "hwnd": hwnd, "settle_seconds": 0.1,
                "capabilities": CAPS})


def shot_png() -> "np.ndarray":
    r = requests.get(
        "http://127.0.0.1:8030/api/ghost/screenshot",
        params={"monitor": 1, "fmt": "png", "scale": 1.0}, timeout=30)
    r.raise_for_status()
    img = Image.open(io.BytesIO(base64.b64decode(r.json()["image_b64"])))
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def detect_canvas(img: "np.ndarray", rect: tuple):
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


def find_blue_swatch(img: "np.ndarray", rect: tuple):
    """在窗口顶部 25% 的色板区找蓝色圆形色板；返回 (x,y,area) 或 None。"""
    l, t, r, b = rect
    w, h = r - l, b - t
    if w < 200 or h < 200:
        return None
    x0 = l + int(w * 0.40)
    x1 = l + int(w * 0.80)
    y0 = t + int(h * 0.04)
    y1 = t + int(h * 0.26)
    crop = img[y0:y1, x0:x1]
    if crop.size == 0:
        return None
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([95, 120, 80]),
                       np.array([135, 255, 255]))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                            np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    best = None
    for c in contours:
        area = cv2.contourArea(c)
        if area < 60 or area > 3000:
            continue
        x, y, cw, ch = cv2.boundingRect(c)
        if cw < 10 or ch < 10 or not (0.6 <= cw / float(ch) <= 1.7):
            continue
        # 只认“实心蓝色圆点”：中心像素必须蓝；排除选中态蓝圈（中心是黑色）
        if area / float(max(1, cw * ch)) < 0.35:
            continue
        cx = x0 + x + cw // 2
        cy = y0 + y + ch // 2
        b, g, r = [int(v) for v in img[cy, cx]]
        if not (b > 140 and b > r + 30 and b > g + 10):
            continue
        if best is None or cy < best[1] or (cy == best[1] and area > best[2]):
            best = (cx, cy, area)
    return best


def chaikin(points, iterations: int = 3, closed: bool = False):
    pts = [(float(x), float(y)) for x, y in points]
    for _ in range(iterations):
        out = []
        n = len(pts)
        if not closed:
            out.append(pts[0])
        for i in range(n if closed else n - 1):
            p0 = pts[i]
            p1 = pts[(i + 1) % n]
            out.append((0.75 * p0[0] + 0.25 * p1[0],
                        0.75 * p0[1] + 0.25 * p1[1]))
            out.append((0.25 * p0[0] + 0.75 * p1[0],
                        0.25 * p0[1] + 0.75 * p1[1]))
        if not closed:
            out.append(pts[-1])
        pts = out
    if closed:
        pts.append(pts[0])
    return pts


def map_points(points, box):
    x0, y0, w, h = box
    return [[int(round(x0 + nx * w)), int(round(y0 + ny * h))]
            for nx, ny in points]


def circle_points(cx, cy, r, n=28):
    return [(cx + r * np.cos(2 * np.pi * i / n),
             cy + r * np.sin(2 * np.pi * i / n)) for i in range(n)]


def heart_points(cx, cy, s, n=48):
    pts = []
    for i in range(n):
        t = 2 * np.pi * i / n
        x = 16 * np.sin(t) ** 3
        y = 13 * np.cos(t) - 5 * np.cos(2 * t) - 2 * np.cos(3 * t) \
            - np.cos(4 * t)
        pts.append((cx + x * s / 16.0, cy - y * s / 16.0))
    return pts


def main() -> int:
    print("== moray-ghost v0.9 pure-mouse whale drawing ==", flush=True)
    existing = paint_pids()
    if existing:
        print("[SKIP] 检测到既有 mspaint.exe 进程 %s；测试红线只碰自建窗口，"
              "请先关闭画图再跑。" % sorted(existing), flush=True)
        return 2

    srv = None
    paint = None
    win = None
    hwnd = 0
    keeper = None
    try:
        check("0 前提：端口 8030 空闲", port_free())
        srv = spawn_server()
        check("0 前提：Ghost 服务启动", wait_healthy())
        if srv.poll() is not None:
            return 1

        before = {p.pid for p in psutil.process_iter(["pid"])}
        paint = spawn(["mspaint.exe"])
        win = find_paint_window(before, paint)
        check("1 自建画图窗口已就位（PID 差集绑定）", win is not None,
              f"win={(win or {}).get('title')!r}")
        if not win:
            return 1
        hwnd = win["hwnd"]
        check("1 画图强占前台", force_foreground(hwnd))
        time.sleep(0.6)

        img0 = shot_png()
        rect = win32gui.GetWindowRect(hwnd)
        canvas = detect_canvas(img0, rect)
        check("1 服务端像素检测画布（最大白色连通域）", canvas is not None,
              f"rect={rect} canvas={canvas}")
        if not canvas:
            return 1
        cx, cy, cw, ch = canvas
        mx, my = int(cw * 0.06), int(ch * 0.08)
        box = (cx + mx, cy + my, cw - 2 * mx, ch - 2 * my)

        keeper = FocusKeeper(hwnd)
        keeper.start()

        # ---- 1) 铅笔细节（默认黑色，先画，避免后面蓝色覆盖） ----
        hk_p = hotkey("p", hwnd)
        check("2 快捷键 P（铅笔）", hk_p.get("ok") is True
              and hk_p.get("http_status") == 200,
              f"status={hk_p.get('http_status')}")
        eye = chaikin(circle_points(0.285, 0.475, 0.024)[:22],
                      2, closed=False)
        smile = chaikin([(0.205, 0.565), (0.285, 0.625), (0.365, 0.575)],
                        2)
        spout = chaikin([(0.315, 0.315), (0.285, 0.235),
                         (0.315, 0.165), (0.345, 0.115)], 2)
        strokes = [
            ("眼睛", eye, 800),
            ("微笑", smile, 500),
            ("喷水", spout, 700),
            ("气泡1", chaikin(circle_points(0.235, 0.175, 0.012),
                              1, closed=False), 400),
            ("气泡2", chaikin(circle_points(0.405, 0.135, 0.009),
                              1, closed=False), 350),
        ]
        for name, pts, ms in strokes:
            r = drag(map_points(pts, box), ms, hwnd)
            check("3 细节笔画 " + name, r.get("ok") is True
                  and r.get("http_status") == 200,
                  f"status={r.get('http_status')} pts={len(pts)}")

        # ---- 2) 蓝色色板（CV 找蓝色圆点，找不到则用默认黑） ----
        swatch = find_blue_swatch(img0, rect)
        color_ok = False
        if swatch:
            sx, sy, area = swatch
            r = act({"action": "click", "x": sx, "y": sy, "hwnd": hwnd,
                     "settle_seconds": 0.2, "capabilities": CAPS})
            color_ok = r.get("ok") is True and r.get("http_status") == 200
            check("2 点击蓝色色板", color_ok,
                  f"click=({sx},{sy}) area={int(area)}")
        else:
            check("2 点击蓝色色板（未找到则默认黑）", False,
                  "no blue swatch detected")

        # ---- 3) 身体/尾鳍/胸鳍：仍用 P 铅笔；身体双线模拟 2px 笔触 ----
        body_ctrl = [
            (0.12, 0.55), (0.17, 0.38), (0.36, 0.28), (0.58, 0.30),
            (0.72, 0.36), (0.84, 0.24), (0.93, 0.30), (0.83, 0.46),
            (0.93, 0.58), (0.82, 0.63), (0.70, 0.52), (0.58, 0.68),
            (0.36, 0.76), (0.18, 0.70),
        ]
        body = chaikin(body_ctrl, 3, closed=False)[::3]
        fin = chaikin([(0.42, 0.585), (0.49, 0.70), (0.565, 0.655),
                       (0.50, 0.575)], 2, closed=False)
        body_screen = map_points(body, box)
        r = drag(body_screen, 2400, hwnd)
        check("3 主体笔画 鲸鱼身体+尾鳍", r.get("ok") is True
              and r.get("http_status") == 200,
              f"status={r.get('http_status')} pts={len(body_screen)}")
        r = drag([[p[0] + 1, p[1] + 1] for p in body_screen], 2400, hwnd)
        check("3 主体加粗（双线）", r.get("ok") is True
              and r.get("http_status") == 200,
              f"status={r.get('http_status')}")
        gap = chaikin([(0.18, 0.70), (0.145, 0.63), (0.12, 0.55)],
                      2, closed=False)
        r = drag(map_points(gap, box), 350, hwnd)
        check("3 主体补缺口", r.get("ok") is True
              and r.get("http_status") == 200,
              f"status={r.get('http_status')}")
        r = drag(map_points(fin, box), 700, hwnd)
        check("3 主体笔画 胸鳍", r.get("ok") is True
              and r.get("http_status") == 200,
              f"status={r.get('http_status')} pts={len(fin)}")

        # ---- 4) 细线：腹部、爱心、海浪（P 仍然有效） ----
        belly = chaikin([(0.20, 0.62), (0.32, 0.705), (0.48, 0.735),
                         (0.62, 0.665)], 2)
        heart = chaikin(heart_points(0.52, 0.12, 0.045)[:38],
                        1, closed=False)[::3]
        waves = []
        for i in range(81):
            nx = 0.10 + 0.80 * i / 80
            waves.append((nx, 0.855 + 0.018 * np.sin(nx * 4 * np.pi)))
        for name, pts, ms in (("腹部线", belly, 600),
                              ("爱心", heart, 900),
                              ("海浪", waves, 1100)):
            r = drag(map_points(pts, box), ms, hwnd)
            check("3 收尾笔画 " + name, r.get("ok") is True
                  and r.get("http_status") == 200,
                  f"status={r.get('http_status')} pts={len(pts)}")

        # ---- 5) 像素验证 + 截图 ----
        time.sleep(0.4)
        img1 = shot_png()
        l = max(0, cx)
        t = max(0, cy)
        r = min(img1.shape[1], cx + cw)
        b = min(img1.shape[0], cy + ch)
        d0 = img0[t:b, l:r]
        d1 = img1[t:b, l:r]
        diff = cv2.absdiff(d0, d1)
        gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
        _, mask = cv2.threshold(gray, 14, 255, cv2.THRESH_BINARY)
        count = int(cv2.countNonZero(mask))
        check("4 画布产生真实笔迹像素（≥1200px）", count >= 1200,
              f"changed={count}px")
        pts = cv2.findNonZero(mask)
        if pts is None:
            check("4 笔迹 bbox 在画布内且尺寸合理", False, "no pixels")
        else:
            x, y, bw, bh = cv2.boundingRect(pts)
            inside = (x >= 0 and y >= 0 and x + bw <= cw
                      and y + bh <= ch)
            check("4 笔迹 bbox 在画布内且尺寸合理", inside
                  and bw >= int(cw * 0.20) and bh >= int(ch * 0.12),
                  f"bbox=[{x},{y},{bw},{bh}] canvas={cw}x{ch}")
        n, _labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        comps = sum(1 for i in range(1, n) if stats[i][4] >= 20)
        check("4 多笔独立元素（≥3 个连通域）", comps >= 3,
              f"components={comps}")
        bgr = d1[mask > 0]
        blue = int(np.sum((bgr[:, 0] > bgr[:, 2] + 30)
                          & (bgr[:, 0] > bgr[:, 1] + 20))) if len(bgr) else 0
        if color_ok:
            check("4 蓝色笔迹（色板点击生效）", blue >= 300,
                  f"blue_pixels={blue}")

        cv2.imwrite(r"E:\moray-ghost\data\screenshots\whale_full.png", img1)
        cv2.imwrite(r"E:\moray-ghost\data\screenshots\whale_art.png",
                    img1[t:b, l:r])
        print("  ART full: E:\\moray-ghost\\data\\screenshots\\whale_full.png",
              flush=True)
        print("  ART crop: E:\\moray-ghost\\data\\screenshots\\whale_art.png",
              flush=True)

    finally:
        if keeper:
            keeper.stop()
        if paint:
            kill_tree(paint)
            try:
                paint.wait(timeout=8)
            except Exception:  # noqa: BLE001
                pass
        gone = True
        if hwnd:
            time.sleep(0.5)
            gone = not win32gui.IsWindow(hwnd)
        check("5 清场：自建画图窗口已销毁", gone)
        if srv:
            stop_proc(srv)
        check("5 清场：服务停止、端口释放", port_free())
    passed = sum(1 for x in RESULTS if x)
    print(f"\n== RESULT: {passed}/{len(RESULTS)} passed ==", flush=True)
    return len(RESULTS) - passed


if __name__ == "__main__":
    sys.exit(main())
