# -*- coding: utf-8 -*-
"""v0.11 侦察工具：自建 Paint 截图标定控件坐标（粗细/画笔/色板/画布）。

红线同 v10：只碰本次 Popen 的 mspaint（PID 差集绑定），清场只杀自己的
进程树。单线程直用 mss（本机多实例并发才是禁忌，单实例主线程是文档化
安全姿势；此时服务未运行，无跨实例竞争）。

用法: .venv\\Scripts\\python.exe scripts\\_v11_recon.py [passive|popup]
  passive = 只截窗口/功能区/色板（默认）
  popup   = 额外点一次"粗细"候选点并截下拉弹层（坐标由 pass1 结果硬编码）
"""
from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except OSError:
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import win32gui  # noqa: E402
import mss  # noqa: E402

# 复用 v10 的窗口基建（红线同源）
from test_v10_draw_doubao import (  # noqa: E402
    close_own_paint, force_foreground, open_own_paint)

OUT = ROOT / "data" / "screenshots"

# popup 模式：pass1 读图后标定的控件候选点（窗口相对分数，按最新 rect 换算；
# zoom 实测 win-rel 像素：画笔库 (600,200)、粗细 (1042,180) / 窗口 2048x1280）
SIZE_DROP_FRAC = (0.5088, 0.1578)    # 形状组右侧"粗细"下拉（≡图标下方 ⌄ 箭头）
BRUSH_GALLERY_FRAC = (0.2930, 0.1563)  # "画笔"库按钮下半（下箭头开弹层）


def grab_screen() -> "np.ndarray":
    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[0])
        return cv2.cvtColor(np.asarray(shot), cv2.COLOR_BGRA2BGR)


def save_crop(img, rect, frac, name, scale=2):
    l, t, r, b = rect
    w, h = r - l, b - t
    x0 = l + int(w * frac[0])
    y0 = t + int(h * frac[1])
    x1 = l + int(w * frac[2])
    y1 = t + int(h * frac[3])
    crop = img[max(0, y0):y1, max(0, x0):x1].copy()
    if scale != 1:
        crop = cv2.resize(crop, None, fx=scale, fy=scale,
                          interpolation=cv2.INTER_NEAREST)
    path = OUT / name
    cv2.imwrite(str(path), crop)
    print(f"  saved {path.name}: crop=({x0},{y0})-({x1},{y1}) "
          f"-> {crop.shape[1]}x{crop.shape[0]}", flush=True)
    return (x0, y0, x1, y1)


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "passive").strip().lower()
    OUT.mkdir(parents=True, exist_ok=True)
    paint = open_own_paint("recon")
    if paint is None:
        print("[SKIP] 无法自建 Paint（已有实例？）", flush=True)
        return 1
    hwnd = paint["hwnd"]
    try:
        force_foreground(hwnd)
        time.sleep(0.8)
        rect = win32gui.GetWindowRect(hwnd)
        print(f"  window rect={rect} size={rect[2]-rect[0]}x{rect[3]-rect[1]}",
              flush=True)
        img = grab_screen()
        l, t, r, b = rect
        cv2.imwrite(str(OUT / "recon_full.png"), img[max(0, t):b, max(0, l):r])
        save_crop(img, rect, (0.0, 0.0, 1.0, 0.22), "recon_ribbon.png", 2)
        save_crop(img, rect, (0.52, 0.03, 0.85, 0.23), "recon_swatches.png", 3)
        save_crop(img, rect, (0.0, 0.20, 1.0, 1.00), "recon_canvas.png", 1)
        if mode in ("popup", "sizeonly"):
            import pyautogui
            pyautogui.PAUSE = 0.05
            w, h = rect[2] - rect[0], rect[3] - rect[1]
            if mode == "popup":
                # 1) 画笔库弹层
                bx = l + int(w * BRUSH_GALLERY_FRAC[0])
                by = t + int(h * BRUSH_GALLERY_FRAC[1])
                print(f"  click brush-gallery ({bx},{by})", flush=True)
                pyautogui.click(bx, by)
                time.sleep(0.7)
                img1 = grab_screen()
                save_crop(img1, rect, (0.254, 0.156, 0.561, 0.742),
                          "recon_brushes.png", 2)
                pyautogui.press("escape")
                time.sleep(0.4)
            # 2) 粗细下拉弹层
            px = l + int(w * SIZE_DROP_FRAC[0])
            py = t + int(h * SIZE_DROP_FRAC[1])
            print(f"  click size-dropdown candidate ({px},{py})", flush=True)
            pyautogui.click(px, py)
            time.sleep(0.7)
            img2 = grab_screen()
            cv2.imwrite(str(OUT / "recon_popup_full.png"),
                        img2[max(0, t):b, max(0, l):r])
            save_crop(img2, rect, (0.430, 0.110, 0.730, 0.560),
                      "recon_popup.png", 2)
            pyautogui.press("escape")
            time.sleep(0.3)
    finally:
        close_own_paint(paint, hwnd)
    print("  recon done.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
