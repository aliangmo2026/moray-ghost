"""v0.12 CV 粗探：在 Tk 表单截图上试验按钮/输入框矩形检测参数。"""
from __future__ import annotations

import ctypes
import json
import subprocess
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

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import win32gui  # noqa: E402

from app.core import screen_mss  # noqa: E402


def rects_from_edges(gray, img_w, img_h):
    edges = cv2.Canny(gray, 40, 120)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=2)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST,
                                   cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if w < 28 or h < 18:
            continue
        if w > 0.75 * img_w or h > 0.5 * img_h:
            continue
        aspect = w / float(h)
        if aspect < 1.0 or aspect > 14.0:
            continue
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.02 * peri, True)
        if not (4 <= len(approx) <= 12):
            continue
        area = cv2.contourArea(c)
        if area < 0.45 * w * h:
            continue
        out.append((x, y, w, h, round(area / (w * h), 2), len(approx)))
    # dedup containment
    out.sort(key=lambda r: r[2] * r[3], reverse=True)
    kept = []
    for r in out:
        x, y, w, h = r[:4]
        if any(x >= k[0] - 2 and y >= k[1] - 2 and x + w <= k[0] + k[2] + 2
               and y + h <= k[1] + k[3] + 2 for k in kept):
            continue
        kept.append(r)
    return kept[:80]


def rects_from_thresh(gray, img_w, img_h):
    out = []
    for inverted in (False, True):
        g = 255 - gray if inverted else gray
        _, th = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, kernel, iterations=1)
        contours, _ = cv2.findContours(th, cv2.RETR_LIST,
                                       cv2.CHAIN_APPROX_SIMPLE)
        for c in contours:
            x, y, w, h = cv2.boundingRect(c)
            if w < 28 or h < 18:
                continue
            if w > 0.75 * img_w or h > 0.5 * img_h:
                continue
            aspect = w / float(h)
            if aspect < 1.0 or aspect > 14.0:
                continue
            area = cv2.contourArea(c)
            if area < 0.55 * w * h:
                continue
            out.append((x, y, w, h, round(area / (w * h), 2), inverted))
    out.sort(key=lambda r: r[2] * r[3], reverse=True)
    kept = []
    for r in out:
        x, y, w, h = r[:4]
        if any(x >= k[0] - 2 and y >= k[1] - 2 and x + w <= k[0] + k[2] + 2
               and y + h <= k[1] + k[3] + 2 for k in kept):
            continue
        kept.append(r)
    return kept[:80]


def main() -> int:
    stem = "cv" + str(int(time.time()))[-6:]
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "_v12_form_target.py"), stem],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")
    hwnd = 0
    try:
        line = proc.stdout.readline().strip()
        payload = json.loads(line[len("READY "):])
        hwnd = int(payload["hwnd"])
        time.sleep(0.6)
        rect = win32gui.GetWindowRect(hwnd)
        img, origin = screen_mss.grab(1)
        left = max(0, rect[0] - origin["left"])
        top = max(0, rect[1] - origin["top"])
        crop = img.crop((left, top, min(img.width, rect[2] - origin["left"]),
                         min(img.height, rect[3] - origin["top"])))
        path = ROOT / "data" / "screenshots" / "_v12_form.png"
        crop.save(path)
        gray = cv2.cvtColor(np.array(crop.convert("RGB")),
                            cv2.COLOR_RGB2GRAY)
        print("crop", crop.size, "saved", path, flush=True)
        for name, fn in (("edges", rects_from_edges),
                         ("thresh", rects_from_thresh)):
            print("==", name, flush=True)
            for r in fn(gray, crop.width, crop.height):
                print("  ", r, flush=True)
        print("truth:", flush=True)
        # 转成 crop 坐标打印
        for c in payload["controls"]:
            x, y, w, h = c["bbox"]
            print("  ", [c["text"], c["type"], x - rect[0], y - rect[1], w, h],
                  flush=True)
    finally:
        try:
            win32gui.PostMessage(int(hwnd), 0x0010, 0, 0)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.4)
        if proc.poll() is None:
            proc.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
