"""自建 Tk 靶窗（v0.7 红线验收用）：点击窗内任意处 → 标题改为「保存成功」（精确值）。

用途：
- post_assert 的可验证对象（window_title_is / contains / changed 断言）；
- 红线/白名单点击靶（python.exe 宿主，纯 win32 宿主无记事本怪癖）。
坐标文件握手（顶层 hwnd + 内容区左上），与 _v06_text_target.py 同工艺。
初始标题带唯一 stem（测试脚本差集绑定）；点击后标题为精确的「保存成功」（无后缀，
供 window_title_is 断言）。
用法：python _v07_target.py <初始标题前缀> <唯一stem> <坐标文件> [geometry]

DPI 红线：必须在 import tkinter 前声明 Per-Monitor 感知——本机 150% 缩放下，
DPI 无感知进程的 geometry/winfo 坐标会被系统虚拟化（请求 (1200,600) 实际落在
物理 (1800,900)），坐标文件里的点位将全部错位、点击落进别的窗口。
"""
import ctypes
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except OSError:
        pass

import tkinter as tk  # noqa: E402

title_prefix = sys.argv[1]
stem = sys.argv[2]
coord_file = Path(sys.argv[3])
geometry = sys.argv[4] if len(sys.argv) > 4 else "520x260+240+240"

root = tk.Tk()
root.title(title_prefix + "-" + stem)
root.geometry(geometry)
root.attributes("-topmost", True)
lbl = tk.Label(root, text="v0.7 redline target\n(点击此处 → 窗口标题变为「保存成功」)",
               font=("Segoe UI", 12))
lbl.pack(fill="both", expand=True, padx=10, pady=10)


def on_click(_event):
    root.title("保存成功")   # 精确改名：post_assert window_title_is 的验证对象


lbl.bind("<Button-1>", on_click)
root.update_idletasks()
root.update()


def publish():
    top = ctypes.windll.user32.GetAncestor(root.winfo_id(), 2)  # GA_ROOT
    coord_file.write_text("%d\n%d\n%d\n" % (top, lbl.winfo_rootx(),
                                            lbl.winfo_rooty()),
                          encoding="utf-8")


root.after(300, publish)
root.mainloop()
