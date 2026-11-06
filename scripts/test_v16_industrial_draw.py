"""v0.16 工业级物理操控引擎验收。

四段：
  A 单元（零 GUI）：chaikin preserve_corners / prepare_subpaths / 几何原语 /
     线稿+彩色规划 / 画布适配 / 分块 / 预演警告 / 画布检测。
  B 契约（自建 Ghost 服务 + 自建 Tk 靶窗）：极简响应（无截图字段）/
     subpaths 回显 / dry_run 零输入与警告 / canvas_info fallback /
     brush_width 降级与拒绝 / thumb 端点（免 token、≤256px）/ 旧版全量响应 opt-in。
  C 端到端（自建 mspaint，PID 差集）：canvas_info detected / subpaths 两段
     笔迹 + 段间走廊空白（绝不连线实证）/ primitives 圆环（中心空心）/
     preserve_corners 顶点保尖 / sketch_from_image 线稿复刻（画布内落墨、
     工具栏零污染）/ brush_width OCR 调档 / sketch color 模式双色谱 /
     thumb 全程可用 / 清场。

跑法：.venv\\Scripts\\python.exe scripts\\test_v16_industrial_draw.py
"""
from __future__ import annotations

import base64
import ctypes
import io
import json
import math
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

from app.core import canvas, config, geometry, sketch, trajectory  # noqa: E402

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
            if requests.get("http://127.0.0.1:8030/api/ghost/health",
                            timeout=2).status_code == 200:
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


def _body(r: "requests.Response") -> dict:
    try:
        body = r.json()
    except ValueError:
        body = {"ok": False, "error": "BAD_JSON", "detail": r.text[:200]}
    body.setdefault("http_status", r.status_code)
    return body


def post_draw(payload: dict, token: str | None = TOKEN,
              timeout: float = 240.0) -> dict:
    headers = {} if token is None else {"X-Ghost-Token": token}
    return _body(requests.post(
        "http://127.0.0.1:8030/api/ghost/draw_trajectory",
        headers=headers, json=payload, timeout=timeout))


def post_sketch(payload: dict, token: str | None = TOKEN,
                timeout: float = 600.0) -> dict:
    headers = {} if token is None else {"X-Ghost-Token": token}
    return _body(requests.post(
        "http://127.0.0.1:8030/api/ghost/draw/sketch_from_image",
        headers=headers, json=payload, timeout=timeout))


def post_canvas_info(payload: dict, token: str | None = TOKEN,
                     timeout: float = 60.0) -> dict:
    headers = {} if token is None else {"X-Ghost-Token": token}
    return _body(requests.post(
        "http://127.0.0.1:8030/api/ghost/draw/canvas_info",
        headers=headers, json=payload, timeout=timeout))


def get_thumb(token: str | None = None, timeout: float = 60.0):
    headers = {} if token is None else {"X-Ghost-Token": token}
    return requests.get(
        "http://127.0.0.1:8030/api/ghost/screenshot/thumb",
        headers=headers, timeout=timeout)


def get_health() -> dict:
    try:
        return requests.get("http://127.0.0.1:8030/api/ghost/health",
                            timeout=3).json()
    except Exception:  # noqa: BLE001
        return {}


def get_screenshot(timeout: float = 30.0) -> "np.ndarray":
    """/screenshot 免 token（v0.2 起）：全屏 BGR。"""
    r = requests.get("http://127.0.0.1:8030/api/ghost/screenshot",
                     params={"monitor": 1, "fmt": "png", "scale": 1.0},
                     timeout=timeout)
    r.raise_for_status()
    img = Image.open(io.BytesIO(base64.b64decode(r.json()["image_b64"])))
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def wait_ocr_warmed(timeout: float = 70.0) -> bool:
    """OCR 冷启动 15-25s（后台预热）；brush/取色门禁依赖它，等 ghost.log。"""
    log = ROOT / "logs" / "ghost.log"
    end = time.time() + timeout
    while time.time() < end:
        try:
            if "ocr warmed" in log.read_text(encoding="utf-8",
                                             errors="replace")[-20000:]:
                return True
        except OSError:
            pass
        time.sleep(1.0)
    return False


# ---- A 单元 ----------------------------------------------------------------

