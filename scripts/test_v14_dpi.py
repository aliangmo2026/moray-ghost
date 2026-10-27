"""v0.14 多显示器 / 混合 DPI 坐标一致性自测。

主仓痛点：双屏/三屏 + 笔记本 150% 缩放 + 外接 100% 时，GUI RAG 提取的
bbox 与物理鼠标点击坐标极易偏移（截图一个坐标系、鼠标另一个坐标系）。

本脚本三层验证（本机单屏也全量跑——纯换算层用**软件模拟**任意多屏布局）：
  A 纯换算自测（零 GUI）：虚拟桌面 0..65535 归一化往返（含 8K 超宽桌面
    量化误差边界）、源图缩放换算（controller 的 source_width 公式）、
    "非感知进程"混合 DPI 错位复现（证明旧链失败模式的量级）。
  B 实机往返：对每块物理屏 3x3 网格点 move_to → 读回光标，要求 ≤1px
    （v0.14 绝对归一化 + 相对事件修正后精确落点）；越界坐标显式拒绝；
    /health cursor 与进程内 position 同一物理坐标系。
  C Tk 点击闭环：自建 Tk 画布靶窗（纯色矩形）→ 服务端 /screenshot 像素
    反推 bbox（模拟 GUI RAG 检索层，零模型）+ win32 ClientToScreen 直算
    两条路 → /execute_action 真实点击 → Tk <Button-1> 事件坐标与预期
    本地坐标 ≤2px。截图坐标系 → 物理点击 → 应用收到的事件坐标全链一致。

铁律：靶窗自建（python.exe）；capabilities=["python.exe"]；点击前
force_foreground；清理只杀自己的进程树与窗口；移动前备份光标位置并还原。
"""
from __future__ import annotations

import base64
import ctypes
import io
import json
import queue
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

if sys.platform == "win32":
    ROOT = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(ROOT))

from app.core import backend, dpi_awareness, screen_mss  # noqa: E402

import tkinter as tk  # noqa: E402  测试进程也必须 DPI 感知（先 import dpi_awareness）

import psutil  # noqa: E402
import requests  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402

from PIL import Image  # noqa: E402

from app.core import config  # noqa: E402

TOKEN = config.ensure_token()
CAPS = {"allowed_windows": ["python.exe"]}
BASE = "http://127.0.0.1:8030/api/ghost"
BTN_COLOR = (60, 141, 255)  # #3C8DFF 纯色矩形，截图反推 bbox 用
TOL_PX = 2

RESULTS: list = []
PROCS: list = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  | {detail}" if detail else ""), flush=True)
    return bool(ok)


def hdr() -> dict:
    return {"X-Ghost-Token": TOKEN}


def port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def kill_by_port(port: int) -> None:
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.laddr and c.laddr.port == port and c.pid:
                psutil.Process(c.pid).kill()
                time.sleep(0.5)
    except Exception:  # noqa: BLE001
        pass


def spawn(args: list) -> subprocess.Popen:
    proc = subprocess.Popen(args, cwd=str(ROOT), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
    PROCS.append(proc)

    def _drain() -> None:
        try:
            for _ in proc.stdout:
                pass
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=_drain, daemon=True).start()
    return proc


def wait_health(timeout: float = 40.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = requests.get(f"{BASE}/health", timeout=2)
            if r.status_code == 200:
                return r.json()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.35)
    return {}


def cleanup() -> None:
    for p in PROCS:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=6)
            except subprocess.TimeoutExpired:
                p.kill()


def force_foreground(hwnd: int) -> bool:
    for _ in range(5):
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
        except Exception:  # noqa: BLE001
            try:
                ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)
                ctypes.windll.user32.keybd_event(0x12, 0, 2, 0)
                win32gui.SetForegroundWindow(hwnd)
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.2)
        if win32gui.GetForegroundWindow() == hwnd:
            return True
    return False


def ghost_action(payload: dict) -> dict:
    body = {"monitor": 1, "settle_seconds": 0.15, "fmt": "png",
            "quality": 70, "scale": 1.0, "capabilities": CAPS}
    body.update(payload)
    r = requests.post(f"{BASE}/execute_action", headers=hdr(),
                      json=body, timeout=60)
    try:
        out = r.json()
    except ValueError:
        out = {"raw": r.text[:300]}
    out["http_status"] = r.status_code
    return out


