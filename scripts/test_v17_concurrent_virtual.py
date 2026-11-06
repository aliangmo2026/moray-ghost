"""v0.17 并发调度与虚拟输入通道验收。

覆盖：
  A 单元：细粒度窗口锁（不同 hwnd 并行 / 同 hwnd 串行）、任务工作池并发、
    virtual 通道参数门禁（无 hwnd / 不支持动作）、默认关闭实验性合成笔。
  B 契约：/health 回显 virtual_input + action_locks；virtual 类型/拖拽走
    controller 全门禁；virtual fallback=false 时无效果必须 409 且零物理输入。
  C 真机：
    C1 Win11 记事本 virtual type —— WM_CHAR 直达 RichEdit，WM_GETTEXT 回读；
    C2 经典 Win32 画布 virtual drag —— PostMessage 落墨，128px 像素校验；
    C3 Win11 画图（WinUI）virtual drag 且 fallback=false —— 409 零输入；
    C4 Win11 画图 virtual drag 默认降级 —— 成功落墨但走 physical（审计标签）。
  D 并发：线程 A 记事本 virtual 打字 + 线程 B 经典画布 virtual 落墨，同时
    物理鼠标沿屏幕底部空区移动；断言鼠标轨迹不受虚拟动作干扰、两窗口都成功。

诚实边界（本机 Windows 11 26200 实测）：
- 新版画图是 WinUI 内容岛，忽略 WM_LBUTTONDOWN/UP/MOUSEMOVE；合成笔
  （InjectSyntheticPointerInput）4 笔仅 1 笔落墨，不能作为稳定通道，默认关闭。
- 因此「画图」在并发证明段使用自建经典 Win32 画布靶窗；Win11 画图单独
  验证「自动 physical 降级 + 审计」，绝不把偶尔成功写成 100%。

跑法：.venv\\Scripts\\python.exe scripts\\test_v17_concurrent_virtual.py
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    if sys.platform == "win32":
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
except OSError:
    pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psutil  # noqa: E402
import requests  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402

from app.core import action_locks, config, task_queue, virtual_input  # noqa: E402
from app.core.audit_sink import tail as audit_tail  # noqa: E402

PORT = 8032
TOKEN = config.ensure_token()
NOTE_FILE = ROOT / "data" / "_v17_notepad.txt"
RESULTS: List[bool] = []
PROCS: List[subprocess.Popen] = []
SERVICE_LOG: "deque[str]" = deque(maxlen=80)
APP_PIDS: set = set()


def app_processes() -> List[Tuple[int, str]]:
    out: List[Tuple[int, str]] = []
    for proc in psutil.process_iter(["pid", "name"]):
        name = (proc.info.get("name") or "").lower()
        if name in ("notepad.exe", "mspaint.exe"):
            out.append((int(proc.info["pid"]), name))
    return out


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(bool(ok))
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name,
                        ("  | " + detail) if detail else ""), flush=True)
    return bool(ok)


def port_free(port: int = PORT) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def kill_by_port(port: int = PORT) -> None:
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.laddr and conn.laddr.port == port and conn.pid:
                psutil.Process(conn.pid).kill()
                time.sleep(0.8)
    except Exception as exc:  # noqa: BLE001
        print("  (kill_by_port: " + type(exc).__name__ + ")", flush=True)


def stop_proc(proc: Optional[subprocess.Popen],
              port: Optional[int] = PORT) -> None:
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    if port and not port_free(port):
        kill_by_port(port)


def spawn_service() -> subprocess.Popen:
    env = os.environ.copy()
    env.update({
        "GHOST_PORT": str(PORT),
        "GHOST_ENV": "dev",
        "GHOST_GUARD": "off",          # 本测试没有 click，关掉 OCR 加速启动
        "GHOST_VIRTUAL_INPUT": "true",
        "GHOST_VIRTUAL_FALLBACK": "true",
        "GHOST_VIRTUAL_VERIFY": "true",
        "GHOST_VIRTUAL_POINTER": "false",
        "GHOST_MAX_CONCURRENT_TASKS": "4",
        "PYTHONUNBUFFERED": "1",
    })
    proc = subprocess.Popen(
        [sys.executable, "run.py"], cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace")
    PROCS.append(proc)

    def _drain() -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                SERVICE_LOG.append(str(line).rstrip())
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=_drain, daemon=True).start()
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


def health() -> Dict[str, Any]:
    try:
        return requests.get(
            "http://127.0.0.1:%d/api/ghost/health" % PORT, timeout=4).json()
    except Exception:  # noqa: BLE001
        return {}


def post_action(payload: Dict[str, Any], timeout: float = 120.0) -> Dict[str, Any]:
    try:
        resp = requests.post(
            "http://127.0.0.1:%d/api/ghost/execute_action" % PORT,
            headers={"X-Ghost-Token": TOKEN}, json=payload, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "REQUEST_FAILED", "_status": 0,
                "detail": type(exc).__name__ + ": " + str(exc)}
    try:
        body = resp.json()
    except ValueError:
        body = {"ok": False, "error": "BAD_JSON", "raw": resp.text[:300]}
    body["_status"] = resp.status_code
    return body


# ============================================================================
# A. 单元（零 GUI 输入）
# ============================================================================

def unit_locks() -> None:
    barrier = threading.Barrier(2)
    done: List[str] = []

    def worker(hwnd: int, tag: str) -> None:
        lease = action_locks.acquire_window(hwnd, 2.0)
        if lease is None:
            return
        try:
            barrier.wait(timeout=2.0)
            time.sleep(0.3)
            done.append(tag)
        finally:
            lease.release()

    t0 = time.perf_counter()
    t1 = threading.Thread(target=worker, args=(0xA110, "a"))
    t2 = threading.Thread(target=worker, args=(0xA220, "b"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    elapsed = time.perf_counter() - t0
    check("A1 不同 hwnd 窗口锁可并行（0.3s 不再串行排队）",
          elapsed < 0.55 and sorted(done) == ["a", "b"],
          "elapsed=%.2fs done=%s" % (elapsed, sorted(done)))

    lease = action_locks.acquire_window(0xA330, 1.0)
    result: Dict[str, Any] = {}

    def contender() -> None:
        result["lease"] = action_locks.acquire_window(0xA330, 0.2)

    t3 = threading.Thread(target=contender)
    t3.start()
    t3.join()
    blocked = result.get("lease")
    check("A2 同 hwnd 窗口锁严格串行（另一线程获取超时）",
          lease is not None and blocked is None,
          "blocked=%r" % (blocked,))
    if lease is not None:
        lease.release()

    snap = action_locks.snapshot()
    check("A3 锁统计记录并发峰值 >= 2",
          int(snap.get("max_concurrent") or 0) >= 2,
          "snapshot=%s" % json.dumps(snap, ensure_ascii=False)[:220])


def unit_task_pool() -> None:
    from app.core.task_runner import TaskRun

    task_queue.start_dispatcher()
    t1 = TaskRun("v17_pool_1", [{"step_id": 1, "kind": "wait", "ms": 500}],
                 "v17 concurrency probe")
    t2 = TaskRun("v17_pool_2", [{"step_id": 1, "kind": "wait", "ms": 500}],
                 "v17 concurrency probe")
    # 生产链路由路由层在 enqueue 前落 queued；工作池只派发 queued（防御脏条目）
    t1.status = "queued"
    t2.status = "queued"
    task_queue.enqueue(t1)
    task_queue.enqueue(t2)
    peak = 0
    end = time.time() + 6.0
    while time.time() < end:
        peak = max(peak, task_queue.running_count())
        if t1.status == "done" and t2.status == "done":
            break
        time.sleep(0.02)
    task_queue.stop_dispatcher()
    check("A4 任务工作池同时运行 >= 2 个任务", peak >= 2, "peak=%d" % peak)
    check("A5 两个并发任务均完成",
          t1.status == "done" and t2.status == "done",
          "status=%s/%s" % (t1.status, t2.status))


def unit_virtual_gates() -> None:
    from app.core import controller

    try:
        controller.run_action({
            "action": "type", "text": "x", "channel": "virtual",
            "capabilities": {"allowed_windows": ["python.exe"]}})
        check("A6 virtual 无 hwnd 必须拒绝", False, "unexpected success")
    except controller.ActionError as exc:
        check("A6 virtual 无 hwnd 必须拒绝",
              exc.status == 400 and exc.error == "SAFETY_BLOCKED"
              and "hwnd" in exc.detail,
              "%s/%s" % (exc.status, exc.error))

    check("A7 实验性合成笔默认关闭（WinUI 上不稳定）",
          config.VIRTUAL_POINTER_ENABLED is False,
          "GHOST_VIRTUAL_POINTER=%r" % config.VIRTUAL_POINTER_ENABLED)
    desc = virtual_input.describe()
    check("A8 虚拟通道能力自述可用",
          bool(desc.get("available")) and bool(desc.get("synthetic_pointer")),
          json.dumps(desc, ensure_ascii=False)[:240])


# ============================================================================
# 经典 Win32 画布靶窗（真实 WM_LBUTTONDOWN/MOUSEMOVE/UP，PostMessage 可落墨）
# ============================================================================

_u = ctypes.WinDLL("user32", use_last_error=True)
_g = ctypes.WinDLL("gdi32", use_last_error=True)
_k = ctypes.WinDLL("kernel32", use_last_error=True)
_LRESULT = ctypes.c_ssize_t
_WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class _WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT), ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR),
    ]


class _PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", wt.HDC), ("fErase", wt.BOOL), ("rcPaint", wt.RECT),
        ("fRestore", wt.BOOL), ("fIncUpdate", wt.BOOL),
        ("rgbReserved", ctypes.c_byte * 32),
    ]


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wt.HWND), ("message", wt.UINT), ("wParam", wt.WPARAM),
        ("lParam", wt.LPARAM), ("time", wt.DWORD), ("pt", wt.POINT),
    ]


_k.GetModuleHandleW.argtypes = [wt.LPCWSTR]
_k.GetModuleHandleW.restype = wt.HMODULE
_u.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
_u.RegisterClassW.restype = wt.ATOM
_u.CreateWindowExW.argtypes = [
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
_u.CreateWindowExW.restype = wt.HWND
_u.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
_u.DefWindowProcW.restype = _LRESULT
_u.GetMessageW.argtypes = [ctypes.POINTER(_MSG), wt.HWND, wt.UINT, wt.UINT]
_u.GetMessageW.restype = wt.BOOL
_u.TranslateMessage.argtypes = [ctypes.POINTER(_MSG)]
_u.DispatchMessageW.argtypes = [ctypes.POINTER(_MSG)]
_u.DispatchMessageW.restype = _LRESULT
_u.BeginPaint.argtypes = [wt.HWND, ctypes.POINTER(_PAINTSTRUCT)]
_u.BeginPaint.restype = wt.HDC
_u.EndPaint.argtypes = [wt.HWND, ctypes.POINTER(_PAINTSTRUCT)]
_u.InvalidateRect.argtypes = [wt.HWND, wt.LPVOID, wt.BOOL]
_u.FillRect.argtypes = [wt.HDC, ctypes.POINTER(wt.RECT), wt.HBRUSH]
_u.FillRect.restype = ctypes.c_int
_g.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wt.COLORREF]
_g.CreatePen.restype = wt.HPEN
_g.GetStockObject.argtypes = [ctypes.c_int]
_g.GetStockObject.restype = wt.HGDIOBJ
_g.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
_g.SelectObject.restype = wt.HGDIOBJ
_g.MoveToEx.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, wt.LPVOID]
_g.LineTo.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
_g.DeleteObject.argtypes = [wt.HGDIOBJ]


class NativeCanvas:
    """经典 Win32 窗口：WM_LBUTTONDOWN/MOUSEMOVE/UP → 记录并画线。

    关键：窗口必须在跑 GetMessage 的同一个线程创建；否则 PostMessage 会进
    创建线程的队列而永远不被取出（v0.17 实弹踩坑）。
    """

    def __init__(self) -> None:
        self.hwnd = 0
        self.points: List[Tuple[int, int, int, int]] = []
        self.clicks: List[Tuple[str, int, int]] = []
        self._ready = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc = None
        self._last: Optional[Tuple[int, int]] = None
        self._drawing = False
        self._lock = threading.Lock()

    def start(self) -> bool:
        self._thread = threading.Thread(target=self._run, name="v17-win32-canvas",
                                        daemon=True)
        self._thread.start()
        return self._ready.wait(4.0) and bool(self.hwnd)

    def _xy(self, lp: int) -> Tuple[int, int]:
        return (ctypes.c_short(lp & 0xFFFF).value,
                ctypes.c_short((lp >> 16) & 0xFFFF).value)

    def _run(self) -> None:
        def wndproc(hwnd, msg, wp, lp):  # noqa: ANN001
            if msg == 0x0201:      # WM_LBUTTONDOWN
                x, y = self._xy(lp)
                with self._lock:
                    self.clicks.append(("down", x, y))
                    self._last = (x, y)
                    self._drawing = True
                return 0
            if msg == 0x0200:      # WM_MOUSEMOVE
                x, y = self._xy(lp)
                if self._drawing and self._last is not None:
                    with self._lock:
                        self.points.append((self._last[0], self._last[1], x, y))
                        self._last = (x, y)
                    _u.InvalidateRect(hwnd, None, False)
                return 0
            if msg == 0x0202:      # WM_LBUTTONUP
                x, y = self._xy(lp)
                with self._lock:
                    self.clicks.append(("up", x, y))
                    self._drawing = False
                    self._last = None
                _u.InvalidateRect(hwnd, None, False)
                return 0
            if msg == 0x000F:      # WM_PAINT
                ps = _PAINTSTRUCT()
                hdc = _u.BeginPaint(hwnd, ctypes.byref(ps))
                rect = wt.RECT()
                _u.GetClientRect(hwnd, ctypes.byref(rect))
                _u.FillRect(hdc, ctypes.byref(rect), _g.GetStockObject(0))
                pen = _g.CreatePen(0, 4, 0)
                old = _g.SelectObject(hdc, pen)
                with self._lock:
                    pts = list(self.points)
                for x1, y1, x2, y2 in pts:
                    _g.MoveToEx(hdc, x1, y1, None)
                    _g.LineTo(hdc, x2, y2)
                _g.SelectObject(hdc, old)
                _g.DeleteObject(pen)
                _u.EndPaint(hwnd, ctypes.byref(ps))
                return 0
            if msg == 0x0010:      # WM_CLOSE
                _u.DestroyWindow(hwnd)
                return 0
            if msg == 0x0002:      # WM_DESTROY
                _u.PostQuitMessage(0)
                return 0
            return _u.DefWindowProcW(hwnd, msg, wp, lp)

        self._proc = _WNDPROC(wndproc)  # 保持回调存活
        cls = "GhostV17Canvas_%d" % os.getpid()
        wc = _WNDCLASSW()
        wc.lpfnWndProc = self._proc
        wc.hInstance = _k.GetModuleHandleW(None)
        wc.lpszClassName = cls
        wc.hbrBackground = _g.GetStockObject(0)
        _u.RegisterClassW(ctypes.byref(wc))
        hwnd = _u.CreateWindowExW(
            0, cls, "GhostV17Canvas", 0x00CF0000,
            160, 900, 720, 480, None, None, wc.hInstance, None)
        self.hwnd = int(hwnd or 0)
        _u.ShowWindow(hwnd, 5)
        _u.UpdateWindow(hwnd)
        self._ready.set()
        msg = _MSG()
        while _u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _u.TranslateMessage(ctypes.byref(msg))
            _u.DispatchMessageW(ctypes.byref(msg))

    def stop(self) -> None:
        if self.hwnd:
            try:
                _u.PostMessageW(wt.HWND(self.hwnd), 0x0010, 0, 0)
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)


# ============================================================================
# 桌面 / 窗口辅助
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


def find_window(cls: str, title_substr: str = "",
                timeout: float = 12.0) -> Optional[int]:
    end = time.time() + timeout
    fallback: Optional[int] = None
    while time.time() < end:
        found: List[Tuple[int, str]] = []

        def _cb(hwnd, _lp):  # noqa: ANN001
            if win32gui.IsWindowVisible(hwnd) and win32gui.GetClassName(hwnd) == cls:
                found.append((int(hwnd), win32gui.GetWindowText(hwnd)))
            return True

        win32gui.EnumWindows(_cb, None)
        for hwnd, title in found:
            if title_substr and title_substr.lower() in title.lower():
                return hwnd
        if found and fallback is None:
            fallback = found[0][0]
        time.sleep(0.3)
    return fallback


def set_topmost(hwnd: int, x: int, y: int, w: int, h: int) -> None:
    win32gui.SetWindowPos(
        hwnd, win32con.HWND_TOPMOST, x, y, w, h,
        win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW)


def drop_topmost(hwnd: int) -> None:
    try:
        win32gui.SetWindowPos(
            hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE
            | win32con.SWP_NOACTIVATE)
    except Exception:  # noqa: BLE001
        pass


def root_at_point(x: int, y: int) -> Optional[int]:
    try:
        leaf = win32gui.WindowFromPoint((int(x), int(y)))
        return int(win32gui.GetAncestor(leaf, 2)) if leaf else None
    except Exception:  # noqa: BLE001
        return None


def cursor() -> Optional[List[int]]:
    return virtual_input._cursor_pos()


def move_cursor_physical(x: int, y: int) -> None:
    from app.core import backend
    backend._mouse_event_absolute(int(x), int(y))


def close_app_window(hwnd: Optional[int]) -> None:
    if hwnd:
        try:
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        except Exception:  # noqa: BLE001
            pass


# ============================================================================
# B/C. 契约 + 真机
# ============================================================================

def contract_health() -> None:
    h = health()
    vi = h.get("virtual_input") or {}
    locks = h.get("action_locks") or {}
    check("B1 /health 回显虚拟通道能力",
          bool(vi.get("available")) and vi.get("enabled") is True
          and "synthetic_pointer" in vi,
          json.dumps(vi, ensure_ascii=False)[:220])
    check("B2 /health 回显细粒度锁策略",
          locks.get("policy") == "fine_grained_window_lock",
          json.dumps(locks, ensure_ascii=False)[:220])
    check("B3 实验性合成笔默认关闭（health 一致）",
          vi.get("pointer_enabled") is False,
          "pointer_enabled=%r" % vi.get("pointer_enabled"))


def virtual_gate_checks(nh: int) -> None:
    from app.core import controller
    caps = {"allowed_windows": ["notepad.exe", "mspaint.exe", "python.exe"]}
    try:
        controller.run_action({
            "action": "hotkey", "text": "ctrl+s", "hwnd": nh,
            "channel": "virtual", "capabilities": caps})
        check("B4 virtual hotkey 必须 400 VIRTUAL_UNSUPPORTED", False)
    except controller.ActionError as exc:
        check("B4 virtual hotkey 必须 400 VIRTUAL_UNSUPPORTED",
              exc.status == 400 and exc.error == "VIRTUAL_UNSUPPORTED",
              "%s/%s" % (exc.status, exc.error))


def notepad_checks(nh: int, caps: Dict[str, Any]) -> str:
    token = "[V17-NOTEPAD-%d]" % int(time.time() * 1000 % 1000000)
    c0 = cursor()
    body = post_action({
        "action": "type", "text": token, "hwnd": nh, "channel": "virtual",
        "capabilities": caps, "settle_seconds": 0.15,
        "include_after_b64": False, "include_before_b64": False,
    }, timeout=60)
    vi = body.get("virtual") or {}
    verify = vi.get("verify") or {}
    check("C1 记事本 virtual type 成功",
          body.get("_status") == 200 and body.get("ok") is True
          and body.get("channel") == "virtual",
          "status=%s error=%s" % (body.get("_status"), body.get("error")))
    check("C2 打字走 WM_CHAR 且 WM_GETTEXT 验证通过",
          vi.get("mode") == "postmessage"
          and verify.get("verified") is True
          and verify.get("method") == "wm_gettext",
          json.dumps(verify, ensure_ascii=False)[:260])
    check("C3 虚拟打字未使用物理指针",
          body.get("physical_pointer_used") is False
          and body.get("fallback_from_virtual") is not True,
          "physical_pointer_used=%r fallback=%r"
          % (body.get("physical_pointer_used"),
             body.get("fallback_from_virtual")))
    text = virtual_input.read_text(nh) or ""
    check("C4 记事本实际文本包含本次 token", token in text,
          "text_tail=%r" % text[-120:])
    c1 = cursor()
    check("C5 visual type 物理鼠标未移动",
          c0 is not None and c1 is not None
          and max(abs(c1[0] - c0[0]), abs(c1[1] - c0[1])) <= 1,
          "cursor %s -> %s" % (c0, c1))
    return token


def classic_canvas_checks(canvas: NativeCanvas,
                          caps: Dict[str, Any]) -> None:
    hwnd = canvas.hwnd
    ox, oy = virtual_input.client_origin(hwnd)
    pts = [[ox + 80 + i * 8, oy + 80 + i * 3] for i in range(30)]
    before = len(canvas.points)
    c0 = cursor()
    body = post_action({
        "action": "drag", "points": pts, "duration_ms": 300, "hwnd": hwnd,
        "channel": "virtual", "virtual_fallback": False,
        "capabilities": caps, "settle_seconds": 0.15,
        "include_after_b64": False, "include_before_b64": False,
    }, timeout=90)
    vi = body.get("virtual") or {}
    verify = vi.get("verify") or {}
    check("C6 经典 Win32 画布 virtual drag 成功",
          body.get("_status") == 200 and body.get("ok") is True
          and body.get("channel") == "virtual",
          "status=%s error=%s" % (body.get("_status"), body.get("error")))
    check("C7 画布收到 down/move/up 且实际落墨",
          len(canvas.points) > before + 15
          and bool(canvas.clicks)
          and verify.get("verified") is True,
          "new_segments=%d clicks=%d verify=%s"
          % (len(canvas.points) - before, len(canvas.clicks),
             verify.get("verified")))
    check("C8 虚拟拖拽未使用物理指针",
          body.get("physical_pointer_used") is False
          and body.get("fallback_from_virtual") is not True,
          "physical_pointer_used=%r fallback=%r"
          % (body.get("physical_pointer_used"),
             body.get("fallback_from_virtual")))
    c1 = cursor()
    check("C9 虚拟拖拽物理鼠标未移动",
          c0 is not None and c1 is not None
          and max(abs(c1[0] - c0[0]), abs(c1[1] - c0[1])) <= 1,
          "cursor %s -> %s" % (c0, c1))


def paint_no_fallback_checks(ph: int, caps: Dict[str, Any]) -> None:
    cr = virtual_input.client_rect(ph)
    ox, oy = virtual_input.client_origin(ph)
    x = ox + (cr[2] - cr[0]) // 2
    y = oy + (cr[3] - cr[1]) // 2
    pts = [[x - 120 + i * 8, y - 40 + i * 2] for i in range(25)]
    sig0 = virtual_input.stable_signature(ph)
    c0 = cursor()
    body = post_action({
        "action": "drag", "points": pts, "duration_ms": 300, "hwnd": ph,
        "channel": "virtual", "virtual_fallback": False,
        "capabilities": caps, "settle_seconds": 0.15,
        "include_after_b64": False, "include_before_b64": False,
    }, timeout=90)
    time.sleep(0.35)
    sig1 = virtual_input.capture_signature(ph)
    cmp = virtual_input.compare_signatures(sig0, sig1)
    c1 = cursor()
    check("C10 Win11 画图（WinUI）PostMessage 无效必须 409",
          body.get("_status") == 409
          and body.get("error") == "VIRTUAL_INPUT_UNVERIFIED",
          "status=%s error=%s" % (body.get("_status"), body.get("error")))
    check("C11 409 路径零物理输入、零落墨",
          body.get("physical_pointer_used") is False
          and int(cmp.get("changed_pixels") or 0) < 50
          and c0 is not None and c1 is not None
          and max(abs(c1[0] - c0[0]), abs(c1[1] - c0[1])) <= 1,
          "cursor %s -> %s cmp=%s"
          % (c0, c1, json.dumps(cmp, ensure_ascii=False)[:160]))


def paint_fallback_checks(ph: int, caps: Dict[str, Any]) -> None:
    cr = virtual_input.client_rect(ph)
    ox, oy = virtual_input.client_origin(ph)
    x = ox + (cr[2] - cr[0]) // 2
    y = oy + (cr[3] - cr[1]) // 2
    pts = [[x - 80 + i * 8, y + 60 + i * 2] for i in range(25)]
    sig0 = virtual_input.stable_signature(ph)
    c0 = cursor()
    body = post_action({
        "action": "drag", "points": pts, "duration_ms": 300, "hwnd": ph,
        "channel": "virtual", "capabilities": caps, "settle_seconds": 0.2,
        "include_after_b64": False, "include_before_b64": False,
    }, timeout=120)
    time.sleep(0.7)
    sig1 = virtual_input.capture_signature(ph)
    cmp = virtual_input.compare_signatures(sig0, sig1)
    c1 = cursor()
    vi = body.get("virtual") or {}
    check("C12 画图 virtual 无效果时自动降级 physical",
          body.get("_status") == 200 and body.get("ok") is True
          and body.get("channel") == "physical"
          and body.get("fallback_from_virtual") is True,
          "status=%s channel=%s fallback=%r"
          % (body.get("_status"), body.get("channel"),
             body.get("fallback_from_virtual")))
    check("C13 降级确实使用物理指针并落墨",
          body.get("physical_pointer_used") is True
          and cmp.get("verified") is True
          and vi.get("verify", {}).get("verified") is False,
          "pointer=%r cmp=%s virtual_verify=%r"
          % (body.get("physical_pointer_used"), cmp.get("verified"),
             vi.get("verify", {}).get("verified")))
    events = audit_tail(400)
    hit = [e for e in events
           if e.get("event") == "virtual_fallback"
           and int(e.get("hwnd") or 0) == int(ph)]
    check("C14 自动降级写入 virtual_fallback 审计事件",
          bool(hit), "events=%d hit=%s"
          % (len(events), json.dumps(hit[-1], ensure_ascii=False)[:220]
             if hit else "none"))
    if c0 and c1:
        check("C15 降级路径物理鼠标确实移动（审计可见的代价）",
              max(abs(c1[0] - c0[0]), abs(c1[1] - c0[1])) > 1,
              "cursor %s -> %s" % (c0, c1))


# ============================================================================
# D. 并发 + 物理鼠标扰动
# ============================================================================

def concurrency_checks(nh: int, canvas: NativeCanvas,
                       caps: Dict[str, Any]) -> None:
    stop = threading.Event()
    samples: List[Tuple[int, int, int, int]] = []
    errors: List[str] = []
    start_cursor = cursor() or [400, 1450]

    def mover() -> None:
        i = 0
        while not stop.is_set():
            x = 300 + (i * 47) % 1600
            y = 1450 + ((i * 7) % 40)
            try:
                move_cursor_physical(x, y)
            except Exception as exc:  # noqa: BLE001
                errors.append(type(exc).__name__ + ": " + str(exc))
                break
            time.sleep(0.03)
            pos = cursor()
            if pos:
                samples.append((pos[0], pos[1], x, y))
            i += 1

    mover_thread = threading.Thread(target=mover, name="v17-physical-mover",
                                    daemon=True)
    mover_thread.start()
    barrier = threading.Barrier(2)
    results_a: List[Dict[str, Any]] = []
    results_b: List[Dict[str, Any]] = []
    canvas_before = len(canvas.points)
    text_before = virtual_input.read_text(nh) or ""

    def worker_a() -> None:
        barrier.wait(timeout=5)
        for i in range(4):
            results_a.append(post_action({
                "action": "type", "text": "[A%d]" % i, "hwnd": nh,
                "channel": "virtual", "capabilities": caps,
                "settle_seconds": 0.1, "include_after_b64": False,
                "include_before_b64": False,
            }, timeout=90))

    def worker_b() -> None:
        barrier.wait(timeout=5)
        ox, oy = virtual_input.client_origin(canvas.hwnd)
        for i in range(4):
            pts = [[ox + 80 + j * 8, oy + 80 + i * 70 + j * 2]
                   for j in range(30)]
            results_b.append(post_action({
                "action": "drag", "points": pts, "duration_ms": 250,
                "hwnd": canvas.hwnd, "channel": "virtual",
                "virtual_fallback": False, "capabilities": caps,
                "settle_seconds": 0.1, "include_after_b64": False,
                "include_before_b64": False,
            }, timeout=90))

    ta = threading.Thread(target=worker_a, name="v17-virtual-typing")
    tb = threading.Thread(target=worker_b, name="v17-virtual-drawing")
    t0 = time.perf_counter()
    ta.start()
    tb.start()
    ta.join()
    tb.join()
    elapsed = time.perf_counter() - t0
    stop.set()
    mover_thread.join(timeout=3)
    time.sleep(0.2)
    try:
        move_cursor_physical(start_cursor[0], start_cursor[1])
        time.sleep(0.2)
    except Exception:  # noqa: BLE001
        pass

    def ok_virtual(body: Dict[str, Any]) -> bool:
        return (body.get("_status") == 200 and body.get("ok") is True
                and body.get("channel") == "virtual"
                and body.get("physical_pointer_used") is False
                and body.get("fallback_from_virtual") is not True
                and bool((body.get("virtual") or {}).get("verify", {})
                         .get("verified")))

    check("D1 并发 A：4 次记事本 virtual 打字全部成功且未降级",
          len(results_a) == 4 and all(ok_virtual(x) for x in results_a),
          "elapsed=%.2fs statuses=%s"
          % (elapsed, [x.get("_status") for x in results_a]))
    check("D2 并发 B：4 次经典画布 virtual 落墨全部成功且未降级",
          len(results_b) == 4 and all(ok_virtual(x) for x in results_b),
          "statuses=%s modes=%s"
          % ([x.get("_status") for x in results_b],
             [(x.get("virtual") or {}).get("mode") for x in results_b]))
    new_segments = len(canvas.points) - canvas_before
    check("D3 画布实际收到并发笔迹", new_segments >= 60,
          "new_segments=%d" % new_segments)
    text_after = virtual_input.read_text(nh) or ""
    missing = [("[A%d]" % i) for i in range(4) if ("[A%d]" % i) not in text_after]
    check("D4 记事本实际文本包含全部并发 token",
          not missing and text_after != text_before,
          "missing=%s tail=%r" % (missing, text_after[-120:]))
    bad = [s for s in samples
           if max(abs(s[0] - s[2]), abs(s[1] - s[3])) > 4]
    check("D5 物理鼠标轨迹未被虚拟动作干扰",
          len(samples) >= 30 and not bad and not errors,
          "samples=%d bad=%d errors=%s first_bad=%s"
          % (len(samples), len(bad), errors[:2], bad[:2]))
    end_cursor = cursor() or []
    check("D6 测试结束物理鼠标已归位",
          len(end_cursor) == 2
          and max(abs(end_cursor[0] - start_cursor[0]),
                  abs(end_cursor[1] - start_cursor[1])) <= 4,
          "%s -> %s" % (start_cursor, end_cursor))

    h = health()
    locks = h.get("action_locks") or {}
    check("D7 服务端锁统计证明确实并发（峰值 >= 2）",
          int(locks.get("max_concurrent") or 0) >= 2,
          json.dumps(locks, ensure_ascii=False)[:260])
    check("D8 并发结束后无窗口锁泄漏",
          not (locks.get("held_by_hwnd") or {})
          and int(locks.get("held_global") or 0) == 0,
          json.dumps(locks, ensure_ascii=False)[:260])


# ============================================================================
# main
# ============================================================================

def main() -> int:
    print("== moray-ghost v0.17 concurrent virtual input acceptance ==",
          flush=True)
    srv = None
    canvas: Optional[NativeCanvas] = None
    note_hwnd = None
    paint_hwnd = None
    note_proc = None
    paint_proc = None
    try:
        if desktop_locked():
            print("[SKIP] 锁屏/安全桌面：拒绝运行任何物理输入验收。", flush=True)
            return 2
        check("0 前提：端口 %d 空闲" % PORT, port_free())
        existing = app_processes()
        if not check("0 前提：无用户记事本/画图实例（避免误伤）",
                     not existing, "existing=%s" % existing):
            return 1

        unit_locks()
        unit_task_pool()
        unit_virtual_gates()

        srv = spawn_service()
        check("0 前提：Ghost 服务启动", wait_healthy())
        if srv.poll() is not None:
            print("service exited early; last logs:", flush=True)
            print("\n".join(list(SERVICE_LOG)[-30:]), flush=True)
            return 1
        contract_health()

        # ---- 真机窗口 ----
        NOTE_FILE.parent.mkdir(parents=True, exist_ok=True)
        NOTE_FILE.write_text("", encoding="utf-8")
        note_proc = subprocess.Popen(
            ["notepad.exe", str(NOTE_FILE)], cwd=str(ROOT),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        PROCS.append(note_proc)
        paint_proc = subprocess.Popen(
            ["mspaint.exe"], cwd=str(ROOT),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        PROCS.append(paint_proc)
        time.sleep(1.0)
        APP_PIDS.update(pid for pid, _name in app_processes())

        note_hwnd = find_window("Notepad", NOTE_FILE.name, timeout=15)
        paint_hwnd = find_window("MSPaintApp", "", timeout=15)
        check("0 前提：记事本窗口就绪", note_hwnd is not None,
              "hwnd=%r" % note_hwnd)
        check("0 前提：画图窗口就绪", paint_hwnd is not None,
              "hwnd=%r" % paint_hwnd)
        if not note_hwnd or not paint_hwnd:
            return 1

        set_topmost(note_hwnd, 1100, 60, 860, 760)
        set_topmost(paint_hwnd, 40, 60, 1040, 820)
        time.sleep(0.5)
        virtual_input.activate_window(note_hwnd, timeout=2.0)

        canvas = NativeCanvas()
        if not check("0 前提：经典 Win32 画布靶窗创建", canvas.start(),
                     "hwnd=%r" % (canvas.hwnd if canvas else None)):
            return 1
        set_topmost(canvas.hwnd, 160, 900, 720, 480)
        time.sleep(0.3)

        caps = {"allowed_windows":
                ["notepad.exe", "mspaint.exe", "python.exe"]}
        virtual_gate_checks(note_hwnd)
        notepad_checks(note_hwnd, caps)
        classic_canvas_checks(canvas, caps)
        paint_no_fallback_checks(paint_hwnd, caps)
        paint_fallback_checks(paint_hwnd, caps)
        virtual_input.activate_window(note_hwnd, timeout=2.0)
        concurrency_checks(note_hwnd, canvas, caps)
    finally:
        try:
            if canvas is not None:
                canvas.stop()
        except Exception:  # noqa: BLE001
            pass
        # 直接终止本次测试启动的记事本/画图进程（避免 WM_CLOSE 弹出保存对话框）。
        # APP_PIDS 只包含测试开始后新出现的实例；开始前若已有用户实例会 fail-fast。
        for pid in list(APP_PIDS):
            try:
                proc = psutil.Process(pid)
                for child in proc.children(recursive=True):
                    try:
                        child.terminate()
                    except Exception:  # noqa: BLE001
                        pass
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        time.sleep(1.0)
        for proc in (note_proc, paint_proc):
            if proc is not None:
                try:
                    children = psutil.Process(proc.pid).children(recursive=True)
                except Exception:  # noqa: BLE001
                    children = []
                for child in children:
                    try:
                        child.terminate()
                    except Exception:  # noqa: BLE001
                        pass
                stop_proc(proc, None)
        for proc in list(PROCS):
            if proc is srv:
                continue
            if proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
        stop_proc(srv, PORT)
        try:
            NOTE_FILE.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        check("9 清场：服务停止、端口释放", port_free())
        passed = sum(1 for x in RESULTS if x)
        print("\n== RESULT: %d/%d passed ==" % (passed, len(RESULTS)),
              flush=True)
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