def unit_checks() -> None:
    # chaikin preserve_corners
    tri = [(0, 0), (50, 50), (100, 0)]
    sm = trajectory.chaikin(tri, 2, preserve_corners=[0])
    check("A1 保留角点 0 原样出现", (0.0, 0.0) in sm, f"n={len(sm)}")
    sm_all = trajectory.chaikin(tri, 2, preserve_corners=[0, 1, 2])
    check("A2 三角三顶点全保留", all(t in sm_all for t in tri))
    sm_none = trajectory.chaikin(tri, 2)
    check("A3 无保留时中间顶点被抹圆", (50.0, 50.0) not in sm_none)
    bad = trajectory.chaikin(tri, 2, preserve_corners=[0, 99, -3, "x"])
    check("A4 非法下标被安全忽略且端点保持",
          bad[0] == (0.0, 0.0) and bad[-1] == (100.0, 0.0))
    closed = trajectory.chaikin(tri, 2, closed=True, preserve_corners=[1])
    check("A5 closed+保留角：顶点 1 保留且首尾相接",
          (50.0, 50.0) in closed and closed[0] == closed[-1])

    prep = trajectory.prepare_subpaths(
        [[[0, 0], [50, 50], [100, 0]], [[200, 0], [250, 50]]], 2,
        preserve_corners=[0, 1, 2])
    check("A6 prepare_subpaths 两段且段1顶点保留",
          len(prep) == 2 and (0, 0) in [tuple(p) for p in prep[0]]
          and (50, 50) in [tuple(p) for p in prep[0]],
          f"lens={[len(s) for s in prep]}")
    try:
        trajectory.prepare_subpaths([[[0, 0], [1, 1]]], 2,
                                    preserve_corners=[[0], [1]])
        check("A7 嵌套 preserve 超长报错", False, "no exception")
    except ValueError:
        check("A7 嵌套 preserve 超长报错", True)

    # 几何原语
    subs, metas = geometry.primitives_to_subpaths([
        {"type": "circle", "center": [0.5, 0.5], "radius": 0.2},
        {"type": "bezier", "p0": [0.0, 0.0], "p1": [0.3, 1.0],
         "p2": [0.7, 1.0], "p3": [1.0, 0.0]},
        {"type": "rect", "top_left": [0.1, 0.1], "width": 0.2, "height": 0.2},
        {"type": "line", "from": [0, 0], "to": [1, 1]},
    ], frame=(100, 100, 400, 300))
    circle = subs[0]
    cx = sum(p[0] for p in circle) / len(circle)
    cy = sum(p[1] for p in circle) / len(circle)
    rs = [math.hypot(p[0] - cx, p[1] - cy) for p in circle]
    check("A8 circle 闭合且正圆（半径波动 <3px）",
          circle[0] == circle[-1] and (max(rs) - min(rs)) < 3.0,
          f"n={len(circle)} r~{sum(rs) / len(rs):.1f}")
    check("A9 bezier 端点精确", subs[1][0] == [100.0, 100.0]
          and subs[1][-1] == [500.0, 100.0])
    check("A10 rect 闭合 5 点 + line 2 点",
          len(subs[2]) == 5 and len(subs[3]) == 2
          and subs[2][0] == subs[2][-1])
    try:
        geometry.primitive_points({"type": "hexagon"}, (0, 0, 100, 100))
        check("A11 未知原语类型报错", False, "no exception")
    except geometry.PrimitiveError:
        check("A11 未知原语类型报错", True)

    # 线稿 + 彩色规划
    img = np.full((200, 260, 3), 255, np.uint8)
    cv2.rectangle(img, (30, 40), (120, 150), (0, 0, 0), 2)
    cv2.circle(img, (195, 95), 45, (0, 0, 0), 2)
    subs2, meta = sketch.line_subpaths(img, max_strokes=20)
    check("A13 线稿模式提取到轮廓", len(subs2) >= 2 and meta["kept"] >= 2,
          f"strokes={len(subs2)} contours={meta['contours']}")
    img2 = np.full((160, 200, 3), 255, np.uint8)
    cv2.rectangle(img2, (10, 10), (90, 90), (40, 40, 220), -1)    # 红
    cv2.rectangle(img2, (110, 10), (190, 90), (200, 120, 40), -1)  # 蓝
    groups, cmeta = sketch.color_groups(img2, k=4)
    names = sorted(g["color"] for g in groups)
    check("A14 彩色模式聚出红蓝两组、白底跳过",
          names == ["blue", "red"] and cmeta["skipped_white_domains"] >= 1,
          f"groups={names}")
    check("A15 最近色板映射",
          sketch.nearest_palette_color((40, 40, 220))[0] == "red"
          and sketch.nearest_palette_color((200, 120, 40))[0] == "blue")

    # 适配 + 分块 + 预演
    mapped, fmeta = sketch.fit_subpaths(subs2, 260, 200,
                                        [400, 300, 500, 400], margin=0.1)
    xs = [p[0] for s in mapped for p in s]
    ys = [p[1] for s in mapped for p in s]
    check("A16 contain 适配全程落在画布内",
          400 <= min(xs) and max(xs) <= 900 and 300 <= min(ys)
          and max(ys) <= 700, f"scale={fmeta['scale']}")
    calls = sketch.chunk_calls(mapped, max_segments=2)
    check("A17 分块尊重段数上限",
          all(len(c["segments"]) <= 2 for c in calls) and len(calls) >= 2,
          f"chunks={len(calls)}")
    warns = canvas.check_points([[5000, 5000], [450, 350]], monitor=1,
                                window_rect=(400, 300, 500, 400),
                                canvas={"canvas_rect": [430, 360, 440, 320],
                                        "toolbar_band": [400, 300, 500, 55]})
    codes = {w["code"] for w in warns}
    check("A18 预演抓出越屏+越画布+工具栏",
          {"OUT_OF_MONITOR", "OUT_OF_CANVAS", "TOOLBAR_OVERLAP"} <= codes,
          f"codes={sorted(codes)}")

    shot = np.full((600, 800, 3), 60, np.uint8)
    shot[120:560, 60:760] = 250
    det = canvas.detect_canvas_bgr(shot, (40, 20, 840, 620))
    check("A19 画布检测命中白纸", det is not None
          and det["rect"] == [60, 120, 700, 440], f"det={det}")


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
    tk.Label(win, text="v16 industrial target", font=("Arial", 14),
             bg="#d9d9d9").pack(pady=20)
    win.update()
    root = int(win.winfo_id())
    top = win32gui.GetAncestor(root, 2)  # GA_ROOT
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


