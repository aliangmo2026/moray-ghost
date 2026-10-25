"""诊断 M4-3d/e 的 0.0 分：复现 test_v04 流程，逐步对比裁剪/匹配时的像素。

步骤：开#1 → 裁按钮 → 关#1 → 开#2@(900,500) → 等5秒逐秒裁按钮并两两比对
→ 用窗口 ROI 跑一次真实 matcher → 存全部 PNG 取证。
"""
from __future__ import annotations

import base64
import ctypes
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
import win32con  # noqa: E402
import win32gui  # noqa: E402

from app.core import macro_matcher, window_locator as wl  # noqa: E402
from app.core.screen_mss import grab  # noqa: E402

tmp_dir = ROOT / "data" / "tmp"
tmp_dir.mkdir(parents=True, exist_ok=True)
dbg = ROOT / "data" / "screenshots"
opened: list = []


def crop_arr(img, cx: int, cy: int, size: int = 64) -> np.ndarray:
    half = size // 2
    arr = np.asarray(img)[cy - half:cy + half, cx - half:cx + half]
    return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)


def close_np(hwnd: int) -> None:
    try:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    except Exception:
        pass
    for _ in range(10):
        time.sleep(0.3)
        if wl.get_rect(hwnd) is None:
            return


def main() -> int:
    before = {w["hwnd"] for w in wl.list_windows()}
    f1 = tmp_dir / ("diag_t_" + str(int(time.time() * 1000)) + ".txt")
    f1.write_text("", encoding="utf-8")
    opened.append(f1)
    subprocess.Popen(["notepad.exe", str(f1)])
    h1 = 0
    for _ in range(60):
        time.sleep(0.25)
        for w in wl.list_windows():
            if w["hwnd"] not in before and f1.stem in w["title"]:
                h1 = w["hwnd"]
                break
        if h1:
            break
    print("hwnd1:", h1, flush=True)
    if not h1:
        return 1
    r1 = wl.get_rect(h1)
    tpl1 = crop_arr(grab(1)[0], r1[0] + 60, r1[1] + 86)
    cv2.imwrite(str(dbg / "diag_tpl1.png"), tpl1)
    print("tpl1 std:", round(float(cv2.cvtColor(tpl1, cv2.COLOR_BGR2GRAY).std()), 1),
          flush=True)
    close_np(h1)
    print("hwnd1 closed:", wl.get_rect(h1) is None, flush=True)

    before2 = {w["hwnd"] for w in wl.list_windows()}
    f2 = tmp_dir / ("diag_t_" + str(int(time.time() * 1000)) + ".txt")
    f2.write_text("", encoding="utf-8")
    opened.append(f2)
    subprocess.Popen(["notepad.exe", str(f2)])
    h2 = 0
    for _ in range(60):
        time.sleep(0.25)
        for w in wl.list_windows():
            if w["hwnd"] not in before2 and f2.stem in w["title"]:
                h2 = w["hwnd"]
                break
        if h2:
            break
    print("hwnd2:", h2, flush=True)
    win32gui.MoveWindow(h2, 900, 500, 800, 600, True)
    wl.wait_stable(h2, timeout=4.0)
    win32gui.SetWindowPos(h2, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                          win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
    time.sleep(0.4)

    prev = None
    for i in range(6):
        r2 = wl.get_rect(h2)
        arr = crop_arr(grab(1)[0], r2[0] + 60, r2[1] + 86)
        cv2.imwrite(str(dbg / ("diag_tpl2_t" + str(i) + ".png")), arr)
        g = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
        diff = ("%.1f" % float(np.abs(g.astype(int) - prev.astype(int)).mean())) \
            if prev is not None else "-"
        prev = g
        b64 = base64.b64encode(
            cv2.imencode(".png", arr)[1].tobytes()).decode("ascii")
        res = macro_matcher.find(grab(1)[0], b64, 0.8, hwnd=h2)
        print("t+%ds std=%.1f prev_diff=%s match=%s center=%s score=%s" % (
            i, float(g.std()), diff, res.get("status"),
            res.get("center"), round(res.get("score") or res.get("best_score") or 0, 3)),
            flush=True)
        time.sleep(1.0)

    close_np(h2)
    print("hwnd2 closed:", wl.get_rect(h2) is None, flush=True)
    for f in opened:
        try:
            f.unlink(missing_ok=True)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
