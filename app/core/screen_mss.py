"""mss 截图：DPI 感知（必须先于 import mss）+ 专用抓屏线程 + PIL 编码。

Windows 红线：DPI 感知必须在 import mss / import pyautogui 之前设置，
否则截图分辨率与点击坐标系不一致（物理像素 vs 虚拟化坐标）。v0.14 起
收口到 dpi_awareness 模块（Per-Monitor V2 首选 + HRESULT 严格检查，
修掉旧 `except OSError` 永不触发、混合 DPI 下静默 ×1.5 错位的隐患）。

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
import io
import logging
import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

LOG = logging.getLogger("moray-ghost.screen_mss")

from . import dpi_awareness  # noqa: E402  顺序红线：必须在 import mss 之前

DPI_AWARE = dpi_awareness.DPI_AWARE  # 兼容旧引用（app.main 启动日志）
DPI_MODE = dpi_awareness.MODE

import mss  # noqa: E402
from mss.exception import ScreenShotError  # noqa: E402
from PIL import Image  # noqa: E402

from .config import SCREENSHOT_DIR, SHOT_MAX_FILES  # noqa: E402

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


# ---------------- v0.14 多屏 / 混合 DPI ----------------

def virtual_screen() -> Dict[str, int]:
    """虚拟桌面（全屏联合区）物理矩形。"""
    return dpi_awareness.virtual_screen()


def dpi_status() -> Dict[str, Any]:
    """DPI 感知档位 + 各屏物理矩形 + 虚拟桌面一致性自检。

    自检项：monitors[0]（联合区）必须覆盖每一屏、与 GetSystemMetrics
    虚拟桌面完全一致；主屏必须落在 (0,0)（Windows 虚拟屏定义）。
    """
    mons = monitors()
    out: Dict[str, Any] = dict(dpi_awareness.status())
    out["monitors"] = mons
    try:
        v = virtual_screen()
        out["virtual_screen_matches_monitors0"] = bool(
            mons and mons[0]["left"] == v["left"] and mons[0]["top"] == v["top"]
            and mons[0]["width"] == v["width"]
            and mons[0]["height"] == v["height"])
    except OSError as exc:
        out["virtual_screen_matches_monitors0"] = None
        out["virtual_screen_error"] = str(exc)
    return out


def monitor_at(x: int, y: int) -> Optional[Dict[str, int]]:
    """包含物理点 (x,y) 的屏（不含联合区 index 0）；不在任何屏上返回 None。"""
    for m in monitors():
        if m["index"] == 0:
            continue
        if (m["left"] <= x < m["left"] + m["width"]
                and m["top"] <= y < m["top"] + m["height"]):
            return m
    return None


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
    _prune_shots()
    return path


# ---------------- v0.14 审计截图保留（防无界增长撑爆磁盘） ----------------

_PRUNE_EVERY = 16  # 每 16 次落盘修剪一次（避免每次保存都遍历目录）
_prune_lock = threading.Lock()
_prune_counter = _PRUNE_EVERY - 1  # 首次落盘即修剪（清掉历史无界累积）


def _prune_shots() -> None:
    """截图目录只保留最新 SHOT_MAX_FILES 张（0 = 关闭）。

    挂机场景实测：审计 before/after 截图按动作数无界累积（13,594 张 /
    3.4GB），连续运行数周必然写满磁盘 → 崩溃。按 mtime 删最旧；最新
    N 张恒在保留集内，返回给调用方的当次落盘文件不会被删。
    """
    global _prune_counter
    cap = int(SHOT_MAX_FILES)
    if cap <= 0:
        return
    with _prune_lock:
        _prune_counter += 1
        if _prune_counter < _PRUNE_EVERY:
            return
        _prune_counter = 0
        try:
            files = [p for p in SCREENSHOT_DIR.glob("*") if p.is_file()]
            if len(files) <= cap:
                return
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            for old in files[cap:]:
                try:
                    old.unlink()
                except OSError:
                    pass  # 正被读取/已删：单文件失败不阻断
        except OSError as exc:
            LOG.warning("screenshot prune failed: %s", exc)


def save_audit(img: Image.Image, purpose: str, fmt: str, quality: int) -> Path:
    """审计截图：全分辨率落盘（不缩放），保证可回查。"""
    return save_bytes(encode(img, fmt, quality, 1.0), purpose, fmt)


def b64_of(img: Image.Image, fmt: str, quality: int, scale: float) -> str:
    return base64.b64encode(encode(img, fmt, quality, scale)).decode("ascii")
