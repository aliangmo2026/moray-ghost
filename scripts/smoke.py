"""moray-ghost 冒烟脚本（契约 v0.2）。

前置：服务已启动（.\\start.ps1）。在项目 venv 内运行：
    .venv\\Scripts\\python scripts\\smoke.py             # 基础检查 + 授权 click
    .venv\\Scripts\\python scripts\\smoke.py --failsafe  # 追加 Fail-Safe 409 现场测试
    .venv\\Scripts\\python scripts\\smoke.py --no-click  # 不动鼠标的最小检查

本脚本只探测本机服务，所有 URL 均为调用点内联字面量（127.0.0.1:8030）。
"""
from __future__ import annotations

import argparse
import ctypes
import os
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except OSError:
        pass

import requests

ROOT = Path(__file__).resolve().parents[1]
RESULTS: list = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(ok)
    tag = "PASS" if ok else "FAIL"
    line = f"[{tag}] {name}"
    if detail:
        line += f"  | {detail}"
    print(line, flush=True)
    return ok


def load_token() -> str:
    tok = os.getenv("GHOST_TOKEN", "").strip()
    if tok:
        return tok
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("GHOST_TOKEN="):
                return line.split("=", 1)[1].strip()
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-click", action="store_true", help="跳过真实 click 动作")
    ap.add_argument("--failsafe", action="store_true", help="测试 Fail-Safe 409（会把鼠标甩到角落再还原）")
    args = ap.parse_args()
    H = {"X-Ghost-Token": load_token()}
    ok_all = True

    # 1) /health（免 token）
    r = requests.get("http://127.0.0.1:8030/health", timeout=10)
    h = r.json()
    ok_all &= check("GET /health -> 200", r.status_code == 200 and h.get("status") == "ok",
                    f"service={h.get('service')} version={h.get('version')} "
                    f"fail_safe={h.get('fail_safe_enabled')} token_required={h.get('token_required')} "
                    f"monitors={len(h.get('monitors', []))}")
    mons = [m for m in h.get("monitors", []) if m.get("primary")] or h.get("monitors") or [{}]
    m0 = mons[0]
    safe_x = int(m0.get("left", 0)) + int(m0.get("width", 1920)) // 2
    safe_y = int(m0.get("top", 0)) + int(m0.get("height", 1080)) // 2
    ok_all &= check("health fields complete",
                    all(k in h for k in ("backends", "monitors", "cursor", "db_path")),
                    f"cursor={h.get('cursor')}")

    # 2) /screenshot（免 token）
    r = requests.get("http://127.0.0.1:8030/screenshot",
                     params={"fmt": "jpeg", "quality": 70, "scale": 0.5, "save": "true"},
                     timeout=20)
    s = r.json()
    spath = s.get("screenshot_path")
    ok_all &= check("GET /screenshot -> 200", r.status_code == 200 and s.get("ok") is True,
                    f"{s.get('width')}x{s.get('height')} origin={s.get('monitor_origin')} "
                    f"scale={s.get('scale')} elapsed={s.get('elapsed_ms')}ms")
    ok_all &= check("screenshot file saved + b64",
                    bool(spath) and Path(spath).is_file() and len(s.get("image_b64") or "") > 1000,
                    str(spath))

    # 3) /execute_action 缺 token -> 401
    r = requests.post("http://127.0.0.1:8030/execute_action",
                      json={"action": "click", "x": safe_x, "y": safe_y}, timeout=10)
    detail = r.text[:80]
    try:
        detail = str(r.json().get("error"))
    except Exception:
        pass
    ok_all &= check("POST /execute_action (no token) -> 401", r.status_code == 401, detail)

    # 4) 宏系统
    r = requests.get("http://127.0.0.1:8030/macro", headers=H, timeout=10)
    ok_all &= check("GET /macro (token) -> 200 列表可用",
                    r.status_code == 200 and isinstance(r.json().get("macros"), list),
                    f"count={r.json().get('count')}")
    r = requests.post("http://127.0.0.1:8030/macro/whatever/replay",
                      json={"dry_run": True}, timeout=10)
    ok_all &= check("POST /macro/whatever/replay (no token) -> 401", r.status_code == 401)
    r = requests.post("http://127.0.0.1:8030/macro/whatever/replay", headers=H,
                      json={"dry_run": True, "allow_fallback": False, "task_id": None},
                      timeout=10)
    ok_all &= check("POST /macro/whatever/replay (token) -> 404（真实重放：宏不存在）",
                    r.status_code == 404, str(r.json().get("error")))

    # 5) 授权 click（真实动作，落到安全坐标 = 主屏中心）
    if not args.no_click:
        r = requests.post("http://127.0.0.1:8030/execute_action", headers=H,
                          json={"action": "click", "x": safe_x, "y": safe_y,
                                "settle_seconds": 0.3, "fmt": "jpeg", "quality": 70,
                                "scale": 0.5}, timeout=30)
        b = r.json()
        bp = b.get("before_screenshot_path")
        apath = b.get("after_screenshot_path")
        ok_all &= check("POST /execute_action click (token) -> 200",
                        r.status_code == 200 and b.get("ok") is True,
                        f"x={b.get('x')} y={b.get('y')} cursor_before={b.get('cursor_before')} "
                        f"cursor_after={b.get('cursor_after')} elapsed={b.get('elapsed_ms')}ms")
        ok_all &= check("click before/after 审计截图落盘",
                        bool(bp) and bool(apath) and Path(bp).is_file() and Path(apath).is_file(),
                        f"{Path(bp).name} + {Path(apath).name}" if bp and apath else str(b)[:120])

    # 5.5) test_scaled_click：源图坐标 → 物理像素换算（缩放图中心点回中心）
    if not args.no_click:
        r = requests.get("http://127.0.0.1:8030/screenshot", params={"scale": 0.5}, timeout=20)
        s = r.json()
        ow = s.get("original_width")
        oh = s.get("original_height")
        exp_w = int(m0.get("width", 0))
        exp_h = int(m0.get("height", 0))
        ok_all &= check("test_scaled_click: original size matches monitor",
                        r.status_code == 200 and ow == exp_w and oh == exp_h,
                        f"original={ow}x{oh} scaled={s.get('width')}x{s.get('height')} expect={exp_w}x{exp_h}")
        r = requests.post("http://127.0.0.1:8030/execute_action", headers=H,
                          json={"action": "click", "x": 640, "y": 400,
                                "source_width": 1280, "source_height": 800,
                                "settle_seconds": 0.1}, timeout=30)
        b = r.json()
        rp = b.get("resolved_physical") or []
        ca = b.get("cursor_after") or []
        near_center = (len(rp) == 2 and abs(rp[0] - exp_w // 2) <= 1
                       and abs(rp[1] - exp_h // 2) <= 1)
        ok_all &= check("test_scaled_click: resolved_physical == screen center (±1px)",
                        r.status_code == 200 and near_center
                        and b.get("coord_source") == "scaled",
                        f"resolved_physical={rp} coord_source={b.get('coord_source')}")
        cursor_ok = (len(rp) == 2 and len(ca) == 2
                     and abs(ca[0] - rp[0]) <= 3 and abs(ca[1] - rp[1]) <= 3)
        ok_all &= check("test_scaled_click: cursor_after within 3px of resolved",
                        cursor_ok, f"cursor_after={ca}")
        r = requests.post("http://127.0.0.1:8030/execute_action", headers=H,
                          json={"action": "click", "x": 640, "y": 400,
                                "settle_seconds": 0.1}, timeout=30)
        b = r.json()
        rp2 = b.get("resolved_physical") or []
        ok_all &= check("test_scaled_click: backward compat (no source) resolved == [640,400]",
                        r.status_code == 200 and rp2 == [640, 400]
                        and b.get("coord_source") == "physical",
                        f"resolved_physical={rp2} coord_source={b.get('coord_source')}")

    # 6) Fail-Safe 409（鼠标甩到左上角，服务端应拒动作并附现场截图）
    if args.failsafe:
        import pyautogui
        prev = pyautogui.position()
        pyautogui.FAILSAFE = False
        got_409 = False
        b = {}
        for attempt in range(5):
            pyautogui.moveTo(0, 0)
            time.sleep(0.15)
            pos = pyautogui.position()
            if pos.x > 2 or pos.y > 2:
                print(f"  (attempt {attempt}: mouse left corner at {pos}, retrying)", flush=True)
                continue
            r = requests.post("http://127.0.0.1:8030/execute_action", headers=H,
                              json={"action": "click", "x": safe_x, "y": safe_y}, timeout=30)
            b = r.json()
            if r.status_code == 409:
                got_409 = True
                break
            print(f"  (attempt {attempt}: status={r.status_code}, mouse raced back)", flush=True)
        pyautogui.FAILSAFE = False
        pyautogui.moveTo(*prev)
        pyautogui.FAILSAFE = True
        scene = b.get("scene_screenshot_path")
        ok_all &= check("mouse in corner -> 409 FAIL_SAFE_TRIGGERED + 现场截图",
                        got_409 and bool(scene) and Path(scene).is_file(),
                        f"cursor={b.get('cursor')} scene={Path(scene).name if scene else None}")

    passed = sum(1 for o in RESULTS if o)
    print(f"== smoke: {passed}/{len(RESULTS)} passed ==")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
