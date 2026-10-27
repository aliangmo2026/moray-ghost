"""v0.13 自建迷你 CAD 控制台（命令流自动化靶窗）。

本机无 AutoCAD/FreeCAD，替代验证方案的核心靶子：一个接受 CAD 命令流的
控制台窗口——键入 RECT/CIRCLE/ZOOM/SAVEAS + 绝对坐标（mm），与真实 CAD
命令行同语义。解析后实体画在画布上，SAVEAS 经 ezdxf 产出真实 DXF
（含 DIMENSION 尺寸标注，$INSUNITS=4 毫米）。

用法：python _v13_cad_console.py <唯一stem>
就绪时 stdout 打印一行：
    READY {"hwnd":..., "canvas":[x,y,w,h], "entry":[x,y,w,h], ...}
bbox 均为 DPI aware 物理像素屏幕坐标。窗口标题随命令推进追加
    |N:<输入行数>|S:<状态>|E:<实体数>|SC:<当前比例>|SAVED:<路径>
供测试确定性断言。点击画布/标签即把键盘焦点交给命令行（CAD 语义）。

关闭方式：测试脚本对自己 hwnd 发 WM_CLOSE。
"""
from __future__ import annotations

import ctypes
import json
import math
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except OSError:
        pass

import tkinter as tk  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
stem = sys.argv[1] if len(sys.argv) > 1 else "t"
BASE_TITLE = "moray-v13-cad-" + stem

BG = "#14161c"
GRID = "#262b35"
AXIS = "#3a4252"
ENTITY = "#e6edf3"
DIMG = "#d4a72c"
LABEL = "#aab4c8"

# 初始视口（世界坐标 mm）：故意放得很宽，ZOOM E 后按实体范围收紧 ——
# 比例必须可见变化，测试断言 SC 前后不同。
INITIAL_VIEW = (-60.0, -60.0, 200.0, 120.0)

root = tk.Tk()
root.title(BASE_TITLE)
root.geometry("1000x760+140+90")
root.attributes("-topmost", True)
root.configure(bg="#1b1f2a")

canvas = tk.Canvas(root, width=960, height=470, bg=BG,
                   highlightthickness=1, highlightbackground="#2d3546")
canvas.pack(padx=18, pady=(14, 6))

cmd_row = tk.Frame(root, bg="#1b1f2a")
cmd_row.pack(fill="x", padx=18, pady=(4, 2))
tk.Label(cmd_row, text="命令:", font=("Consolas", 11, "bold"),
         fg="#9fe3b8", bg="#1b1f2a").pack(side="left")
entry = tk.Entry(cmd_row, font=("Consolas", 12), width=72,
                 insertbackground="#9fe3b8")
entry.pack(side="left", padx=(6, 0), fill="x", expand=True)

prompt = tk.Label(root, text="命令:", font=("Consolas", 10),
                  fg="#8b93a3", bg="#1b1f2a", anchor="w")
prompt.pack(fill="x", padx=18)

history = tk.Listbox(root, height=6, font=("Consolas", 9),
                     bg="#11141b", fg="#c9d1d9", highlightthickness=0,
                     selectbackground="#2d3546")
history.pack(fill="both", padx=18, pady=(2, 10), ipady=2)

state = {"mode": "idle", "n": 0, "p1": None, "center": None,
         "rect": None, "circle": None, "scale": None, "saved": "",
         "count": 0}
view = list(INITIAL_VIEW)          # [x0, y0, x1, y1] 世界 mm
tf = {"sx": 1.0, "sy": 1.0, "ox": 0.0, "oy": 0.0}

PROMPTS = {
    "idle": "命令:",
    "rect_p1": "指定第一角点:",
    "rect_p2": "指定对角点:",
    "circ_c": "指定圆心点:",
    "circ_r": "指定半径:",
    "zoom_opt": "缩放选项 [E=范围]:",
    "save_name": "文件名:",
}


def w2s(x: float, y: float) -> tuple:
    return tf["ox"] + (x - view[0]) * tf["sx"], \
        tf["oy"] + (view[3] - y) * tf["sy"]


def compute_tf(w: int, h: int) -> None:
    pad = 14
    sx = (w - 2 * pad) / (view[1] and (view[2] - view[0]) or 1)
    sy = (h - 2 * pad) / (view[3] - view[1])
    s = min(sx, sy)
    tf["sx"] = tf["sy"] = s
    # 居中
    dw = (view[2] - view[0]) * s
    dh = (view[3] - view[1]) * s
    tf["ox"] = pad + ((w - 2 * pad) - dw) / 2
    tf["oy"] = pad + ((h - 2 * pad) - dh) / 2


