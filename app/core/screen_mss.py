"""mss 截图：DPI 感知（必须先于 import mss）+ 专用抓屏线程 + PIL 编码。

Windows 红线：SetProcessDpiAwareness(2) 必须在 import mss / import pyautogui
之前调用，否则截图分辨率与点击坐标系不一致（物理像素 vs 虚拟化坐标）。

线程模型：BitBlt 抓屏全部收口到单一 capture 工作线程（一个 mss 实例、
队列化、带退避重试与实例自愈）。实测多线程各自持 mss 实例时 BitBlt 会
间歇性报“拒绝访问/no error”，单线程收口可彻底消除该类故障；同时天然
满足“禁止请求线程反复 new mss（GDI 句柄泄漏）”的红线。
monitors()/monitor_rect() 仅做显示器枚举（无 BitBlt），保留 thread-local
实例缓存。
"""
from __future__ import annotations

import base64
import collections
import ctypes
import io
import logging
import queue
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

LOG = logging.getLogger("moray-ghost.screen_mss")

DPI_AWARE = False


def _set_dpi_awareness() -> None:
    global DPI_AWARE
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        DPI_AWARE = True
    except OSError:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
            DPI_AWARE = True
        except Exception:
            DPI_AWARE = False


_set_dpi_awareness()  # 顺序红线：必须在 import mss 之前

import mss  # noqa: E402
from mss.exception import ScreenShotError  # noqa: E402
from PIL import Image  # noqa: E402

from .config import SCREENSHOT_DIR  # noqa: E402

_tls = threading.local()

# 监视器枚举（无 BitBlt）用的 thread-local 实例。
def _local_mss() -> Any:
    sct = getattr(_tls, "sct", None)
    if sct is None:
        sct = _tls.sct = mss.mss()
    return sct


# ---------------- 专用抓屏线程（BitBlt 单线程收口） ----------------

_capture_q: "queue.Queue[Any]" = queue.Queue()
_worker_lock = threading.Lock()
_worker_started = False

# 抓屏失败退避序列（桌面瞬时不可读 / DC 失效时的自愈窗口，共约 0.65s）
_GRAB_BACKOFF = (0.0, 0.05, 0.2, 0.4)

# 抓屏健康统计（供 /environment 报告最近 1 分钟成功率）
_health_lock = threading.Lock()
_grab_attempts: "collections.deque[Any]" = collections.deque(maxlen=1000)


def _record_attempt(ok: bool) -> None:
    with _health_lock:
        _grab_attempts.append((time.time(), bool(ok)))


def grab_health(window_sec: float = 60.0) -> Dict[str, Any]:
    """最近 window_sec 秒内的抓屏尝试统计 + 最后一次尝试结果。"""
    now = time.time()
    with _health_lock:
        attempts = [(ts, ok) for ts, ok in _grab_attempts if now - ts <= window_sec]
    ok_count = sum(1 for _ts, ok in attempts if ok)
    fail_count = len(attempts) - ok_count
    last_ok: Any = attempts[-1][1] if attempts else None
    return {
        "attempts_1min": len(attempts),
        "successes_1min": ok_count,
        "failures_1min": fail_count,
        "success_rate": round(ok_count / len(attempts), 4) if attempts else None,
        "last_attempt_ok": last_ok,
    }


def _capture_worker() -> None:
    sct: Any = None
    while True:
        item = _capture_q.get()
        if item is None:
            break
        monitor_index, done, box = item
        try:
            last_exc: Optional[Exception] = None
            for delay in _GRAB_BACKOFF:
                if delay:
                    time.sleep(delay)
                if sct is None:
                    sct = mss.mss()
                try:
                    idx = max(0, min(int(monitor_index), len(sct.monitors) - 1))
                    shot = sct.grab(sct.monitors[idx])
                    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                    box["img"] = img
                    box["origin"] = {"left": shot.left, "top": shot.top,
                                     "width": shot.width, "height": shot.height}
                    _record_attempt(True)
                    last_exc = None
                    break
                except ScreenShotError as exc:
                    _record_attempt(False)
                    last_exc = exc
                    LOG.warning("grab failed, recreating capture instance: %s", exc)
                    try:
                        sct.close()
                    except Exception:
                        pass
                    sct = None
            if last_exc is not None:
                box["exc"] = last_exc
        finally:
            done.set()


def _ensure_worker() -> None:
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        threading.Thread(target=_capture_worker, name="moray-ghost-capture",
                         daemon=True).start()
        _worker_started = True


def grab(monitor_index: int = 1, timeout: float = 8.0) -> Tuple[Image.Image, Dict[str, int]]:
    """抓屏并转 RGB。monitor_index=1 为主屏，0 为全屏联合区。

    返回 monitor_origin（left/top）：多屏下主屏原点可能不是 (0,0)。
    所有 BitBlt 经由专用抓屏线程执行（见模块 docstring）。
    """
    _ensure_worker()
    done = threading.Event()
    box: Dict[str, Any] = {}
    _capture_q.put((int(monitor_index), done, box))
    if not done.wait(timeout):
        raise TimeoutError(f"screen capture timed out after {timeout}s")
    if "exc" in box:
        raise box["exc"]
    return box["img"], box["origin"]


# ---------------- 显示器枚举 ----------------

def monitors() -> List[Dict[str, Any]]:
    """monitors[0] 为全屏联合区，monitors[1..n] 为各屏；primary 按 (0,0) 原点启发式判定。"""
    out: List[Dict[str, Any]] = []
    for i, m in enumerate(_local_mss().monitors):
        out.append({
            "index": i,
            "left": m["left"],
            "top": m["top"],
            "width": m["width"],
            "height": m["height"],
            "primary": i != 0 and m["left"] == 0 and m["top"] == 0,
        })
    return out


def monitor_rect(index: int) -> Dict[str, int]:
    return dict(_local_mss().monitors[index])


# ---------------- 编码与落盘 ----------------

def encode(img: Image.Image, fmt: str = "jpeg", quality: int = 70, scale: float = 1.0) -> bytes:
    if scale and 0 < scale < 1.0:
        w = max(1, round(img.width * scale))
        h = max(1, round(img.height * scale))
        img = img.resize((w, h), Image.LANCZOS)
    buf = io.BytesIO()
    if fmt == "png":
        img.save(buf, format="PNG")
    else:
        img.convert("RGB").save(buf, format="JPEG", quality=int(quality))
    return buf.getvalue()


def new_shot_path(purpose: str, fmt: str) -> Path:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    ext = "png" if fmt == "png" else "jpg"
    ts = time.strftime("%Y%m%d_%H%M%S")
    name = f"{ts}_{time.time_ns() % 1_000_000:06d}_{uuid.uuid4().hex[:4]}_{purpose}.{ext}"
    return SCREENSHOT_DIR / name


def save_bytes(data: bytes, purpose: str, fmt: str) -> Path:
    path = new_shot_path(purpose, fmt)
    path.write_bytes(data)
    return path


def save_audit(img: Image.Image, purpose: str, fmt: str, quality: int) -> Path:
    """审计截图：全分辨率落盘（不缩放），保证可回查。"""
    return save_bytes(encode(img, fmt, quality, 1.0), purpose, fmt)


def b64_of(img: Image.Image, fmt: str, quality: int, scale: float) -> str:
    return base64.b64encode(encode(img, fmt, quality, scale)).decode("ascii")
