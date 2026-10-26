"""v0.7.2 验收靶窗（单文件双模式，全部自建、差集可验、stdout 握手）。

用法：
  python _v072_targets.py uac    # 标题「用户账户控制」的 Tk 窗（UAC FATAL 验证对象）
  python _v072_targets.py type   # Tk 文本靶窗（硬超时打字验证对象）

就绪后向 stdout 打印一行 `READY <hwnd> <cx> <cy>`（cx/cy = 工作区中心物理坐标），
随后进入 mainloop；由测试脚本按自己的 hwnd 经 WM_CLOSE 销毁。

DPI 红线：import tkinter 前声明 Per-Monitor 感知（150% 缩放下坐标否则 ×1.5 错位）。
"""
from __future__ import annotations

import ctypes
import sys

if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except OSError:
        pass

import tkinter as tk  # noqa: E402

mode = sys.argv[1] if len(sys.argv) > 1 else "uac"

root = tk.Tk()
if mode == "uac":
    root.title("用户账户控制")
    root.geometry("460x200+520+320")
    work = tk.Frame(root, bg="#f3f3f3")
    work.pack(fill="both", expand=True)
    tk.Label(work, text="用户账户控制", font=("Segoe UI", 16)).pack(pady=24)
    tk.Label(work, text="（自建假窗：验证 Ghost 绝不点击 UAC 提权弹窗）",
             fg="#666").pack()
else:
    root.title("moray-v072-type-target")
    root.geometry("720x420+320+280")
    work = tk.Text(root, font=("Consolas", 11), wrap="none")
    work.pack(fill="both", expand=True)
    work.insert("1.0", "v072 hard-timeout typing target\n")

root.attributes("-topmost", True)
root.update_idletasks()
root.update()


def publish() -> None:
    top = ctypes.windll.user32.GetAncestor(root.winfo_id(), 2)  # GA_ROOT
    cx = work.winfo_rootx() + max(20, work.winfo_width() // 2)
    cy = work.winfo_rooty() + max(20, work.winfo_height() // 2)
    print("READY", top, cx, cy, flush=True)


root.after(300, publish)
root.mainloop()
