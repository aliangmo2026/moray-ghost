"""v0.12 GUI RAG + Ghost Guard 验收（自建 Tk 表单靶窗，绝不触碰真实账户）。

覆盖：
  0 前提：服务/靶窗/健康回显（guard.rag 配置）。
  1 检索层 extract_ui_elements：10 个真实控件（3 输入框/1 复选框/6 按钮）
    的召回/精确率、bbox IoU、中心误差、类型、输入值、危险元素标注、
    编号唯一性、编号截图、二次抽取稳定性。
  2 选择层 ground_target：文本精确路径 + VLM 选号 10/10、登录/退出登录
    消歧、中文模糊目标、bbox 与检索元素逐字节一致（零坐标幻觉）、延迟。
  3 Ghost Guard：安全点击 200 + visual_guard=allow；删除/转账/退出登录
    409 PENDING_APPROVAL 且标题不变；错误审批令牌仍 409；正确审批令牌
    200 并真实点击；审计落 guard_pending/guard_approved；事后服务未冻结。
  4 MCP click_element：RAG 路径 target_id/method 透传；危险元素在 MCP
    层即被 PENDING_APPROVAL 拦截（agent 不能自批）；Tools 仍为 5 个。
  5 清场：只关自己 hwnd、停服务、端口释放、鼠标归位。

铁律：靶窗自建 + hwnd 差集绑定；危险按钮只改本地标题（编号回显，不含
「删除」以免触发窗口黑名单），不执行任何真实业务。
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except OSError:
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import anyio  # noqa: E402
import psutil  # noqa: E402
import requests  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from app.core import config  # noqa: E402

TOKEN = config.ensure_token()
APPROVAL = "v12-human-approval-secret"
CAPS = {"allowed_windows": ["python.exe"]}
AGENT = "v12-gui-rag-test"
RESULTS: list = []
PROCS: list = []
SERVER: dict = {"proc": None}

DANGER_LABELS = ("删除账户", "转账", "退出登录")
SAFE_LABELS = ("登录", "取消", "保存成功")
ALL_LABELS = ("用户名", "密码", "备注", "记住我") + SAFE_LABELS + DANGER_LABELS
QUERY_LABELS = list(ALL_LABELS)


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(bool(ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  | {detail}" if detail else ""), flush=True)
    return bool(ok)


def hdr() -> dict:
    return {"X-Ghost-Token": TOKEN}


def port_free(port: int = 8030) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def kill_by_port(port: int = 8030) -> None:
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.laddr and c.laddr.port == port and c.pid:
                psutil.Process(c.pid).kill()
                time.sleep(0.6)
    except Exception as exc:  # noqa: BLE001
        print("  (kill_by_port: " + type(exc).__name__ + ")", flush=True)


def spawn(args: list, env_extra: dict | None = None,
          drain: bool = True) -> subprocess.Popen:
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.Popen(args, cwd=str(ROOT), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env,
                            text=True, encoding="utf-8", errors="replace")
    PROCS.append(proc)
    if not drain:
        return proc

    def _drain() -> None:
        try:
            for _ in proc.stdout:
                pass
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=_drain, daemon=True).start()
    return proc


def start_server() -> subprocess.Popen:
    return spawn([sys.executable, str(ROOT / "run.py")],
                 {"GHOST_APPROVAL_TOKEN": APPROVAL})


def wait_health(timeout: float = 35.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = requests.get(
                "http://127.0.0.1:8030/api/ghost/health", timeout=2)
            if r.status_code == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.35)
    return False


def stop_proc(proc: subprocess.Popen, port: int | None = 8030) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=6)
    if port and not port_free(port):
        kill_by_port(port)


def close_hwnd(hwnd: int) -> bool:
    if not hwnd or not win32gui.IsWindow(hwnd):
        return True
    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    for _ in range(25):
        time.sleep(0.2)
        if not win32gui.IsWindow(hwnd):
            return True
    return not win32gui.IsWindow(hwnd)


def force_foreground(hwnd: int) -> bool:
    for _ in range(5):
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
        except Exception:  # noqa: BLE001
            try:
                ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)
                ctypes.windll.user32.keybd_event(0x12, 0, 2, 0)
                win32gui.SetForegroundWindow(hwnd)
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.25)
        if win32gui.GetForegroundWindow() == hwnd:
            return True
    return False


def title_of(hwnd: int) -> str:
    try:
        return win32gui.GetWindowText(int(hwnd))
    except Exception:  # noqa: BLE001
        return ""


def wait_title(hwnd: int, pred, timeout: float = 5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred(title_of(hwnd)):
            return True
        time.sleep(0.15)
    return pred(title_of(hwnd))


def cursor_pos() -> tuple:
    import ctypes.wintypes
    pt = ctypes.wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def restore_cursor(pos: tuple) -> None:
    try:
        ctypes.windll.user32.SetCursorPos(int(pos[0]), int(pos[1]))
    except Exception:  # noqa: BLE001
        pass


def iou(a: list, b: list) -> float:
    ax, ay, aw, ah = [float(v) for v in a]
    bx, by, bw, bh = [float(v) for v in b]
    x1, y1 = max(ax, bx), max(ay, by)
    x2, y2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if x2 <= x1 or y2 <= y1:
        return 0.0
    inter = (x2 - x1) * (y2 - y1)
    return inter / (aw * ah + bw * bh - inter)


def center_err(a: list, b: list) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return max(abs((ax + aw / 2) - (bx + bw / 2)),
               abs((ay + ah / 2) - (by + bh / 2)))


def vl_elements(**kw) -> dict:
    body = {"window": kw.pop("window", None), "backend": "auto"}
    body.update(kw)
    r = requests.post(
        "http://127.0.0.1:8030/api/ghost/vl/elements",
        headers=hdr(), json=body, timeout=180)
    try:
        return r.json()
    except ValueError:
        return {"ok": False, "error": "BAD_JSON", "raw": r.text[:300],
                "http_status": r.status_code}


def vl_ground(query: str, **kw) -> dict:
    body = {"window": kw.pop("window", None), "query": query,
            "backend": "auto", "strategy": "auto"}
    body.update(kw)
    r = requests.post(
        "http://127.0.0.1:8030/api/ghost/vl/ground",
        headers=hdr(), json=body, timeout=300)
    try:
        return r.json()
    except ValueError:
        return {"ok": False, "error": "BAD_JSON", "raw": r.text[:300],
                "http_status": r.status_code}


def execute_click(x: int, y: int, hwnd: int, approval: str | None = None,
                  settle: float = 0.1) -> dict:
    payload = {"action": "click", "x": int(x), "y": int(y),
               "hwnd": int(hwnd), "monitor": 1, "settle_seconds": settle,
               "fmt": "png", "quality": 70, "scale": 0.25,
               "capabilities": CAPS}
    if approval is not None:
        payload["approval_token"] = approval
    r = requests.post(
        "http://127.0.0.1:8030/api/ghost/execute_action",
        headers=hdr(), json=payload, timeout=60)
    try:
        body = r.json()
    except ValueError:
        body = {"raw": r.text[:300]}
    body["http_status"] = r.status_code
    return body


def by_label(elements: list, label: str) -> dict | None:
    for e in elements:
        if label in (e.get("text"), e.get("label")):
            return e
    return None


def tool_body(res) -> dict:
    sc = getattr(res, "structured_content", None) or getattr(
        res, "structuredContent", None)
    if isinstance(sc, dict) and sc:
        if set(sc.keys()) == {"result"} and isinstance(sc.get("result"), dict):
            return sc["result"]
        if "ok" in sc or "error" in sc or "status" in sc:
            return sc
    texts = [getattr(c, "text", None)
             for c in (getattr(res, "content", None) or [])]
    joined = "\n".join(t for t in texts if t)
    if joined.strip():
        try:
            loaded = json.loads(joined)
            if isinstance(loaded, dict):
                return loaded
        except ValueError:
            pass
    return {"ok": False, "error": "MCP_EMPTY_RESULT",
            "detail": joined[:300]}


def mcp_params() -> StdioServerParameters:
    env = dict(os.environ)
    env.pop("GHOST_APPROVAL_TOKEN", None)  # agent 侧绝不持有审批令牌
    env["GHOST_CAPABILITIES"] = '["python.exe"]'
    env["GHOST_TOKEN"] = TOKEN
    env["GHOST_AGENT_ID"] = AGENT
    return StdioServerParameters(command=sys.executable,
                                 args=[str(ROOT / "mcp_server.py")], env=env)


# ============================== scenarios ==================================
def scenario_extract(hwnd: int, truth: list, base_title: str) -> dict:
    print("\n== 1 GUI RAG 检索层 ==", flush=True)
    force_foreground(hwnd)
    t0 = time.perf_counter()
    ext = vl_elements(window=base_title, annotate=True)
    cold_ms = int((time.perf_counter() - t0) * 1000)
    els = ext.get("elements") or []
    check("1.1 /vl/elements → 200 且 ok", ext.get("ok") is True,
          f"backend={ext.get('backend')} count={ext.get('count')} "
          f"cold={cold_ms}ms")
    check("1.2 后端为 RAG OCR+CV 检索", ext.get("backend") == "rag_ocr_cv",
          str(ext.get("backend")))

    ctrls = [e for e in els if e.get("type") in ("input", "button", "checkbox")]
    matched, missing, ious, errs, type_ok = [], [], [], [], []
    for t in truth:
        label = t.get("label") or t.get("text")
        e = by_label(els, label)
        if e is None:
            missing.append(label)
            continue
        matched.append(e)
        ious.append(iou(e["bbox"], t["bbox"]))
        errs.append(center_err(e["bbox"], t["bbox"]))
        type_ok.append(e.get("type") == t.get("type"))
    check("1.3 10 个真实控件全部召回（按标签）",
          len(matched) == len(truth) and not missing,
          f"{len(matched)}/{len(truth)} missing={missing}")
    check("1.4 控件级精确率 10/10（无多余控制元素）",
          len(ctrls) == len(truth),
          f"extracted={[(e.get('text'), e.get('type')) for e in ctrls]}")
    check("1.5 bbox IoU 全部 ≥0.75（物理像素）",
          bool(ious) and min(ious) >= 0.75,
          "min=%.3f avg=%.3f all=%s" % (
              min(ious) if ious else -1,
              sum(ious) / len(ious) if ious else -1,
              [round(v, 3) for v in ious]))
    check("1.6 bbox 中心误差全部 ≤3px",
          bool(errs) and max(errs) <= 3.0,
          "max=%.1f avg=%.1f" % (max(errs) if errs else -1,
                                 sum(errs) / len(errs) if errs else -1))
    check("1.7 控件类型全部正确（input/button/checkbox）", all(type_ok),
          str([(by_label(els, t.get('label') or t.get('text')) or {}).get('type')
               for t in truth]))

    user = by_label(els, "用户名") or {}
    pwd = by_label(els, "密码") or {}
    note = by_label(els, "备注") or {}
    check("1.8 输入框标签/值分离正确",
          user.get("value") == "moray_user" and pwd.get("value") == ""
          and note.get("value") == "像素级自动化",
          f"用户名={user.get('value')!r} 密码={pwd.get('value')!r} "
          f"备注={note.get('value')!r}")

    ids = [int(e.get("id") or 0) for e in els]
    check("1.9 编号唯一且连续 1..N",
          ids == list(range(1, len(els) + 1)), str(ids))

    guard = ext.get("guard") or {}
    dangerous = [by_label(els, lab) for lab in DANGER_LABELS]
    dangerous_ok = all(e is not None and e.get("dangerous") for e in dangerous)
    safe_ok = all(not (by_label(els, lab) or {}).get("dangerous")
                  for lab in SAFE_LABELS)
    check("1.10 危险元素全部标注 dangerous（删除/转账/退出登录）",
          dangerous_ok and set(guard.get("categories") or [])
          >= {"deletion", "payment", "session"},
          f"ids={guard.get('dangerous_ids')} cats={guard.get('categories')}")
    check("1.11 安全元素零误标", safe_ok,
          str([(lab, bool((by_label(els, lab) or {}).get('dangerous')))
               for lab in SAFE_LABELS]))

    b64 = ext.get("annotated_b64") or ""
    png_ok = False
    try:
        raw = base64.b64decode(b64)
        png_ok = raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) > 1000
    except Exception:  # noqa: BLE001
        png_ok = False
    check("1.12 编号截图 annotated_b64 为有效 PNG", png_ok,
          f"size={ext.get('annotated_size')} b64={len(b64)}")

    ext2 = vl_elements(window=base_title)
    els2 = ext2.get("elements") or []
    sig1 = [(e.get("text"), e.get("label"), tuple(e.get("bbox") or []))
            for e in els]
    sig2 = [(e.get("text"), e.get("label"), tuple(e.get("bbox") or []))
            for e in els2]
    check("1.13 二次抽取稳定（标签+bbox 完全一致）", sig1 == sig2,
          f"n1={len(sig1)} n2={len(sig2)}")
    warm_ms = ext2.get("elapsed_ms")
    print(f"  timings: cold={cold_ms}ms warm={warm_ms}ms "
          f"{json.dumps(ext.get('timings') or {}, ensure_ascii=False)}",
          flush=True)
    return {"elements": els}


def scenario_ground(hwnd: int, base_title: str, els: list, truth: list) -> None:
    print("\n== 2 GUI RAG 选择层（VLM target_id） ==", flush=True)
    force_foreground(hwnd)
    t0 = time.perf_counter()
    auto = vl_ground("保存成功", window=base_title, strategy="auto")
    auto_ms = int((time.perf_counter() - t0) * 1000)
    auto_el = auto.get("element") or {}
    check("2.1 精确标签 auto 路径 → text_exact（零模型）",
          auto.get("ok") and auto.get("method") == "text_exact"
          and auto_el.get("text") == "保存成功",
          f"method={auto.get('method')} id={auto.get('target_id')} "
          f"{auto_ms}ms")

    correct, within, ids_ok, methods, lat = [], [], [], [], []
    first_ms = None
    for label in QUERY_LABELS:
        t = time.perf_counter()
        r = vl_ground(label, window=base_title, strategy="vlm",
                      use_image=False)
        ms = int((time.perf_counter() - t) * 1000)
        if first_ms is None:
            first_ms = ms
        lat.append(ms)
        el = r.get("element") or {}
        ok = r.get("ok") is True and bool(el.get("type")) and \
            label in (el.get("text"), el.get("label"))
        correct.append(ok)
        methods.append(r.get("method"))
        if ok:
            t_truth = next((x for x in truth
                            if (x.get("label") or x.get("text")) == label),
                           None)
            if t_truth:
                within.append(iou(el["bbox"], t_truth["bbox"]) >= 0.75)
                src = next((x for x in els if x.get("id") == r.get("target_id")),
                           None)
                ids_ok.append(src is not None
                              and src.get("bbox") == el.get("bbox"))
    check("2.2 VLM 选号 10/10（只回 target_id，无坐标生成）",
          all(correct),
          f"{sum(correct)}/{len(QUERY_LABELS)} methods={set(methods)}")
    check("2.3 选中元素 bbox 与检索表逐字节一致（零坐标幻觉）",
          all(ids_ok) and len(ids_ok) == len(QUERY_LABELS),
          f"{sum(ids_ok)}/{len(QUERY_LABELS)}")
    check("2.4 选中 bbox 与真实控件 IoU ≥0.75", all(within),
          f"{sum(within)}/{len(within)}")

    dis = vl_ground("登录", window=base_title, strategy="vlm",
                    use_image=False)
    dis_el = dis.get("element") or {}
    check("2.5 「登录」消歧：不选中「退出登录」",
          dis_el.get("text") == "登录",
          f"got={dis_el.get('text')!r} id={dis.get('target_id')} "
          f"cands={[(c.get('id'), c.get('text')) for c in (dis.get('candidates') or [])]}")

    fuzzy_ok = []
    for q, want in (("保存", "保存成功"), ("密码框", "密码")):
        r = vl_ground(q, window=base_title, strategy="vlm", use_image=False)
        el = r.get("element") or {}
        fuzzy_ok.append((q, el.get("text"), el.get("text") == want))
    check("2.6 模糊目标（保存/密码框）由 VLM 语义选号",
          all(ok for _q, _t, ok in fuzzy_ok), str(fuzzy_ok))

    warm = lat[1:] or lat
    avg_warm = sum(warm) / len(warm)
    check("2.7 VLM 热路径均值 <2000ms（GPU 2B 目录选号）",
          avg_warm < 2000.0,
          f"first={first_ms}ms warm_avg={avg_warm:.0f}ms "
          f"min={min(warm)} max={max(warm)}")

    ann = vl_ground("保存成功", window=base_title, strategy="vlm",
                    use_image=True, annotate=True)
    b64 = ann.get("annotated_b64") or ""
    check("2.8 编号截图模式可返回 PNG（image 选择路径）",
          ann.get("ok") is True and b64.startswith("iVBOR"),
          f"method={ann.get('method')} size={ann.get('annotated_size')}")


def scenario_guard(hwnd: int, base_title: str, els: list) -> None:
    print("\n== 3 Ghost Guard 动态门禁 ==", flush=True)
    login = by_label(els, "登录") or {}
    cx, cy = login.get("center") or [0, 0]
    check("3.0 靶窗强占前台", force_foreground(hwnd))
    safe = execute_click(cx, cy, hwnd)
    vg = safe.get("visual_guard") or {}
    check("3.1 安全点击「登录」→ 200，visual_guard=allow",
          safe.get("ok") is True and safe.get("http_status") == 200
          and vg.get("verdict") == "allow",
          f"status={safe.get('http_status')} guard={vg.get('verdict')} "
          f"src={vg.get('source')} texts={vg.get('texts')}")
    check("3.2 安全点击真实生效（标题 -BTN:登录）",
          wait_title(hwnd, lambda t: t.endswith("-BTN:登录")),
          title_of(hwnd))

    before = title_of(hwnd)
    dele = by_label(els, "删除账户") or {}
    dx, dy = dele.get("center") or [0, 0]
    force_foreground(hwnd)
    pend = execute_click(dx, dy, hwnd)
    hits = pend.get("hits") or (pend.get("visual_guard") or {}).get("hits") or []
    check("3.3 点击「删除账户」→ 409 PENDING_APPROVAL",
          pend.get("http_status") == 409
          and pend.get("error") == "PENDING_APPROVAL"
          and pend.get("requires_approval") is True,
          f"status={pend.get('http_status')} error={pend.get('error')} "
          f"hits={[h.get('token') for h in hits]}")
    check("3.4 无审批时动作完全未执行（标题不变）",
          title_of(hwnd) == before, f"{before!r} -> {title_of(hwnd)!r}")

    before = title_of(hwnd)
    wrong = execute_click(dx, dy, hwnd, approval="wrong-" + APPROVAL)
    check("3.5 错误审批令牌 → 仍 409，标题不变",
          wrong.get("http_status") == 409
          and wrong.get("error") == "PENDING_APPROVAL"
          and title_of(hwnd) == before,
          f"status={wrong.get('http_status')} title={title_of(hwnd)!r}")

    force_foreground(hwnd)
    approved = execute_click(dx, dy, hwnd, approval=APPROVAL)
    vg = approved.get("visual_guard") or {}
    check("3.6 正确审批令牌 → 200 且 visual_guard=approved",
          approved.get("ok") is True and approved.get("http_status") == 200
          and vg.get("verdict") == "approved",
          f"status={approved.get('http_status')} guard={vg.get('verdict')}")
    check("3.7 审批后真实点击生效（标题 -BTN:DANGER-1）",
          wait_title(hwnd, lambda t: t.endswith("-BTN:DANGER-1")),
          title_of(hwnd))

    results = []
    for label in ("转账", "退出登录"):
        el = by_label(els, label) or {}
        x, y = el.get("center") or [0, 0]
        force_foreground(hwnd)
        p = execute_click(x, y, hwnd)
        force_foreground(hwnd)
        a = execute_click(x, y, hwnd, approval=APPROVAL)
        results.append((label, p.get("error"), a.get("http_status"),
                        (a.get("visual_guard") or {}).get("verdict")))
    check("3.8 「转账/退出登录」均先 409、审批后 200",
          all(r[1] == "PENDING_APPROVAL" and r[2] == 200
              and r[3] == "approved" for r in results), str(results))

    force_foreground(hwnd)
    save = by_label(els, "保存成功") or {}
    x, y = save.get("center") or [0, 0]
    post_click = execute_click(x, y, hwnd)
    check("3.9 多次拦截/审批后服务未冻结，安全点击仍 200",
          post_click.get("ok") is True
          and post_click.get("http_status") == 200,
          f"status={post_click.get('http_status')}")

    try:
        audit = (ROOT / "data" / "ghost_audit.jsonl").read_text(
            encoding="utf-8", errors="replace").splitlines()[-400:]
    except OSError:
        audit = []
    pending_rows = [ln for ln in audit if '"event": "guard_pending"' in ln]
    approved_rows = [ln for ln in audit if '"event": "guard_approved"' in ln]
    check("3.10 审计含 guard_pending 与 guard_approved 事件",
          bool(pending_rows) and bool(approved_rows),
          f"pending={len(pending_rows)} approved={len(approved_rows)}")
    _ = base_title


def scenario_mcp(hwnd: int, base_title: str) -> None:
    print("\n== 4 MCP click_element 全链路 ==", flush=True)

    async def _run() -> None:
        async with stdio_client(mcp_params()) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = await s.list_tools()
                names = sorted(t.name for t in tools.tools)
                # v0.16 起新增 canvas_info / sketch_from_image（共 7 个）；v0.12
                # 的 5 个能力必须全部保留，只增不减才算未破坏协议白名单。
                required = {"analyze_screen", "click_element",
                            "draw_trajectory", "execute_task", "type_text"}
                check("4.1 MCP Tools 保留 v0.12 白名单 5 个（允许新增）",
                      required.issubset(set(names)), str(names))
                force_foreground(hwnd)
                clk = tool_body(await s.call_tool("click_element", {
                    "text": "保存成功", "window": base_title}))
                check("4.2 click_element 走 GUI RAG（target_id/method 透传）→ 200",
                      clk.get("ok") is True and clk.get("http_status") == 200
                      and clk.get("target_id") is not None
                      and clk.get("method") in ("text_exact", "vlm_text"),
                      f"status={clk.get('http_status')} "
                      f"id={clk.get('target_id')} "
                      f"method={clk.get('method')} "
                      f"clicked={json.dumps(clk.get('clicked'), ensure_ascii=False)[:150]}")
                check("4.3 MCP 点击真实生效（标题 -BTN:保存成功）",
                      wait_title(hwnd, lambda t: "-BTN:保存成功" in t),
                      title_of(hwnd))
                force_foreground(hwnd)
                before = title_of(hwnd)
                bad = tool_body(await s.call_tool("click_element", {
                    "text": "删除账户", "window": base_title}))
                check("4.4 MCP 点击危险元素 → PENDING_APPROVAL（agent 不能自批）",
                      bad.get("error") == "PENDING_APPROVAL"
                      and bad.get("http_status") == 409
                      and bad.get("requires_approval") is True,
                      f"error={bad.get('error')} status={bad.get('http_status')} "
                      f"target={bad.get('target_id')}")
                check("4.5 MCP 危险点击零副作用（标题不变）",
                      title_of(hwnd) == before,
                      f"{before!r} -> {title_of(hwnd)!r}")
    anyio.run(_run)


def main() -> int:
    print("== moray-ghost v0.12 GUI RAG + Ghost Guard acceptance ==",
          flush=True)
    cursor0 = cursor_pos()
    hwnd = 0
    try:
        check("0.1 前提：端口 8030 空闲", port_free())
        if not port_free():
            kill_by_port(8030)
        srv = start_server()
        SERVER["proc"] = srv
        check("0.2 Ghost v0.12 服务启动（带审批令牌）", wait_health())
        if srv.poll() is not None:
            return 1
        h = requests.get(
            "http://127.0.0.1:8030/api/ghost/health", timeout=5).json()
        check("0.3 /health version=" + config.VERSION,
              str(h.get("version")) == config.VERSION, str(h.get("version")))
        g = h.get("guard") or {}
        rag = h.get("rag") or {}
        check("0.4 /health 回显 Guard/RAG 配置",
              g.get("enabled") is True and g.get("approval_token_configured")
              is True and rag.get("max_candidates", 0) >= 1,
              f"guard={g} rag={rag}")

        stem = "t" + str(int(time.time() * 1000))[-8:]
        base_title = "moray-v12-" + stem
        tgt = spawn([sys.executable,
                     str(ROOT / "scripts" / "_v12_form_target.py"), stem],
                    drain=False)
        line = tgt.stdout.readline().strip()
        payload = json.loads(line[len("READY "):]) if line.startswith(
            "READY ") else {}
        hwnd = int(payload.get("hwnd") or 0)
        truth = payload.get("controls") or []
        check("0.5 靶窗就位（自建 Tk 表单，10 控件）",
              hwnd > 0 and len(truth) == 10,
              f"hwnd={hwnd} controls={len(truth)}")
        if not hwnd or len(truth) != 10:
            return 1
        force_foreground(hwnd)

        ext = scenario_extract(hwnd, truth, base_title)
        scenario_ground(hwnd, base_title, ext["elements"], truth)
        scenario_guard(hwnd, base_title, ext["elements"])
        scenario_mcp(hwnd, base_title)
    finally:
        if hwnd:
            check("5.1 靶窗销毁（只认自己 hwnd 消失）", close_hwnd(hwnd))
        for p in PROCS:
            try:
                stop_proc(p, port=8030 if p is SERVER.get("proc") else None)
            except Exception:  # noqa: BLE001
                pass
        if not port_free(8030):
            kill_by_port(8030)
        check("5.2 服务停止、端口释放", port_free(8030))
        restore_cursor(cursor0)
        print("  鼠标已归位", cursor0, flush=True)
    passed = sum(1 for x in RESULTS if x)
    print(f"\n== RESULT: {passed}/{len(RESULTS)} passed ==", flush=True)
    return len(RESULTS) - passed


if __name__ == "__main__":
    sys.exit(main())
