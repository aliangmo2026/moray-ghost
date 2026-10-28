"""Windows 平台驱动实现（v0.15 阶段二自 app.core.{screen_mss,backend} 迁入）。

本模块是跨平台驱动层的 Windows 真身，承载两块历史红线实证代码（逐字迁移、
零行为改动；app.core.screen_mss / app.core.backend 保留为 re-export 垫片，
所有既有 import 与单一模块状态——capture 线程、健康统计、修剪计数器、
BACKEND 单例——仍指向本模块的同一份对象）：

A. 抓屏（原 app/core/screen_mss.py，v0.14 封板 28f9954）：
   - DPI 红线：dpi_awareness 必须在 import mss / pyautogui 之前设置，
     否则截图分辨率与点击坐标系不一致（物理像素 vs 虚拟化坐标）；
   - 单 capture 线程：BitBlt 全部收口到一个 mss 实例 + 队列 + 退避自愈
     （多线程各持实例会间歇性"拒绝访问"，且 new mss 泄漏 GDI 句柄）；
   - monitors()[0] 为联合区；虚拟桌面/混合 DPI 自检（v0.14）；
   - 审计截图按 mtime 修剪到 SHOT_MAX_FILES（防 13,594 张/3.4GB 撑爆磁盘）。

B. 输入（原 app/core/backend.py，v0.7~v0.14 红线）：
   - 路由/控制层不得直接 import pyautogui，一律走 InputBackend 抽象；
   - Fail-Safe 四角常开；mouse_event 真实事件（画图语义）+ 相对事件像素级
     修正（v0.14 混合 DPI）+ SetCursorPos 兜底 + 越界显式拒绝；
   - 键盘：ASCII 走 keyboard.write（IME VK_PROCESSKEY 实证），非 ASCII 走
     剪贴板粘贴；任何路径 finally 释放全部修饰键/鼠标键（绝不泄漏按住态）。

C. 跨平台适配（v0.15 新增）：WindowsScreen / WindowsInput 实现
   app.platforms.base 的抽象契约，方法体委托本模块同名函数/BACKEND 单例
   （类方法名不进入函数作用域，委托即取模块级真身，无递归）。
"""
from __future__ import annotations

import base64
import collections
import ctypes
import ctypes.wintypes
import io
import logging
import queue
import sys
import threading
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pyperclip  # pyautogui 自带依赖；非 ASCII 文本走剪贴板粘贴

from app.core import dpi_awareness  # noqa: E402  顺序红线：DPI 感知先于 import mss/pyautogui
from .base import BaseInput, BaseScreen

LOG = logging.getLogger("moray-ghost.screen_mss")  # 保持历史 logger 名（日志检索连续性）

DPI_AWARE = dpi_awareness.DPI_AWARE  # 兼容旧引用（app.main 启动日志）
DPI_MODE = dpi_awareness.MODE

import mss  # noqa: E402
from mss.exception import ScreenShotError  # noqa: E402
from PIL import Image  # noqa: E402

import pyautogui  # noqa: E402  顺序红线：必须在 dpi_awareness 之后

pyautogui.FAILSAFE = True   # 常开；鼠标甩到角落立即中止整个动作/宏
pyautogui.PAUSE = 0.05

FailSafeException = pyautogui.FailSafeException

from app.core.config import SCREENSHOT_DIR, SHOT_MAX_FILES  # noqa: E402


# ============================================================================
# A. 抓屏（原 app/core/screen_mss.py 逐字迁入）
# ============================================================================

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
            and mons[0]["width"] == v["width"] and mons[0]["height"] == v["height"])
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


# ============================================================================
# B. 输入（原 app/core/backend.py 逐字迁入）
# =================================================================

# 所有可能被按下的修饰键（含左右变体）。释放清单宁可多不可少。
_MODIFIER_KEYS = ("ctrl", "alt", "shift", "win", "winleft", "winright",
                  "lctrl", "rctrl", "lalt", "ralt", "lshift", "rshift",
                  "lwin", "rwin")


