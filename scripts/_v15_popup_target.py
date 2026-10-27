"""v0.15 System1 弹窗靶窗：本地 Tk 弹窗（绝不触碰真实软件/真实账户）。

用法：python _v15_popup_target.py <stem> <mode> [marker_path]
  mode=normal   弹窗（标题/正文含特征词 + 按钮 稍后/关闭）——预期被 System1 点「关闭」
  mode=uac      假 UAC 提权窗（标题「用户账户控制」，正文含特征词 + 关闭按钮）
                ——System1 必须拒绝（FATAL 零输入），按钮绝不可被点
  mode=blacklist 高危标题窗（标题含「删除」）——System1 必须跳过
  mode=benign   无特征词普通小窗（只有「关闭」按钮）——规则必须不命中（AND 语义）

点击「关闭/稍后」→ 写 marker 文件（记录按钮名）并销毁窗口（本地可观察证据）。
marker 缺失 = 反射未点击。READY {"hwnd":..., "mode":...}。
"""
from __future__ import annotations

import ctypes
import json
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except OSError:
        pass

import tkinter as tk  # noqa: E402

stem = sys.argv[1] if len(sys.argv) > 1 else "t"
mode = sys.argv[2] if len(sys.argv) > 2 else "normal"
marker = Path(sys.argv[3]) if len(sys.argv) > 3 else Path.cwd() / ("v15_" + stem + ".marker")

if mode == "uac":
    # 标题同时含 FATAL token 与触发词：保证经 title 预筛进候选（不依赖焦点），
    # 而 uac 拒绝发生在 OCR 之前——锁屏/无焦点环境也能验证"零输入拒绝"。
    title = "用户账户控制 软件更新 " + stem
elif mode == "blacklist":
    title = "moray-v15-" + stem + " 删除确认 软件更新"
elif mode == "benign":
    title = "moray-v15-" + stem + " 普通窗口"
else:
    title = "moray-v15-" + stem + " 软件更新"

root = tk.Tk()
root.title(title)
# 位置避开 Fail-Safe 四角；对话框体量（≤45% 屏由反射侧校验，这里给足小窗）
root.geometry("460x260+420+300")
root.attributes("-topmost", True)

if mode == "benign":
    body = "这是一段普通说明文字，没有触发词。"
else:
    body = "每日提示：发现新版本，点击立即升级"

tk.Label(root, text=body, font=("Segoe UI", 13), wraplength=400
         ).pack(pady=(24, 12))

btns = tk.Frame(root)
btns.pack(pady=8)


def on_btn(label: str) -> None:
    try:
        marker.write_text(label, encoding="utf-8")
    except OSError:
        pass
    root.destroy()


# 稍后优先级低于关闭（规则库顺序），用于验证反射按优先级选「关闭」
for label in ("稍后", "关闭"):
    b = tk.Button(btns, text=label, font=("Segoe UI", 12), width=8,
                  command=lambda l=label: on_btn(l))
    b.pack(side="left", padx=12)

tip = tk.Label(root, text="仅本地演示，不连接真实账户",
               font=("Segoe UI", 9), fg="#888")
tip.pack(pady=(8, 4))

root.update_idletasks()
root.update()


def publish_ready() -> None:
    top = ctypes.windll.user32.GetAncestor(root.winfo_id(), 2)  # GA_ROOT
    rect = [root.winfo_rootx(), root.winfo_rooty(),
            root.winfo_width(), root.winfo_height()]
    print("READY " + json.dumps({"hwnd": int(top), "mode": mode,
                                 "rect": rect, "marker": str(marker)}),
          flush=True)


root.after(120, publish_ready)
root.mainloop()
