"""v0.12 诊断探针：对比 native 枚举 / OCR / CV 在 Tk 表单窗上的原始输出。

用法：python scripts/_v12_probe_extract.py [stem]
自建 Tk 靶窗（hwnd 差集绑定），打印：
  - 靶窗父子件逐个 class/text/rect（native 原始）
  - OCR 原始 [box,text,score]
  - 预期控件几何（Tk 自报）
不调用任何服务/安全管道，纯只读。
"""
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

import win32gui  # noqa: E402
from PIL import Image  # noqa: E402

from app.core import screen_mss, vl_driver, window_locator as wl  # noqa: E402


def find_by_title(stem: str, timeout: float = 8.0):
    end = time.time() + timeout
    while time.time() < end:
        for w in wl.list_windows():
            if stem in w["title"]:
                return w
        time.sleep(0.3)
    return None


def main() -> int:
    stem = sys.argv[1] if len(sys.argv) > 1 else "p" + str(int(time.time()))[-6:]
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "_v12_form_target.py"), stem],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace")
    hwnd = 0
    try:
        line = proc.stdout.readline().strip()
        print("READY LINE:", line[:2000], flush=True)
        if not line.startswith("READY "):
            return 1
        payload = json.loads(line[len("READY "):])
        hwnd = int(payload["hwnd"])
        truth = payload.get("controls") or []
        print("\n== Tk truth ==", flush=True)
        for t in truth:
            print(json.dumps(t, ensure_ascii=False), flush=True)

        print("\n== win32 children (raw) ==", flush=True)
        rect = win32gui.GetWindowRect(hwnd)
        print("window rect", rect, "title", win32gui.GetWindowText(hwnd), flush=True)

        def _cb(ch, _):
            if not win32gui.IsWindowVisible(ch):
                return
            r = win32gui.GetWindowRect(ch)
            print(json.dumps({"hwnd": ch,
                              "class": win32gui.GetClassName(ch),
                              "text": win32gui.GetWindowText(ch),
                              "rect": list(r)}, ensure_ascii=False), flush=True)
        win32gui.EnumChildWindows(hwnd, _cb, None)

        print("\n== native analyze ==", flush=True)
        print(json.dumps(vl_driver._analyze_native(hwnd), ensure_ascii=False),
              flush=True)

        print("\n== screenshot crop + OCR raw ==", flush=True)
        img, origin = screen_mss.grab(1)
        left = max(0, rect[0] - origin["left"])
        top = max(0, rect[1] - origin["top"])
        crop = img.crop((left, top, min(img.width, rect[2] - origin["left"]),
                         min(img.height, rect[3] - origin["top"])))
        engine = vl_driver._get_ocr()
        t0 = time.perf_counter()
        result, elapse = engine(crop)
        print("ocr elapsed %.0fms" % ((time.perf_counter() - t0) * 1000),
              flush=True)
        for item in result or []:
            box, text, score = item[0], str(item[1]), float(item[2])
            xs = [int(p[0]) for p in box]
            ys = [int(p[1]) for p in box]
            print(json.dumps({"text": text, "score": round(score, 3),
                              "bbox": [min(xs), min(ys),
                                       max(xs) - min(xs), max(ys) - min(ys)]},
                             ensure_ascii=False), flush=True)
        print("\n== cv2 boxes (will be implemented) ==", flush=True)
        print("done", flush=True)

        print("\n== extract_ui_elements ==", flush=True)
        ext = vl_driver.extract_ui_elements(hwnd=hwnd, backend="auto",
                                            include_icons=False)
        print(json.dumps({k: v for k, v in ext.items()
                          if k not in ("elements",)}, ensure_ascii=False),
              flush=True)
        for e in ext.get("elements") or []:
            print(json.dumps(e, ensure_ascii=False), flush=True)
    finally:
        try:
            win32gui.PostMessage(int(hwnd), 0x0010, 0, 0)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
        if proc.poll() is None:
            proc.terminate()
        print("cleanup", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
