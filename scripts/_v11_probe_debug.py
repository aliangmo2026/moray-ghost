# -*- coding: utf-8 -*-
"""v0.11 探针隔离实验：滑杆点击 + 探针拖拽 + 撤销，四步截图对比取证。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import cv2  # noqa: E402
import win32gui  # noqa: E402

from test_v10_draw_doubao import (  # noqa: E402
    FRAC_SIZE_TRACK_BOT, FRAC_SIZE_TRACK_TOP, FRAC_SIZE_TRACK_X,
    close_own_paint, detect_canvas, draw, force_foreground, hotkey,
    open_own_paint, port_free, safe_act, shot_png, spawn, stop_proc,
    wait_healthy)

OUT = ROOT / "data" / "screenshots"


def diffbox(a, b, canvas):
    cx, cy, cw, ch = canvas
    l0, t0 = max(0, cx), max(0, cy)
    r1 = min(a.shape[1], cx + cw)
    b1 = min(a.shape[0], cy + ch)
    d = cv2.absdiff(b[t0:b1, l0:r1], a[t0:b1, l0:r1])
    g = cv2.cvtColor(d, cv2.COLOR_BGR2GRAY)
    _, m = cv2.threshold(g, 20, 255, cv2.THRESH_BINARY)
    n = int(cv2.countNonZero(m))
    pts = cv2.findNonZero(m)
    box = cv2.boundingRect(pts) if pts is not None else None
    return n, box


def main() -> int:
    import sys as _sys
    srv = None
    if not port_free():
        print("port 8030 busy — service already up?", flush=True)
    else:
        srv = spawn([_sys.executable, "run.py"], drain=True)
        if not wait_healthy():
            print("service failed to start", flush=True)
            return 1
    try:
        return _body()
    finally:
        if srv is not None:
            stop_proc(srv)


def _body() -> int:
    paint = open_own_paint("probe-debug")
    if paint is None:
        print("SKIP: cannot open own paint", flush=True)
        return 1
    hwnd = paint["hwnd"]
    caps = {"allowed_windows": ["mspaint.exe"]}
    try:
        force_foreground(hwnd)
        time.sleep(0.8)
        rect = win32gui.GetWindowRect(hwnd)
        a = shot_png()
        canvas = detect_canvas(a, rect)
        print(f"canvas={canvas}", flush=True)
        cx, cy, cw, ch = canvas

        def crop(img, name):
            c = img[max(0, cy):cy + ch, max(0, cx):cx + cw]
            cv2.imwrite(str(OUT / name), c)

        crop(a, "dbg_A_base.png")

        # 1) 滑杆点击 f=0.5
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        sx = rect[0] + int(w * FRAC_SIZE_TRACK_X)
        ky = rect[1] + int(h * (FRAC_SIZE_TRACK_TOP
                                + 0.5 * (FRAC_SIZE_TRACK_BOT - FRAC_SIZE_TRACK_TOP)))
        r = safe_act({"action": "click", "x": sx, "y": ky, "hwnd": hwnd,
                      "settle_seconds": 0.2, "capabilities": caps}, hwnd)
        print(f"slider click ({sx},{ky}) -> {r.get('http_status')}", flush=True)
        time.sleep(0.4)
        b = shot_png()
        crop(b, "dbg_B_slider.png")
        print("A->B", diffbox(a, b, canvas), flush=True)

        # 2) 探针拖拽
        px, py1, py2 = cx + 30, cy + 120, cy + 220
        rd = draw([[px, py1], [px, py2]], 250, hwnd, caps, smooth=0)
        print(f"probe drag -> {rd.get('http_status')}", flush=True)
        time.sleep(0.3)
        c = shot_png()
        crop(c, "dbg_C_probe.png")
        print("B->C", diffbox(b, c, canvas), flush=True)
        print("A->C", diffbox(a, c, canvas), flush=True)

        # 3) 撤销
        hotkey("ctrl+z", hwnd, caps)
        time.sleep(0.4)
        d = shot_png()
        crop(d, "dbg_D_undo.png")
        print("A->D residue", diffbox(a, d, canvas), flush=True)
    finally:
        close_own_paint(paint, hwnd)
    print("done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
