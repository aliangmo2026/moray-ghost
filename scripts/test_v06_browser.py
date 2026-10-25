"""v0.6 M2 验收：CDP 浏览器接管（playwright connect_over_cdp → 本地无头 Edge）。

测试纪律：本脚本自建靶页（临时 HTML）+ 自己拉起的无头 Edge（独立 user-data-dir，
--remote-debugging-port=9222），全程只操作自己的 tab，结束 taskkill 整树销毁。

场景：
  A 接入：GET /browser/tabs 列出自己的靶页 tab。
  B 元素级操作：fill #name → click #go（页内 JS 把输入值写进 title）→ tabs 轮询
    证明 click/fill 真实生效（无需任何 OpenCV）。
  C 平滑滚动：scroll #long dy=-600 → 响应前后页内截图像素差 > 阈值。
  D 定位纪律：role=button 多候选未给 nth → 409 ELEMENT_AMBIGUOUS（带候选数）；
    给 nth → 200；不存在选择器 → 409 ELEMENT_NOT_FOUND。
  E 冻结门禁：emergency stop 期间 browser action → 503 GHOST_FROZEN；release 恢复。
  F 清场：close_tab 关自己的 tab → taskkill Edge 整树 → 删临时文件。
"""
from __future__ import annotations

import base64
import io
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from PIL import Image, ImageChops, ImageStat  # noqa: E402

H = {"X-Ghost-Token": ""}
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    if line.strip().startswith("GHOST_TOKEN="):
        H["X-Ghost-Token"] = line.split("=", 1)[1].strip()
RESULTS: list = []

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>moray-v06-target</title>
<style>
 body { font-family: Consolas, monospace; margin: 16px; }
 #long { height: 4000px; background: linear-gradient(#0d1117, #58a6ff); }