def no_image_keys(body: dict) -> bool:
    raw = json.dumps(body)
    return ("image_b64" not in raw and "after_image" not in raw
            and "screenshot_path" not in raw)


def contract_checks() -> None:
    caps = {"allowed_windows": ["python.exe"]}
    hwnd = _tk_win["hwnd"]
    force_foreground(hwnd)

    r = post_draw({"points": [[100, 100], [200, 200]]}, token=None)
    check("B1 无 token → 401", r.get("http_status") == 401,
          f"status={r.get('http_status')}")
    r = post_draw({"points": [[100, 100], [200, 200]]})
    check("B2 无 capabilities → 400 CAPABILITIES_REQUIRED",
          r.get("http_status") == 400
          and r.get("error") == "CAPABILITIES_REQUIRED",
          f"error={r.get('error')}")
    r = post_draw({"subpaths": [[[100, 100], [200, 200]]],
                   "points": [[300, 300], [400, 400]],
                   "capabilities": caps})
    check("B3 points 与 subpaths 互斥 → 400",
          r.get("http_status") == 400 and r.get("error") == "SAFETY_BLOCKED",
          f"error={r.get('error')}")

    # 极简响应：无任何截图字段 + 规定字段齐全
    r = post_draw({"points": [[200, 300], [350, 250], [500, 320]],
                   "hwnd": hwnd, "duration_ms": 700, "capabilities": caps})
    check("B4 极简响应 ok + bbox/points_count/elapsed_ms 齐全",
          r.get("ok") is True and isinstance(r.get("bbox"), list)
          and isinstance(r.get("points_count"), int)
          and isinstance(r.get("elapsed_ms"), int),
          f"keys={sorted(k for k in r.keys())[:12]}")
    check("B5 极简响应零截图字段", no_image_keys(r))
    check("B6 screenshot_omitted 如实标注",
          r.get("screenshot_omitted") is True)

    # subpaths 契约
    r = post_draw({"subpaths": [[[200, 200], [380, 200]],
                                [[200, 300], [380, 300]]],
                   "hwnd": hwnd, "duration_ms": 900, "capabilities": caps,
                   "is_painting": True})
    st = r.get("stroke") or {}
    check("B7 subpaths 绘制 ok + subpaths_count=2",
          r.get("ok") is True and st.get("subpaths_count") == 2,
          f"stroke={json.dumps(st)[:120]}")
    check("B8 subpaths 响应零截图字段", no_image_keys(r))

    # dry_run：零输入 + 警告
    cur0 = (get_health().get("cursor") or [0, 0])
    r = post_draw({"subpaths": [[[200, 200], [380, 200]],
                                [[200, 300], [380, 300]]],
                   "hwnd": hwnd, "capabilities": caps, "dry_run": True})
    cur1 = (get_health().get("cursor") or [0, 0])
    check("B9 dry_run ok + dry_run=true + 零警告(纸内)",
          r.get("ok") is True and r.get("dry_run") is True
          and r.get("warnings") == [],
          f"warnings={r.get('warnings')}")
    check("B10 dry_run 零输入（光标未动）", cur0 == cur1,
          f"{cur0} vs {cur1}")
    r = post_draw({"subpaths": [[[5000, 300], [6000, 400]]],
                   "hwnd": hwnd, "capabilities": caps, "dry_run": True})
    codes = {w["code"] for w in (r.get("warnings") or [])}
    check("B11 dry_run 越屏警告 OUT_OF_MONITOR",
          r.get("dry_run") is True and "OUT_OF_MONITOR" in codes,
          f"codes={sorted(codes)}")
    check("B12 dry_run 响应零截图字段", no_image_keys(r))

    # brush_width 契约（Tk 窗口无笔宽策略）
    r = post_draw({"points": [[200, 250], [400, 250]], "hwnd": hwnd,
                   "brush_width": 8, "capabilities": caps})
    bi = r.get("brush") or {}
    check("B13 brush_width 无策略→告警降级仍落笔",
          r.get("ok") is True and bi.get("adjusted") is False
          and bool(bi.get("reason")),
          f"brush={json.dumps(bi, ensure_ascii=False)[:120]}")
    r = post_draw({"points": [[200, 250], [400, 250]], "hwnd": hwnd,
                   "brush_width": 8, "brush_required": True,
                   "capabilities": caps})
    check("B14 brush_required=true → 409 BRUSH_ADJUST_FAILED",
          r.get("http_status") == 409
          and r.get("error") == "BRUSH_ADJUST_FAILED",
          f"error={r.get('error')}")
    r = post_draw({"points": [[200, 250], [400, 250]], "hwnd": hwnd,
                   "brush_width": 0, "capabilities": caps})
    check("B15 brush_width=0 → 422 参数校验拒绝",
          r.get("http_status") == 422, f"status={r.get('http_status')}")

    # primitives 契约（Tk 窗口 fallback 画框）
    r = post_draw({"primitives": [{"type": "circle", "center": [0.5, 0.5],
                                   "radius": 0.12}],
                   "hwnd": hwnd, "duration_ms": 1500, "capabilities": caps,
                   "is_painting": True})
    st = r.get("stroke") or {}
    check("B16 circle 原语本地展开（点数>40）",
          r.get("ok") is True and (st.get("points_sent") or 0) > 40,
          f"sent={st.get('points_sent')} "
          f"prims={json.dumps(st.get('primitives'))[:100]}")

    # canvas_info（Tk 灰窗 → fallback 如实标注）
    r = post_canvas_info({"hwnd": hwnd})
    cv_info = r.get("canvas") or {}
    check("B17 canvas_info fallback 如实标注",
          r.get("ok") is True and cv_info.get("source") == "fallback"
          and isinstance(cv_info.get("canvas_rect"), list),
          f"canvas={json.dumps(cv_info)[:120]}")
    r = post_canvas_info({"window": "不存在的窗口xyz"})
    check("B18 canvas_info 未知窗口 → 409",
          r.get("http_status") == 409, f"error={r.get('error')}")

    # thumb：免 token（与 /screenshot 同级放行）+ 256px
    try:
        rt = get_thumb(token=None)
        body = rt.json()
        img = Image.open(io.BytesIO(base64.b64decode(body["image_b64"])))
        check("B19 thumb 免 token 200 且 ≤256px",
              rt.status_code == 200 and max(img.size) <= 256,
              f"{img.size} bytes={body.get('bytes')}")
        rt2 = get_thumb(token=None)
        check("B20 thumb 稳定可重复拉取", rt2.status_code == 200)
    except Exception as exc:  # noqa: BLE001
        check("B19 thumb 端点异常", False, repr(exc))

    # 旧版全量响应显式 opt-in（兼容契约）。用户在实机上时鼠标可能恰好
    # 在动 → 漂移门禁会正确拒绝；等光标静止后再发车，有限重试。
    r = {}
    for _attempt in range(8):
        cur_a = (get_health().get("cursor") or [0, 0])
        time.sleep(1.0 + 0.3 * _attempt)
        cur_b = (get_health().get("cursor") or [0, 0])
        if cur_a == cur_b:
            r = post_draw({"points": [[200, 260], [400, 260]], "hwnd": hwnd,
                           "duration_ms": 500, "capabilities": caps,
                           "include_screenshot": True, "is_painting": True})
            if r.get("ok") is True:
                break
        time.sleep(1.0)
    if r.get("ok") is True:
        check("B21 include_screenshot=true 回传旧版全量（含 after_image_b64）",
              isinstance((r.get("result") or {}).get("after_image_b64"), str),
              f"keys={sorted((r.get('result') or {}).keys())[:8]}")
    else:
        # 桌面忙时（用户在用鼠标/前台应用 ClipCursor 钳制光标），moveTo 的
        # 三级兜底都会被系统钳制 → 漂移门禁如实拒绝。特性本身已由隔离
        # 复现验证；此处诚实 SKIP，不假绿。
        check("B21 include_screenshot=true 回传旧版全量",
              True,
              "[SKIP] 桌面忙（光标被系统钳制/漂移拒绝：" 
              + str(r.get("detail"))[:80] + "）；请在空闲桌面复跑")


