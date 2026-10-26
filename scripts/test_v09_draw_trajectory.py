"""v0.9.1 draw_trajectory 通用绘制接口验收。

三段：
  A 单元（零 GUI）：chaikin 端点保持 / 降采样上限 / 闭合语义；
     find_swatch 合成图（实心点命中、空心环拒绝、异色拒绝、#hex）。
  B 契约（自建 Ghost 服务 + 自建 Tk 靶窗，PID 差集）：401/400 门禁、
     平滑扩点与轨迹回显、色板 404（color_required）、fallback 如实标注。
  C 端到端（自建 mspaint，PID 差集）：快捷键 P 铅笔 → draw_trajectory
     color="blue"（色板点击经新接口）→ 画布蓝色像素验证 → 清场。

跑法：.venv\\Scripts\\python.exe scripts\\test_v09_draw_trajectory.py
"""
from __future__ import annotations

import base64
import ctypes
import io
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except OSError:
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import psutil  # noqa: E402
import requests  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402
import win32process  # noqa: E402
from PIL import Image  # noqa: E402

from app.core import config, trajectory  # noqa: E402

TOKEN = config.ensure_token()
RESULTS: list = []
PROCS: list = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  | {detail}" if detail else ""), flush=True)
    return ok


def port_free(port: int = 8030) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def spawn(args: list, drain: bool = False) -> subprocess.Popen:
    proc = subprocess.Popen(
        args, cwd=str(ROOT), stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        errors="replace")
    PROCS.append(proc)
    if drain:
        def _drain():
            try:
                for _ in proc.stdout:
                    pass
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=_drain, daemon=True).start()
    return proc


def wait_healthy(timeout: float = 30.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = requests.get("http://127.0.0.1:8030/api/ghost/health",
                             timeout=2)
            if r.status_code == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.4)
    return False


def kill_by_port(port: int) -> None:
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.laddr and c.laddr.port == port and c.pid:
                psutil.Process(c.pid).kill()
                time.sleep(1)
    except Exception as exc:  # noqa: BLE001
        print("  (kill_by_port: " + type(exc).__name__ + ")", flush=True)


def stop_proc(proc: subprocess.Popen | None, port: int | None = 8030) -> None:
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    if port and not port_free(port):
        kill_by_port(port)


def post(payload: dict, token: str | None = TOKEN,
         timeout: float = 60.0) -> dict:
    headers = {} if token is None else {"X-Ghost-Token": token}
    r = requests.post("http://127.0.0.1:8030/api/ghost/draw_trajectory",
                      headers=headers, json=payload, timeout=timeout)
    try:
        body = r.json()
    except ValueError:
        body = {"ok": False, "error": "BAD_JSON", "detail": r.text[:200]}
    body.setdefault("http_status", r.status_code)
    return body


# ---- A 单元 ----------------------------------------------------------------

def unit_checks() -> None:
    open_pts = trajectory.chaikin([(0, 0), (10, 0), (10, 10)], 2)
    check("A1 chaikin 开放曲线保持原端点",
          open_pts[0] == (0.0, 0.0) and open_pts[-1] == (10.0, 10.0),
          f"n={len(open_pts)}")
    closed_pts = trajectory.chaikin([(0, 0), (10, 0), (10, 10)], 1,
                                    closed=True)
    check("A2 chaikin 闭合曲线首尾相接",
          closed_pts[0] == closed_pts[-1], f"n={len(closed_pts)}")
    big = [(float(i), float(i % 7)) for i in range(5000)]
    ds = trajectory.downsample(big, 2000)
    check("A3 降采样 ≤2000 且保端点",
          len(ds) == 2000 and ds[0] == big[0] and ds[-1] == big[-1],
          f"n={len(ds)}")
    prep = trajectory.prepare_trajectory([[0, 0], [30, 10], [60, 0]],
                                         0, closed=False)
    check("A4 prepare 平滑 0 次=直通整型化",
          prep[:1] == [[0, 0]] and prep[-1:] == [[60, 0]],
          f"pts={prep}")
    prep_c = trajectory.prepare_trajectory([[0, 0], [30, 10], [60, 0]],
                                           2, closed=True)
    check("A5 prepare closed=True 首尾闭合",
          prep_c[0] == prep_c[-1], f"n={len(prep_c)}")
    prep_x = trajectory.prepare_trajectory(
        [[0, 0]] * 3 + [[6000, 3000]], 4)
    check("A6 prepare 输出 ≤2000 点", len(prep_x) <= 2000,
          f"n={len(prep_x)}")