def release_all_modifiers() -> None:
    """强制释放所有修饰键——键盘状态绝不泄漏到系统。

    任何 keyDown/hotkey/write 都必须包在 try...finally 里并在 finally 调用本函数：
    进程异常退出前、Fail-Safe 中止后、异常路径上，一律先释放。
    释放期间临时关闭 Fail-Safe——清理动作本身不能因为光标恰好在角落而中断
    （否则挂起的修饰键比一切都危险）。keyUp 对未按下的键是无害空操作。
    """
    prev = pyautogui.FAILSAFE
    pyautogui.FAILSAFE = False
    try:
        for key in _MODIFIER_KEYS:
            try:
                pyautogui.keyUp(key)
            except Exception:
                pass
    finally:
        pyautogui.FAILSAFE = prev


class InputBackend(ABC):
    """输入后端抽象（原 app.core.backend.InputBackend，ABC 契约保持不变）。"""

    @abstractmethod
    def position(self) -> Tuple[int, int]:
        ...

    @abstractmethod
    def move_to(self, x: int, y: int) -> None:
        ...

    @abstractmethod
    def click(self, clicks: int = 1, button: str = "left") -> None:
        ...

    @abstractmethod
    def write_text(self, text: str, interval: float = 0.02) -> None:
        ...

    @abstractmethod
    def hotkey(self, *names: str) -> None:
        ...

    @abstractmethod
    def key_names(self) -> List[str]:
        ...

    @abstractmethod
    def mouse_down(self, button: str = "left") -> None:
        ...

    @abstractmethod
    def mouse_up(self, button: str = "left") -> None:
        ...

    @abstractmethod
    def scroll(self, dy: int) -> None:
        ...


def release_pointer_buttons() -> None:
    """强制释放所有鼠标键——绝不把"按住"状态泄漏到系统（拖拽异常路径的 finally 用）。

    与 release_all_modifiers 同哲学：清理动作本身不能因为光标恰好在角落而中断，
    所以临时关闭 Fail-Safe。keyUp/mouseUp 对未按下的键是无害空操作。
    """
    prev = pyautogui.FAILSAFE
    pyautogui.FAILSAFE = False
    try:
        for btn in ("left", "middle", "right"):
            try:
                pyautogui.mouseUp(button=btn)
            except Exception:
                pass
    finally:
        pyautogui.FAILSAFE = prev


_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_ABSOLUTE = 0x8000
_MOUSEEVENTF_VIRTUALDESK = 0x4000
_USER32 = None
_MOUSE_EVENT = None
if sys.platform == "win32":
    # 私有 DLL 句柄：只给本模块的 mouse_event 设签名，绝不污染
    # ctypes.windll.user32 上 pyautogui 共用的函数指针（否则 mouseDown
    # 会因 argtypes 被改写而抛 c_long/ArgumentError）。
    _USER32 = ctypes.WinDLL("user32", use_last_error=True)
    _USER32.GetSystemMetrics.argtypes = (ctypes.c_int,)
    _USER32.GetSystemMetrics.restype = ctypes.c_int
    _MOUSE_EVENT = _USER32.mouse_event
    _MOUSE_EVENT.argtypes = (ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
                             ctypes.c_uint, ctypes.c_ulong)
    _MOUSE_EVENT.restype = None


def _mouse_event_absolute(x: int, y: int) -> None:
    """真实鼠标移动事件（不是 SetCursorPos）。

    现代画图（WinUI/指针输入）只把真正的鼠标移动事件画进墨迹；
    SetCursorPos 只改光标位置，拖拽会被压缩成起点→终点的一条直线。
    坐标按虚拟桌面归一化到 0..65535（MOUSEEVENTF_VIRTUALDESK）。
    注意归一化存在量化误差：宽虚拟桌面（多屏 4K 级）下 1 像素 < 65535
    的一个步进，落点可能差 1-2px——move_to 用相对事件做像素级修正。
    """
    if not _MOUSE_EVENT:
        pyautogui.moveTo(int(x), int(y), duration=0)
        return
    xv = int(_USER32.GetSystemMetrics(76))   # SM_XVIRTUALSCREEN
    yv = int(_USER32.GetSystemMetrics(77))   # SM_YVIRTUALSCREEN
    wv = int(_USER32.GetSystemMetrics(78))   # SM_CXVIRTUALSCREEN
    hv = int(_USER32.GetSystemMetrics(79))   # SM_CYVIRTUALSCREEN
    nx = int(round((int(x) - xv) * 65535.0 / max(1, wv - 1)))
    ny = int(round((int(y) - yv) * 65535.0 / max(1, hv - 1)))
    nx = max(0, min(65535, nx))
    ny = max(0, min(65535, ny))
    _MOUSE_EVENT(_MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE
                 | _MOUSEEVENTF_VIRTUALDESK, nx, ny, 0, 0)


