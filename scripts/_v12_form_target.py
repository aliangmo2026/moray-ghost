"""v0.12 GUI RAG/Guard 靶窗：本地 Tk 表单（绝不触碰真实微信/支付）。

用法：python _v12_form_target.py <唯一stem>
就绪时 stdout 打印一行：
    READY {"hwnd":..., "controls":[{"text","type","bbox":[x,y,w,h]}, ...]}
其中 bbox 是 Tk 自报的物理像素屏幕坐标（DPI aware），供 GUI RAG 准确率
对账。点击任意按钮只把标题改成 `-BTN:<label>`（纯本地可观察证据，不执行
任何真实业务/删除/转账操作）。危险词按钮用于 Ghost Guard 拦截验收——
被拦截时标题不会变化。

关闭方式：测试脚本对自己 hwnd 发 WM_CLOSE。
"""
from __future__ import annotations

import ctypes
import json
import sys

if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except OSError:
        pass

import tkinter as tk  # noqa: E402

stem = sys.argv[1] if len(sys.argv) > 1 else "t"
BASE_TITLE = "moray-v12-" + stem

root = tk.Tk()
root.title(BASE_TITLE)
root.geometry("660x540+360+250")
root.attributes("-topmost", True)

widgets = []


def add(widget, label: str, kind: str) -> None:
    widgets.append({"widget": widget, "text": label, "type": kind})


tk.Label(root, text="Ghost GUI RAG 本地表单", font=("Segoe UI", 15, "bold")
         ).pack(pady=(14, 8))

form = tk.Frame(root)
form.pack(padx=34, fill="x")

tk.Label(form, text="用户名", font=("Segoe UI", 11)).grid(
    row=0, column=0, sticky="e", padx=(0, 8), pady=6)
user = tk.Entry(form, font=("Consolas", 11), width=30)
user.grid(row=0, column=1, sticky="w", pady=6)
user.insert(0, "moray_user")
add(user, "用户名", "input")

tk.Label(form, text="密码", font=("Segoe UI", 11)).grid(
    row=1, column=0, sticky="e", padx=(0, 8), pady=6)
pwd = tk.Entry(form, font=("Consolas", 11), width=30, show="*")
pwd.grid(row=1, column=1, sticky="w", pady=6)
add(pwd, "密码", "input")

tk.Label(form, text="备注", font=("Segoe UI", 11)).grid(
    row=2, column=0, sticky="e", padx=(0, 8), pady=6)
note = tk.Entry(form, font=("Consolas", 11), width=30)
note.grid(row=2, column=1, sticky="w", pady=6)
note.insert(0, "像素级自动化")
add(note, "备注", "input")

agree = tk.Checkbutton(form, text="记住我", font=("Segoe UI", 10))
agree.grid(row=3, column=1, sticky="w", pady=2)
add(agree, "记住我", "checkbox")

status = tk.Label(root, text="就绪", font=("Segoe UI", 10), fg="#444")
status.pack(pady=(6, 4))

btns = tk.Frame(root)
btns.pack(pady=6)
normal = [("登录", "button"), ("取消", "button"), ("保存成功", "button")]
danger = [("删除账户", "button"), ("转账", "button"), ("退出登录", "button")]

for idx, (text, kind) in enumerate(normal + danger):
    def _cmd(t=text):
        if t == "保存成功":
            save_summary()
        else:
            publish(t)
    b = tk.Button(btns, text=text, font=("Segoe UI", 11), width=10,
                  command=_cmd)
    b.grid(row=idx // 3, column=idx % 3, padx=5, pady=4)
    add(b, text, kind)

tip = tk.Label(root, text="仅本地演示，不连接真实账户",
               font=("Segoe UI", 9), fg="#888")
tip.pack(pady=(10, 4))


def save_summary() -> None:
    """保存按钮的真实证据：读取输入框 → 状态栏 + 标题回显（供测试断言）。"""
    u = user.get().strip()
    n = note.get().strip()
    status.config(text="已保存: 用户名=" + u + " / 备注=" + n)
    root.title(BASE_TITLE + "-BTN:保存成功|user=" + u + "|note=" + n)


def on_edit(_event=None) -> None:
    status.config(text="输入中: " + user.get().strip()
                  + " / " + note.get().strip())


for _w in (user, pwd, note):
    _w.bind("<KeyRelease>", on_edit)


DANGER_TITLE = {"删除账户": "DANGER-1", "转账": "DANGER-2",
                "退出登录": "DANGER-3"}


def publish(label: str) -> None:
    # 危险按钮用编号回显：标题若含「删除」会触发窗口黑名单，后续点击全被
    # 403（测试自身也会误伤）。状态栏仍显示真实标签。
    token = DANGER_TITLE.get(label, label)
    root.title(BASE_TITLE + "-BTN:" + token)
    status.config(text="已点击 " + label)


root.update_idletasks()
root.update()


def rect_of(widget) -> list:
    x = widget.winfo_rootx()
    y = widget.winfo_rooty()
    return [int(x), int(y), int(widget.winfo_width()),
            int(widget.winfo_height())]


def publish_ready() -> None:
    top = ctypes.windll.user32.GetAncestor(root.winfo_id(), 2)  # GA_ROOT
    controls = []
    for item in widgets:
        w = item["widget"]
        label = item["text"]
        value = ""
        text = label
        if item["type"] == "input":
            value = w.get() or ""
            text = value or label
        controls.append({"text": text, "label": label, "value": value,
                         "type": item["type"], "bbox": rect_of(w)})
    print("READY " + json.dumps({"hwnd": int(top), "controls": controls,
                                 "title": root.title()},
                                ensure_ascii=False), flush=True)


root.after(400, publish_ready)
root.mainloop()