def dims_zone() -> list:
    """已画实体 + 尺寸标注的整体范围（世界 mm），供 ZOOM E 收紧视口。"""
    boxes = []
    if state["rect"]:
        x0, y0, x1, y1 = state["rect"]
        boxes += [(x0, y0, x1, y1),
                  (x0, y0 - 17, x1, y0 - 5),      # 水平尺寸线区
                  (x1 + 5, y0 - 2, x1 + 26, y1 + 2)]  # 垂直尺寸线区
    if state["circle"]:
        cx, cy, r = state["circle"]
        boxes += [(cx - r, cy - r, cx + r, cy + r),
                  (cx, cy - 4, cx + r + 30, cy + 20)]  # R 引线+文字
    return boxes


def fit_extents() -> None:
    boxes = dims_zone()
    if not boxes:
        return
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[2] for b in boxes)
    y1 = max(b[3] for b in boxes)
    px = max(6.0, (x1 - x0) * 0.06)
    py = max(6.0, (y1 - y0) * 0.06)
    view[:] = [x0 - px, y0 - py, x1 + px, y1 + py]


def arrow_head(x: float, y: float, ang: float, size: float,
               color: str) -> None:
    a1 = ang + math.pi * 0.84
    a2 = ang - math.pi * 0.84
    p1 = (x + size * math.cos(a1), y + size * math.sin(a1))
    p2 = (x + size * math.cos(a2), y + size * math.sin(a2))
    s0 = w2s(x, y)
    s1 = w2s(*p1)
    s2 = w2s(*p2)
    canvas.create_polygon(s0[0], s0[1], s1[0], s1[1], s2[0], s2[1],
                          fill=color, outline=color)


def draw_grid(w: int, h: int) -> None:
    step = 10.0
    vx0, vy0, vx1, vy1 = view
    x = math.floor(vx0 / step) * step
    while x <= vx1:
        sx, _ = w2s(x, vy0)
        if 0 <= sx <= w:
            canvas.create_line(sx, 0, sx, h, fill=GRID)
        x += step
    y = math.floor(vy0 / step) * step
    while y <= vy1:
        _, sy = w2s(vx0, y)
        if 0 <= sy <= h:
            canvas.create_line(0, sy, w, sy, fill=GRID)
        y += step
    # 坐标轴
    if vx0 <= 0 <= vx1:
        sx, _ = w2s(0, 0)
        canvas.create_line(sx, 0, sx, h, fill=AXIS, width=2)
    if vy0 <= 0 <= vy1:
        _, sy = w2s(0, 0)
        canvas.create_line(0, sy, w, sy, fill=AXIS, width=2)


