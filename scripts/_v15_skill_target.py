"""v0.15 阶段三技能录制靶窗：三个按钮 + 计数落盘（回放可验证）。

三按钮 2x2 网格布局，物理坐标由窗口 rect + 布局比例推得；每次点击在
marker 文件追加一行 button 名——录制/回放两侧都用它证明"点到了哪"。
READY 握手（与 _v12_form_target 同一模式）：打印 READY {json} 后台运行。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# 顺序红线（与 _v15_popup_target 一致）：DPI 感知必须先于 import tkinter，
# 否则 winfo/ClientToScreen 给出虚拟化坐标——不是物理屏坐标，点击所有权全错位。
import ctypes  # noqa: E402
if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except OSError:
        pass

import ctypes.wintypes  # noqa: E402
import tkinter as tk  # noqa: E402

MARKER = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/_v15_skill_marker.txt")


def _click(btn: str) -> None:
    with MARKER.open("a", encoding="utf-8") as f:
        f.write(btn + "\n")


root = tk.Tk()
root.title("moray-v15-skill 技能靶")
root.geometry("360x180+200+700")  # 远离 Fail-Safe 四角
root.attributes("-topmost", True)  # 用户正在用电脑：保证靶窗不被其他窗口盖住
root.update_idletasks()
root.update()
# Tk 的 -topmost 偶尔不落 exstyle：win32 强制插入顶置带（用户 topmost 浏览器会盖住
# 非 topmost 窗）。注意必须作用于根包装窗（GA_ROOT）——winfo_id() 是客户区子窗。
_u32 = ctypes.windll.user32
_child = int(root.winfo_id())
_root_hwnd = int(_u32.GetAncestor(_child, 2))  # GA_ROOT
_u32.SetWindowPos(_root_hwnd, -1, 0, 0, 0, 0, 0x0002 | 0x0001)  # HWND_TOPMOST

# 三按钮：上排 Alpha/Beta，下排 Gamma——间距足够大防误点
for i, (name, r, c) in enumerate((("Alpha", 0, 0), ("Beta", 0, 1), ("Gamma", 1, 0))):
    b = tk.Button(root, text=name, width=10, height=2,
                  command=lambda n=name: _click(n))
    b.grid(row=r, column=c, padx=18, pady=(24 if r == 0 else 8), ipadx=4)

root.update()

# 按钮中心物理坐标：直接对按钮子窗 hwnd 用 win32 GetWindowRect（物理像素）——
# Tk 的 winfo_rooty/ClientToScreen 在 DPI 混合环境下可能给出与物理屏错位的坐标，
# GetWindowRect 是权威。
user32 = ctypes.windll.user32
centers = {}
for child in root.winfo_children():
    bh = int(child.winfo_id())
    rect = ctypes.wintypes.RECT()
    user32.GetWindowRect(bh, ctypes.byref(rect))
    centers[child.cget("text")] = [
        int((rect.left + rect.right) // 2),
        int((rect.top + rect.bottom) // 2),
    ]

print("READY " + json.dumps({
    "hwnd": _root_hwnd,  # 根包装窗：前台/所有权/关闭都走它
    "title": root.title(),
    "centers": centers,
    "marker": str(MARKER),
}, ensure_ascii=False), flush=True)

root.mainloop()