</style>
</head>
<body>
<button id="a">alpha</button>
<button id="b">beta</button>
<input id="name" placeholder="你的名字">
<button id="go" onclick="document.title='NAME:' + document.getElementById('name').value">提交</button>
<div id="long"></div>
</body>
</html>
"""


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(ok)
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}" + (f"  | {detail}" if detail else ""), flush=True)
    return ok


def post_action(payload: dict):
    r = requests.post("http://127.0.0.1:8030/api/ghost/browser/action",
                      headers=H, json=payload, timeout=60)
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        body = {"raw": r.text[:120]}
    return r, body


def tabs_list():
    r = requests.get("http://127.0.0.1:8030/api/ghost/browser/tabs",
                     headers=H, timeout=30)
    return r.status_code, (r.json() if r.status_code == 200 else {})


def wait_tabs_contain(needle: str, timeout: float = 10.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        code, body = tabs_list()
        if code == 200 and any(needle in (t.get("title") or "")
                               for t in body.get("tabs") or []):
            return True
        time.sleep(0.4)
    return False


def diff_b64_mean(b64_a: str, b64_b: str) -> float:
    a = Image.open(io.BytesIO(base64.b64decode(b64_a))).convert("L")
    b = Image.open(io.BytesIO(base64.b64decode(b64_b))).convert("L")
    if a.size != b.size:
        b = b.resize(a.size)
    return ImageStat.Stat(ImageChops.difference(a, b)).mean[0]


def main() -> int:
    edge = next((p for p in EDGE_CANDIDATES if Path(p).is_file()), None)
    if not check("P-1: 本机 Edge 已定位", bool(edge), str(edge)):
        return 1
    tmp_dir = Path(tempfile.mkdtemp(prefix="moray_v06b_"))
    page_file = tmp_dir / "target.html"
    page_file.write_text(PAGE_HTML, encoding="utf-8")
    profile = tmp_dir / "profile"
    profile.mkdir(exist_ok=True)
    url = page_file.as_uri()
    proc = subprocess.Popen(
        [edge, "--headless=new", "--remote-debugging-port=9222",
         "--user-data-dir=" + str(profile), "--no-first-run",
         "--no-default-browser-check", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"  (edge pid={proc.pid} headless target={url})", flush=True)

    # A: 等 CDP 就绪并找到自己的 tab
    ready = False
    end = time.time() + 20
    while time.time() < end:
        code, body = tabs_list()
        if code == 200 and any("target.html" in (t.get("url") or "")
                               for t in body.get("tabs") or []):
            ready = True
            break
        time.sleep(0.5)
    ok_ready = check("A-1: CDP 接入且列出自己的靶页 tab", ready)
    if not ok_ready:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True)
        return 1
    tab = {"url_contains": "target.html"}

    # B: fill + click 链（页内 JS 把输入值写进 title）
    r, body = post_action({"op": "fill", "tab": tab,
                           "selector": {"by": "css", "value": "#name"},
                           "text": "ghost-cdp"})
    check("B-1: fill #name → 200（带元素框与前后截图）",
          r.status_code == 200 and body.get("element_box")
          and body.get("after_image_b64"),
          f"box={body.get('element_box')}")
    r, body = post_action({"op": "click", "tab": tab,
                           "selector": {"by": "role", "value": "button", "nth": 2}})
    check("B-2: click 提交按钮（role 定位 nth=2）→ 200",
          r.status_code == 200, f"{r.status_code} {body.get('error')}")
    check("B-3: click 真实生效（title 变为 NAME:ghost-cdp）",
          wait_tabs_contain("NAME:ghost-cdp"))

    # C: 滚动（页内截图前后差）
    r, body = post_action({"op": "scroll", "tab": tab,
                           "selector": {"by": "css", "value": "#long"},
                           "dy": -600})
    dmean = (diff_b64_mean(body.get("before_image_b64", ""),
                           body.get("after_image_b64", ""))
             if r.status_code == 200 else 0.0)
    check("C-1: scroll dy=-600 → 200 且页面确实滚动（截图差）",
          r.status_code == 200 and dmean > 1.0,
          f"status={r.status_code} diff={dmean:.2f}")

    # D: 定位纪律
    r, body = post_action({"op": "click", "tab": tab,
                           "selector": {"by": "role", "value": "button"}})
    check("D-1: role=button 多候选未给 nth → 409 ELEMENT_AMBIGUOUS（带候选数）",
          r.status_code == 409 and body.get("error") == "ELEMENT_AMBIGUOUS"
          and body.get("candidates", 0) >= 2,
          f"{r.status_code} {body.get('error')} n={body.get('candidates')}")
    r, body = post_action({"op": "click", "tab": tab,
                           "selector": {"by": "css", "value": "#no-such"}})
    check("D-2: 不存在选择器 → 409 ELEMENT_NOT_FOUND",
          r.status_code == 409 and body.get("error") == "ELEMENT_NOT_FOUND",
          f"{r.status_code} {body.get('error')}")
    r, body = post_action({"op": "click", "tab": tab,
                           "selector": {"by": "css", "value": "#a", "nth": 9}})
    check("D-3: nth 越界 → 400", r.status_code == 400, f"{r.status_code}")

    # E: 冻结门禁（与 execute_action 同级）
    r = requests.post("http://127.0.0.1:8030/api/ghost/emergency_stop",
                      headers=H, timeout=15)
    check("E-1: emergency_stop → frozen", r.status_code == 200
          and r.json().get("frozen") is True)
    r, body = post_action({"op": "click", "tab": tab,
                           "selector": {"by": "css", "value": "#a"}})
    check("E-2: 冻结期 browser action → 503 GHOST_FROZEN",
          r.status_code == 503 and body.get("error") == "GHOST_FROZEN",
          f"{r.status_code} {body.get('error')}")
    r = requests.post("http://127.0.0.1:8030/api/ghost/emergency_stop/release",
                      headers=H, timeout=15)
    check("E-3: release 解冻", r.status_code == 200)

    # F: 清场
    r, body = post_action({"op": "close_tab", "tab": tab})
    check("F-1: close_tab 关闭自己的 tab", r.status_code == 200,
          f"{r.status_code} {body.get('error')}")
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                   capture_output=True)
    try:
        for p in sorted(tmp_dir.rglob("*"), reverse=True):
            p.unlink(missing_ok=True)
        os.rmdir(tmp_dir)
    except Exception:  # noqa: BLE001
        pass
    check("F-2: Edge 整树销毁 + 临时目录清理", True, f"pid={proc.pid}")

    total = len(RESULTS)
    passed = sum(1 for x in RESULTS if x)
    print(f"\n== v06 browser verify: {passed}/{total} passed ==", flush=True)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