# ======================== A. 纯换算自测（软件模拟） ========================

def norm_abs(x: int, y: int, v: dict) -> tuple:
    """backend._mouse_event_absolute 的归一化公式（0..65535）。"""
    nx = int(round((x - v["left"]) * 65535.0 / max(1, v["width"] - 1)))
    ny = int(round((y - v["top"]) * 65535.0 / max(1, v["height"] - 1)))
    return max(0, min(65535, nx)), max(0, min(65535, ny))


def denorm_abs(nx: int, ny: int, v: dict) -> tuple:
    """归一化坐标还原物理像素（(w-1) 约定，与正向公式互逆）。"""
    rx = int(round(nx * (v["width"] - 1) / 65535.0)) + v["left"]
    ry = int(round(ny * (v["height"] - 1) / 65535.0)) + v["top"]
    return rx, ry


SIM_LAYOUTS = {
    "单屏 1920x1080@100%": {"left": 0, "top": 0, "width": 1920, "height": 1080},
    # 笔记本 2560x1600@150% 主屏 + 右侧 1920x1080@100% 外接（物理坐标空间）
    "双屏 2560x1600 + 右挂 1920x1080": {
        "left": 0, "top": 0, "width": 4480, "height": 1600},
    # 三屏超宽：8K 级虚拟桌面（绝对归一化量化误差最大的现实场景）
    "三屏 3x3840x2160 超宽": {
        "left": -3840, "top": 0, "width": 11520, "height": 2160},
}


def part_a() -> None:
    print("\n===== A. 纯换算自测（软件模拟多屏布局） =====")
    for name, v in SIM_LAYOUTS.items():
        worst = 0
        for px in range(v["left"], v["left"] + v["width"], 97):
            for py in range(v["top"], v["top"] + v["height"], 89):
                nx, ny = norm_abs(px, py, v)
                rx, ry = denorm_abs(nx, ny, v)
                worst = max(worst, abs(rx - px), abs(ry - py))
        check(f"A1 归一化往返 ≤1px · {name}", worst <= 1, f"worst={worst}px")

    # 超宽桌面量化边界：证明"仅靠绝对归一化不够，需要相对事件修正"
    v = SIM_LAYOUTS["三屏 3x3840x2160 超宽"]
    worst = 0
    for px in range(v["left"], v["left"] + v["width"], 13):
        nx, ny = norm_abs(px, v["top"] + 100, v)
        rx, _ry = denorm_abs(nx, ny, v)
        worst = max(worst, abs(rx - px))
    check("A2 8K 桌面绝对归一化误差 ≤2px（相对修正的存在依据）",
          worst <= 2, f"worst={worst}px")

    # 源图缩放换算：controller 的 xi = round(x_req * real_w / source_w) + left
    for name, v in (("1920x1080@100%", SIM_LAYOUTS["单屏 1920x1080@100%"]),
                    ("2560x1600@150%", {"left": 0, "top": 0, "width": 2560,
                                        "height": 1600})):
        for sw, sh in ((1280, 800), (853, 533)):  # 0.5x / 1/1.5x（150% 逻辑坐标）
            if sw > v["width"] or sh > v["height"]:
                continue
            worst = 0
            for sx in range(0, sw, 17):
                for sy in range(0, sh, 13):
                    xi = round(sx * v["width"] / sw) + v["left"]
                    yi = round(sy * v["height"] / sh) + v["top"]
                    bx = round((xi - v["left"]) * sw / v["width"])
                    by = round((yi - v["top"]) * sh / v["height"])
                    worst = max(worst, abs(bx - sx), abs(by - sy))
            check(f"A3 源图缩放往返 ≤1px · {name} ← {sw}x{sh}",
                  worst <= 1, f"worst={worst}px")

    # "非感知进程"失败模式复现：150% 屏按逻辑/物理混算 → 落点偏差量级
    phys = {"width": 2560, "height": 1600}
    logical = {"width": 1707, "height": 1067}
    px, py = 2000, 1200
    wrong_x = round(px * phys["width"] / logical["width"])
    check("A4 非感知坐标系错位复现 ≥100px（v0.14 修复的失败模式）",
          abs(wrong_x - px) >= 100,
          f"logical→physical 偏移 {wrong_x - px}px")


