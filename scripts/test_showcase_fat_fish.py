"""v0.17 开源首图：Ghost 全自动绘制 DeepSeek 鲸鱼娘（正面试图）+ 窗口局部录屏。

安全纪律（与全仓 GUI 测试一致）：
- 只操作本脚本 Popen 的自建 mspaint：启动前记录 mspaint PID/hwnd 差集，
  启动后只认新出现的窗口；清理只杀本次新增 PID 及其子进程。
- 启动前若发现任何既有 mspaint → 直接拒绝运行（绝不碰用户窗口）。
- 锁屏/安全桌面 → 拒绝运行（不产生任何物理输入）。
- 绘制期间目标窗口 HWND_TOPMOST + force_foreground；任何落点都被
  controller 的归属门禁复核。
- 录屏只截取 canvas_rect（画图画布区域），不录全屏；录屏走服务端
  /screenshot 单一 capture 线程，不额外开 BitBlt 实例。

流程：
  1. 从 data/fat_fish.png 裁左侧正面视图（约 1/3），去背景噪点，
     生成 data/fat_fish_face.png。
  2. 启动自建 Ghost 服务 + 自建 mspaint（竖版窗口）。
  3. canvas_info 定位画布 → dry_run 预演 → 后台录屏线程开始。
  4. POST /draw/sketch_from_image（line 模式）真实落笔。
  5. 合成加速 GIF（≤3MB）docs/assets/fat_fish_drawing.gif；
     最终画布 PNG docs/assets/fat_fish_final.png。

跑法：
  .venv\\Scripts\\python.exe scripts\\test_showcase_fat_fish.py
  （可选 --rebuild-face 强制重新预处理原图）
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    if sys.platform == "win32":
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
except OSError:
    pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import psutil  # noqa: E402
import requests  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402
import win32process  # noqa: E402
from PIL import Image  # noqa: E402

from app.core import config  # noqa: E402

PORT = 8030
TOKEN = config.ensure_token()
SRC = ROOT / "data" / "fat_fish.png"
FACE = ROOT / "data" / "fat_fish_face.png"
FACE_LINE = ROOT / "data" / "fat_fish_face_line.png"
INPUT = Path(os.environ.get("FAT_FISH_INPUT", str(FACE)))
MODE = (os.environ.get("FAT_FISH_MODE", "line").strip().lower() or "line")
K = int(os.environ.get("FAT_FISH_K", "6") or 6)
EPS = float(os.environ.get("FAT_FISH_EPS", "0.0025") or 0.0025)
ASSETS = ROOT / "docs" / "assets"
GIF_PATH = ASSETS / "fat_fish_drawing.gif"
FINAL_PATH = ASSETS / "fat_fish_final.png"
GIF_LIMIT = 3 * 1024 * 1024
TRACKED_PIDS: set = set()
SERVICE: Optional[subprocess.Popen] = None
RECORDER: Optional["CanvasRecorder"] = None
ORIGINAL_CURSOR: Optional[List[int]] = None
PAINT_HWND: Optional[int] = None


def log(msg: str) -> None:
    print("[showcase] " + msg, flush=True)


def check(ok: bool, name: str, detail: str = "") -> bool:
    log(("PASS " if ok else "FAIL ") + name + ((" | " + detail) if detail else ""))
    return bool(ok)


# ============================================================================
# 1. 图像预处理
# ============================================================================

def prepare_face() -> Path:
    """裁正面视图（左侧 1/3）+ 去背景细小噪点 + 轻度保边去纹理。"""
    if not SRC.is_file():
        raise SystemExit("原图不存在: " + str(SRC))
    src = Image.open(SRC).convert("RGBA")
    bg = Image.new("RGBA", src.size, (255, 255, 255, 255))
    img = Image.alpha_composite(bg, src).convert("RGB")
    # 正面角色范围约 x22..445, y27..629；留边距并排除「正面」标签/下方详情图
    crop = img.crop((10, 10, 458, 642))
    a = np.array(crop)

    mx = a.max(2)
    mn = a.min(2)
    near_white = (mn >= 242) & ((mx - mn) <= 12)
    a[near_white] = 255

    import cv2
    mask = (((a.max(2) - a.min(2)) > 12) | (a.max(2) < 235)).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    if n > 1:
        areas = stats[1:, cv2.CC_STAT_AREA]
        big = int(np.argmax(areas)) + 1
        bx = int(stats[big, cv2.CC_STAT_LEFT])
        by = int(stats[big, cv2.CC_STAT_TOP])
        bw = int(stats[big, cv2.CC_STAT_WIDTH])
        bh = int(stats[big, cv2.CC_STAT_HEIGHT])
        keep = np.zeros(n, bool)
        for i in range(1, n):
            x, y, w, h, area = (int(v) for v in stats[i])
            inside = (x >= bx - 8 and y >= by - 8
                      and x + w <= bx + bw + 8 and y + h <= by + bh + 8)
            if area >= 6 or inside:
                keep[i] = True
        a[~keep[labels]] = 255

    # 轻度保边平滑：压掉 JPEG/背景纹理，保留角色轮廓
    a = cv2.bilateralFilter(a, 5, 25, 25)
    out = Image.fromarray(a)
    out.save(FACE)
    log("preprocessed %s -> %s (%dx%d)" % (SRC.name, FACE.name,
                                            out.width, out.height))
    return FACE


# ============================================================================
# 2. 环境 / 窗口 / 服务
# ============================================================================

def desktop_locked() -> bool:
    try:
        fg = int(win32gui.GetForegroundWindow() or 0)
        cls = win32gui.GetClassName(fg) if fg else ""
        if cls in ("LockApp", "LockScreen"):
            return True
        u = ctypes.WinDLL("user32")
        u.OpenInputDesktop.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        u.OpenInputDesktop.restype = wt.HANDLE
        u.CloseDesktop.argtypes = [wt.HANDLE]
        h = u.OpenInputDesktop(0, False, 0x0100)
        if h:
            u.CloseDesktop(h)
            return False
        return True
    except Exception:  # noqa: BLE001
        return True


def port_free(port: int = PORT) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def mspaint_processes() -> List[psutil.Process]:
    out = []
    for p in psutil.process_iter(["pid", "name"]):
        if (p.info.get("name") or "").lower() in (
                "mspaint.exe", "paintapp.exe", "paint.exe"):
            out.append(p)
    return out


def paint_windows() -> List[int]:
    out: List[int] = []

    def _cb(hwnd, _lp):  # noqa: ANN001
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetClassName(hwnd) == "MSPaintApp":
            out.append(int(hwnd))
        return True

    win32gui.EnumWindows(_cb, None)
    return out


def cursor_pos() -> Optional[List[int]]:
    try:
        pt = wt.POINT()
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
            return [int(pt.x), int(pt.y)]
    except Exception:  # noqa: BLE001
        pass
    return None


def move_cursor(x: int, y: int) -> None:
    try:
        ctypes.windll.user32.SetCursorPos(int(x), int(y))
    except Exception:  # noqa: BLE001
        pass


def force_foreground(hwnd: int) -> bool:
    for _ in range(8):
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
        if int(win32gui.GetForegroundWindow() or 0) == int(hwnd):
            return True
    return False


def spawn_service() -> subprocess.Popen:
    env = os.environ.copy()
    env.update({
        "GHOST_PORT": str(PORT),
        "GHOST_ENV": "dev",
        "GHOST_GUARD": "off",
        "GHOST_VIRTUAL_POINTER": "false",
        "PYTHONUNBUFFERED": "1",
    })
    proc = subprocess.Popen(
        [sys.executable, "run.py"], cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc


def wait_healthy(timeout: float = 40.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if requests.get(
                    "http://127.0.0.1:%d/api/ghost/health" % PORT,
                    timeout=2).status_code == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.4)
    return False


def wait_ocr_warmed(offset: int, timeout: float = 80.0) -> bool:
    """等 OCR 预热完成，避免后台 CPU 抢占影响绘制/录屏节奏。"""
    path = ROOT / "logs" / "ghost.log"
    end = time.time() + timeout
    while time.time() < end:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(offset)
                if "ocr warmed" in fh.read()[-40000:]:
                    return True
        except OSError:
            pass
        time.sleep(1.0)
    return False


def stop_proc(proc: Optional[subprocess.Popen], port: Optional[int] = PORT) -> None:
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=6)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
    if port and not port_free(port):
        try:
            for c in psutil.net_connections(kind="inet"):
                if c.laddr and c.laddr.port == port and c.pid:
                    psutil.Process(c.pid).kill()
                    time.sleep(0.6)
        except Exception:  # noqa: BLE001
            pass


def cleanup_paint() -> None:
    for pid in list(TRACKED_PIDS):
        try:
            p = psutil.Process(pid)
            for child in p.children(recursive=True):
                try:
                    child.terminate()
                except Exception:  # noqa: BLE001
                    pass
            p.terminate()
        except Exception:  # noqa: BLE001
            pass


# ============================================================================
# 3. HTTP
# ============================================================================

def post(path: str, payload: Dict[str, Any], timeout: float = 120.0) -> Dict[str, Any]:
    try:
        r = requests.post(
            "http://127.0.0.1:%d/api/ghost%s" % (PORT, path),
            headers={"X-Ghost-Token": TOKEN}, json=payload, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "REQUEST_FAILED", "_status": 0,
                "detail": type(exc).__name__ + ": " + str(exc)}
    try:
        body = r.json()
    except ValueError:
        body = {"ok": False, "error": "BAD_JSON", "raw": r.text[:400]}
    body["_status"] = r.status_code
    return body


def get_screenshot() -> Tuple[Image.Image, List[int]]:
    r = requests.get(
        "http://127.0.0.1:%d/api/ghost/screenshot" % PORT,
        params={"monitor": 1, "fmt": "jpeg", "quality": 90, "scale": 1.0},
        timeout=20)
    r.raise_for_status()
    data = r.json()
    img = Image.open(io.BytesIO(base64.b64decode(data["image_b64"]))).convert("RGB")
    origin = data.get("monitor_origin") or [0, 0]
    return img, [int(origin[0]), int(origin[1])]


def crop_canvas(img: Image.Image, origin: List[int],
                rect: List[int], pad: int = 0) -> Image.Image:
    l, t, w, h = (int(v) for v in rect)
    x0 = max(0, l - origin[0] - pad)
    y0 = max(0, t - origin[1] - pad)
    x1 = min(img.width, l - origin[0] + w + pad)
    y1 = min(img.height, t - origin[1] + h + pad)
    return img.crop((x0, y0, x1, y1))


def inner_canvas_rect(rect: List[int], left: float = 0.12,
                      top: float = 0.03, right: float = 0.12,
                      bottom: float = 0.04) -> List[int]:
    """画布内缩：排除 Paint 左侧画笔滑条、右侧/底部滚动条。

    canvas_rect 覆盖整个画布视图（含 Win11 Paint 的左缘笔宽/不透明度滑条与
    滚动条）。角色按 contain 居中，左右有大量留白，内缩 12% 不会切到笔迹；
    上下内缩 3%/4% 避开滚动条同时保留角色脚部/尾巴。
    """
    l, t, w, h = (int(v) for v in rect)
    ix = l + int(round(w * left))
    iy = t + int(round(h * top))
    iw = max(1, int(round(w * (1.0 - left - right))))
    ih = max(1, int(round(h * (1.0 - top - bottom))))
    return [ix, iy, iw, ih]


# ============================================================================
# 4. 画布局部录屏
# ============================================================================

class CanvasRecorder:
    def __init__(self, canvas_rect: List[int], interval: float = 0.28,
                 max_width: int = 720) -> None:
        self.canvas_rect = [int(v) for v in canvas_rect]
        self.interval = interval
        self.max_width = max_width
        self.frames: List[Tuple[float, bytes]] = []
        self.errors = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="fat-fish-recorder",
                                        daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.time()
            try:
                img, origin = get_screenshot()
                crop = crop_canvas(img, origin, self.canvas_rect)
                if crop.width > self.max_width:
                    h = max(1, round(crop.height * self.max_width / crop.width))
                    crop = crop.resize((self.max_width, h), Image.LANCZOS)
                buf = io.BytesIO()
                crop.convert("RGB").save(buf, format="JPEG", quality=85)
                self.frames.append((time.time(), buf.getvalue()))
            except Exception:  # noqa: BLE001
                self.errors += 1
            self._stop.wait(max(0.02, self.interval - (time.time() - t0)))

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=6.0)


# ============================================================================
# 5. GIF 合成
# ============================================================================

def _ink_pixels(img: Image.Image) -> int:
    a = np.asarray(img.convert("RGB"))
    # 只数深色/有彩度的墨迹；不把浅灰画布底当成墨
    gray = a.mean(2)
    return int((((a.max(2) - a.min(2)) > 25) | (gray < 200)).sum())


def build_gif(frames: List[Tuple[float, bytes]], out_path: Path) -> Dict[str, Any]:
    if not frames:
        raise RuntimeError("no recorded frames")
    imgs = [Image.open(io.BytesIO(b)).convert("RGB") for _ts, b in frames]
    # 去掉开头纯白等待帧（保留 2 帧前摇）
    first = 0
    for i, im in enumerate(imgs):
        if _ink_pixels(im) > 40:
            first = max(0, i - 2)
            break
    imgs = imgs[first:]
    if not imgs:
        raise RuntimeError("all frames blank")
    # 选帧上限（保持首尾）
    if len(imgs) > 140:
        idx = [round(i * (len(imgs) - 1) / 139) for i in range(140)]
        imgs = [imgs[i] for i in idx]

    # 多档压缩：先 640px/64 色，超 3MB 再降
    attempts = [(640, 64, 140), (560, 48, 120), (480, 32, 100)]
    for width, colors, max_frames in attempts:
        use = list(imgs)
        if len(use) > max_frames:
            idx = [round(i * (len(use) - 1) / (max_frames - 1))
                   for i in range(max_frames)]
            use = [use[i] for i in idx]
        use.append(use[-1].copy())  # 最终定格
        durations = [80] * (len(use) - 1) + [1400]

        def _fit(im: Image.Image) -> Image.Image:
            if im.width == width:
                return im
            h = max(1, round(im.height * width / im.width))
            return im.resize((width, h), Image.LANCZOS)

        fitted = [_fit(im) for im in use]
        base = fitted[-1].quantize(colors=colors, method=Image.MEDIANCUT)
        p_frames = []
        for im in fitted:
            p_frames.append(im.quantize(palette=base, dither=Image.NONE))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        p_frames[0].save(
            out_path, save_all=True, append_images=p_frames[1:],
            duration=durations, loop=0, optimize=True, disposal=2)
        size = out_path.stat().st_size
        log("gif attempt %dx%d/%d colors/%d frames -> %.2f MB"
            % (width, fitted[0].height, colors, len(p_frames), size / 1048576))
        if size <= GIF_LIMIT:
            return {"width": width, "height": fitted[0].height,
                    "colors": colors, "frames": len(p_frames),
                    "bytes": size, "duration_ms": sum(durations)}
    raise RuntimeError("GIF still exceeds 3MB after all compression attempts")


# ============================================================================
# 6. main
# ============================================================================

def main() -> int:
    global SERVICE, RECORDER, ORIGINAL_CURSOR, PAINT_HWND
    started = time.time()
    log("== fat fish showcase ==")
    if not check(not desktop_locked(), "desktop unlocked"):
        return 2
    if not check(port_free(), "port %d free" % PORT):
        log("stop any running Ghost service first")
        return 2
    existing = mspaint_processes()
    if existing:
        log("existing mspaint detected: %s" % [p.pid for p in existing])
        log("请先关闭用户自己的画图窗口，展示脚本绝不触碰既有窗口。")
        return 2

    try:
        prepare_face()
        log("sketch input: %s mode=%s k=%d eps=%s" % (INPUT, MODE, K, EPS))
        if not INPUT.is_file():
            log("sketch input not found: %s" % INPUT)
            return 2
        ORIGINAL_CURSOR = cursor_pos()
        if ORIGINAL_CURSOR and ORIGINAL_CURSOR[0] <= 2 and ORIGINAL_CURSOR[1] <= 2:
            log("cursor parked in Fail-Safe corner; move it first")
            return 2

        before_pids = {p.pid for p in mspaint_processes()}
        before_hwnds = set(paint_windows())
        log_offset = 0
        try:
            log_offset = (ROOT / "logs" / "ghost.log").stat().st_size
        except OSError:
            pass

        SERVICE = spawn_service()
        if not check(wait_healthy(), "Ghost service healthy"):
            return 1
        if not check(wait_ocr_warmed(log_offset), "OCR warmup settled"):
            log("(continue without waiting)")

        # 自建 mspaint：PID + hwnd 双差集
        paint = subprocess.Popen(
            ["mspaint.exe"], cwd=str(ROOT),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        hwnd = 0
        end = time.time() + 20
        while time.time() < end and not hwnd:
            for h in paint_windows():
                if h not in before_hwnds:
                    hwnd = int(h)
                    break
            time.sleep(0.3)
        after_pids = {p.pid for p in mspaint_processes()} - before_pids
        if hwnd:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            after_pids.add(int(pid))
        TRACKED_PIDS.update(after_pids)
        PAINT_HWND = int(hwnd) if hwnd else None
        if not check(bool(hwnd), "self-built mspaint window",
                     "hwnd=%r pids=%s" % (hwnd, sorted(after_pids))):
            return 1

        caps = {"allowed_windows": ["mspaint.exe"]}
        # 试多种窗口尺寸，优先 canvas_info=deteced 且 dry_run 无越画布警告
        chosen = None
        for size, pos in (((900, 1250), (680, 60)),
                          ((1200, 1100), (100, 60)),
                          ((1400, 1000), (100, 60))):
            win32gui.SetWindowPos(
                hwnd, win32con.HWND_TOPMOST, pos[0], pos[1], size[0], size[1],
                win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW)
            time.sleep(0.5)
            force_foreground(hwnd)
            cinfo = post("/draw/canvas_info", {"hwnd": hwnd, "capabilities": caps},
                         timeout=60)
            canvas = cinfo.get("canvas") or {}
            rect = canvas.get("canvas_rect")
            log("canvas_info size=%s source=%s rect=%s" %
                (size, canvas.get("source"), rect))
            if not (cinfo.get("ok") and rect and int(rect[2]) > 300
                    and int(rect[3]) > 300):
                continue
            plan = post("/draw/sketch_from_image", {
                "image_path": str(INPUT), "mode": MODE, "hwnd": hwnd,
                "capabilities": caps, "dry_run": True,
                "max_strokes": 120, "max_total_points": 8000, "margin": 0.05,
                "k": K, "line_epsilon_scale": EPS,
            }, timeout=120)
            warnings = plan.get("warnings") or []
            bad = [w for w in warnings
                   if str(w.get("code")) in ("OUT_OF_CANVAS", "TOOLBAR_OVERLAP",
                                             "OUT_OF_WINDOW", "OUT_OF_MONITOR")]
            log("dry_run strokes=%r points=%r warnings=%d bad=%d" %
                (plan.get("strokes_planned"), plan.get("points_total"),
                 len(warnings), len(bad)))
            if plan.get("ok") and (plan.get("strokes_planned") or 0) >= 3 and not bad:
                chosen = (rect, plan)
                break
        if chosen is None:
            # 最后兜底：用当前画布，只要 canvas_info 可用
            cinfo = post("/draw/canvas_info", {"hwnd": hwnd, "capabilities": caps},
                         timeout=60)
            rect = (cinfo.get("canvas") or {}).get("canvas_rect")
            if not (cinfo.get("ok") and rect):
                log("no usable canvas rect")
                return 1
            chosen = (rect, {})
            log("fallback to canvas rect %s (warnings may remain)" % (rect,))
        canvas_rect, plan = chosen
        # 录制/最终截图只取画布内缩区，排除 Paint 左缘滑条与滚动条
        record_rect = inner_canvas_rect(canvas_rect)
        check(True, "canvas selected", "full=%s record=%s"
              % (canvas_rect, record_rect))
        if plan:
            log("plan: strokes=%s points=%s warnings=%s"
                % (plan.get("strokes_planned"), plan.get("points_total"),
                   [w.get("code") for w in (plan.get("warnings") or [])][:6]))

        # 录屏（只画布内缩区域）
        RECORDER = CanvasRecorder(record_rect, interval=0.15)
        RECORDER.start()
        time.sleep(0.4)

        log("drawing ...")
        t0 = time.time()
        result = post("/draw/sketch_from_image", {
            "image_path": str(INPUT), "mode": MODE, "hwnd": hwnd,
            "capabilities": caps, "max_strokes": 120,
            "max_total_points": 8000, "margin": 0.05, "k": K,
            "line_epsilon_scale": EPS,
        }, timeout=900)
        elapsed = time.time() - t0
        log("draw request done in %.1fs: ok=%r strokes=%r points=%r chunks=%r"
            % (elapsed, result.get("ok"), result.get("strokes_drawn"),
               result.get("points_drawn"), result.get("chunks_executed")))
        if result.get("_status") != 200 or not result.get("ok"):
            log("draw failed: " + json.dumps(result, ensure_ascii=False)[:800])
            return 1
        if (result.get("strokes_drawn") or 0) < 1:
            log("no strokes drawn")
            return 1

        time.sleep(1.6)  # 让最后几笔渲染完 + 录屏收尾
        RECORDER.stop()
        log("recorded %d frames (%d errors)" % (len(RECORDER.frames),
                                                RECORDER.errors))

        # 最终画布 PNG（全分辨率，画布内缩区）
        img, origin = get_screenshot()
        final = crop_canvas(img, origin, record_rect)
        ASSETS.mkdir(parents=True, exist_ok=True)
        final.save(FINAL_PATH)
        ink = _ink_pixels(final)
        check(ink > 500, "final canvas has ink", "ink_px=%d size=%dx%d"
              % (ink, final.width, final.height))

        # GIF
        gif_info = build_gif(RECORDER.frames, GIF_PATH)
        check(GIF_PATH.stat().st_size <= GIF_LIMIT, "gif <= 3MB",
              "%.2f MB" % (GIF_PATH.stat().st_size / 1048576))
        log("gif: %s" % GIF_PATH)
        log("final: %s" % FINAL_PATH)
        log("gif_info: " + json.dumps(gif_info, ensure_ascii=False))
        log("total %.1fs" % (time.time() - started))
        return 0
    finally:
        try:
            if RECORDER is not None:
                RECORDER.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            if PAINT_HWND:
                try:
                    win32gui.SetWindowPos(
                        PAINT_HWND, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                        win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
                        | win32con.SWP_NOACTIVATE)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
        cleanup_paint()
        if ORIGINAL_CURSOR:
            move_cursor(ORIGINAL_CURSOR[0], ORIGINAL_CURSOR[1])
        stop_proc(SERVICE, PORT)
        log("cleanup done; port_free=%r" % port_free())


if __name__ == "__main__":
    sys.exit(main())