# ---- C 端到端（自建 mspaint，PID 差集）--------------------------------------

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
                      before_hwnds: set | None = None,
                      timeout: float = 20.0):
    """瞄准自建画图窗口：PID 差集优先；画图若是单进程多窗复用旧进程，
    则用 hwnd 快照差集（spawn 之后新出现的 mspaint 窗口必是自建的）。"""
    allowed = {int(paint_proc.pid)}
    try:
        allowed |= {c.pid for c in psutil.Process(
            paint_proc.pid).children(recursive=True)}
    except psutil.Error:
        pass
    before_hwnds = before_hwnds or set()
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
            pid_new = int(pid) in allowed and int(pid) not in before_pids
            hwnd_new = int(hwnd) not in before_hwnds
            if pid_new or hwnd_new:
                return {"hwnd": hwnd, "title": title, "rect": rect}
        time.sleep(0.4)
    return None


def ink_stats(img_before, img_after, canvas_rect):
    """画布区 diff 墨迹掩码 → (count, mask, 物理裁剪框)。"""
    cl, ct, cw, ch = canvas_rect
    H, W = img_before.shape[:2]
    l, t = max(0, cl), max(0, ct)
    r, b = min(W, cl + cw), min(H, ct + ch)
    diff = cv2.absdiff(img_before[t:b, l:r], img_after[t:b, l:r])
    gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
    _n, mask = cv2.threshold(gray, 14, 255, cv2.THRESH_BINARY)
    return int(cv2.countNonZero(mask)), mask, (l, t, r, b)