def _mouse_event_relative(dx: int, dy: int) -> None:
    """相对物理像素移动：无归一化量化误差（v0.14 混合 DPI 像素级修正）。

    mouse_event 的 dx/dy 在 MSDN 语义里是带符号 LONG；经 c_uint 传参需
    手工按 32 位补码转 unsigned。相对位移单位恒为物理像素（DPI 感知进程），
    与屏幕缩放比例无关——这正是绝对归一化在多屏混合 DPI 下不具备的性质。
    """
    if not _MOUSE_EVENT:
        pyautogui.moveRel(int(dx), int(dy), duration=0)
        return
    _MOUSE_EVENT(_MOUSEEVENTF_MOVE, int(dx) & 0xFFFFFFFF,
                 int(dy) & 0xFFFFFFFF, 0, 0)


def _fail_safe_hit() -> None:
    """mouse_event 绕过了 pyautogui 的调用级 fail-safe，这里补回四角检查。"""
    if not pyautogui.FAILSAFE:
        return
    p = pyautogui.position()
    for fx, fy in getattr(pyautogui, "FAILSAFE_POINTS", [(0, 0)]):
        if abs(p.x - fx) <= 2 and abs(p.y - fy) <= 2:
            raise FailSafeException(
                "mouse is parked in a Fail-Safe corner after move")


class PyAutoGUIBackend(InputBackend):
    def position(self) -> Tuple[int, int]:
        p = pyautogui.position()
        return int(p.x), int(p.y)

    def move_to(self, x: int, y: int) -> None:
        x, y = int(x), int(y)
        # v0.14 多屏加固：绝对事件的目标必须落在虚拟桌面内。越界坐标被
        # 归一化钳到边缘会被"静默"移到另一块屏上（混合 DPI 下正是最难
        # 排查的错位来源）——宁可显式拒绝。控制器本就先做屏内校验，
        # 这里是最后一道防线（编程错误才可能触发）。
        if sys.platform == "win32" and not dpi_awareness.in_virtual_screen(x, y):
            v = dpi_awareness.virtual_screen()
            raise ValueError(
                f"move target ({x},{y}) outside virtual screen "
                f"({v['left']},{v['top']},{v['width']}x{v['height']})")
        # 必须发真实鼠标移动事件：画图等指针输入应用只认 mouse_event，
        # 不认 SetCursorPos；否则拖拽轨迹会被压缩成直线。
        _mouse_event_absolute(x, y)
        p = pyautogui.position()
        dx, dy = x - p.x, y - p.y
        if dx or dy:
            # 绝对事件的 0..65535 归一化量化误差（多屏宽桌面 >1px 常见）
            # 用相对事件做像素级修正——仍是真实鼠标事件，画图语义不变。
            _mouse_event_relative(dx, dy)
            p = pyautogui.position()
        if abs(p.x - x) > 2 or abs(p.y - y) > 2:
            pyautogui.moveTo(x, y, duration=0)  # SetCursorPos 精确兜底
        _fail_safe_hit()

    def click(self, clicks: int = 1, button: str = "left") -> None:
        # 不传坐标：调用前已完成 moveTo + 漂移校验，避免二次移动引入竞态
        pyautogui.click(clicks=int(clicks), interval=0.08 if clicks > 1 else 0.0, button=button)

    def mouse_down(self, button: str = "left") -> None:
        pyautogui.mouseDown(button=button)

    def mouse_up(self, button: str = "left") -> None:
        pyautogui.mouseUp(button=button)

    def scroll(self, dy: int) -> None:
        # 不传坐标：调用前已完成 moveTo，避免二次移动引入竞态
        pyautogui.scroll(int(dy))

    def write_text(self, text: str, interval: float = 0.02) -> None:
        text = str(text)
        try:
            if all(ord(ch) < 128 for ch in text):
                # v0.8.0：本机中文 IME 会把 pyautogui.write 的注入字母处理成
                # VK_PROCESSKEY(229)——目标应用收到 keysym='??'、char=''（实证
                # Tk 等按字符翻译的控件完全收不到文本）。keyboard.write 送出规范
                # VK+扫描码 → 真实字符（实证 char='a'）。失败回落 pyautogui.write。
                try:
                    import keyboard as _keyboard
                    _keyboard.write(text, delay=interval)
                except Exception:
                    pyautogui.write(text, interval=interval)
            else:
                # 非 ASCII（中文/emoji）：pyautogui.write 不支持，走剪贴板粘贴
                _paste_text(text)
        finally:
            release_all_modifiers()  # 键盘状态绝不泄漏到系统

    def hotkey(self, *names: str) -> None:
        try:
            pyautogui.hotkey(*names)
        finally:
            release_all_modifiers()  # 组合键中途失败也必须全部释放

    def key_names(self) -> List[str]:
        return list(pyautogui.KEYBOARD_KEYS)