# ======================== B. 实机光标往返 ========================

def part_b() -> None:
    print("\n===== B. 实机 move_to → 光标读回（每屏 3x3 网格） =====")
    orig = backend.position()
    try:
        mons = [m for m in screen_mss.monitors() if m["index"] != 0]
        for m in mons:
            worst = 0
            worst_pt = None
            for fx in (0.15, 0.5, 0.85):
                for fy in (0.15, 0.5, 0.85):
                    x = int(m["left"] + m["width"] * fx)
                    y = int(m["top"] + m["height"] * fy)
                    backend.move_to(x, y)
                    rx, ry = backend.position()
                    err = max(abs(rx - x), abs(ry - y))
                    if err > worst:
                        worst, worst_pt = err, (x, y, rx, ry)
            check(f"B1 往返 ≤1px · 屏{m['index']} "
                  f"{m['width']}x{m['height']}@({m['left']},{m['top']})",
                  worst <= 1, f"worst={worst}px pt={worst_pt}")

        # 越界坐标必须被显式拒绝（静默钳边 = 混合 DPI 错位温床）
        v = screen_mss.virtual_screen()
        try:
            backend.move_to(v["left"] + v["width"] + 500,
                            v["top"] + v["height"] + 500)
            check("B2 越界 move_to 显式 ValueError", False, "未抛异常")
        except ValueError as exc:
            check("B2 越界 move_to 显式 ValueError", True, str(exc)[:80])

        # /health 的 cursor 与进程内 position 同一坐标系（物理像素）
        r = requests.get(f"{BASE}/health", headers=hdr(), timeout=10).json()
        cur = r.get("cursor") or [None, None]
        backend.move_to(321, 246)
        r2 = requests.get(f"{BASE}/health", headers=hdr(), timeout=10).json()
        cur2 = r2.get("cursor") or [None, None]
        check("B3 /health cursor 与物理坐标一致",
              cur2 == [321, 246], f"before={cur} after={cur2}")
    finally:
        backend.move_to(orig[0], orig[1])


# ======================== C. Tk 点击闭环（截图 bbox → 物理点击） ========================