def redraw() -> None:
    canvas.delete("all")
    w = int(canvas["width"])
    h = int(canvas["height"])
    compute_tf(w, h)
    draw_grid(w, h)

    if state["rect"]:
        x0, y0, x1, y1 = state["rect"]
        s0 = w2s(x0, y0)
        s1 = w2s(x1, y1)
        canvas.create_rectangle(s0[0], s0[1], s1[0], s1[1],
                                outline=ENTITY, width=2)
        # 水平尺寸 100：尺寸线 y0-10，界线/箭头/文字
        dy = y0 - 10
        for xx in (x0, x1):
            a = w2s(xx, y0)
            b = w2s(xx, dy - 3)
            canvas.create_line(a[0], a[1], b[0], b[1], fill=DIMG)
        a = w2s(x0, dy)
        b = w2s(x1, dy)
        canvas.create_line(a[0], a[1], b[0], b[1], fill=DIMG)
        arrow_head(x0, dy, 0.0, 7, DIMG)
        arrow_head(x1, dy, math.pi, 7, DIMG)
        mid = w2s((x0 + x1) / 2, dy)
        canvas.create_text(mid[0], mid[1] - 9,
                           text=f"{abs(x1 - x0):g}", fill=DIMG,
                           font=("Segoe UI", 11, "bold"))
        # 垂直尺寸 50：尺寸线 x1+15
        dx = x1 + 15
        for yy in (y0, y1):
            a = w2s(x1, yy)
            b = w2s(dx + 3, yy)
            canvas.create_line(a[0], a[1], b[0], b[1], fill=DIMG)
        a = w2s(dx, y0)
        b = w2s(dx, y1)
        canvas.create_line(a[0], a[1], b[0], b[1], fill=DIMG)
        arrow_head(dx, y0, math.pi / 2, 7, DIMG)
        arrow_head(dx, y1, -math.pi / 2, 7, DIMG)
        mid = w2s(dx, (y0 + y1) / 2)
        canvas.create_text(mid[0] + 16, mid[1],
                           text=f"{abs(y1 - y0):g}", fill=DIMG,
                           font=("Segoe UI", 11, "bold"))

    if state["circle"]:
        cx, cy, r = state["circle"]
        c = w2s(cx, cy)
        e0 = w2s(cx - r, cy + r)
        e1 = w2s(cx + r, cy - r)
        canvas.create_oval(e0[0], e0[1], e1[0], e1[1],
                           outline=ENTITY, width=2)
        # 十字中心标记（短，不触圆周）
        k = r * 0.28
        a = w2s(cx - k, cy)
        b = w2s(cx + k, cy)
        canvas.create_line(a[0], a[1], b[0], b[1], fill=ENTITY)
        a = w2s(cx, cy - k)
        b = w2s(cx, cy + k)
        canvas.create_line(a[0], a[1], b[0], b[1], fill=ENTITY)
        # R 引线 45°
        ang = math.pi / 4
        ex = cx + r * math.cos(ang)
        ey = cy + r * math.sin(ang)
        a = w2s(cx, cy)
        b = w2s(ex, ey)
        canvas.create_line(a[0], a[1], b[0], b[1], fill=DIMG)
        arrow_head(cx, cy, ang + math.pi, 7, DIMG)
        t = w2s(ex + 3, ey + 4)
        canvas.create_text(t[0], t[1], text=f"R{r:g}", fill=DIMG,
                           font=("Segoe UI", 11, "bold"), anchor="sw")

    # OCR/RAG 锚点标签：固定放画布右下角，避开实体与尺寸标注的
    # 像素测量通道（行/列扫描），点击它即聚焦命令行。
    canvas.create_text(w - 12, h - 12, text="绘图区", fill=LABEL,
                       font=("Segoe UI", 11), anchor="se")

    state["scale"] = round(tf["sx"], 1)


def update_title() -> None:
    parts = [BASE_TITLE,
             "N:" + str(state["n"]),
             "S:" + state["mode"],
             "E:" + str(state["count"]),
             "SC:" + str(state["scale"])]
    if state["saved"]:
        parts.append("SAVED:" + state["saved"])
    root.title("|".join(parts))


def set_prompt() -> None:
    prompt.config(text=PROMPTS.get(state["mode"], "命令:"))


def parse_point(line: str):
    parts = [p.strip() for p in line.split(",")]
    if len(parts) != 2:
        return None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        return None


def emit_dxf(path: Path) -> None:
    import ezdxf
    import logging
    logging.disable(logging.CRITICAL)
    doc = ezdxf.new("R2010", setup=True)
    doc.header["$INSUNITS"] = 4  # 毫米
    msp = doc.modelspace()
    x0, y0, x1, y1 = state["rect"]
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    msp.add_lwpolyline(corners, close=True)
    cx, cy, r = state["circle"]
    msp.add_circle((cx, cy), r)
    d1 = msp.add_linear_dim(base=((x0 + x1) / 2, y0 - 10),
                            p1=(x0, y0), p2=(x1, y0))
    d2 = msp.add_linear_dim(base=(x1 + 15, (y0 + y1) / 2),
                            p1=(x1, y0), p2=(x1, y1), angle=90)
    d3 = msp.add_radius_dim(center=(cx, cy), radius=r, angle=math.pi / 4)
    d1.render()
    d2.render()
    d3.render()
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(str(path))


