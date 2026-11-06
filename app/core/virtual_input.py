"""v0.17 虚拟输入通道：向指定窗口投递消息，物理鼠标/键盘一根毛都不动。

设计红线
--------
- **绝不使用 SendInput / mouse_event / keybd_event / SetCursorPos**。本模块只
  通过 ``PostMessageW`` 把消息投进目标窗口的消息队列。物理光标位置、按键
  状态、前台焦点都不改变（这是与 physical 通道的本质区别）。
- **不改变窗口 Z 序/激活状态**。常规路径绝不调用 SetForegroundWindow；
  只有 controller 在「虚拟通道验证失败 → 自动降级 physical」时才显式调用
  ``activate_window``（该动作会落审计）。
- **Win32 传统控件优先**。记事本 Edit/RichEdit、经典画图画布等对
  WM_LBUTTONDOWN/UP、WM_MOUSEMOVE、WM_CHAR 有原生处理；Electron/WinUI
  可能完全忽略投递消息——这正是 controller 必须做 128px 像素校验 +
  自动降级的原因，本模块不假装成功。

坐标契约
--------
``virtual_click/drag/scroll`` 接收的是**屏幕物理像素**（与 controller 的
resolved_physical 完全一致）。内部用 WindowFromPoint / ScreenToClient 解析
到最深层子控件后再投递——画图的画布、记事本的编辑区都是子窗口，投给顶层
框架是收不到的。

线程安全
--------
无模块级可变状态；每次调用独立解析窗口/子控件。PostMessage 本身线程安全。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import logging
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image

LOG = logging.getLogger("moray-ghost.virtual_input")

# ---- Win32 常量 -----------------------------------------------------------
GA_ROOT = 2
GW_OWNER = 4
SW_RESTORE = 9

WM_SETTEXT = 0x000C
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_RBUTTONDBLCLK = 0x0206
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A
WM_CHAR = 0x0102
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101

MK_LBUTTON = 0x0001
MK_RBUTTON = 0x0002
MK_MBUTTON = 0x0010

CWP_SKIPINVISIBLE = 0x0001
CWP_SKIPDISABLED = 0x0004

SMTO_ABORTIFHUNG = 0x0002
PW_RENDERFULLCONTENT = 0x0002

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

_CHAR_CLASS_HINTS = ("edit", "richedit", "scintilla", "textbox", "textfield")
_MAX_TEXT_READ = 1_000_000

# ---- 合成指针（v0.17 第二层虚拟通道：WinUI 内容岛也吃） --------------------
# Win11 记事本/画图的部分控件是 WinUI 内容岛，完全忽略 PostMessage。Windows
# 官方 CreateSyntheticPointerDevice + InjectSyntheticPointerInput 可以注入
# 一支独立的虚拟笔：系统按坐标把 PEN 消息送给目标窗口，**不移动物理鼠标**。
# 代价：指针按下会把目标窗口带到前台（注入结束后恢复原前台窗口）。
PT_TOUCH = 2
PT_PEN = 3
POINTER_FEEDBACK_INDIRECT = 1

POINTER_FLAG_NEW = 0x00000001
POINTER_FLAG_INRANGE = 0x00000002
POINTER_FLAG_INCONTACT = 0x00000004
POINTER_FLAG_PRIMARY = 0x00002000
POINTER_FLAG_DOWN = 0x00010000
POINTER_FLAG_UPDATE = 0x00020000
POINTER_FLAG_UP = 0x00040000
PEN_MASK_PRESSURE = 0x00000001
POINTER_CHANGE_FIRSTBUTTON_DOWN = 1
POINTER_CHANGE_FIRSTBUTTON_UP = 2


class POINTER_INFO(ctypes.Structure):
    _fields_ = (
        ("pointerType", ctypes.c_uint32),
        ("pointerId", ctypes.c_uint32),
        ("frameId", ctypes.c_uint32),
        ("pointerFlags", ctypes.c_uint32),
        ("sourceDevice", wintypes.HANDLE),
        ("hwndTarget", wintypes.HWND),
        ("ptPixelLocation", wintypes.POINT),
        ("ptHimetricLocation", wintypes.POINT),
        ("ptPixelLocationRaw", wintypes.POINT),
        ("ptHimetricLocationRaw", wintypes.POINT),
        ("dwTime", wintypes.DWORD),
        ("historyCount", ctypes.c_uint32),
        ("InputState", ctypes.c_int32),
        ("KeyStates", ctypes.c_uint32),
        ("PerformanceCount", ctypes.c_uint64),
        ("ButtonChangeType", ctypes.c_uint32),
    )


class TOUCH_INFO(ctypes.Structure):
    _fields_ = (
        ("pointerInfo", POINTER_INFO),
        ("touchFlags", ctypes.c_uint32),
        ("touchMask", ctypes.c_uint32),
        ("rcContact", wintypes.RECT),
        ("rcContactRaw", wintypes.RECT),
        ("orientation", ctypes.c_uint32),
        ("pressure", ctypes.c_uint32),
    )


class PEN_INFO(ctypes.Structure):
    _fields_ = (
        ("pointerInfo", POINTER_INFO),
        ("penMask", ctypes.c_uint32),
        ("pressure", ctypes.c_uint32),
        ("rotation", ctypes.c_uint32),
        ("tiltX", ctypes.c_int32),
        ("tiltY", ctypes.c_int32),
    )


class MOUSE_INFO(ctypes.Structure):
    _fields_ = (
        ("pointerInfo", POINTER_INFO),
        ("mouseState", ctypes.c_uint32),
    )


class _PointerTypeUnion(ctypes.Union):
    _fields_ = (
        ("pointerInfo", POINTER_INFO),
        ("touchInfo", TOUCH_INFO),
        ("penInfo", PEN_INFO),
        ("mouseInfo", MOUSE_INFO),
    )


class POINTER_TYPE_INFO(ctypes.Structure):
    _fields_ = (
        ("type", ctypes.c_uint32),
        ("u", _PointerTypeUnion),
    )



class VirtualInputError(Exception):
    """虚拟通道不可用/目标不可达/坐标非法（调用方转 4xx 语义）。"""

    def __init__(self, detail: str, code: str = "VIRTUAL_INPUT_ERROR",
                 **data: Any) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code
        self.data = data


# ---- 私有 user32/kernel32（绝不动 ctypes.windll 上 pyautogui 共用的签名） ----
if sys.platform == "win32":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _user32.PostMessageW.argtypes = (
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    _user32.PostMessageW.restype = wintypes.BOOL
    _user32.SendMessageTimeoutW.argtypes = (
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t))
    _user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
    _user32.IsWindow.argtypes = (wintypes.HWND,)
    _user32.IsWindow.restype = wintypes.BOOL
    _user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    _user32.IsWindowVisible.restype = wintypes.BOOL
    _user32.IsIconic.argtypes = (wintypes.HWND,)
    _user32.IsIconic.restype = wintypes.BOOL
    _user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
    _user32.GetAncestor.restype = wintypes.HWND
    _user32.WindowFromPoint.argtypes = (wintypes.POINT,)
    _user32.WindowFromPoint.restype = wintypes.HWND
    _user32.ChildWindowFromPointEx.argtypes = (
        wintypes.HWND, wintypes.POINT, wintypes.UINT)
    _user32.ChildWindowFromPointEx.restype = wintypes.HWND
    _user32.ScreenToClient.argtypes = (
        wintypes.HWND, ctypes.POINTER(wintypes.POINT))
    _user32.ScreenToClient.restype = wintypes.BOOL
    _user32.ClientToScreen.argtypes = (
        wintypes.HWND, ctypes.POINTER(wintypes.POINT))
    _user32.ClientToScreen.restype = wintypes.BOOL
    _user32.GetClientRect.argtypes = (
        wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    _user32.GetClientRect.restype = wintypes.BOOL
    _user32.GetWindowRect.argtypes = (
        wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    _user32.GetWindowRect.restype = wintypes.BOOL
    _user32.GetWindowThreadProcessId.argtypes = (
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.GetClassNameW.argtypes = (
        wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    _user32.GetClassNameW.restype = ctypes.c_int
    _user32.IsChild.argtypes = (wintypes.HWND, wintypes.HWND)
    _user32.IsChild.restype = wintypes.BOOL
    _user32.GetForegroundWindow.argtypes = ()
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    _user32.SetForegroundWindow.restype = wintypes.BOOL
    _user32.BringWindowToTop.argtypes = (wintypes.HWND,)
    _user32.BringWindowToTop.restype = wintypes.BOOL
    _user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    _user32.ShowWindow.restype = wintypes.BOOL
    _user32.AttachThreadInput.argtypes = (
        wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
    _user32.AttachThreadInput.restype = wintypes.BOOL
    _user32.PrintWindow.argtypes = (
        wintypes.HWND, wintypes.HDC, wintypes.UINT)
    _user32.PrintWindow.restype = wintypes.BOOL
    _kernel32.GetCurrentThreadId.argtypes = ()
    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    _kernel32.GetTickCount.argtypes = ()
    _kernel32.GetTickCount.restype = wintypes.DWORD
    _user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
    _user32.GetCursorPos.restype = wintypes.BOOL

    # 合成指针 API（Windows 10 1809+；缺失时 synthetic_pointer_available()
    # 返回 False，虚拟通道只保留 PostMessage 层，绝不假装可用）。
    _create_pointer_device = getattr(_user32, "CreateSyntheticPointerDevice", None)
    _inject_pointer_input = getattr(_user32, "InjectSyntheticPointerInput", None)
    _destroy_pointer_device = getattr(
        _user32, "DestroySyntheticPointerDevice", None)
    if _create_pointer_device is not None:
        _create_pointer_device.argtypes = (
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32)
        _create_pointer_device.restype = wintypes.HANDLE
    if _inject_pointer_input is not None:
        _inject_pointer_input.argtypes = (
            wintypes.HANDLE, ctypes.POINTER(POINTER_TYPE_INFO),
            ctypes.c_uint32)
        _inject_pointer_input.restype = wintypes.BOOL
    if _destroy_pointer_device is not None:
        _destroy_pointer_device.argtypes = (wintypes.HANDLE,)
        _destroy_pointer_device.restype = None

    WNDENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _user32.EnumChildWindows.argtypes = (
        wintypes.HWND, WNDENUMPROC, wintypes.LPARAM)
    _user32.EnumChildWindows.restype = wintypes.BOOL

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = (
            ("cbSize", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("hwndActive", wintypes.HWND),
            ("hwndFocus", wintypes.HWND),
            ("hwndCapture", wintypes.HWND),
            ("hwndMenuOwner", wintypes.HWND),
            ("hwndMoveSize", wintypes.HWND),
            ("hwndCaret", wintypes.HWND),
            ("rcCaret", wintypes.RECT),
        )

    _user32.GetGUIThreadInfo.argtypes = (
        wintypes.DWORD, ctypes.POINTER(GUITHREADINFO))
    _user32.GetGUIThreadInfo.restype = wintypes.BOOL
else:  # pragma: no cover - 非 Windows 平台不导入本模块的实际能力
    _user32 = None
    _kernel32 = None
    GUITHREADINFO = None  # type: ignore[assignment,misc]
    WNDENUMPROC = None  # type: ignore[assignment,misc]
    _create_pointer_device = None
    _inject_pointer_input = None
    _destroy_pointer_device = None


# ---- 基础工具 -------------------------------------------------------------

def available() -> bool:
    """虚拟通道是否可在本平台使用（Windows + user32 句柄已就绪）。"""
    return sys.platform == "win32" and _user32 is not None


def _require() -> None:
    if not available():
        raise VirtualInputError(
            "virtual input channel is only available on Windows",
            code="VIRTUAL_UNAVAILABLE")


def _hwnd(value: Any) -> int:
    try:
        h = int(value)
    except (TypeError, ValueError) as exc:
        raise VirtualInputError("invalid hwnd: " + repr(value),
                                code="VIRTUAL_BAD_HWND") from exc
    if h <= 0 or not _user32.IsWindow(wintypes.HWND(h)):
        raise VirtualInputError("window hwnd " + str(h) + " does not exist",
                                code="VIRTUAL_WINDOW_GONE", hwnd=h)
    return h


def root_window(hwnd: Any) -> int:
    h = _hwnd(hwnd)
    root = _user32.GetAncestor(wintypes.HWND(h), GA_ROOT)
    return int(root) if root else h


def window_class(hwnd: Any) -> str:
    try:
        buf = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(wintypes.HWND(int(hwnd)), buf, 256)
        return buf.value
    except Exception:  # noqa: BLE001
        return ""


def window_rect(hwnd: Any) -> Tuple[int, int, int, int]:
    r = wintypes.RECT()
    if not _user32.GetWindowRect(wintypes.HWND(_hwnd(hwnd)), ctypes.byref(r)):
        raise VirtualInputError("GetWindowRect failed", code="VIRTUAL_WINDOW_GONE")
    return int(r.left), int(r.top), int(r.right), int(r.bottom)


def client_rect(hwnd: Any) -> Tuple[int, int, int, int]:
    r = wintypes.RECT()
    if not _user32.GetClientRect(wintypes.HWND(_hwnd(hwnd)), ctypes.byref(r)):
        raise VirtualInputError("GetClientRect failed", code="VIRTUAL_WINDOW_GONE")
    return int(r.left), int(r.top), int(r.right), int(r.bottom)


def client_origin(hwnd: Any) -> Tuple[int, int]:
    pt = wintypes.POINT(0, 0)
    if not _user32.ClientToScreen(wintypes.HWND(_hwnd(hwnd)), ctypes.byref(pt)):
        raise VirtualInputError("ClientToScreen failed", code="VIRTUAL_WINDOW_GONE")
    return int(pt.x), int(pt.y)


def screen_to_client(hwnd: Any, x: int, y: int) -> Tuple[int, int]:
    pt = wintypes.POINT(int(x), int(y))
    if not _user32.ScreenToClient(wintypes.HWND(_hwnd(hwnd)), ctypes.byref(pt)):
        raise VirtualInputError("ScreenToClient failed", code="VIRTUAL_WINDOW_GONE")
    return int(pt.x), int(pt.y)


def _makelparam(x: int, y: int) -> int:
    return ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)


def _post(hwnd: int, msg: int, wparam: int = 0, lparam: int = 0) -> bool:
    ok = _user32.PostMessageW(wintypes.HWND(int(hwnd)), int(msg),
                              int(wparam), int(lparam))
    if not ok:
        err = ctypes.get_last_error()
        raise VirtualInputError(
            "PostMessage failed (winerr=" + str(err) + ")",
            code="VIRTUAL_POST_FAILED", hwnd=int(hwnd), message=int(msg))
    return True


def _deepest_child(root: int, sx: int, sy: int) -> int:
    """屏幕点 → 目标根窗口下的最深层子控件（画布/编辑区）。

    先按真实 Z 序 WindowFromPoint：命中且根窗口一致就用它。窗口被遮挡时
    WindowFromPoint 会返回遮挡者——这不是错误（后台场景下目标就是被遮挡的），
    此时退回 ChildWindowFromPointEx 逐层向下，忽略 Z 序。
    """
    try:
        leaf = _user32.WindowFromPoint(wintypes.POINT(int(sx), int(sy)))
        if leaf:
            leaf_root = _user32.GetAncestor(leaf, GA_ROOT)
            if int(leaf_root or 0) == int(root):
                return int(leaf)
    except Exception:  # noqa: BLE001
        pass
    hwnd = int(root)
    for _ in range(12):
        pt = wintypes.POINT(int(sx), int(sy))
        if not _user32.ScreenToClient(wintypes.HWND(hwnd), ctypes.byref(pt)):
            break
        child = _user32.ChildWindowFromPointEx(
            wintypes.HWND(hwnd), pt,
            CWP_SKIPINVISIBLE | CWP_SKIPDISABLED)
        if not child or int(child) == hwnd:
            break
        hwnd = int(child)
    return hwnd


def _enum_children(root: int) -> List[int]:
    out: List[int] = []

    def _cb(hwnd, _lparam):  # noqa: ANN001
        out.append(int(hwnd))
        return True

    try:
        cb = WNDENUMPROC(_cb)
        _user32.EnumChildWindows(wintypes.HWND(int(root)), cb, 0)
    except Exception:  # noqa: BLE001
        pass
    return out


def _looks_like_text_control(hwnd: int) -> bool:
    cls = window_class(hwnd).lower()
    return any(hint in cls for hint in _CHAR_CLASS_HINTS)


def _text_priority(hwnd: int) -> int:
    """控件类名优先级：RichEdit/Scintilla（真编辑器）> Edit > TextBox > 其它。

    新版记事本同时有 NotepadTextBox 与 RichEditD2DPT 两个同尺寸子窗；
    WM_CHAR 只有投给真正的 RichEdit 才会落字，所以不能只按面积挑。
    """
    cls = window_class(hwnd).lower()
    if "richedit" in cls or "scintilla" in cls:
        return 4
    if cls == "edit" or cls.endswith("edit"):
        return 3
    if "textbox" in cls:
        return 2
    if "text" in cls:
        return 1
    return 0


def _focus_child(root: int) -> Optional[int]:
    """目标线程当前焦点控件（后台窗口也可以保留焦点）。"""
    try:
        tid = _user32.GetWindowThreadProcessId(wintypes.HWND(int(root)), None)
        if not tid:
            return None
        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(GUITHREADINFO)
        if not _user32.GetGUIThreadInfo(tid, ctypes.byref(info)):
            return None
        focus = int(info.hwndFocus or 0)
        if focus and (focus == int(root)
                      or _user32.IsChild(wintypes.HWND(int(root)),
                                         wintypes.HWND(focus))):
            return focus
    except Exception:  # noqa: BLE001
        pass
    return None


def _control_area(hwnd: int) -> int:
    try:
        cr = client_rect(hwnd)
        return max(0, cr[2] - cr[0]) * max(0, cr[3] - cr[1])
    except Exception:  # noqa: BLE001
        return 0


def text_target(hwnd: Any) -> int:
    """打字目标控件：文本类焦点 > 面积最大的 Edit 族子控件 > 焦点 > 根窗口。

    面积优先很关键：新版记事本/浏览器有搜索框、地址栏等多个 Edit，第一个
    枚举到的往往不是主编辑区；主编辑区在窗口里面积最大。
    """
    root = root_window(hwnd)
    focus = _focus_child(root)
    if focus and _looks_like_text_control(focus):
        return focus
    candidates = [child for child in _enum_children(root)
                  if _looks_like_text_control(child)]
    if candidates:
        return max(candidates, key=lambda c: (_text_priority(c),
                                              _control_area(c)))
    return focus or root


def read_text(hwnd: Any, ctrl: Optional[int] = None,
              timeout_ms: int = 800) -> Optional[str]:
    """尽力读取目标控件文本（WM_GETTEXT）。不支持/超时 → None（不猜）。"""
    _require()
    try:
        target = int(ctrl) if ctrl else text_target(hwnd)
    except VirtualInputError:
        return None
    try:
        result = ctypes.c_size_t(0)
        n = int(_user32.SendMessageTimeoutW(
            wintypes.HWND(target), WM_GETTEXTLENGTH, 0, 0,
            SMTO_ABORTIFHUNG, int(timeout_ms), ctypes.byref(result)))
        if n < 0 or result.value > _MAX_TEXT_READ:
            return None
        length = int(result.value)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.SendMessageTimeoutW(
            wintypes.HWND(target), WM_GETTEXT, length + 1,
            ctypes.cast(buf, ctypes.c_void_p).value,
            SMTO_ABORTIFHUNG, int(timeout_ms), ctypes.byref(result))
        return buf.value
    except Exception:  # noqa: BLE001
        return None


# ---- 窗口截图（128px 验证） -------------------------------------------------

@dataclass
class WindowSignature:
    hwnd: int
    gray: bytes
    size: Tuple[int, int]
    client_size: Tuple[int, int]
    method: str
    captured_at: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        return {"hwnd": self.hwnd, "size": list(self.size),
                "client_size": list(self.client_size), "method": self.method,
                "captured_at": round(self.captured_at, 3)}


def _capture_window_image(hwnd: int) -> Tuple[Image.Image, str]:
    """PrintWindow(PW_RENDERFULLCONTENT) 抓窗口；失败退回窗口 DC BitBlt。

    返回整窗图像（含非客户区）与所用方法名。GDI 句柄在 finally 全部释放。
    """
    import win32con
    import win32gui
    import win32ui

    left, top, right, bottom = window_rect(hwnd)
    w, h = right - left, bottom - top
    if w <= 0 or h <= 0:
        raise VirtualInputError("window has zero size", code="VIRTUAL_WINDOW_GONE")

    hwnd_dc = None
    mfc_dc = None
    save_dc = None
    bmp = None
    method = "printwindow"
    try:
        hwnd_dc = win32gui.GetWindowDC(int(hwnd))
        if not hwnd_dc:
            raise VirtualInputError("GetWindowDC failed",
                                    code="VIRTUAL_CAPTURE_FAILED")
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(mfc_dc, w, h)
        save_dc.SelectObject(bmp)
        ok = 0
        try:
            ok = int(_user32.PrintWindow(
                wintypes.HWND(hwnd), save_dc.GetSafeHdc(),
                PW_RENDERFULLCONTENT))
        except Exception:  # noqa: BLE001
            ok = 0
        if not ok:
            method = "bitblt"
            save_dc.BitBlt((0, 0), (w, h), mfc_dc, (0, 0), win32con.SRCCOPY)
        bits = bmp.GetBitmapBits(True)
        img = Image.frombuffer("RGB", (w, h), bits, "raw", "BGRX", 0, 1)
        return img.copy(), method
    finally:
        for obj, closer in ((save_dc, "DeleteDC"), (mfc_dc, "DeleteDC")):
            try:
                if obj is not None:
                    getattr(obj, closer)()
            except Exception:  # noqa: BLE001
                pass
        try:
            if hwnd_dc:
                win32gui.ReleaseDC(int(hwnd), hwnd_dc)
        except Exception:  # noqa: BLE001
            pass
        try:
            if bmp is not None:
                win32gui.DeleteObject(bmp.GetHandle())
        except Exception:  # noqa: BLE001
            pass


def capture_signature(hwnd: Any, max_side: int = 128) -> Optional[WindowSignature]:
    """抓取窗口客户区并缩到最长边 max_side 的灰度签名（验证用，默认 128px）。

    任何失败返回 None——调用方按「无法验证」处理，绝不假装成功。
    """
    _require()
    try:
        target = root_window(hwnd)
        img, method = _capture_window_image(target)
        # 只保留客户区：PrintWindow/BitBlt 都包含标题栏/边框，标题文字变化
        # 会污染像素比对（比如标题里的文件名/星号）。
        wr = window_rect(target)
        cr = client_rect(target)
        ox, oy = client_origin(target)
        cw, ch = max(1, cr[2] - cr[0]), max(1, cr[3] - cr[1])
        box = (max(0, ox - wr[0]), max(0, oy - wr[1]),
               min(img.width, ox - wr[0] + cw), min(img.height, oy - wr[1] + ch))
        if box[2] > box[0] and box[3] > box[1]:
            img = img.crop(box)
        scale = min(1.0, float(max_side) / max(img.width, img.height))
        if scale < 1.0:
            img = img.resize((max(1, round(img.width * scale)),
                              max(1, round(img.height * scale))), Image.BILINEAR)
        gray = img.convert("L").tobytes()
        return WindowSignature(target, gray, (img.width, img.height), (cw, ch),
                               method)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("capture_signature hwnd=%s failed: %s", hwnd, exc)
        return None


def compare_signatures(before: Optional[WindowSignature],
                       after: Optional[WindowSignature],
                       pixel_tol: int = 12,
                       min_ratio: float = 0.0008,
                       min_changed_px: int = 12,
                       min_mean_delta: float = 0.35) -> Dict[str, Any]:
    """两帧灰度签名差异 → 是否可观测变化。

    阈值刻意很低：128px 下记事本敲一个字符可能只改变个位数像素。``pixel_tol``
    滤掉 ClearType/抗锯齿噪声；任一条件满足即视为有变化。
    """
    if before is None or after is None:
        return {"verified": False, "reason": "capture_unavailable",
                "changed_pixels": 0, "changed_ratio": 0.0, "mean_delta": 0.0}
    if before.size != after.size:
        return {"verified": False, "reason": "size_changed",
                "before_size": list(before.size),
                "after_size": list(after.size),
                "changed_pixels": 0, "changed_ratio": 0.0, "mean_delta": 0.0}
    a = before.gray
    b = after.gray
    if not a or not b:
        return {"verified": False, "reason": "empty_signature",
                "changed_pixels": 0, "changed_ratio": 0.0, "mean_delta": 0.0}
    try:
        import numpy as np
        av = np.frombuffer(a, dtype=np.uint8).astype(np.int16)
        bv = np.frombuffer(b, dtype=np.uint8).astype(np.int16)
        diff = np.abs(av - bv)
        changed_px = int((diff > int(pixel_tol)).sum())
        total = int(diff.size) or 1
        mean_delta = float(diff.mean())
    except Exception:  # noqa: BLE001  numpy 缺失时纯 Python 兜底
        changed_px = 0
        total = len(a)
        total_delta = 0
        for x, y in zip(a, b):
            d = abs(int(x) - int(y))
            total_delta += d
            if d > pixel_tol:
                changed_px += 1
        mean_delta = total_delta / max(1, total)
    ratio = changed_px / max(1, total)
    verified = (changed_px >= int(min_changed_px)
                or ratio >= float(min_ratio)
                or mean_delta >= float(min_mean_delta))
    return {"verified": bool(verified),
            "reason": "observed_change" if verified else "no_observable_change",
            "changed_pixels": changed_px,
            "changed_ratio": round(ratio, 5),
            "mean_delta": round(mean_delta, 4),
            "pixel_tol": int(pixel_tol)}


def stable_signature(hwnd: Any, max_side: int = 128,
                     timeout: float = 0.4,
                     interval: float = 0.07) -> Optional[WindowSignature]:
    """等到窗口画面连续两帧不再变化，再返回基线签名。

    v0.17 实弹：WinUI 画图的墨迹可能异步渲染，上一个动作的笔迹会在下一个
    动作的 before 之后才出现——直接取单帧会把「旧笔迹延迟出现」误判成
    「本动作有效果」。先等画面稳定，再执行、再比对。
    """
    prev: Optional[WindowSignature] = None
    cur: Optional[WindowSignature] = None
    end = time.monotonic() + max(0.0, float(timeout))
    while True:
        cur = capture_signature(hwnd, max_side)
        if prev is not None and cur is not None and prev.size == cur.size:
            cmp = compare_signatures(
                prev, cur, pixel_tol=6, min_ratio=0.0,
                min_changed_px=4, min_mean_delta=0.15)
            if not cmp.get("verified"):
                return cur
        if cur is not None:
            prev = cur
        if time.monotonic() >= end:
            return cur or prev
        time.sleep(max(0.0, float(interval)))


# ---- 输入投递 -------------------------------------------------------------

def _validate_client_point(hwnd: int, x: int, y: int
                           ) -> Tuple[int, int, Tuple[int, int, int, int]]:
    cx, cy = screen_to_client(hwnd, x, y)
    cr = client_rect(hwnd)
    cw, ch = cr[2] - cr[0], cr[3] - cr[1]
    if cx < 0 or cy < 0 or cx >= cw or cy >= ch:
        raise VirtualInputError(
            "screen point (" + str(x) + "," + str(y) + ") is outside target "
            "window client area " + str((cw, ch)),
            code="VIRTUAL_OUT_OF_BOUNDS", hwnd=int(hwnd), x=int(x), y=int(y),
            client=[cx, cy], client_size=[cw, ch])
    return cx, cy, cr


def virtual_click(hwnd: Any, x: int, y: int, button: str = "left",
                  double: bool = False, press_ms: int = 12) -> Dict[str, Any]:
    """向窗口投递一次点击（完全不移动物理鼠标）。

    消息序列：WM_MOUSEMOVE → WM_LBUTTONDOWN → (WM_LBUTTONDBLCLK) → WM_LBUTTONUP。
    目标是最深层子控件（画布/按钮），坐标换算为该控件的客户坐标。
    """
    _require()
    root = root_window(hwnd)
    target = _deepest_child(root, int(x), int(y))
    cx, cy, _cr = _validate_client_point(target, int(x), int(y))
    btn = str(button or "left").lower()
    if btn == "right":
        down, up, dbl, wparam = (WM_RBUTTONDOWN, WM_RBUTTONUP,
                                 WM_RBUTTONDBLCLK, MK_RBUTTON)
    elif btn == "middle":
        down, up, dbl, wparam = (WM_MBUTTONDOWN, WM_MBUTTONUP,
                                 WM_MBUTTONDBLCLK, MK_MBUTTON)
    else:
        down, up, dbl, wparam = (WM_LBUTTONDOWN, WM_LBUTTONUP,
                                 WM_LBUTTONDBLCLK, MK_LBUTTON)
    lp = _makelparam(cx, cy)
    posted = 0
    _post(target, WM_MOUSEMOVE, 0, lp)
    posted += 1
    _post(target, down, wparam, lp)
    posted += 1
    if double:
        _post(target, dbl, wparam, lp)
        posted += 1
    if press_ms > 0:
        time.sleep(min(max(int(press_ms), 0), 200) / 1000.0)
    _post(target, up, 0, lp)
    posted += 1
    return {"channel": "virtual", "op": "click", "root_hwnd": root,
            "target_hwnd": int(target), "target_class": window_class(target),
            "client_point": [cx, cy], "messages_posted": posted,
            "button": btn, "double": bool(double)}


def virtual_drag(hwnd: Any, points: Sequence[Sequence[int]],
                 duration_ms: int = 300, press_ms: int = 10,
                 max_points: int = 400) -> Dict[str, Any]:
    """向窗口投递一次拖拽（真实鼠标纹丝不动）。

    全程投递给**同一个**子控件：真实系统里 WM_LBUTTONDOWN 后鼠标被该控件
    捕获，后续移动自然归它；这里用显式目标复现同一语义（画图画布）。
    """
    _require()
    pts: List[Tuple[int, int]] = []
    for p in points:
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            raise VirtualInputError("drag points must be [x,y] pairs",
                                    code="VIRTUAL_BAD_POINTS")
        pts.append((int(p[0]), int(p[1])))
    if len(pts) < 2:
        raise VirtualInputError("virtual drag requires >=2 points",
                                code="VIRTUAL_BAD_POINTS")
    if len(pts) > max_points:
        step = (len(pts) - 1) / (max_points - 1)
        pts = [pts[round(i * step)] for i in range(max_points)]
    root = root_window(hwnd)
    target = _deepest_child(root, pts[0][0], pts[0][1])
    local: List[Tuple[int, int, int, int]] = []
    for px, py in pts:
        cx, cy, _cr = _validate_client_point(target, px, py)
        local.append((cx, cy, px, py))
    total_s = min(max(int(duration_ms), 0), 10000) / 1000.0
    delay = total_s / max(1, len(local) - 1)
    lp0 = _makelparam(local[0][0], local[0][1])
    posted = 0
    _post(target, WM_MOUSEMOVE, 0, lp0)
    _post(target, WM_LBUTTONDOWN, MK_LBUTTON, lp0)
    posted += 2
    if press_ms > 0:
        time.sleep(min(max(int(press_ms), 0), 200) / 1000.0)
    t0 = time.perf_counter()
    for i, (cx, cy, _sx, _sy) in enumerate(local[1:], start=1):
        _post(target, WM_MOUSEMOVE, MK_LBUTTON, _makelparam(cx, cy))
        posted += 1
        if delay > 0:
            ahead = t0 + delay * i - time.perf_counter()
            if ahead > 0:
                time.sleep(min(ahead, 0.05))
    _post(target, WM_LBUTTONUP, 0, _makelparam(local[-1][0], local[-1][1]))
    posted += 1
    return {"channel": "virtual", "op": "drag", "root_hwnd": root,
            "target_hwnd": int(target), "target_class": window_class(target),
            "messages_posted": posted, "points": len(local),
            "duration_ms": int(total_s * 1000)}


def virtual_type(hwnd: Any, text: str, interval_ms: float = 0.0,
                 ctrl: Optional[int] = None) -> Dict[str, Any]:
    """向窗口焦点编辑控件投递 WM_CHAR（支持中文/emoji 代理对）。

    不经过 IME、不碰剪贴板、不按任何物理键——这正是后台并发的关键。
    """
    _require()
    text = str(text)
    if not text:
        raise VirtualInputError("virtual type requires non-empty text",
                                code="VIRTUAL_BAD_TEXT")
    root = root_window(hwnd)
    target = int(ctrl) if ctrl else text_target(root)
    posted = 0
    for ch in text:
        if ch == "\r":
            continue
        code = 0x0D if ch == "\n" else ord(ch)
        if code <= 0xFFFF:
            units_list = [code]
        else:
            # 辅助平面（emoji 等）：按 UTF-16 代理对逐单元投递
            raw = ch.encode("utf-16-le")
            units_list = [int.from_bytes(raw[i:i + 2], "little")
                          for i in range(0, len(raw), 2)]
        for unit in units_list:
            _post(target, WM_CHAR, unit, 0x00000001)
            posted += 1
        if interval_ms and interval_ms > 0:
            time.sleep(min(float(interval_ms), 50.0) / 1000.0)
        elif posted % 256 == 0:
            # 目标消息队列上限约 1 万条；超长文本分批让出，避免 PostMessage
            # 在队列满时失败（正常 AI 打字远小于此，纯防御）。
            time.sleep(0.001)
    return {"channel": "virtual", "op": "type", "root_hwnd": root,
            "target_hwnd": int(target), "target_class": window_class(target),
            "messages_posted": posted, "text_length": len(text)}


def virtual_scroll(hwnd: Any, x: int, y: int, dy: int) -> Dict[str, Any]:
    """投递 WM_MOUSEWHEEL（lParam 用屏幕坐标，wParam 高 16 位为 delta）。"""
    _require()
    root = root_window(hwnd)
    target = _deepest_child(root, int(x), int(y))
    # WM_MOUSEWHEEL 的 lParam 是屏幕坐标（与其它鼠标消息不同）
    delta = max(-120 * 20, min(120 * 20, int(dy)))
    wp = ((int(delta) & 0xFFFF) << 16)
    lp = _makelparam(int(x), int(y))
    _post(target, WM_MOUSEWHEEL, wp, lp)
    return {"channel": "virtual", "op": "scroll", "root_hwnd": root,
            "target_hwnd": int(target), "target_class": window_class(target),
            "messages_posted": 1, "delta": int(delta)}


# ---- 合成指针层（v0.17）：WinUI/内容岛的第二条后台通道 ---------------------
_synth_lock = threading.RLock()
_synth_available: Optional[bool] = None


def synthetic_pointer_available() -> bool:
    """本机是否支持合成笔注入（Win10 1809+，且 CreateSyntheticPointerDevice
    接受 PEN + INDIRECT 反馈）。结果缓存；失败绝不假装可用。"""
    global _synth_available
    if _synth_available is not None:
        return _synth_available
    if not available() or _create_pointer_device is None \
            or _inject_pointer_input is None or _destroy_pointer_device is None:
        _synth_available = False
        return False
    dev = None
    try:
        dev = _create_pointer_device(PT_PEN, 1, POINTER_FEEDBACK_INDIRECT)
        _synth_available = bool(dev)
    except Exception:  # noqa: BLE001
        _synth_available = False
    finally:
        if dev:
            try:
                _destroy_pointer_device(dev)
            except Exception:  # noqa: BLE001
                pass
    return bool(_synth_available)


def _cursor_pos() -> Optional[List[int]]:
    try:
        pt = wintypes.POINT()
        if _user32.GetCursorPos(ctypes.byref(pt)):
            return [int(pt.x), int(pt.y)]
    except Exception:  # noqa: BLE001
        pass
    return None


def points_target_window(hwnd: Any,
                         points: Sequence[Sequence[int]]
                         ) -> Tuple[bool, Optional[List[int]], Any]:
    """合成指针是坐标投递：每个点都必须命中目标窗口（否则会打到遮挡者）。"""
    _require()
    root = root_window(hwnd)
    if not _user32.IsWindowVisible(wintypes.HWND(root)):
        return False, None, "not_visible"
    if _user32.IsIconic(wintypes.HWND(root)):
        return False, None, "minimized"
    for p in points:
        pt = wintypes.POINT(int(p[0]), int(p[1]))
        leaf = _user32.WindowFromPoint(pt)
        got = int(_user32.GetAncestor(leaf, GA_ROOT) or 0) if leaf else 0
        if got != root:
            return False, [int(p[0]), int(p[1])], got
    return True, None, root


def _pen_info(px: int, py: int, phase: str) -> POINTER_TYPE_INFO:
    info = POINTER_TYPE_INFO()
    info.type = PT_PEN
    pi = info.u.penInfo.pointerInfo
    pi.pointerType = PT_PEN
    pi.pointerId = 1
    if phase == "down":
        pi.pointerFlags = (POINTER_FLAG_NEW | POINTER_FLAG_INRANGE
                           | POINTER_FLAG_INCONTACT | POINTER_FLAG_DOWN)
        pi.ButtonChangeType = POINTER_CHANGE_FIRSTBUTTON_DOWN
    elif phase == "move":
        pi.pointerFlags = (POINTER_FLAG_INRANGE | POINTER_FLAG_INCONTACT
                           | POINTER_FLAG_UPDATE)
        pi.ButtonChangeType = 0
    else:
        pi.pointerFlags = (POINTER_FLAG_INRANGE | POINTER_FLAG_UP)
        pi.ButtonChangeType = POINTER_CHANGE_FIRSTBUTTON_UP
    pi.sourceDevice = None
    pi.hwndTarget = None
    pi.ptPixelLocation = wintypes.POINT(int(px), int(py))
    pi.ptHimetricLocation = wintypes.POINT(int(px), int(py))
    pi.ptPixelLocationRaw = wintypes.POINT(int(px), int(py))
    pi.ptHimetricLocationRaw = wintypes.POINT(int(px), int(py))
    pi.dwTime = int(_kernel32.GetTickCount())
    pi.historyCount = 1
    pi.InputState = 0
    pi.KeyStates = 0
    pi.PerformanceCount = 0
    info.u.penInfo.penMask = PEN_MASK_PRESSURE
    info.u.penInfo.pressure = 512
    info.u.penInfo.rotation = 0
    info.u.penInfo.tiltX = 0
    info.u.penInfo.tiltY = 0
    return info


def _inject_pen(dev: Any, px: int, py: int, phase: str) -> None:
    info = _pen_info(px, py, phase)
    ok = _inject_pointer_input(dev, ctypes.byref(info), 1)
    if not ok:
        raise VirtualInputError(
            "InjectSyntheticPointerInput(" + phase + ") failed (winerr="
            + str(ctypes.get_last_error()) + ")",
            code="VIRTUAL_POINTER_FAILED", phase=phase)


def _restore_focus(prev_fg: int, target: int) -> bool:
    if not prev_fg or prev_fg == target or not _user32.IsWindow(wintypes.HWND(prev_fg)):
        return False
    try:
        return activate_window(prev_fg, timeout=0.8)
    except Exception:  # noqa: BLE001
        return False


def virtual_pointer_click(hwnd: Any, x: int, y: int, press_ms: int = 25,
                          restore_focus: bool = True) -> Dict[str, Any]:
    """合成笔点按（不移动物理鼠标；目标窗口可能被短暂激活）。"""
    _require()
    if not synthetic_pointer_available():
        raise VirtualInputError(
            "synthetic pointer device is not available",
            code="VIRTUAL_POINTER_UNAVAILABLE")
    root = root_window(hwnd)
    ok, bad, blocker = points_target_window(root, [(x, y)])
    if not ok:
        raise VirtualInputError(
            "target window is not the topmost window at pointer point "
            + str([x, y]) + " (blocker=" + repr(blocker) + ")",
            code="VIRTUAL_POINTER_OCCLUDED", hwnd=root,
            point=[int(x), int(y)], blocker=blocker)
    cursor_before = _cursor_pos()
    with _synth_lock:
        prev_fg = int(_user32.GetForegroundWindow() or 0)
        dev = _create_pointer_device(PT_PEN, 1, POINTER_FEEDBACK_INDIRECT)
        if not dev:
            raise VirtualInputError(
                "CreateSyntheticPointerDevice failed (winerr="
                + str(ctypes.get_last_error()) + ")",
                code="VIRTUAL_POINTER_UNAVAILABLE")
        try:
            _inject_pen(dev, x, y, "down")
            # DOWN→UP 至少跨过一个 GetTickCount 刻度（15.6ms）。
            time.sleep(max(0.02, min(max(int(press_ms), 0), 200) / 1000.0))
            _inject_pen(dev, x, y, "up")
        finally:
            try:
                _destroy_pointer_device(dev)
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.12)
        fg_after = int(_user32.GetForegroundWindow() or 0)
        restored = _restore_focus(prev_fg, root) if restore_focus else False
    return {"channel": "virtual", "mode": "synthetic_pointer",
            "op": "click", "pointer_type": "pen", "root_hwnd": root,
            "point": [int(x), int(y)], "messages_posted": 2,
            "foreground_before": prev_fg, "foreground_after_inject": fg_after,
            "focus_restored": bool(restored),
            "cursor_before": cursor_before, "cursor_after": _cursor_pos()}


def virtual_pointer_drag(hwnd: Any, points: Sequence[Sequence[int]],
                         duration_ms: int = 300, max_points: int = 200,
                         restore_focus: bool = True) -> Dict[str, Any]:
    """合成笔拖拽（画图/白板等 WinUI 内容岛；不移动物理鼠标）。"""
    _require()
    if not synthetic_pointer_available():
        raise VirtualInputError(
            "synthetic pointer device is not available",
            code="VIRTUAL_POINTER_UNAVAILABLE")
    pts: List[Tuple[int, int]] = []
    for p in points:
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            raise VirtualInputError("pointer drag points must be [x,y] pairs",
                                    code="VIRTUAL_BAD_POINTS")
        pts.append((int(p[0]), int(p[1])))
    if len(pts) < 2:
        raise VirtualInputError("pointer drag requires >=2 points",
                                code="VIRTUAL_BAD_POINTS")
    if len(pts) > max_points:
        step = (len(pts) - 1) / (max_points - 1)
        pts = [pts[round(i * step)] for i in range(max_points)]
    root = root_window(hwnd)
    ok, bad, blocker = points_target_window(root, pts)
    if not ok:
        raise VirtualInputError(
            "target window is not topmost at one of the pointer drag points "
            "(point=" + repr(bad) + " blocker=" + repr(blocker) + ")",
            code="VIRTUAL_POINTER_OCCLUDED", hwnd=root,
            point=bad, blocker=blocker)
    total_s = min(max(int(duration_ms), 0), 10000) / 1000.0
    # 每个 UPDATE 至少间隔 20ms：GetTickCount 15.6ms 刻度内连发两条
    # UPDATE 会被 InjectSyntheticPointerInput 以 ERROR_INVALID_PARAMETER
    # 拒绝（实测 Win11 画图）。20 点以内的小拖拽会因此略慢于声明时长。
    delay = max(0.02, total_s / max(1, len(pts) - 1))
    cursor_before = _cursor_pos()
    with _synth_lock:
        prev_fg = int(_user32.GetForegroundWindow() or 0)
        dev = _create_pointer_device(PT_PEN, 1, POINTER_FEEDBACK_INDIRECT)
        if not dev:
            raise VirtualInputError(
                "CreateSyntheticPointerDevice failed (winerr="
                + str(ctypes.get_last_error()) + ")",
                code="VIRTUAL_POINTER_UNAVAILABLE")
        try:
            _inject_pen(dev, pts[0][0], pts[0][1], "down")
            # 合成指针的 DOWN→UPDATE 不能落在同一 GetTickCount 刻度（15.6ms），
            # 否则系统以 ERROR_INVALID_PARAMETER(87) 拒绝 UPDATE（实测）。
            time.sleep(0.02)
            t0 = time.perf_counter()
            for i, (px, py) in enumerate(pts[1:], start=1):
                _inject_pen(dev, px, py, "move")
                if delay > 0:
                    ahead = t0 + delay * i - time.perf_counter()
                    if ahead > 0:
                        time.sleep(min(ahead, 0.05))
                else:
                    time.sleep(0.005)
            _inject_pen(dev, pts[-1][0], pts[-1][1], "up")
        finally:
            try:
                _destroy_pointer_device(dev)
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.12)
        fg_after = int(_user32.GetForegroundWindow() or 0)
        restored = _restore_focus(prev_fg, root) if restore_focus else False
    return {"channel": "virtual", "mode": "synthetic_pointer",
            "op": "drag", "pointer_type": "pen", "root_hwnd": root,
            "points": len(pts), "duration_ms": int(total_s * 1000),
            "messages_posted": len(pts) + 1,
            "foreground_before": prev_fg, "foreground_after_inject": fg_after,
            "focus_restored": bool(restored),
            "cursor_before": cursor_before, "cursor_after": _cursor_pos()}


# ---- 降级专用：把目标窗口带到前台（会改变焦点，必须审计） -------------------

def activate_window(hwnd: Any, timeout: float = 1.5) -> bool:
    """physical 降级前的必要一步：让目标成为前台窗口（不移动鼠标）。

    带 AttachThreadInput 兜底，避免 Windows 前台锁拒绝 SetForegroundWindow。
    返回是否在 timeout 内观察到目标成为前台。
    """
    _require()
    target = int(hwnd)
    if not _user32.IsWindow(wintypes.HWND(target)):
        return False
    try:
        if _user32.IsIconic(wintypes.HWND(target)):
            _user32.ShowWindow(wintypes.HWND(target), SW_RESTORE)
    except Exception:  # noqa: BLE001
        pass
    fg = int(_user32.GetForegroundWindow() or 0)
    if fg == target:
        return True
    cur = int(_kernel32.GetCurrentThreadId())
    fg_tid = int(_user32.GetWindowThreadProcessId(
        wintypes.HWND(fg), None)) if fg else 0
    tgt_tid = int(_user32.GetWindowThreadProcessId(
        wintypes.HWND(target), None))
    attached: List[int] = []
    for tid in (fg_tid, tgt_tid):
        if tid and tid != cur and tid not in attached:
            try:
                if _user32.AttachThreadInput(cur, tid, True):
                    attached.append(tid)
            except Exception:  # noqa: BLE001
                pass
    try:
        _user32.BringWindowToTop(wintypes.HWND(target))
        _user32.SetForegroundWindow(wintypes.HWND(target))
    except Exception:  # noqa: BLE001
        pass
    finally:
        for tid in attached:
            try:
                _user32.AttachThreadInput(cur, tid, False)
            except Exception:  # noqa: BLE001
                pass
    end = time.time() + max(0.1, float(timeout))
    while time.time() < end:
        if int(_user32.GetForegroundWindow() or 0) == target:
            return True
        time.sleep(0.05)
    return False


def describe() -> Dict[str, Any]:
    """能力自述（/health 用）。"""
    return {"available": available(), "platform": sys.platform,
            "synthetic_pointer": synthetic_pointer_available(),
            "methods": ["PostMessageW/WM_LBUTTON*", "PostMessageW/WM_MOUSEMOVE",
                        "PostMessageW/WM_CHAR", "PostMessageW/WM_MOUSEWHEEL",
                        "PrintWindow(PW_RENDERFULLCONTENT) 128px verify",
                        "CreateSyntheticPointerDevice(PEN)+InjectSyntheticPointerInput"],
            "physical_cursor_untouched": True,
            "synthetic_pointer_may_activate_target": True,
            "requires_windows": True}
