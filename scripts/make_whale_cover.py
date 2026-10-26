# -*- coding: utf-8 -*-
"""生成项目官方封面 docs/assets/whale_cover.html（自包含单文件）。

内容：纯鼠标鲸鱼的真实截图（base64 内嵌）+ 轨迹重放动画（按鲸鱼验收
脚本的笔画数据，服务端同款 Chaikin 平滑），深海底色 + 重播按钮。
跑法：.venv\\Scripts\\python.exe scripts\\make_whale_cover.py
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core import trajectory  # noqa: E402

ART = ROOT / "data" / "screenshots" / "whale_art.png"
OUT = ROOT / "docs" / "assets" / "whale_cover.html"


def circle_points(cx, cy, r, n=28):
    return [(cx + r * np.cos(2 * np.pi * i / n),
             cy + r * np.sin(2 * np.pi * i / n)) for i in range(n)]


def heart_points(cx, cy, s, n=48):
    pts = []
    for i in range(n):
        t = 2 * np.pi * i / n
        x = 16 * np.sin(t) ** 3
        y = 13 * np.cos(t) - 5 * np.cos(2 * t) - 2 * np.cos(3 * t) \
            - np.cos(4 * t)
        pts.append((cx + x * s / 16.0, cy - y * s / 16.0))
    return pts


def strokes_normalized():
    """与 scripts/test_v09_draw_whale.py 完全同源的笔画数据（归一化盒坐标）。"""
    out = []
    eye = trajectory.chaikin(circle_points(0.285, 0.475, 0.024)[:22], 2)
    out.append(("black", eye))
    out.append(("black", trajectory.chaikin(
        [(0.205, 0.565), (0.285, 0.625), (0.365, 0.575)], 2)))
    out.append(("black", trajectory.chaikin(
        [(0.315, 0.315), (0.285, 0.235), (0.315, 0.165), (0.345, 0.115)], 2)))
    out.append(("black", trajectory.chaikin(
        circle_points(0.235, 0.175, 0.012), 1)))
    out.append(("black", trajectory.chaikin(
        circle_points(0.405, 0.135, 0.009), 1)))
    body_ctrl = [
        (0.12, 0.55), (0.17, 0.38), (0.36, 0.28), (0.58, 0.30),
        (0.72, 0.36), (0.84, 0.24), (0.93, 0.30), (0.83, 0.46),
        (0.93, 0.58), (0.82, 0.63), (0.70, 0.52), (0.58, 0.68),
        (0.36, 0.76), (0.18, 0.70)]
    body = trajectory.chaikin(body_ctrl, 3)[::3]
    out.append(("blue", body))
    out.append(("blue", trajectory.chaikin(
        [(0.42, 0.585), (0.49, 0.70), (0.565, 0.655), (0.50, 0.575)], 2)))
    out.append(("blue", trajectory.chaikin(
        [(0.18, 0.70), (0.145, 0.63), (0.12, 0.55)], 2)))
    out.append(("blue", trajectory.chaikin(
        [(0.20, 0.62), (0.32, 0.705), (0.48, 0.735), (0.62, 0.665)], 2)))
    out.append(("blue", trajectory.chaikin(
        heart_points(0.52, 0.12, 0.045)[:38], 1)[::3]))
    out.append(("blue", [(0.10 + 0.80 * i / 80,
                          0.855 + 0.018 * np.sin(
                              (0.10 + 0.80 * i / 80) * 4 * np.pi))
                         for i in range(81)]))
    return out


def main() -> int:
    if not ART.is_file():
        print("missing " + str(ART))
        return 1
    from PIL import Image
    with Image.open(ART) as im:
        art_w, art_h = im.size
    img_b64 = base64.b64encode(ART.read_bytes()).decode("ascii")
    # 鲸鱼验收脚本里 box = 画布内缩 6%/8% → 相对 whale_art.png 同款内缩
    strokes = []
    for color, pts in strokes_normalized():
        mapped = [[round(0.06 + nx * 0.88, 4), round(0.08 + ny * 0.84, 4)]
                  for nx, ny in pts]
        strokes.append({"color": color, "points": mapped})
    cw = 1400
    ch = int(round(cw * art_h / art_w))
    html = TEMPLATE.replace("__IMG_B64__", img_b64) \
                   .replace("__STROKES__", json.dumps(strokes)) \
                   .replace("{{N_STROKES}}", str(len(strokes))) \
                   .replace("__CV_W__", str(cw)).replace("__CV_H__", str(ch))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print("wrote " + str(OUT) + " (" + format(OUT.stat().st_size, ",")
          + " bytes, " + str(len(strokes)) + " strokes, canvas "
          + str(cw) + "x" + str(ch) + ")")
    return 0


TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>moray-ghost · 纯鼠标画出的鲸鱼</title>
<style>
  :root { --ink:#0b1d2e; --glow:#3fa7ff; --soft:#9fc9e8; }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { min-height:100vh; display:flex; flex-direction:column;
    align-items:center; justify-content:center; gap:18px;
    font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif;
    background:radial-gradient(1200px 600px at 50% -10%, #10344f 0%,
      #0b1d2e 55%, #06121d 100%); color:#dceefc; padding:32px 16px; }
  h1 { font-size:clamp(22px,4vw,38px); font-weight:600; letter-spacing:.02em; }
  h1 .w { color:var(--glow); }
  p.sub { color:var(--soft); font-size:clamp(12px,1.6vw,15px);
    text-align:center; max-width:760px; line-height:1.8; }
  .frame { position:relative; border-radius:18px; overflow:hidden;
    box-shadow:0 24px 80px rgba(0,0,0,.55), 0 0 0 1px rgba(143,203,255,.18);
    background:#f4f7fa; max-width:min(92vw,880px); }
  canvas, img.art { display:block; width:100%; height:auto; }
  img.art { position:absolute; inset:0; opacity:0; transition:opacity 1.2s ease; }
  img.art.show { opacity:1; }
  .badge { position:absolute; right:12px; bottom:10px; font-size:11px;
    color:#274b66; background:rgba(255,255,255,.75); padding:3px 10px;
    border-radius:999px; backdrop-filter:blur(4px); }
  button { cursor:pointer; border:1px solid rgba(143,203,255,.35);
    background:rgba(63,167,255,.12); color:#bfe0ff; font-size:14px;
    padding:9px 26px; border-radius:999px; transition:.2s; }
  button:hover { background:rgba(63,167,255,.25); }
  .meta { color:#6f95ad; font-size:12px; text-align:center; line-height:1.9; }
  .meta code { color:var(--soft); }
</style>
</head>
<body>
  <h1>moray-ghost · 一只<span class="w">纯鼠标</span>画出的鲸鱼 🐋</h1>
  <p class="sub">本地 AI 服务通过 <b>mouse_event 绝对移动</b>（不是 SetCursorPos）
  发出真实鼠标移动事件：Chaikin 平滑 + 降采样 + 逐点 Fail-Safe，把
  {{N_STROKES}} 条控制点轨迹画进 Win11 画图的墨迹里——随后本地 Qwen2-VL-2B
  看着自己的画作答：“一条<b>蓝色</b>的鱼，有<b>水花、爱心、海浪、泡泡</b>。”</p>
  <div class="frame">
    <canvas id="cv" width="__CV_W__" height="__CV_H__"></canvas>
    <img id="real" class="art" alt="真实截图" src="data:image/png;base64,__IMG_B64__">
    <div class="badge">moray-ghost v0.9 · data/screenshots/whale_art.png</div>
  </div>
  <button id="replay">↻ 重播轨迹</button>
  <div class="meta">
    验收 <code>scripts/test_v09_draw_whale.py 25/25</code> ·
    通用接口 <code>draw_trajectory(points, color)</code> 31/31 ·
    MCP 5 工具 21/21 · VLM 读图 4/4<br>
    踩坑沉淀见 <code>docs/底层物理控制.md</code>：SetCursorPos 不触发轨迹 ·
    ctypes 签名污染 · 画图软件合并闭合线
  </div>
<script>
const STROKES = __STROKES__;
const cv = document.getElementById('cv'), ctx = cv.getContext('2d');
const real = document.getElementById('real');
const W = cv.width, H = cv.height;
const COLORS = { black:'#20303d', blue:'#2f7fd0' };
let raf = null;
function drawStroke(ctx, pts, color, upto) {
  ctx.strokeStyle = COLORS[color] || color;
  ctx.lineWidth = Math.max(2.2, W / 420);
  ctx.lineCap = 'round'; ctx.lineJoin = 'round';
  ctx.beginPath();
  const n = Math.min(pts.length, upto);
  ctx.moveTo(pts[0][0]*W, pts[0][1]*H);
  for (let i = 1; i < n; i++) ctx.lineTo(pts[i][0]*W, pts[i][1]*H);
  ctx.stroke();
}
function play() {
  if (raf) cancelAnimationFrame(raf);
  real.classList.remove('show');
  cv.width = cv.width; // clear
  let si = 0, pi = 1;
  const totalPts = STROKES.reduce((a,s)=>a+s.points.length,0);
  let drawn = 0;
  function frame() {
    if (si >= STROKES.length) {
      real.classList.add('show'); // 真实截图淡入：动画→现实的交接
      return;
    }
    const s = STROKES[si];
    // 每帧推进与总点数成比例，保证不同长度笔画耗时相近
    const perFrame = Math.max(1, Math.round(totalPts / 260));
    for (let k = 0; k < perFrame && si < STROKES.length; k++) {
      drawStroke(ctx, s.points, s.color, pi + 1);
      pi++; drawn++;
      if (pi >= s.points.length) { si++; pi = 1; }
    }
    raf = requestAnimationFrame(frame);
  }
  frame();
}
document.getElementById('replay').addEventListener('click', play);
window.addEventListener('load', () => setTimeout(play, 400));
</script>
</body>
</html>
"""

if __name__ == "__main__":
    sys.exit(main())
