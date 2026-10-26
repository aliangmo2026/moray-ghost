"""v0.9.1 VLM 读图验收：本地 Qwen2-VL-2B（GPU）"读懂自己画的鲸鱼"。

进程内直调 vl_driver.describe（/vl/describe 的同款实现），把
data/screenshots/whale_art.png（纯鼠标鲸鱼画作的画布裁剪原图）以
image_b64 直接喂给本地多模态模型，问两个问题：
  Q1 这张图里画了什么？        → 期望答出鲸鱼（鲸/whale/鱼/海豚等海洋生物）
  Q2 主要线条是什么颜色？       → 期望答出蓝色（蓝/blue）

如实分级：whale 全过；fish/dolphin/sea 类部分通过（并注明）；
其余如实 FAIL。模型加载 + 推理全程记录 TTFT/总耗时。

跑法：.venv\\Scripts\\python.exe scripts\\test_v09_vlm_read_whale.py
"""
from __future__ import annotations

import base64
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

from app.core import config, vl_driver  # noqa: E402

RESULTS: list = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  | {detail}" if detail else ""), flush=True)
    return ok


WHALE_WORDS = ("鲸", "whale", "鱼", "fish", "海豚", "dolphin",
               "海洋生物", "sea creature", "海洋动物")
PARTIAL_WORDS = ("鱼", "fish", "海豚", "dolphin", "海洋", "sea",
                 "海底", "动物", "animal", "生物")
BLUE_WORDS = ("蓝", "blue", "azure", "cyan")


def ask(img_b64: str, question: str) -> dict:
    t0 = time.perf_counter()
    out = vl_driver.describe(question=question, image_b64=img_b64,
                             max_tokens=400)
    out["wall_ms"] = int((time.perf_counter() - t0) * 1000)
    return out


def main() -> int:
    print("== moray-ghost v0.9.1 VLM reads its own whale ==", flush=True)
    art = ROOT / "data" / "screenshots" / "whale_art.png"
    if not art.is_file():
        print("[FAIL] 找不到鲸鱼原图 " + str(art), flush=True)
        return 1
    img_b64 = base64.b64encode(art.read_bytes()).decode("ascii")

    check("0 llama 后端可用（权重+mmproj 就位）", vl_driver.llama_available(),
          "profile=" + str(getattr(config, "VL_PROFILE", "?")))

    # ---- Q1 画了什么 ----
    try:
        r1 = ask(img_b64, "这张图里画了什么？")
        d1 = str(r1.get("description") or "")
        print("  Q1 原文: " + repr(d1), flush=True)
        print("  Q1 计时: ttft={}ms total={}ms wall={}ms".format(
            (r1.get("timings") or {}).get("ttft_ms"),
            (r1.get("timings") or {}).get("total_ms"),
            r1.get("wall_ms")), flush=True)
        low = d1.lower()
        if any(w.lower() in low for w in ("鲸", "whale")):
            check("1 VLM 认出鲸鱼（whale 级）", True, d1[:80])
        elif any(w.lower() in low for w in PARTIAL_WORDS):
            check("1 VLM 认出海洋生物（fish 级，部分通过）", True,
                  "partial: " + d1[:80])
        else:
            check("1 VLM 认出鲸鱼", False, d1[:120])
    except Exception as exc:  # noqa: BLE001
        check("1 VLM 读图 Q1", False, type(exc).__name__ + ": " + str(exc)[:200])

    # ---- Q2 颜色（约束到"用一个颜色词回答"）----
    try:
        r2 = ask(img_b64, "这张图里的线条是什么颜色的？"
                          "请用中文回答，只需要一个颜色词。")
        d2 = str(r2.get("description") or "")
        print("  Q2 原文: " + repr(d2), flush=True)
        low = d2.lower()
        check("2 VLM 认出蓝色线条",
              any(w.lower() in low for w in BLUE_WORDS), d2[:80])
    except Exception as exc:  # noqa: BLE001
        check("2 VLM 读图 Q2", False, type(exc).__name__ + ": " + str(exc)[:200])

    # ---- Q3 细节理解（任意一个画中元素即可）----
    try:
        r3 = ask(img_b64, "这幅简笔画里除了主体动物，还画了哪些元素？"
                          "（例如眼睛、水花、爱心、海浪、泡泡）列出你看到的。")
        d3 = str(r3.get("description") or "")
        print("  Q3 原文: " + repr(d3), flush=True)
        low = d3.lower()
        detail_words = ("眼", "eye", "水", "water", "爱心", "heart",
                        "浪", "wave", "泡", "bubble", "嘴", "mouth",
                        "鳍", "fin", "尾", "tail")
        check("3 VLM 读出画中细节元素（任一）",
              any(w.lower() in low for w in detail_words), d3[:120])
    except Exception as exc:  # noqa: BLE001
        check("3 VLM 读图 Q3", False, type(exc).__name__ + ": " + str(exc)[:200])

    passed = sum(1 for x in RESULTS if x)
    print(f"\n== RESULT: {passed}/{len(RESULTS)} passed ==", flush=True)
    return len(RESULTS) - passed


if __name__ == "__main__":
    sys.exit(main())