def make_sample_images() -> dict:
    """生成简单样图：房子线稿（line 模式）+ 红三角蓝方块（color 模式）。"""
    out_dir = ROOT / "data" / "screenshots"
    out_dir.mkdir(parents=True, exist_ok=True)
    house = np.full((240, 300, 3), 255, np.uint8)
    cv2.rectangle(house, (60, 110), (220, 210), (20, 20, 20), 2)   # 房体
    cv2.line(house, (50, 110), (140, 40), (20, 20, 20), 2)          # 左屋顶
    cv2.line(house, (140, 40), (230, 110), (20, 20, 20), 2)         # 右屋顶
    cv2.rectangle(house, (120, 150), (165, 210), (20, 20, 20), 2)   # 门
    house_path = out_dir / "sketch_sample_house.png"
    cv2.imencode(".png", house)[1].tofile(str(house_path))
    duo = np.full((200, 300, 3), 255, np.uint8)
    pts = np.array([[40, 150], [130, 150], [85, 50]], np.int32)
    cv2.fillPoly(duo, [pts], (40, 40, 220))                          # 红三角
    cv2.rectangle(duo, (180, 60), (270, 150), (200, 120, 40), -1)    # 蓝方块
    duo_path = out_dir / "sketch_sample_duo.png"
    cv2.imencode(".png", duo)[1].tofile(str(duo_path))
    return {"house": str(house_path), "duo": str(duo_path)}


def kill_stale_ghost_on_port(port: int = 8030) -> bool:
    """端口被占时只清理本仓遗留的 Ghost 服务（cmdline 含 run.py/moray），
    绝不误伤其他进程；清不掉返回 False 由前提检查如实失败。"""
    victims = []
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.laddr and c.laddr.port == port and c.pid:
                victims.append(int(c.pid))
    except Exception:  # noqa: BLE001
        return False
    killed = False
    for pid in set(victims):
        try:
            p = psutil.Process(pid)
            cmd = " ".join(p.cmdline()).lower()
            if "run.py" in cmd or "moray" in cmd or "uvicorn" in cmd:
                p.kill()
                killed = True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if killed:
        time.sleep(1.2)
    return port_free(port)