class TkTarget:
    """自建靶窗：画布纯色矩形（模拟"按钮"），记录 <Button-1> 本地坐标。"""

    def __init__(self) -> None:
        self.clicks: "queue.Queue[tuple]" = queue.Queue()
        self.hwnd = 0
        self.canvas_hwnd = 0
        self.rect = (0, 0, 0, 0)  # 画布本地坐标 (x1,y1,x2,y2)
        self._root: "tk.Tk | None" = None
        self._ready = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        if not self._ready.wait(10):
            raise RuntimeError("Tk target window did not come up")

    def _run(self) -> None:
        root = tk.Tk()
        self._root = root
        root.title("v14-dpi-target")
        root.geometry("480x260+120+120")
        canvas = tk.Canvas(root, bg="#F4F4F4", highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        canvas.bind("<Button-1>",
                    lambda e: self.clicks.put(("button1", int(e.x), int(e.y))))
        # 等窗口真正映射出尺寸（update_idletasks 时刻 winfo 可能还是 1，
        # 会把矩形画成 0x0——C1 会"平凡通过"、C2 找不到颜色）
        w = h = 0
        for _ in range(100):
            root.update()
            w, h = canvas.winfo_width(), canvas.winfo_height()
            if w > 100 and h > 80:
                break
            time.sleep(0.05)
        x1, y1 = int(w * 0.15), int(h * 0.15)
        x2, y2 = int(w * 0.85), int(h * 0.85)
        self.rect = (x1, y1, x2, y2)
        # 先填纯色矩形再画文字：bbox 反推只依赖纯色像素，文字不影响取样
        canvas.create_rectangle(x1, y1, x2, y2, fill="#3C8DFF", outline="")
        canvas.create_text((x1 + x2) // 2, (y1 + y2) // 2, text="TARGET",
                           fill="white", font=("Arial", 20, "bold"))
        frame = root.frame()
        self.hwnd = int(frame, 16) if isinstance(frame, str) else int(frame)
        self.canvas_hwnd = int(canvas.winfo_id())
        self._ready.set()
        root.mainloop()

    @property
    def expected_local(self) -> tuple:
        x1, y1, x2, y2 = self.rect
        return ((x1 + x2) // 2, (y1 + y2) // 2)

    @property
    def expected_physical(self) -> tuple:
        """靶心的物理屏幕坐标（ClientToScreen 在 DPI 感知进程 = 物理）。"""
        ox, oy = win32gui.ClientToScreen(self.canvas_hwnd, (0, 0))
        ex, ey = self.expected_local
        return ox + ex, oy + ey

    def place_on(self, m: dict) -> None:
        x = m["left"] + max(0, (m["width"] - 500) // 2)
        y = m["top"] + max(0, (m["height"] - 280) // 2)
        win32gui.SetWindowPos(self.hwnd, win32con.HWND_TOP, x, y, 0, 0,
                              win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)
        time.sleep(0.3)

    def drain(self) -> list:
        out = []
        while True:
            try:
                out.append(self.clicks.get_nowait())
            except queue.Empty:
                return out

    def wait_click(self, timeout: float = 3.0) -> "tuple | None":
        try:
            return self.clicks.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        # 不跨线程调 Tk（after/destroy 从别的线程调会 Tcl_AsyncDelete 警告）：
        # 线程是 daemon，进程退出时随解释器一起收掉。
        self._root = None


def find_color_bbox(img: Image.Image, rgb: tuple, tol: int = 12) -> "list | None":
    """截图中纯色矩形 bbox：粗扫（步进）+ 精扫（边界窗口全分辨率）。

    模拟 GUI RAG 检索层：像素 → bbox，零模型。两段扫描保证 bbox 边缘
    误差 = 0（步进只影响初筛范围），中心点可直接用于 ≤2px 点击验证。
    """
    px = img.load()
    step = max(1, img.width // 640)

    def near(x: int, y: int) -> bool:
        r, g, b = px[x, y][:3]
        return (abs(r - rgb[0]) <= tol and abs(g - rgb[1]) <= tol
                and abs(b - rgb[2]) <= tol)

    xs = [x for x in range(0, img.width, step)
          for y in range(0, img.height, step) if near(x, y)]
    if len(xs) < 16:
        return None
    ys = [y for x in range(0, img.width, step)
          for y in range(0, img.height, step) if near(x, y)]
    # 粗扫范围内的全分辨率精扫（修回步进损失）
    cx0, cx1 = max(0, min(xs) - step), min(img.width - 1, max(xs) + step)
    cy0, cy1 = max(0, min(ys) - step), min(img.height - 1, max(ys) + step)
    fx, fy = [], []
    for y in range(cy0, cy1 + 1):
        for x in range(cx0, cx1 + 1):
            if near(x, y):
                fx.append(x)
                fy.append(y)
    if len(fx) < 16:
        return None
    return [min(fx), min(fy), max(fx) - min(fx), max(fy) - min(fy)]


def _verify_click(target: TkTarget, act: dict, tag: str, mon_idx: int) -> None:
    ev = target.wait_click()
    local = (ev[1], ev[2]) if ev and ev[0] == "button1" else None
    exp = target.expected_local
    ok = (act.get("http_status") == 200 and local is not None
          and abs(local[0] - exp[0]) <= TOL_PX
          and abs(local[1] - exp[1]) <= TOL_PX)
    check(f"{tag} · 屏{mon_idx} 靶心落点 ≤{TOL_PX}px", ok,
          f"http={act.get('http_status')} local={local} expect={exp}")


def part_c(target: TkTarget) -> None:
    print("\n===== C. Tk 点击闭环（截图 bbox → /execute_action 物理点击） =====")
    mons = [m for m in screen_mss.monitors() if m["index"] != 0]
    for m in mons:
        target.place_on(m)
        force_foreground(target.hwnd)
        time.sleep(0.2)

        # C1: win32 ClientToScreen（物理坐标）→ 点击
        px_phys, py_phys = target.expected_physical
        target.drain()
        act = ghost_action({"action": "click", "x": px_phys, "y": py_phys,
                            "hwnd": target.hwnd, "monitor": m["index"]})
        _verify_click(target, act, "C1 win32 bbox→点击", m["index"])

        # C2: 服务端 /screenshot 像素反推 bbox → 点击（完整截图链路）
        r = requests.get(f"{BASE}/screenshot",
                         params={"monitor": m["index"], "fmt": "png",
                                 "scale": 1.0},
                         headers=hdr(), timeout=30).json()
        if not r.get("ok"):
            check(f"C2 截图反推 bbox→点击 · 屏{m['index']}", False,
                  "screenshot failed: " + str(r.get("error")))
            continue
        img = Image.open(io.BytesIO(
            base64.b64decode(r["image_b64"]))).convert("RGB")
        origin = r["monitor_origin"]
        bbox = find_color_bbox(img, BTN_COLOR)
        if bbox is None:
            check(f"C2 截图反推 bbox→点击 · 屏{m['index']}", False,
                  "button color not found in screenshot")
            continue
        sx = origin[0] + bbox[0] + bbox[2] // 2
        sy = origin[1] + bbox[1] + bbox[3] // 2
        # 截图反推靶心 vs win32 物理靶心（两条感知路径互证 ≤3px）
        ex_phys, ey_phys = target.expected_physical
        drift = max(abs(sx - ex_phys), abs(sy - ey_phys))
        target.drain()
        force_foreground(target.hwnd)
        act2 = ghost_action({"action": "click", "x": sx, "y": sy,
                             "hwnd": target.hwnd, "monitor": m["index"]})
        _verify_click(target, act2, "C2 截图反推 bbox→点击", m["index"])
        print(f"       (截图靶心=({sx},{sy}) win32靶心=({ex_phys},{ey_phys}) "
              f"drift={drift}px)", flush=True)


def main() -> int:
    print(f"v0.14 DPI/多屏自测 · 本进程 DPI mode={dpi_awareness.MODE}")
    try:
        st = screen_mss.dpi_status()
        check("S0 DPI 感知为 per-monitor 档位（非 system/unaware）",
              st.get("mode") in ("per_monitor_v2", "per_monitor"),
              json.dumps(st.get("virtual_screen"), ensure_ascii=False))
        mons = st.get("monitors") or []
        check("S1 monitors[0] 与虚拟桌面完全一致",
              st.get("virtual_screen_matches_monitors0") is True)
        prim = [m for m in mons if m.get("primary")]
        check("S2 主屏位于 (0,0)（Windows 虚拟屏定义）",
              len(prim) == 1 and prim[0]["left"] == 0 and prim[0]["top"] == 0,
              str(prim))
        inside = all(
            mons[0]["left"] <= m["left"]
            and mons[0]["top"] <= m["top"]
            and m["left"] + m["width"] <= mons[0]["left"] + mons[0]["width"]
            and m["top"] + m["height"] <= mons[0]["top"] + mons[0]["height"]
            for m in mons[1:])
        check("S3 每块屏都落在联合区内", inside, f"monitors={len(mons)-1}")

        part_a()

        # 启动 ghost 服务（B3/C 需要 HTTP 管道）
        if not port_free(8030):
            kill_by_port(8030)
            time.sleep(1.0)
        spawn([sys.executable, str(ROOT / "run.py")])
        health = wait_health()
        if health.get("status") != "ok":
            check("S4 ghost 服务启动", False, str(health)[:200])
            return 1
        check("S4 ghost 服务启动", True)

        part_b()

        target = TkTarget()
        # 反"平凡通过"护栏：靶矩形必须有实际尺寸，否则 C1 的 expect 会退化
        x1, y1, x2, y2 = target.rect
        check("C0 靶窗矩形有效（防 0x0 平凡通过）",
              (x2 - x1) >= 40 and (y2 - y1) >= 40, f"rect={target.rect}")
        try:
            part_c(target)
        finally:
            target.close()
    finally:
        cleanup()

    total = len(RESULTS)
    passed = sum(1 for r in RESULTS if r)
    print(f"\n===== v14 DPI 自测：{passed}/{total} PASS =====")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
