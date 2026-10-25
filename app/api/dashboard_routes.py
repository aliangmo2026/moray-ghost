"""Web 监控面板（v0.5 平台化）：极简单页，无构建、无外部依赖。

- GET /dashboard                    → 单文件 HTML（浏览器直接访问）
- GET /api/ghost/dashboard/state    → 只读状态快照（免 token，与 /screenshot 探活同级）
- POST /api/ghost/emergency_stop    → 在 task_routes（需 token），面板大按钮调用

页面能力：
- 任务状态轮播：running + queued 任务每 2 秒轮换展示；
- 屏幕抓图：每 5 秒拉一次 /api/ghost/screenshot（免 token）显示并标注抓取时刻；
- EMERGENCY STOP：点击 → 清空队列并冻结 Ghost（需在面板里填 token，
  存 localStorage，仅发往本机 Ghost）。冻结后显示横幅与解冻按钮。
"""
from __future__ import annotations

import io
from typing import Any, Dict

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app.core import action_log, task_queue
from app.core.config import SERVICE, VERSION

router = APIRouter()  # 页面挂根路径 /dashboard；state 走下方 ghost 前缀

api_router = APIRouter(prefix="/api/ghost")


@api_router.get("/dashboard/state")
def dashboard_state() -> Dict[str, Any]:
    return {
        "ok": True,
        "service": SERVICE,
        "version": VERSION,
        "frozen": task_queue.is_frozen(),
        "frozen_at": task_queue.frozen_at(),
        "queue_depth": task_queue.depth(),
        "running": task_queue.running_view(),
        "queued": task_queue.snapshot(),
    }


@api_router.get("/actions/recent")
def actions_recent(limit: int = 50) -> Dict[str, Any]:
    """驾驶舱实时动作日志（v0.6 M3，只读，与 /screenshot 同级放行）。"""
    return {"ok": True, "count": min(limit, 200), "actions": action_log.recent(limit)}


@api_router.get("/actions/thumb/{name}")
def actions_thumb(name: str):
    """动作执行后截图的 96px 缩略（basename 白名单，防路径穿越）。"""
    from PIL import Image
    path = action_log.resolve_thumb(name)
    if path is None:
        return JSONResponse(status_code=404, content={
            "ok": False, "error": "NOT_FOUND", "detail": "thumb not found"})
    try:
        img = Image.open(path)
        img.thumbnail((96, 96))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=70)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=500, content={
            "ok": False, "error": "INTERNAL_ERROR",
            "detail": type(exc).__name__ + ": " + str(exc)})
    return Response(content=buf.getvalue(), media_type="image/jpeg")


@router.get("/dashboard")
def dashboard() -> HTMLResponse:
    return HTMLResponse(_PAGE)


