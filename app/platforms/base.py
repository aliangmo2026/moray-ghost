"""跨平台驱动抽象（v0.15 阶段二）：BaseScreen / BaseInput 契约。

设计原则（与 v0.7 backend.py 同哲学）：
- 路由/控制层只依赖本抽象，绝不直接 import mss/pyautogui/平台 DLL；
- 平台实现由 app.platforms 按 sys.platform 动态加载（get_screen/get_input）；
- Windows 实现（windows.py）承载 v0.14 全部红线实证代码——DPI 顺序红线、
  单 capture 线程、mouse_event 像素级修正、IME 剪贴板粘贴、Fail-Safe 等；
- macOS/Linux 为骨架（基于 mss + pyautogui 的跨平台路径），未在目标平台
  验证前方法一律 NotImplementedError——绝不假装能跑。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image


class BaseScreen(ABC):
    """抓屏契约：与 app.core.screen_mss 历史函数面一一对应。"""

    @abstractmethod
    def grab(self, monitor_index: int = 1,
             timeout: float = 8.0) -> Tuple[Image.Image, Dict[str, int]]:
        """抓屏并转 RGB。monitor_index=1 主屏，0 全屏联合区。"""

    @abstractmethod
    def monitors(self) -> List[Dict[str, Any]]:
        """monitors[0] 为全屏联合区，[1..n] 为各屏（含 index/left/top/...）。"""

    @abstractmethod
    def monitor_rect(self, index: int) -> Dict[str, int]:
        """指定屏的物理矩形（mss 语义，index 0 = 联合区）。"""

    @abstractmethod
    def monitor_at(self, x: int, y: int) -> Optional[Dict[str, int]]:
        """包含物理点 (x,y) 的屏（不含联合区）；不在任何屏上返回 None。"""

    @abstractmethod
    def virtual_screen(self) -> Dict[str, int]:
        """虚拟桌面（全屏联合区）物理矩形。"""

    @abstractmethod
    def dpi_status(self) -> Dict[str, Any]:
        """DPI 档位 + 各屏矩形 + 虚拟桌面一致性自检。"""

    @abstractmethod
    def grab_health(self, window_sec: float = 60.0) -> Dict[str, Any]:
        """最近 window_sec 秒抓屏成功率统计。"""

    @abstractmethod
    def encode(self, img: Image.Image, fmt: str = "jpeg",
               quality: int = 70, scale: float = 1.0) -> bytes:
        """图像编码（jpeg/png，可缩放）。"""

    @abstractmethod
    def save_bytes(self, data: bytes, purpose: str, fmt: str) -> Any:
        """字节落盘到截图目录（含修剪）。"""

    @abstractmethod
    def save_audit(self, img: Image.Image, purpose: str, fmt: str,
                   quality: int) -> Any:
        """审计截图：全分辨率落盘（不缩放）。"""

    @abstractmethod
    def b64_of(self, img: Image.Image, fmt: str, quality: int,
               scale: float) -> str:
        """图像 → base64 ASCII 字符串。"""


class BaseInput(ABC):
    """输入契约：与 app.core.backend 历史函数面一一对应。

    红线继承：write_text/hotkey 必须在 finally 释放全部修饰键；
    move_to 必须发真实鼠标事件（画图语义）；Fail-Safe 四角常开。
    """

    @abstractmethod
    def position(self) -> Tuple[int, int]:
        """当前物理光标坐标。"""

    @abstractmethod
    def move_to(self, x: int, y: int) -> None:
        """移动光标到物理坐标（真实鼠标事件 + 像素级修正）。"""

    @abstractmethod
    def click(self, clicks: int = 1, button: str = "left") -> None:
        """点击（坐标已在调用前 moveTo 完成）。"""

    @abstractmethod
    def mouse_down(self, button: str = "left") -> None: ...

    @abstractmethod
    def mouse_up(self, button: str = "left") -> None: ...

    @abstractmethod
    def scroll(self, dy: int) -> None: ...

    @abstractmethod
    def write_text(self, text: str, interval: float = 0.02) -> None:
        """输入文本（ASCII 走真实按键；非 ASCII 走剪贴板粘贴）。"""

    @abstractmethod
    def hotkey(self, *names: str) -> None: ...

    @abstractmethod
    def key_names(self) -> List[str]: ...

    @abstractmethod
    def release_all_modifiers(self) -> None:
        """释放全部修饰键（绝不泄漏按住态）。"""

    @abstractmethod
    def release_pointer_buttons(self) -> None:
        """释放全部鼠标键（绝不泄漏按住态）。"""

    @abstractmethod
    def pressed_modifiers(self) -> List[str]:
        """当前按下的修饰键（如实报告）。"""

    @abstractmethod
    def point_root(self, pt: tuple) -> Optional[int]:
        """屏幕上 pt 处的顶层窗口根句柄（点击落点归属）。"""

    @abstractmethod
    def foreground_hwnd(self) -> Optional[int]:
        """当前前台窗口 hwnd。"""