BACKEND: InputBackend = PyAutoGUIBackend()


def position() -> Tuple[int, int]:
    return BACKEND.position()


def move_to(x: int, y: int) -> None:
    BACKEND.move_to(x, y)


def click(clicks: int = 1, button: str = "left") -> None:
    BACKEND.click(clicks=clicks, button=button)


def mouse_down(button: str = "left") -> None:
    BACKEND.mouse_down(button=button)


def mouse_up(button: str = "left") -> None:
    BACKEND.mouse_up(button=button)


def scroll(dy: int) -> None:
    BACKEND.scroll(dy)


def write_text(text: str, interval: float = 0.02) -> None:
    BACKEND.write_text(text, interval)


def hotkey(*names: str) -> None:
    BACKEND.hotkey(*names)


def key_names() -> List[str]:
    return BACKEND.key_names()


# GetAsyncKeyState 的虚拟键码（修饰键，含左右变体）
_VK_MODIFIERS = (("ctrl", 0x11), ("shift", 0x10), ("alt", 0x12),
                 ("win", 0x5B), ("win", 0x5C))


def pressed_modifiers() -> List[str]:
    """当前处于按下状态的修饰键（GetAsyncKeyState 高位）。

    既有可能是"挂起"（某处 keyDown 后没释放/进程被杀），也可能是用户物理按着——
    本函数只如实报告按下状态，语义判断交给调用方。
    """
    user32 = ctypes.windll.user32
    out: List[str] = []
    for name, vk in _VK_MODIFIERS:
        if user32.GetAsyncKeyState(vk) & 0x8000:
            out.append(name)
    return sorted(set(out))


GA_ROOT = 2


def point_root(pt: tuple) -> Optional[int]:
    """屏幕上 pt 像素处的最顶层窗口根句柄（点击落点归属）；失败返回 None。"""
    try:
        user32_ = ctypes.windll.user32
        leaf = user32_.WindowFromPoint(ctypes.wintypes.POINT(int(pt[0]), int(pt[1])))
        if not leaf:
            return None
        return int(user32_.GetAncestor(leaf, GA_ROOT))
    except Exception:
        return None


def foreground_hwnd() -> Optional[int]:
    """当前前台窗口 hwnd；无则 None。"""
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    return int(hwnd) if hwnd else None