_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>moray-ghost control</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; margin: 0; }
  body {
    font-family: Consolas, "Courier New", monospace;
    background: #0d1117; color: #c9d1d9;
    min-height: 100vh; display: flex; flex-direction: column; align-items: center;
    padding: 18px 12px 190px;
  }
  h1 { font-size: 20px; letter-spacing: 2px; color: #58a6ff; }
  .sub { color: #8b949e; font-size: 12px; margin: 6px 0 14px; }
  .card {
    width: min(720px, 96vw); background: #161b22; border: 1px solid #30363d;
    border-radius: 10px; padding: 14px 16px; margin-bottom: 14px;
  }
  .card h2 { font-size: 13px; color: #8b949e; text-transform: uppercase;
             letter-spacing: 1px; margin-bottom: 10px; }
  #carousel { min-height: 74px; }
  .task-line { padding: 4px 0; }
  .task-line .tid { color: #58a6ff; }
  .st-running { color: #3fb950; font-weight: bold; }
  .st-queued  { color: #d29922; }
  .st-idle    { color: #8b949e; }
  #shot { width: 100%; border-radius: 6px; border: 1px solid #30363d; background: #000; }
  .shot-meta { font-size: 11px; color: #8b949e; margin-top: 6px; }
  #frozen-banner {
    display: none; width: min(720px, 96vw); margin-bottom: 14px;
    background: #da3633; color: #fff; font-weight: bold; text-align: center;
    padding: 12px; border-radius: 10px; letter-spacing: 2px;
  }
  #stop {
    position: fixed; left: 50%; transform: translateX(-50%); bottom: 18px;
    width: min(700px, 94vw); height: 110px;
    font-family: inherit; font-size: 34px; font-weight: 900; letter-spacing: 6px;
    color: #fff; background: #b62324; border: 3px solid #f85149; border-radius: 14px;
    cursor: pointer; box-shadow: 0 0 26px rgba(248, 81, 73, .45);
  }
  #stop:active { transform: translateX(-50%) scale(.985); }
  #release {
    margin-top: 10px; width: 100%;
    font-family: inherit; font-size: 15px; padding: 9px;
    color: #fff; background: #238636; border: 1px solid #2ea043;
    border-radius: 8px; cursor: pointer;
  }
  .token-row { display: flex; gap: 8px; align-items: center; }
  .token-row input {
    flex: 1; background: #0d1117; color: #c9d1d9; border: 1px solid #30363d;
    border-radius: 6px; padding: 7px 9px; font-family: inherit; font-size: 12px;
  }
  .hint { font-size: 11px; color: #8b949e; margin-top: 6px; }
  #toast { position: fixed; top: 12px; right: 12px; background: #30363d;
           color: #fff; padding: 8px 12px; border-radius: 8px; font-size: 12px;
           display: none; max-width: 340px; }
  #actlog { max-height: 260px; overflow-y: auto; font-size: 12px; }
  .act-row { display: flex; align-items: center; gap: 8px; padding: 3px 0;
             border-bottom: 1px solid #21262d; }
  .act-row img { border-radius: 4px; flex: 0 0 auto; background: #000; }
  .act-ts { color: #8b949e; flex: 0 0 auto; }
  .act-ch { color: #58a6ff; flex: 0 0 auto; }
  .act-d { color: #8b949e; overflow: hidden; text-overflow: ellipsis;
           white-space: nowrap; flex: 1 1 auto; }
</style>
</head>
<body>
<h1>MORAY-GHOST CONTROL</h1>
<div class="sub" id="meta">connecting...</div>
<div id="frozen-banner">!!! GHOST FROZEN — QUEUE CLEARED !!!<button id="release">RELEASE (解冻)</button></div>

<div class="card">
  <h2>任务队列 · 状态轮播</h2>
  <div id="carousel" class="st-idle">loading…</div>
</div>

<div class="card">
  <h2>实时动作日志（每秒刷新）</h2>
  <div id="actlog" class="st-idle">loading…</div>
</div>

<div class="card">
  <h2>屏幕抓图（每 5 秒刷新）</h2>
  <img id="shot" alt="screen">
  <div class="shot-meta" id="shot-meta">waiting for first frame…</div>
</div>

<div class="card">
  <h2>面板 token（X-Ghost-Token，仅存本浏览器）</h2>
  <div class="token-row">
    <input id="token" type="password" placeholder="粘贴 GHOST_TOKEN（.env 里）" autocomplete="off">
  </div>
  <div class="hint">只读状态免 token；EMERGENCY STOP 属于状态变更操作，必须携带 token。</div>
</div>

<button id="stop">EMERGENCY STOP</button>
<div id="toast"></div>

<script>
var state = null, rot = 0, rotTimer = null;

function $(id) { return document.getElementById(id); }
function toast(msg) {
  var t = $("toast"); t.textContent = msg; t.style.display = "block";
  clearTimeout(t._h); t._h = setTimeout(function () { t.style.display = "none"; }, 4000);
}
function token() { return $("token").value.trim(); }
$("token").value = localStorage.getItem("ghost_token") || "";
$("token").addEventListener("change", function () {
  localStorage.setItem("ghost_token", $("token").value.trim());
});

function esc(s) {
  var MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" };
  return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
    return MAP[c];
  });
}

function renderCarousel() {
  var box = $("carousel");
  if (!state) { box.textContent = "loading…"; return; }
  var items = [];
  if (state.running) items.push(state.running);
  (state.queued || []).forEach(function (q) { items.push(q); });
  if (!items.length) {
    box.className = "st-idle";
    box.textContent = state.frozen ? "FROZEN — no dispatch" : "idle — no active tasks";
    return;
  }
  var it = items[rot % items.length]; rot += 1;
  box.className = "task-line";
  var pos = (it.queue_position != null && !state.running) ? " (pos " + it.queue_position + ")" : "";
  var step = (it.current_step_id != null) ? " · step " + it.current_step_id + "/" + it.steps_total : "";
  box.innerHTML = "agent <b>" + esc(it.agent_id || "operator") + "</b> · task <span class='tid'>"
    + esc(it.task_id) + "</span> · <span class='st-" + esc(it.status) + "'>" + esc(it.status)
    + "</span>" + esc(pos) + esc(step)
    + "<br><small>" + esc(it.goal || "(no goal)") + "</small>";
}

function pollState() {
  fetch("/api/ghost/dashboard/state", { cache: "no-store" })
    .then(function (r) { return r.json(); })
    .then(function (s) {
      state = s;
      $("meta").textContent = s.service + " v" + s.version
        + " · queue " + s.queue_depth
        + (s.frozen ? " · FROZEN" : " · live");
      $("frozen-banner").style.display = s.frozen ? "block" : "none";
      if (!s.frozen) rot = 0;
      renderCarousel();
    })
    .catch(function () { $("meta").textContent = "ghost unreachable"; });
}

function pollShot() {
  fetch("/api/ghost/screenshot?monitor=1&scale=0.35&quality=55", { cache: "no-store" })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      $("shot").src = "data:image/" + d.format + ";base64," + d.image_b64;
      $("shot-meta").textContent = "monitor " + d.monitor + " · " + d.width + "x" + d.height
        + " · captured " + new Date().toLocaleTimeString() + " (" + d.elapsed_ms + "ms)";
    })
    .catch(function () { $("shot-meta").textContent = "screenshot unavailable"; });
}

$("stop").addEventListener("click", function () {
  if (!token()) { toast("先在下方填入 token（.env 的 GHOST_TOKEN）"); return; }
  if (!confirm("EMERGENCY STOP：立刻清空任务队列并冻结 Ghost？")) return;
  fetch("/api/ghost/emergency_stop", { method: "POST",
    headers: { "X-Ghost-Token": token() } })
    .then(function (r) { return r.json().then(function (b) { return { s: r.status, b: b }; }); })
    .then(function (r) {
      if (r.s === 200 && r.b.ok) {
        toast("已冻结：清空 " + r.b.queue_cleared + " 个排队任务，取消 "
          + ((r.b.canceled_tasks || []).length) + " 个");
      } else {
        toast("失败 " + r.s + ": " + ((r.b && (r.b.error || r.b.detail)) || "unknown"));
      }
      pollState();
    })
    .catch(function (e) { toast("请求失败: " + e); });
});

$("release").addEventListener("click", function () {
  fetch("/api/ghost/emergency_stop/release", { method: "POST",
    headers: { "X-Ghost-Token": token() } })
    .then(function (r) { return r.json(); })
    .then(function (b) { toast(b.ok ? "已解冻，恢复派发" : "release failed"); pollState(); })
    .catch(function (e) { toast("请求失败: " + e); });
});

function pollActions() {
  fetch("/api/ghost/actions/recent?limit=30", { cache: "no-store" })
    .then(function (r) { return r.json(); })
    .then(function (d) {
      var box = $("actlog");
      var items = d.actions || [];
      if (!items.length) {
        box.className = "st-idle";
        box.textContent = "idle — no actions yet";
        return;
      }
      box.className = "";
      var html = "";
      items.forEach(function (a) {
        var th = a.thumb
          ? '<img src="/api/ghost/actions/thumb/' + a.thumb + '" width="56" height="42">'
          : '<span style="width:56px"></span>';
        html += '<div class="act-row">' + th
          + '<span class="act-ts">' + esc(a.ts.slice(11, 23)) + '</span>'
          + '<b class="act-ch">' + esc(a.channel) + '</b> '
          + esc(a.action)
          + '<span class="act-d">' + esc(JSON.stringify(a.detail || {})) + '</span>'
          + '<span class="' + (a.ok ? 'st-running' : 'st-queued') + '">'
          + (a.ok ? 'OK ' : 'ERR ') + esc(a.status) + '</span></div>';
      });
      box.innerHTML = html;
    })
    .catch(function () {});
}

pollState(); pollShot(); pollActions();
setInterval(pollState, 1000);
setInterval(pollShot, 5000);
setInterval(pollActions, 1000);
setInterval(renderCarousel, 2000);
</script>
</body>
</html>
"""
