"""自建 Tk 文本靶窗（v0.6 drag 验收用）：内容从文件读入，坐标写文件。

- 纯 win32 宿主（无 Win11 记事本的标签页怪癖/浮动工具条/注入输入异常）；
- 标题带唯一 stem（测试脚本差集绑定），TOPMOST 常驻；
- 初始化完成后把 "顶层hwnd\\n文本区左上x\\n文本区左上y" 写入坐标文件（测试轮询）。
用法：python _v06_text_target.py <内容文件> <唯一stem> <坐标文件>
"""
import ctypes
import sys
import tkinter as tk
from pathlib import Path

content = Path(sys.argv[1]).read_text(encoding="utf-8")
stem = sys.argv[2]
coord_file = Path(sys.argv[3])

root = tk.Tk()
root.title("moray-v06-target-" + stem)
geometry = sys.argv[4] if len(sys.argv) > 4 else "760x520+240+240"
root.geometry(geometry)
root.attributes("-topmost", True)
txt = tk.Text(root, font=("Consolas", 11), wrap="none")
txt.pack(fill="both", expand=True)
txt.insert("1.0", content)
# Tk Text 默认不绑鼠标滚轮（经典 Tk 行为），显式绑定才能被 wheel 驱动
txt.bind("<MouseWheel>",
         lambda e: txt.yview_scroll(int(-1 * (e.delta / 120)), "units"))
root.update_idletasks()
root.update()


def publish():
    top = ctypes.windll.user32.GetAncestor(root.winfo_id(), 2)  # GA_ROOT
    coord_file.write_text("%d\n%d\n%d\n" % (top, txt.winfo_rootx(),
                                            txt.winfo_rooty()),
                          encoding="utf-8")


root.after(300, publish)
root.mainloop()
