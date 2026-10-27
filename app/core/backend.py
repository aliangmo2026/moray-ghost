"""输入后端抽象：路由/控制层不得直接 import pyautogui，一律走 InputBackend。"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import sys
import time
from abc import ABC, abstractmethod
from typing import List, Optional, Tuple

import pyperclip  # pyautogui 自带依赖；非 ASCII 文本走剪贴板粘贴

from . import dpi_awareness  # noqa: E402  顺序红线：DPI 感知先于 import pyautogui

import pyautogui  # noqa: E402

pyautogui.FAILSAFE = True   # 常开；鼠标甩到角落立即中止整个动作/宏
pyautogui.PAUSE = 0.05

FailSafeException = pyautogui.FailSafeException

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