def submit(raw: str) -> None:
    line = raw.strip()
    entry.delete(0, "end")
    if not line:
        return
    state["n"] += 1
    history.insert(0, f"命令: {line}")
    history.itemconfig(0, fg="#9fe3b8")
    mode = state["mode"]

    if mode == "idle":
        cmd = line.upper()
        if cmd in ("RECT", "RECTANG"):
            state["mode"] = "rect_p1"
        elif cmd == "CIRCLE":
            state["mode"] = "circ_c"
        elif cmd == "ZOOM":
            state["mode"] = "zoom_opt"
        elif cmd == "SAVEAS":
            state["mode"] = "save_name"
        else:
            history.insert(0, f"  未知命令: {line}")
            history.itemconfig(0, fg="#f0a0a0")
    elif mode == "rect_p1":
        pt = parse_point(line)
        if pt is None:
            history.insert(0, "  坐标格式: x,y（如 0,0）")
            history.itemconfig(0, fg="#f0a0a0")
        else:
            state["p1"] = pt
            state["mode"] = "rect_p2"
    elif mode == "rect_p2":
        pt = parse_point(line)
        if pt is None:
            history.insert(0, "  坐标格式: x,y（如 100,50）")
            history.itemconfig(0, fg="#f0a0a0")
        else:
            (ax, ay), (bx, by) = state["p1"], pt
            state["rect"] = (min(ax, bx), min(ay, by),
                             max(ax, bx), max(ay, by))
            state["count"] += 1
            state["mode"] = "idle"
            history.insert(0, f"  矩形 {abs(bx - ax):g} x {abs(by - ay):g} "
                              f"@ ({ax:g},{ay:g})")
            history.itemconfig(0, fg="#c9d1d9")
            redraw()
    elif mode == "circ_c":
        pt = parse_point(line)
        if pt is None:
            history.insert(0, "  圆心格式: x,y（如 50,25）")
            history.itemconfig(0, fg="#f0a0a0")
        else:
            state["center"] = pt
            state["mode"] = "circ_r"
    elif mode == "circ_r":
        try:
            r = float(line)
            if r <= 0:
                raise ValueError
        except ValueError:
            history.insert(0, "  半径须为正数（如 10）")
            history.itemconfig(0, fg="#f0a0a0")
        else:
            cx, cy = state["center"]
            state["circle"] = (cx, cy, r)
            state["count"] += 1
            state["mode"] = "idle"
            history.insert(0, f"  圆心({cx:g},{cy:g}) 半径 {r:g}")
            history.itemconfig(0, fg="#c9d1d9")
            redraw()
    elif mode == "zoom_opt":
        if line.upper() == "E":
            fit_extents()
            redraw()
            history.insert(0, "  缩放到范围 (ZOOM E)")
            history.itemconfig(0, fg="#c9d1d9")
        else:
            history.insert(0, "  支持选项: E（范围）")
            history.itemconfig(0, fg="#f0a0a0")
        state["mode"] = "idle"
    elif mode == "save_name":
        rel = line.replace("\\", "/")
        path = Path(line)
        if not path.is_absolute():
            path = ROOT / path
        if path.suffix.lower() != ".dxf":
            path = path.with_suffix(".dxf")
            rel = str(path.relative_to(ROOT)).replace("\\", "/") \
                if path.is_relative_to(ROOT) else str(path)
        if not state["rect"] or not state["circle"]:
            history.insert(0, "  需先画矩形与圆")
            history.itemconfig(0, fg="#f0a0a0")
            state["mode"] = "idle"
        else:
            try:
                emit_dxf(path)
                state["saved"] = rel
                history.insert(0, f"  已保存 {rel}（DXF/mm，含尺寸标注）")
                history.itemconfig(0, fg="#9fe3b8")
            except Exception as exc:  # noqa: BLE001
                history.insert(0, f"  保存失败: {exc!r}")
                history.itemconfig(0, fg="#f0a0a0")
            state["mode"] = "idle"
    set_prompt()
    update_title()


def focus_entry(_event=None) -> None:
    entry.focus_set()


entry.bind("<Return>", lambda e: submit(entry.get()))
entry.bind("<KP_Enter>", lambda e: submit(entry.get()))
canvas.bind("<Button-1>", focus_entry)
history.bind("<Button-1>", focus_entry)

redraw()
set_prompt()
update_title()
entry.focus_set()


def widget_box(w) -> list:
    return [int(w.winfo_rootx()), int(w.winfo_rooty()),
            int(w.winfo_width()), int(w.winfo_height())]


def publish_ready() -> None:
    hwnd = ctypes.windll.user32.GetAncestor(root.winfo_id(), 2)  # GA_ROOT
    print("READY " + json.dumps({
        "hwnd": int(hwnd), "title": root.title(),
        "canvas": widget_box(canvas), "entry": widget_box(entry),
        "label_anchor": [int(canvas.winfo_rootx() + canvas.winfo_width() - 40),
                         int(canvas.winfo_rooty() + canvas.winfo_height() - 26)],
    }), flush=True)


root.after(400, publish_ready)
root.mainloop()