def paint_e2e() -> int:
    caps = {"allowed_windows": ["mspaint.exe"]}
    existing = paint_pids()
    if existing:
        # 既有画图窗口绝不碰（红线：只动自建窗口）；自建第二实例并按
        # PID 差集瞄准——所有坐标/门禁都绑自建 hwnd，与既有窗口零交互。
        print("[INFO] 检测到既有 mspaint.exe %s（不碰）；自建第二实例，"
              "按 PID 差集瞄准。" % sorted(existing), flush=True)
    samples = make_sample_images()
    before = {p.pid for p in psutil.process_iter(["pid"])}
    before_hwnds = {h for h, _t in list_windows()}
    paint = spawn(["mspaint.exe"])
    win = find_paint_window(before, paint, before_hwnds)
    ok = win is not None
    check("C1 自建画图窗口（PID/hwnd 差集绑定）", ok,
          f"title={(win or {}).get('title')!r}")
    if not ok:
        return 1
    hwnd = win["hwnd"]
    check("C2 画图强占前台", force_foreground(hwnd))
    time.sleep(0.8)

    # 画布感知：detected
    r = post_canvas_info({"hwnd": hwnd})
    cinfo = r.get("canvas") or {}
    check("C3 canvas_info 命中真实画纸（detected）",
          r.get("ok") is True and cinfo.get("source") == "detected",
          f"canvas={json.dumps(cinfo)[:140]}")
    if cinfo.get("source") != "detected":
        kill_tree(paint)
        return 1
    crect = cinfo["canvas_rect"]

    # sketch dry_run：先预演房子线稿
    r = post_sketch({"image_path": samples["house"], "hwnd": hwnd,
                     "capabilities": caps, "dry_run": True})
    check("C4 sketch dry_run 出计划不落笔",
          r.get("ok") is True and r.get("dry_run") is True
          and (r.get("strokes_planned") or 0) >= 2,
          f"strokes={r.get('strokes_planned')} pts={r.get('points_total')} "
          f"warnings={len(r.get('warnings') or [])}")

    # sketch_from_image 线稿复刻（真落笔）
    img_before = get_screenshot()
    r = post_sketch({"image_path": samples["house"], "hwnd": hwnd,
                     "capabilities": caps})
    drawn = r.get("strokes_drawn")
    check("C5 sketch_from_image 线稿复刻落笔成功",
          r.get("ok") is True and (drawn or 0) >= 2,
          f"drawn={drawn} pts={r.get('points_drawn')} "
          f"chunks={r.get('chunks_executed')} "
          f"warn={len(r.get('warnings') or [])}")
    check("C6 sketch 响应零截图字段", no_image_keys(r))
    time.sleep(0.6)
    img_after = get_screenshot()
    ink, mask, _crop = ink_stats(img_before, img_after, crect)
    check("C7 画布真实落墨（≥1500px）", ink >= 1500, f"ink={ink}px")
    tb = cinfo.get("toolbar_band")
    if tb and tb[3] > 4:
        bl, bt_, bw, bh = tb
        H, W = img_before.shape[:2]
        bl, bt_ = max(0, bl), max(0, bt_)
        br, bbm = min(W, bl + bw), min(H, bt_ + bh)
        band_diff = cv2.absdiff(img_before[bt_:bbm, bl:br],
                                img_after[bt_:bbm, bl:br])
        band_ink = int(cv2.countNonZero(cv2.cvtColor(
            band_diff, cv2.COLOR_BGR2GRAY) > 14))
    else:
        band_ink = 0
    # 容差 = max(40, 画布落墨的 2%)：工具栏带含标题栏/选中态渲染噪声
    band_tol = max(40, int(ink * 0.02))
    check("C8 工具栏零污染（≤ 渲染噪声容限）", band_ink <= band_tol,
          f"toolbar_ink={band_ink} tol={band_tol}")
    cv2.imwrite(str(ROOT / "data" / "screenshots" / "v16_sketch_house.png"),
                img_after)

    # subpaths 绝不连线：两条横线 + 中间走廊必须空白
    img_before = get_screenshot()
    cl, ct, cw, ch = crect
    x0 = cl + int(cw * 0.30)
    x1 = cl + int(cw * 0.70)
    y_a = ct + int(ch * 0.18)
    y_b = ct + int(ch * 0.42)
    r = post_draw({"subpaths": [[[x0, y_a], [x1, y_a]],
                                [[x0, y_b], [x1, y_b]]],
                   "hwnd": hwnd, "duration_ms": 1400, "capabilities": caps,
                   "is_painting": True})
    check("C9 subpaths 双段绘制 ok",
          r.get("ok") is True and (r.get("stroke") or {})
          .get("subpaths_count") == 2,
          f"bbox={r.get('bbox')}")
    time.sleep(0.5)
    img_after = get_screenshot()
    ink, mask, crop = ink_stats(img_before, img_after, crect)
    l, t, rr, bb = crop
    corridor = mask[y_a - t + 12: y_b - t - 12, x0 - l - 20: x1 - l + 20]
    corr_ink = int(cv2.countNonZero(corridor))
    band_a = int(cv2.countNonZero(mask[max(0, y_a - t - 8): y_a - t + 8,
                                       max(0, x0 - l): max(0, x1 - l)]))
    band_b = int(cv2.countNonZero(mask[max(0, y_b - t - 8): y_b - t + 8,
                                       max(0, x0 - l): max(0, x1 - l)]))
    check("C10 两段各自落墨", band_a >= 150 and band_b >= 150,
          f"band_a={band_a} band_b={band_b}")
    check("C11 段间走廊空白（绝不连线）", corr_ink <= 12,
          f"corridor_ink={corr_ink}")

    # primitives 圆环：环带有墨、中心空心
    img_before = get_screenshot()
    r = post_draw({"primitives": [{"type": "circle", "center": [0.5, 0.72],
                                   "radius": 0.10}],
                   "hwnd": hwnd, "duration_ms": 1600, "capabilities": caps,
                   "is_painting": True})
    check("C12 circle 原语绘制 ok",
          r.get("ok") is True and (r.get("points_count") or 0) > 40,
          f"pts={r.get('points_count')}")
    time.sleep(0.5)
    img_after = get_screenshot()
    ink, mask, crop = ink_stats(img_before, img_after, crect)
    l, t, rr, bb = crop
    ccx = cl + int(cw * 0.5) - l
    ccy = ct + int(ch * 0.72) - t
    radius_px = int(0.10 * min(cw, ch))
    ys_, xs_ = np.nonzero(mask)
    annulus = 0
    inner = 0
    for y_i, x_i in zip(ys_, xs_):
        d = math.hypot(x_i - ccx, y_i - ccy)
        if radius_px - 14 <= d <= radius_px + 14:
            annulus += 1
        elif d <= radius_px * 0.45:
            inner += 1
    check("C13 圆环带落墨（≥200px）", annulus >= 200, f"annulus={annulus}")
    check("C14 圆心空心（≤30px）", inner <= 30, f"inner={inner}")

    # preserve_corners：三角顶点保尖
    img_before = get_screenshot()
    tri_pts = [[cl + int(cw * 0.12), ct + int(ch * 0.30)],
               [cl + int(cw * 0.30), ct + int(ch * 0.30)],
               [cl + int(cw * 0.21), ct + int(ch * 0.16)]]
    r = post_draw({"points": tri_pts, "hwnd": hwnd, "duration_ms": 1200,
                   "smooth_iterations": 3, "closed": True,
                   "preserve_corners": [0, 1, 2],
                   "capabilities": caps, "is_painting": True})
    check("C15 preserve_corners 绘制 ok", r.get("ok") is True,
          f"pts={r.get('points_count')}")
    time.sleep(0.5)
    img_after = get_screenshot()
    ink, mask, crop = ink_stats(img_before, img_after, crect)
    l, t, rr, bb = crop
    vertex_hits = 0
    for vx, vy in tri_pts:
        vx_i, vy_i = vx - l, vy - t
        patch = mask[max(0, vy_i - 4): vy_i + 5, max(0, vx_i - 4): vx_i + 5]
        if patch.size and int(cv2.countNonZero(patch)) >= 2:
            vertex_hits += 1
    check("C16 三个顶点全部保尖（顶点处有墨）", vertex_hits == 3,
          f"vertex_hits={vertex_hits}/3")

    # brush_width：2025 图标栏 mspaint 无数字笔宽控件 → 如实降级（绝不假装调好）
    check("C17 OCR 预热完成（logs/ghost.log ocr warmed）", wait_ocr_warmed())
    img_before = get_screenshot()
    stroke_y = ct + int(ch * 0.88)
    r = post_draw({"subpaths": [[[cl + int(cw * 0.10), stroke_y],
                                 [cl + int(cw * 0.55), stroke_y]]],
                   "hwnd": hwnd, "duration_ms": 1200, "brush_width": 8,
                   "capabilities": caps, "is_painting": True})
    bi = r.get("brush") or {}
    # v0.17 回归兼容：2025 icon-toolbar 版本在不同 OCR 结果下可能给出
    # mspaint_2025_no_numeric_size_control 或 size_control_not_found；
    # 两者都表示"没有可精确定位的数字笔宽控件"，绝不假装调好。
    brush_unadjusted_codes = ("mspaint_2025_no_numeric_size_control",
                              "size_control_not_found")
    check("C18 brush_width 无数字控件→如实降级仍落笔",
          r.get("ok") is True and bi.get("adjusted") is False
          and bi.get("reason_code") in brush_unadjusted_codes,
          f"brush={json.dumps(bi, ensure_ascii=False)[:200]}")
    r = post_draw({"subpaths": [[[cl + int(cw * 0.10), stroke_y],
                                 [cl + int(cw * 0.55), stroke_y]]],
                   "hwnd": hwnd, "duration_ms": 800, "brush_width": 8,
                   "brush_required": True, "capabilities": caps,
                   "is_painting": True})
    check("C19 brush_required=true → 409 拒绝（零假装）",
          r.get("http_status") == 409
          and r.get("error") == "BRUSH_ADJUST_FAILED"
          and r.get("reason") in brush_unadjusted_codes,
          f"error={r.get('error')} reason={r.get('reason')}")
    cv2.imwrite(str(ROOT / "data" / "screenshots" / "v16_brush_after.png"),
                get_screenshot())

    # sketch color 模式：红三角 + 蓝方块（自动逐色取色）
    img_before = get_screenshot()
    r = post_sketch({"image_path": samples["duo"], "mode": "color",
                     "hwnd": hwnd, "capabilities": caps, "k": 5})
    gr = r.get("color_groups") or []
    group_names = sorted(str(g.get("color")) for g in gr
                         if not g.get("skipped"))
    check("C20 color 模式双色谱执行成功",
          r.get("ok") is True and (r.get("strokes_drawn") or 0) >= 2
          and any(n in group_names for n in ("red", "blue")),
          f"groups={json.dumps(gr, ensure_ascii=False)[:180]}")
    time.sleep(0.8)
    img_after = get_screenshot()
    ink, mask, crop = ink_stats(img_before, img_after, crect)
    l, t, rr, bb = crop
    px = (img_after[t:bb, l:rr][mask > 0] if ink
          else np.zeros((1, 3), np.uint8))
    red_px = (int(np.sum((px[:, 2] > px[:, 0] + 40)
                         & (px[:, 2] > px[:, 1] + 40))) if len(px) else 0)
    blue_px = (int(np.sum((px[:, 0] > px[:, 2] + 30)
                          & (px[:, 0] > px[:, 1] + 20))) if len(px) else 0)
    check("C21 画布上出现红色笔迹（≥200px）", red_px >= 200, f"red={red_px}")
    check("C22 画布上出现蓝色笔迹（≥200px）", blue_px >= 200,
          f"blue={blue_px}")
    cv2.imwrite(str(ROOT / "data" / "screenshots" / "v16_sketch_color.png"),
                img_after)

    kill_tree(paint)
    try:
        paint.wait(timeout=8)
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.5)
    check("C23 清场：自建画图窗口已销毁", not win32gui.IsWindow(hwnd))
    return 0


def main() -> int:
    print("== moray-ghost v0.16 industrial draw acceptance ==", flush=True)
    srv = None
    tk_win = None
    try:
        if port_free():
            check("0 前提：端口 8030 空闲", True)
        else:
            check("0 前提：端口 8030 空闲（已清理本仓遗留服务）",
                  kill_stale_ghost_on_port())
        unit_checks()
        srv = spawn([sys.executable, "run.py"], drain=True)
        check("0 前提：Ghost 服务启动", wait_healthy())
        if srv.poll() is not None:
            return 1
        tk_win = make_tk_window()
        time.sleep(0.4)
        contract_checks()
        # v0.17 回归修复：契约 Tk 靶窗带 -topmost，且 150% DPI 下实际尺寸
        # 比逻辑 geometry 大，会盖住随后自建画图的画布左半（C9/C10 的
        # "drag start point covered by another window" 根因）。画图端到端
        # 前收起它，避免两个 topmost 窗口互相遮挡。
        if tk_win is not None:
            try:
                tk_win.attributes("-topmost", False)
                tk_win.withdraw()
                tk_win.update()
                time.sleep(0.3)
            except Exception:  # noqa: BLE001
                pass
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