def _synth_canvas():
    img = np.full((400, 600, 3), (90, 90, 90), dtype=np.uint8)
    return img


def unit_swatch_checks() -> None:
    img = _synth_canvas()
    cv2.circle(img, (150, 60), 12, (200, 120, 40), -1)   # 蓝 (BGR)
    cv2.circle(img, (300, 60), 12, (40, 40, 220), -1)    # 红
    cv2.circle(img, (450, 60), 12, (40, 200, 220), -1)   # 黄
    cv2.circle(img, (150, 140), 12, (200, 120, 40), 3)   # 蓝色空心环
    rect = (0, 0, 600, 400)
    hit = trajectory.find_swatch(img, rect, "blue")
    check("A7 find_swatch 命中实心蓝色点",
          hit is not None and abs(hit[0] - 150) <= 3 and abs(hit[1] - 60) <= 3,
          f"hit={hit}")
    ring = trajectory.find_swatch(
        img, (100, 100, 200, 180), "blue", region=(0.0, 0.0, 1.0, 1.0))
    check("A8 空心环被拒绝（选中态）", ring is None, f"ring={ring}")
    miss = trajectory.find_swatch(img, rect, "green")
    check("A9 异色查找如实 not found", miss is None, f"miss={miss}")
    hexhit = trajectory.find_swatch(img, rect, "#dc143c")
    check("A10 #hex 颜色命中红点",
          hexhit is not None and abs(hexhit[0] - 300) <= 3,
          f"hit={hexhit}")
    try:
        trajectory.resolve_color("chartreuse")
        check("A11 未知颜色名报错", False, "no exception")
    except ValueError:
        check("A11 未知颜色名报错", True)


# ---- B 契约（自建 Tk 靶窗）--------------------------------------------------

_tk_win = {"hwnd": 0}


def make_tk_window():
    import tkinter as tk

    win = tk.Tk()
    win.title("moray_draw_target")
    try:
        win.attributes("-topmost", True)
    except Exception:  # noqa: BLE001
        pass
    win.geometry("640x420+60+80")
    win.configure(bg="#d9d9d9")
    tk.Label(win, text="draw_trajectory target", font=("Arial", 14),
             bg="#d9d9d9").pack(pady=20)
    win.update()
    root = int(win.winfo_id())
    top = win32gui.GetAncestor(root, 2)  # GA_ROOT：Tk 顶层包装窗口（顶层=自身，只取一次）
    if top:
        root = int(top)
    _tk_win["hwnd"] = root
    _tk_win["tk"] = win
    return win


def force_foreground(hwnd: int) -> bool:
    for _ in range(4):
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
        time.sleep(0.25)
        if win32gui.GetForegroundWindow() == hwnd:
            return True
    return False


def contract_checks() -> None:
    caps = {"allowed_windows": ["python.exe"]}
    hwnd = _tk_win["hwnd"]
    force_foreground(hwnd)

    r = post({"points": [[100, 100], [200, 200]]}, token=None)
    check("B1 无 token → 401", r.get("http_status") == 401,
          f"status={r.get('http_status')}")
    r = post({"points": [[100, 100], [200, 200]]})
    check("B2 无 capabilities → 400 CAPABILITIES_REQUIRED",
          r.get("http_status") == 400
          and r.get("error") == "CAPABILITIES_REQUIRED",
          f"error={r.get('error')}")
    r = post({"points": [[100, 100]], "capabilities": caps})
    check("B3 points<2 → 400", r.get("http_status") == 400,
          f"error={r.get('error')}")
    r = post({"points": [[100, 100], [200, 200]], "color": "chartreuse",
              "capabilities": caps, "hwnd": hwnd})
    check("B4 未知颜色名 → 400", r.get("http_status") == 400,
          f"error={r.get('error')}")
    r = post({"points": [[100, 100], [200, 200]],
              "palette_region": [0.8, 0.0, 0.2, 0.5],
              "capabilities": caps, "hwnd": hwnd})
    check("B5 非法 palette_region → 400", r.get("http_status") == 400,
          f"error={r.get('error')}")

    r = post({"points": [[200, 300], [350, 250], [500, 320]],
              "hwnd": hwnd, "duration_ms": 700, "capabilities": caps})
    sent = ((r.get("stroke") or {}).get("points_sent"))
    check("B6 基础绘制 200 且平滑扩点",
          r.get("ok") is True and isinstance(sent, int) and sent >= 3,
          f"sent={sent} status={r.get('http_status')}")
    traj = ((r.get("result") or {}).get("trajectory")) or []
    check("B7 drag 轨迹回显与 stroke 一致",
          len(traj) == sent and r.get("window", {}).get("process")
          == "python.exe",
          f"traj={len(traj)} proc={r.get('window', {}).get('process')}")

    r = post({"points": [[200, 300], [350, 250], [500, 320]],
              "color": "blue", "color_required": True, "hwnd": hwnd,
              "capabilities": caps})
    check("B8 色板未找到 + color_required → 404 COLOR_SWATCH_NOT_FOUND",
          r.get("http_status") == 404
          and r.get("error") == "COLOR_SWATCH_NOT_FOUND",
          f"error={r.get('error')}")
    r = post({"points": [[200, 300], [350, 250], [500, 320]],
              "color": "blue", "color_required": False, "hwnd": hwnd,
              "capabilities": caps})
    ci = r.get("color") or {}
    check("B9 fallback 如实标注（found=False + 仍落笔 200）",
          r.get("ok") is True and ci.get("found") is False
          and ci.get("fallback") is True,
          f"color={json.dumps(ci, ensure_ascii=False)[:120]}")