def _paste_text(text: str) -> None:
    """非 ASCII 文本（中文/emoji）走剪贴板：备份 → 写入 → Ctrl+V → 还原。

    还原策略：原剪贴板是文本则还原；原为空或图片（pyperclip 无法还原图片）
    则清空。粘贴需要目标应用接受 Ctrl+V（记事本/常规编辑器均适用）。
    """
    prev = None
    try:
        prev = pyperclip.paste()
    except Exception:
        prev = None
    pyperclip.copy(text)
    try:
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.15)  # 等目标应用完成粘贴
    finally:
        release_all_modifiers()
        try:
            if isinstance(prev, str) and prev:
                pyperclip.copy(prev)  # 还原原剪贴板文本
            else:
                pyperclip.copy("")  # 原为空/图片：清空
        except Exception:
            pass


# ============================================================================
# C. 跨平台抽象适配（v0.15 阶段二新增）：Windows 实现
#
# 方法体内的同名调用解析到模块级真身（类方法名不进入函数作用域，无递归）。
# ============================================================================

class WindowsScreen(BaseScreen):
    """Windows 抓屏实现——真身即本模块 A 段（原 app.core.screen_mss）。"""

    def grab(self, monitor_index: int = 1,
             timeout: float = 8.0) -> Tuple[Image.Image, Dict[str, int]]:
        return grab(monitor_index, timeout)

    def monitors(self) -> List[Dict[str, Any]]:
        return monitors()

    def monitor_rect(self, index: int) -> Dict[str, int]:
        return monitor_rect(index)

    def monitor_at(self, x: int, y: int) -> Optional[Dict[str, int]]:
        return monitor_at(x, y)

    def virtual_screen(self) -> Dict[str, int]:
        return virtual_screen()

    def dpi_status(self) -> Dict[str, Any]:
        return dpi_status()

    def grab_health(self, window_sec: float = 60.0) -> Dict[str, Any]:
        return grab_health(window_sec)

    def encode(self, img: Image.Image, fmt: str = "jpeg",
               quality: int = 70, scale: float = 1.0) -> bytes:
        return encode(img, fmt, quality, scale)

    def save_bytes(self, data: bytes, purpose: str, fmt: str) -> Path:
        return save_bytes(data, purpose, fmt)

    def save_audit(self, img: Image.Image, purpose: str, fmt: str,
                   quality: int) -> Path:
        return save_audit(img, purpose, fmt, quality)

    def b64_of(self, img: Image.Image, fmt: str, quality: int,
               scale: float) -> str:
        return b64_of(img, fmt, quality, scale)


class WindowsInput(BaseInput):
    """Windows 输入实现——真身即本模块 B 段（原 app.core.backend）。"""

    def position(self) -> Tuple[int, int]:
        return BACKEND.position()

    def move_to(self, x: int, y: int) -> None:
        BACKEND.move_to(x, y)

    def click(self, clicks: int = 1, button: str = "left") -> None:
        BACKEND.click(clicks=clicks, button=button)

    def mouse_down(self, button: str = "left") -> None:
        BACKEND.mouse_down(button=button)

    def mouse_up(self, button: str = "left") -> None:
        BACKEND.mouse_up(button=button)

    def scroll(self, dy: int) -> None:
        BACKEND.scroll(dy)

    def write_text(self, text: str, interval: float = 0.02) -> None:
        BACKEND.write_text(text, interval)

    def hotkey(self, *names: str) -> None:
        BACKEND.hotkey(*names)

    def key_names(self) -> List[str]:
        return BACKEND.key_names()

    def release_all_modifiers(self) -> None:
        release_all_modifiers()

    def release_pointer_buttons(self) -> None:
        release_pointer_buttons()

    def pressed_modifiers(self) -> List[str]:
        return pressed_modifiers()

    def point_root(self, pt: tuple) -> Optional[int]:
        return point_root(pt)

    def foreground_hwnd(self) -> Optional[int]:
        return foreground_hwnd()


def create_screen() -> WindowsScreen:
    """平台工厂：app.platforms.get_screen() 的 Windows 入口（单例由调用方缓存）。"""
    return WindowsScreen()


def create_input() -> WindowsInput:
    """平台工厂：app.platforms.get_input() 的 Windows 入口（单例由调用方缓存）。"""
    return WindowsInput()
