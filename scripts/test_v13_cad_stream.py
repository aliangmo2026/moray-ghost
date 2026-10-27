"""v0.13 CAD 命令流实战：无真实 CAD 软件时的替代验证（全程自建窗口）。

主仓死命令：用 CAD 画 100x50 矩形 + 中心 R10 圆孔，验证 Ghost「命令流」
自动化（键盘输入快捷键 + 绝对坐标，鼠标拖拽无效）。本机探测确认未安装
AutoCAD/FreeCAD/任何 CAD（注册表/开始菜单/winget/全盘皆空）→ 替代方案：
自建迷你 CAD 控制台 _v13_cad_console.py（接受 RECT/CIRCLE/ZOOM/SAVEAS
命令流，语义与真实 CAD 命令行一致），SAVEAS 用 ezdxf 产出真实 DXF 文件
（mm 单位、含 3 个 DIMENSION 尺寸标注）——纯 ezdxf 代码画图验证不了
键盘命令流，所以命令流走真实 execute_action 键盘注入，落盘走 ezdxf。

流程：
  A GUI RAG：vl/elements 检索控制台 → ground「绘图区」→ 真实点击（聚焦
    命令行，与 CAD 点绘图区进命令输入同语义）。
  B 命令流：execute_action type+hotkey enter 依次键入
    RECT / 0,0 / 100,50 / CIRCLE / 50,25 / 10 / ZOOM E / SAVEAS
    （绝对坐标 mm），每步以窗口标题状态推进为确定性同步。
  C 三重验证：
    ① DXF 文件级——ezdxf 解析 data/cad_test.dxf：闭合 LWPOLYLINE
      (0,0)-(100,0)-(100,50)-(0,50)、CIRCLE(50,25)R10、3×DIMENSION、
      $INSUNITS=4(mm)、audit 0 错误；
    ② 像素级——截图量矩形宽高比 ≈2.0、圆直径/矩形宽 ≈0.2、圆心与矩形
      中心重合、ZOOM E 后矩形像素宽度确实增大；
    ③ 语义级——/vl/describe 问「这张图里画了什么形状的图形？」，验收
      同时命中矩形族 + 圆族关键词（2B 怪癖：失败带语境重问一次）。
  D 录屏：窗口区 3fps → 带阶段字幕 cad_drawing.gif / cad_drawing.mp4。

铁律：靶窗自建（stdout 握手绑定 hwnd，只认自己 hwnd 消失）；capabilities
=["python.exe"]；动作前 force_foreground；遗留 ghost 服务先杀端口；清理
只杀自己的进程树；录屏走服务端单一 capture 线程 /screenshot。
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import shutil
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
import ezdxf  # noqa: E402
import numpy as np  # noqa: E402
import psutil  # noqa: E402
import requests  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from app.core import config  # noqa: E402

TOKEN = config.ensure_token()
CAPS = {"allowed_windows": ["python.exe"]}
ASSETS = ROOT / "docs" / "assets"
DXF_PATH = ROOT / "data" / "cad_test.dxf"
FULLSHOT = ROOT / "data" / "screenshots" / "cad_fullscreen.png"
GIF_PATH = ASSETS / "cad_drawing.gif"
MP4_PATH = ASSETS / "cad_drawing.mp4"
PNG_PATH = ASSETS / "cad_drawing_final.png"

RESULTS: list = []
PROCS: list = []

# 目标命令流（与主仓死命令逐条一致；SAVEAS 输入 .dwg 后缀，控制台按
# ezdxf 能力落 .dxf——DWG 是 Autodesk 专有格式，DXF 是等效开放交付物）。
CMD_STREAM = [
    ("RECT", "|S:rect_p1|"),
    ("0,0", "|S:rect_p2|"),
    ("100,50", "|S:idle|E:1|"),
    ("CIRCLE", "|S:circ_c|"),
    ("50,25", "|S:circ_r|"),
    ("10", "|S:idle|E:2|"),
    ("ZOOM", "|S:zoom_opt|"),
    ("E", "|S:idle|"),
    ("SAVEAS", "|S:save_name|"),
    ("data/cad_test.dwg", "|SAVED:data/cad_test.dxf"),
]


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


def kill_by_port(port: int) -> None:
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.laddr and c.laddr.port == port and c.pid:
                psutil.Process(c.pid).kill()
                time.sleep(0.5)
    except Exception:  # noqa: BLE001
        pass


def spawn(args: list, drain: bool = True) -> subprocess.Popen:
    proc = subprocess.Popen(args, cwd=str(ROOT), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
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


def wait_health(timeout: float = 40.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = requests.get("http://127.0.0.1:8030/api/ghost/health",
                             timeout=2)
            if r.status_code == 200:
                return r.json()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.35)
    return {}


def stop_proc(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=6)


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
        time.sleep(0.2)
        if win32gui.GetForegroundWindow() == hwnd:
            return True
    return False


def is_foreground(hwnd: int) -> bool:
    return win32gui.GetForegroundWindow() == hwnd


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
        time.sleep(0.12)
    return pred(title_of(hwnd))


def window_rect(hwnd: int) -> tuple:
    r = win32gui.GetWindowRect(int(hwnd))
    return int(r[0]), int(r[1]), int(r[2] - r[0]), int(r[3] - r[1])


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


def ghost_action(payload: dict) -> dict:
    body = {"monitor": 1, "settle_seconds": 0.08, "fmt": "png",
            "quality": 70, "scale": 0.25, "capabilities": CAPS}
    body.update(payload)
    r = requests.post("http://127.0.0.1:8030/api/ghost/execute_action",
                      headers=hdr(), json=body, timeout=60)
    try:
        out = r.json()
    except ValueError:
        out = {"raw": r.text[:300]}
    out["http_status"] = r.status_code
    return out


def act(payload: dict, hwnd: int, with_fg: bool = True) -> dict:
    """动作 + 竞态重试（前台被抢/用户鼠标漂移 → 一次恢复重试）。"""
    result: dict = {}
    for attempt in (1, 2):
        if with_fg and not is_foreground(hwnd):
            force_foreground(hwnd)
        result = ghost_action(payload)
        if result.get("http_status") == 200:
            return result
        if attempt == 1 and result.get("http_status") in (400, 409):
            time.sleep(0.7)
            continue
        return result
    return result


def type_line(text: str, xy: tuple, hwnd: int) -> dict:
    """键入一行 + 回车（CAD 命令流的最小单元）。"""
    r1 = act({"action": "type", "text": text,
              "x": int(xy[0]), "y": int(xy[1]), "hwnd": int(hwnd)}, hwnd)
    if r1.get("http_status") != 200:
        return r1
    time.sleep(0.12)
    r2 = act({"action": "hotkey", "text": "enter", "hwnd": int(hwnd)}, hwnd)
    r2["typed_status"] = r1.get("http_status")
    return r2


def vl_elements(window: str) -> dict:
    r = requests.post("http://127.0.0.1:8030/api/ghost/vl/elements",
                      headers=hdr(),
                      json={"window": window, "backend": "auto"},
                      timeout=180)
    try:
        return r.json()
    except ValueError:
        return {"ok": False, "raw": r.text[:300]}


def vl_ground(query: str, window: str) -> dict:
    r = requests.post("http://127.0.0.1:8030/api/ghost/vl/ground",
                      headers=hdr(),
                      json={"window": window, "query": query,
                            "backend": "auto", "strategy": "auto"},
                      timeout=180)
    try:
        return r.json()
    except ValueError:
        return {"ok": False, "raw": r.text[:300]}


def vl_describe(question: str, hwnd: int) -> dict:
    r = requests.post("http://127.0.0.1:8030/api/ghost/vl/describe",
                      headers=hdr(),
                      json={"question": question, "hwnd": int(hwnd),
                            "backend": "auto", "max_tokens": 256},
                      timeout=300)
    try:
        return r.json()
    except ValueError:
        return {"ok": False, "raw": r.text[:300]}


def grab_monitor_png() -> np.ndarray | None:
    r = requests.get("http://127.0.0.1:8030/api/ghost/screenshot",
                     params={"monitor": 1, "fmt": "png", "scale": 1.0},
                     headers=hdr(), timeout=30)
    if r.status_code != 200:
        return None
    raw = base64.b64decode(r.json()["image_b64"])
    return cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)


# ---- 像素几何测量（矩形宽高比 / 圆直径比 / 同心度） -----------------------
def measure(arr: np.ndarray, canvas_box: tuple) -> dict:
    """量画布上的白色实体几何；canvas_box 为屏幕物理坐标 [x,y,w,h]。"""
    x, y, w, h = (int(v) for v in canvas_box)
    if arr is None or arr.shape[0] <= y + 10 or arr.shape[1] <= x + 10:
        return {"error": "screenshot too small"}
    crop = arr[y:y + h, x:x + w]
    b, g, r = crop[:, :, 0].astype(int), crop[:, :, 1].astype(int), \
        crop[:, :, 2].astype(int)
    white = (b > 200) & (g > 200) & (r > 200)
    if white.sum() < 200:
        return {"error": f"too few entity pixels: {int(white.sum())}"}
    ys, xs = np.nonzero(white)
    rx0, rx1, ry0, ry1 = int(xs.min()), int(xs.max()), int(ys.min()), \
        int(ys.max())
    rect_w = rx1 - rx0 + 1
    rect_h = ry1 - ry0 + 1
    out = {"rect_w": rect_w, "rect_h": rect_h,
           "aspect": round(rect_w / max(1, rect_h), 4)}

    # 挖掉矩形边框带（4px）→ 剩下的最外层白像素即圆（十字中心在圆内）。
    # 圆尚未画时（B99 基线）只报矩形指标，不算错误——circle_* 缺省。
    inner = white.copy()
    inner[:ry0 + 4, :] = False
    inner[ry1 - 3:, :] = False
    inner[:, :rx0 + 4] = False
    inner[:, rx1 - 3:] = False
    if inner.sum() < 100:
        out["circle_missing"] = int(inner.sum())
        return out
    ys2, xs2 = np.nonzero(inner)
    cx0, cx1, cy0, cy1 = int(xs2.min()), int(xs2.max()), int(ys2.min()), \
        int(ys2.max())
    circ_d = max(cx1 - cx0 + 1, cy1 - cy0 + 1)
    ccx = (cx0 + cx1) / 2.0
    ccy = (cy0 + cy1) / 2.0
    rcx = (rx0 + rx1) / 2.0
    rcy = (ry0 + ry1) / 2.0
    out.update({"circ_d": circ_d,
                "d_ratio": round(circ_d / max(1, rect_w), 4),
                "center_dx": round(ccx - rcx, 2),
                "center_dy": round(ccy - rcy, 2)})
    return out


# ---- 录屏（3fps 窗口区 + 阶段字幕） ---------------------------------------
class ScreenRecorder:
    def __init__(self, crop: tuple, fps: float = 3.0, max_frames: int = 420):
        self.crop = crop
        self.interval = 1.0 / max(1.0, fps)
        self.max_frames = int(max_frames)
        self.frames: list = []
        self.phases: list = []
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
                            "quality": 80}, headers=hdr(), timeout=15)
                if r.status_code == 200:
                    raw = base64.b64decode(r.json()["image_b64"])
                    arr = cv2.imdecode(np.frombuffer(raw, np.uint8),
                                       cv2.IMREAD_COLOR)
                    if arr is not None:
                        x, y, w, h = self.crop
                        crop = arr[max(0, y):y + h, max(0, x):x + w]
                        if crop.size:
                            ok, buf = cv2.imencode(
                                ".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 84])
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


def _font(size: int):
    for path in (r"C:\Windows\Fonts\msyh.ttc",
                 r"C:\Windows\Fonts\segoeui.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _caption_at(phases: list, ts: float) -> str:
    label = "v0.13 · CAD 命令流自动化"
    for t, text in phases:
        if t <= ts:
            label = text
    return label


def compose_media(rec: ScreenRecorder, gif_path: Path, mp4_path: Path,
                  png_path: Path, max_w: int = 620, colors: int = 96,
                  frame_ms: int = 140) -> dict:
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
        draw.text((16, bar_h - 20), "v0.13 · CAD 命令流 · 本地验证",
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
    return {"frames": len(images), "errors": rec.errors,
            "gif": gif_path.is_file() and gif_path.stat().st_size,
            "mp4": mp4_path.is_file() and mp4_path.stat().st_size,
            "png": png_path.is_file() and png_path.stat().st_size,
            "size": [w, h]}


# ---- C1-C3 DXF 文件级验证 -------------------------------------------------
def verify_dxf() -> dict:
    if not DXF_PATH.is_file():
        return {"ok": False, "error": "file missing"}
    doc = ezdxf.readfile(str(DXF_PATH))
    msp = doc.modelspace()
    poly = circ = None
    dims = 0
    for e in msp:
        t = e.dxftype()
        if t == "LWPOLYLINE":
            poly = e
        elif t == "CIRCLE":
            circ = e
        elif t == "DIMENSION":
            dims += 1
    out = {"dims": dims,
           "insunits": int(doc.header.get("$INSUNITS", 0))}
    auditor = doc.audit()
    out["audit_errors"] = len(auditor.errors)
    if poly is None or circ is None:
        out["ok"] = False
        out["error"] = "missing LWPOLYLINE/CIRCLE"
        return out
    pts = [(round(p[0], 6), round(p[1], 6))
           for p in poly.get_points("xy")]
    out["poly"] = pts
    out["closed"] = bool(poly.closed)
    out["circle"] = [round(float(circ.dxf.center.x), 6),
                     round(float(circ.dxf.center.y), 6),
                     round(float(circ.dxf.radius), 6)]
    expect = [(0.0, 0.0), (100.0, 0.0), (100.0, 50.0), (0.0, 50.0)]
    out["ok"] = (out["closed"] and sorted(pts) == sorted(expect)
                 and out["circle"] == [50.0, 25.0, 10.0]
                 and dims >= 3 and out["insunits"] == 4
                 and out["audit_errors"] == 0)
    return out


def sc_of(title: str) -> float:
    m = re.search(r"\|SC:([0-9.]+)", title)
    return float(m.group(1)) if m else 0.0


def main() -> int:
    print("== moray-ghost v0.13 CAD command-stream demo ==", flush=True)
    ASSETS.mkdir(parents=True, exist_ok=True)
    FULLSHOT.parent.mkdir(parents=True, exist_ok=True)
    cursor0 = cursor_pos()
    hwnd = 0
    rec: ScreenRecorder | None = None
    media: dict = {}
    try:
        # 0 预检：遗留 ghost 先杀端口，再起服务
        if not port_free(8030):
            print("  [pre] 遗留服务占 8030 → kill_by_port", flush=True)
            kill_by_port(8030)
        check("0.1 端口 8030 空闲", port_free(8030))
        if DXF_PATH.is_file():
            DXF_PATH.unlink()
        srv = spawn([sys.executable, str(ROOT / "run.py")])
        health = wait_health()
        check("0.2 Ghost 服务启动且 VL=llama",
              bool(health) and health.get("vl_backend") == "llama",
              f"vl_backend={health.get('vl_backend')} "
              f"vl={health.get('vl')}")
        if srv.poll() is not None:
            return 1

        stem = "t" + str(int(time.time() * 1000))[-8:]
        base_title = "moray-v13-cad-" + stem
        tgt = spawn([sys.executable,
                     str(ROOT / "scripts" / "_v13_cad_console.py"), stem],
                    drain=False)
        line = tgt.stdout.readline().strip()
        payload = json.loads(line[len("READY "):]) if line.startswith(
            "READY ") else {}
        hwnd = int(payload.get("hwnd") or 0)
        canvas_box = payload.get("canvas") or []
        entry_box = payload.get("entry") or []
        check("0.3 自建 CAD 控制台就位（握手绑定 hwnd）",
              hwnd > 0 and len(canvas_box) == 4 and len(entry_box) == 4,
              f"hwnd={hwnd} canvas={canvas_box}")
        if not hwnd:
            return 1
        force_foreground(hwnd)
        time.sleep(0.3)
        entry_xy = (entry_box[0] + entry_box[2] // 2,
                    entry_box[1] + entry_box[3] // 2)

        # A GUI RAG 检索 + 选号点击绘图区
        ext = vl_elements(base_title)
        els = ext.get("elements") or []
        texts = [e.get("text") or "" for e in els]
        check("A1 GUI RAG 检索控制台元素",
              ext.get("ok") is True and len(els) >= 4,
              f"count={ext.get('count')} texts={texts[:8]}")
        check("A2 元素表含「绘图区」锚点",
              any("绘图区" in t for t in texts), str(texts[:12]))

        crop = window_rect(hwnd)
        rec = ScreenRecorder(crop, fps=3.0, max_frames=420)
        rec.start()
        time.sleep(0.4)
        rec.phase("GUI RAG 检索控制台元素")
        time.sleep(0.8)
        rec.phase("RAG 选号「绘图区」→ 真实点击聚焦命令行")
        g = vl_ground("绘图区", base_title)
        gc = ((g.get("element") or {}).get("center")) or []
        check("A3 ground「绘图区」命中",
              g.get("ok") is True and len(gc) == 2,
              f"target_id={g.get('target_id')} method={g.get('method')} "
              f"center={gc}")
        if len(gc) == 2:
            r = act({"action": "click", "x": int(gc[0]), "y": int(gc[1]),
                     "hwnd": int(hwnd)}, hwnd)
            check("A4 RAG 点击绘图区（命令行获焦）",
                  r.get("http_status") == 200,
                  f"status={r.get('http_status')}")

        # B 命令流（键入 + 回车，标题状态推进为同步点）
        sc_before_zoom = 0.0
        rect_w_before = 0
        for i, (cmd, expect_frag) in enumerate(CMD_STREAM, start=1):
            rec.phase(f"键入 {cmd} ⏎")
            r = type_line(cmd, entry_xy, hwnd)
            ok_status = r.get("http_status") == 200
            frag_ok = wait_title(
                hwnd, lambda t, f=expect_frag: f in t, timeout=6.0)
            if not ok_status or not frag_ok:
                print(f"  [cmd {i}] {cmd!r} status="
                      f"{r.get('http_status')} title={title_of(hwnd)!r} "
                      f"err={r.get('error')}", flush=True)
            check(f"B{i:02d} 命令流 `{cmd}` 生效",
                  ok_status and frag_ok,
                  f"status={r.get('http_status')} title="
                  f"{title_of(hwnd)[-70:]}")
            if cmd == "100,50":
                sc_before_zoom = sc_of(title_of(hwnd))
                shot = grab_monitor_png()
                m0 = measure(shot, canvas_box) if shot is not None else {}
                rect_w_before = int(m0.get("rect_w") or 0)
                check("B99 矩形画出后像素测量基线",
                      m0.get("aspect") is not None
                      and 1.85 <= m0["aspect"] <= 2.15,
                      json.dumps(m0, ensure_ascii=False))
            if cmd == "data/cad_test.dwg":
                time.sleep(0.5)
                rec.phase("SAVEAS → data/cad_test.dxf（ezdxf 落盘）")

        # C 三重验证 -------------------------------------------------------
        rec.phase("三重验证：DXF 解析 + 像素几何 + VLM 读图")
        dv = verify_dxf()
        check("C1 DXF 几何精确（矩形四点 + 圆心半径 + 闭合）",
              dv.get("ok") is True and dv.get("closed") is True
              and dv.get("circle") == [50.0, 25.0, 10.0],
              json.dumps(dv, ensure_ascii=False))
        check("C2 DXF 尺寸标注 3 个 + mm 单位 + audit 干净",
              dv.get("dims", 0) >= 3 and dv.get("insunits") == 4
              and dv.get("audit_errors") == 0,
              f"dims={dv.get('dims')} insunits={dv.get('insunits')} "
              f"audit={dv.get('audit_errors')}")

        shot = grab_monitor_png()
        check("C3 全屏截图落盘（cad_fullscreen.png）",
              shot is not None and bool(
                  cv2.imwrite(str(FULLSHOT), shot)),
              f"{FULLSHOT.name} {shot.shape if shot is not None else '-'}")
        m1 = measure(shot, canvas_box) if shot is not None else {}
        sc_after = sc_of(title_of(hwnd))
        check("C4 像素级：矩形宽高比 ≈ 2.0（100×50）",
              m1.get("aspect") is not None
              and 1.85 <= m1["aspect"] <= 2.15,
              json.dumps(m1, ensure_ascii=False))
        check("C5 像素级：圆直径比 ≈ 0.2 且与矩形同心（R10 @正中）",
              m1.get("d_ratio") is not None
              and 0.17 <= m1["d_ratio"] <= 0.23
              and abs(m1.get("center_dx") or 99) <=
              max(6, 0.03 * (m1.get("rect_w") or 1))
              and abs(m1.get("center_dy") or 99) <=
              max(6, 0.03 * (m1.get("rect_h") or 1)),
              f"d_ratio={m1.get('d_ratio')} "
              f"center=({m1.get('center_dx')},{m1.get('center_dy')})")
        check("C6 ZOOM E 真实生效（比例增大 + 矩形像素变宽）",
              sc_before_zoom > 0 and sc_after > sc_before_zoom
              and rect_w_before > 0 and (m1.get("rect_w") or 0)
              > rect_w_before,
              f"SC {sc_before_zoom}→{sc_after} "
              f"rect_w {rect_w_before}→{m1.get('rect_w')}")

        # VLM 语义验证（主仓点名的问题；2B 怪癖 → 失败带语境重问一次）
        q1 = "这张图里画了什么形状的图形？"
        d1 = vl_describe(q1, hwnd)
        ans1 = (d1.get("description") or "").strip()
        rect_kw = ("矩形", "长方形", "方形", "正方", "长方")
        circ_kw = ("圆", "孔", "洞")
        hit = (any(k in ans1 for k in rect_kw)
               and any(k in ans1 for k in circ_kw))
        ans2 = ""
        if not hit:
            q2 = ("这是一张 CAD 软件绘图区的截图，白色线条是绘制的零件轮廓。"
                  + q1)
            d2 = vl_describe(q2, hwnd)
            ans2 = (d2.get("description") or "").strip()
            hit = (any(k in ans2 for k in rect_kw)
                   and any(k in ans2 for k in circ_kw))
        check("C7 VLM 读图认出「带孔矩形」",
              d1.get("ok") is True and hit,
              f"q1答={ans1[:80]!r}"
              + (f" q2答={ans2[:80]!r}" if ans2 else ""))
        time.sleep(1.0)
        rec.phase(f"VLM：{ans1[:24] or ans2[:24]}")
        time.sleep(1.2)
    finally:
        if rec is not None:
            n = rec.stop()
            media = compose_media(rec, GIF_PATH, MP4_PATH, PNG_PATH)
            check("D1 全程录屏 cad_drawing.gif/mp4 合成",
                  n >= 25 and media.get("frames", 0) >= 25
                  and (media.get("gif") or 0) > 30000
                  and (media.get("mp4") or 0) > 10000
                  and (media.get("png") or 0) > 10000,
                  f"frames={n} media={media}")
            if rec.first_exc:
                print(rec.first_exc, flush=True)
        if hwnd:
            check("D2 靶窗销毁（只认自己 hwnd 消失）", close_hwnd(hwnd))
        for p in PROCS:
            try:
                stop_proc(p)
            except Exception:  # noqa: BLE001
                pass
        if not port_free(8030):
            kill_by_port(8030)
        time.sleep(0.5)
        check("D3 服务端口 8030 释放", port_free(8030))
        restore_cursor(cursor0)
        print("  鼠标已归位", cursor0, flush=True)
    passed = sum(1 for x in RESULTS if x)
    print(f"\n== RESULT: {passed}/{len(RESULTS)} passed ==", flush=True)
    for p in (GIF_PATH, MP4_PATH, DXF_PATH, FULLSHOT):
        print(f"  artifact: {p} ({p.stat().st_size if p.is_file() else 0} B)",
              flush=True)
    return len(RESULTS) - passed


if __name__ == "__main__":
    sys.exit(main())