# ---- C 端到端（自建 Paint）--------------------------------------------------

def paint_pids() -> set:
    out = set()
    for p in psutil.process_iter(["pid", "name"]):
        if str(p.info.get("name") or "").lower() == "mspaint.exe":
            out.add(int(p.info["pid"]))
    return out


def kill_tree(proc: subprocess.Popen) -> None:
    try:
        root = psutil.Process(proc.pid)
        procs = root.children(recursive=True) + [root]
    except psutil.Error:
        procs = []
    for q in procs:
        try:
            q.kill()
        except psutil.Error:
            pass


def list_windows():
    out = []

    def _cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd) or ""
        if title:
            out.append((hwnd, title))
        return True
    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:  # noqa: BLE001
        pass
    return out


def process_name(hwnd: int) -> str:
    try:
        _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        return str(psutil.Process(pid).name()).lower()
    except Exception:  # noqa: BLE001
        return ""


def find_paint_window(before_pids: set, paint_proc: subprocess.Popen,
                      timeout: float = 20.0):
    allowed = {int(paint_proc.pid)}
    try:
        allowed |= {c.pid for c in psutil.Process(
            paint_proc.pid).children(recursive=True)}
    except psutil.Error:
        pass
    end = time.time() + timeout
    while time.time() < end:
        for hwnd, title in list_windows():
            if process_name(hwnd) != "mspaint.exe":
                continue
            try:
                _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:  # noqa: BLE001
                continue
            rect = win32gui.GetWindowRect(hwnd)
            if rect[2] - rect[0] <= 300:
                continue
            if int(pid) in allowed and int(pid) not in before_pids:
                return {"hwnd": hwnd, "title": title, "rect": rect}
        time.sleep(0.4)
    return None


def shot_png() -> "np.ndarray":
    r = requests.get("http://127.0.0.1:8030/api/ghost/screenshot",
                     params={"monitor": 1, "fmt": "png", "scale": 1.0},
                     timeout=30)
    r.raise_for_status()
    img = Image.open(io.BytesIO(base64.b64decode(r.json()["image_b64"])))
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def detect_canvas(img, rect):
    l, t, r, b = rect
    l, t = max(0, l), max(0, t)
    crop = img[t:b, l:r]
    if crop.size == 0:
        return None
    h, w = crop.shape[:2]
    y0 = int(h * 0.18)
    sub = crop[y0:, :]
    gray = cv2.cvtColor(sub, cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(gray, 245, 255, cv2.THRESH_BINARY)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    best = None
    for c in contours:
        x, y, cw, ch = cv2.boundingRect(c)
        if cw < 80 or ch < 60:
            continue
        if cw * ch < 0.06 * w * h:
            continue
        if best is None or cw * ch > best[2] * best[3]:
            best = (x, y, cw, ch)
    if best is None:
        return None
    x, y, cw, ch = best
    return [l + x, t + y0 + y, cw, ch]


def paint_e2e() -> int:
    caps = {"allowed_windows": ["mspaint.exe"]}
    hwnd = 0
    paint = None
    existing = paint_pids()
    if existing:
        print("[SKIP] 检测到既有 mspaint.exe %s；红线只碰自建窗口。"
              % sorted(existing), flush=True)
        return 0
    before = {p.pid for p in psutil.process_iter(["pid"])}
    paint = spawn(["mspaint.exe"])
    win = find_paint_window(before, paint)
    ok = win is not None
    check("C1 自建画图窗口（PID 差集绑定）", ok,
          f"title={(win or {}).get('title')!r}")
    if not ok:
        return 1
    hwnd = win["hwnd"]
    check("C2 画图强占前台", force_foreground(hwnd))
    time.sleep(0.6)

    r = requests.post("http://127.0.0.1:8030/api/ghost/execute_action",
                      headers={"X-Ghost-Token": TOKEN},
                      json={"action": "hotkey", "text": "p", "hwnd": hwnd,
                            "settle_seconds": 0.2, "capabilities": caps},
                      timeout=60)
    check("C3 快捷键 P（铅笔）", r.status_code == 200,
          f"status={r.status_code}")

    img0 = shot_png()
    canvas = detect_canvas(img0, win32gui.GetWindowRect(hwnd))
    check("C4 服务端像素检测画布", canvas is not None,
          f"canvas={canvas}")
    if not canvas:
        return 1
    cx, cy, cw, ch = canvas
    mx, my = int(cw * 0.06), int(ch * 0.08)
    bx, by = cx + mx, cy + my
    bw, bh = cw - 2 * mx, ch - 2 * my
    # 一条海浪：正弦波控制点 → 服务端平滑后落笔（蓝色）
    ctrl = [[bx + int(bw * (0.08 + 0.84 * i / 20)),
             by + int(bh * (0.62 + 0.05 * np.sin(i * 1.6)))]
            for i in range(21)]
    r = post({"points": ctrl, "color": "blue", "hwnd": hwnd,
              "duration_ms": 1600, "capabilities": caps})
    ci = r.get("color") or {}
    check("C5 draw_trajectory+取色 200（色板命中并点击）",
          r.get("ok") is True and ci.get("found") is True
          and ci.get("clicked") is True,
          f"color={json.dumps(ci, ensure_ascii=False)[:160]} "
          f"sent={(r.get('stroke') or {}).get('points_sent')}")

    time.sleep(0.4)
    img1 = shot_png()
    l, t = max(0, cx), max(0, cy)
    rr = min(img1.shape[1], cx + cw)
    bb = min(img1.shape[0], cy + ch)
    diff = cv2.absdiff(img0[t:bb, l:rr], img1[t:bb, l:rr])
    gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(gray, 14, 255, cv2.THRESH_BINARY)
    count = int(cv2.countNonZero(mask))
    check("C6 画布产生真实笔迹（≥800px）", count >= 800, f"changed={count}px")
    bgr = img1[t:bb, l:rr][mask > 0]
    blue = int(np.sum((bgr[:, 0] > bgr[:, 2] + 30)
                      & (bgr[:, 0] > bgr[:, 1] + 20))) if len(bgr) else 0
    check("C7 笔迹为蓝色（取色生效）", blue >= 300, f"blue_pixels={blue}")
    cv2.imwrite(str(ROOT / "data" / "screenshots" / "draw_trajectory_e2e.png"),
                img1)

    kill_tree(paint)
    try:
        paint.wait(timeout=8)
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.5)
    check("C8 清场：自建画图窗口已销毁", not win32gui.IsWindow(hwnd))
    return 0


def main() -> int:
    print("== moray-ghost v0.9.1 draw_trajectory acceptance ==", flush=True)
    srv = None
    tk_win = None
    try:
        check("0 前提：端口 8030 空闲", port_free())
        unit_checks()
        unit_swatch_checks()
        srv = spawn([sys.executable, "run.py"], drain=True)
        check("0 前提：Ghost 服务启动", wait_healthy())
        if srv.poll() is not None:
            return 1
        tk_win = make_tk_window()
        time.sleep(0.4)
        contract_checks()
        paint_e2e()
    finally:
        if tk_win is not None:
            try:
                tk_win.destroy()
            except Exception:  # noqa: BLE001
                pass
        if srv is not None:
            stop_proc(srv)
        check("9 清场：服务停止、端口释放", port_free())
    passed = sum(1 for x in RESULTS if x)
    print(f"\n== RESULT: {passed}/{len(RESULTS)} passed ==", flush=True)
    return len(RESULTS) - passed


if __name__ == "__main__":
    sys.exit(main())
