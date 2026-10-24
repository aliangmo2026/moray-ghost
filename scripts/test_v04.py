"""v0.4 M2+M4 验收：GET /environment 环境感知 + POST /macro/{id}/heal。

测试纪律（README 红线）：GUI 靶子全部是本脚本 Popen 新开的记事本（hwnd 差集绑定、
TOPMOST 常驻、编辑区点击激活、typing 前前台门禁、只认自己的 hwnd 消失）。

M2：/environment 解锁态准确性（readable / Default 桌面 / 无锁屏 / input_allowed）；
    门禁逻辑单测（进程内合成快照 → 503 SYSTEM_NOT_READABLE 映射）。
M4：录制宏 → 关窗重开 → heal relocate（新中心即用即裁）→ dry_run/真实重放命中 →
    heal reanchor（换锚点）→ dry_run 命中新锚点位置 → 负例（越界/退化图/版本冲突/404）。
"""
from __future__ import annotations

import base64
import ctypes
import sqlite3
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
import pyautogui  # noqa: E402
import requests  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402

from app.core import window_locator as wl  # noqa: E402
from app.core.backend import release_all_modifiers  # noqa: E402
from app.core.macro_matcher import crop_around  # noqa: E402
from app.core.screen_mss import grab  # noqa: E402

token = ""
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    if line.strip().startswith("GHOST_TOKEN="):
        token = line.split("=", 1)[1].strip()
H = {"X-Ghost-Token": token}
RESULTS: list = []
OWNED: set = set()


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(ok)
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}" + (f"  | {detail}" if detail else ""), flush=True)
    return ok


def action(payload: dict):
    r = requests.post("http://127.0.0.1:8030/execute_action", headers=H,
                      json=payload, timeout=60)
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        body = {"raw": r.text[:120]}
    brief = {k: body.get(k) for k in ("error", "detail", "resolved_physical",
                                      "status") if body.get(k) is not None}
    print(f"  (action {payload.get('action')} @{payload.get('x')},{payload.get('y')})"
          f" -> {r.status_code} {brief}", flush=True)
    return r


def replay(macro_id: str, payload: dict):
    return requests.post(
        f"http://127.0.0.1:8030/macro/{macro_id}/replay", headers=H,
        json=payload, timeout=120)


def heal(macro_id: str, payload: dict):
    return requests.post(
        f"http://127.0.0.1:8030/macro/{macro_id}/heal", headers=H,
        json=payload, timeout=60)


def point_is_ours(pt: tuple, hwnd: int) -> bool:
    try:
        return win32gui.GetAncestor(win32gui.WindowFromPoint(pt), 2) == hwnd
    except Exception:  # noqa: BLE001
        return False


def force_foreground(hwnd: int) -> bool:
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        time.sleep(0.25)
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(0.4)
        if win32gui.GetForegroundWindow() == hwnd:
            return True
    except Exception:  # noqa: BLE001
        pass
    return win32gui.GetForegroundWindow() == hwnd


def open_notepad(owned_before: set, x: int, y: int, w: int = 800, h: int = 600) -> int:
    subprocess.Popen(["notepad.exe"])
    hwnd = None
    for _ in range(60):
        time.sleep(0.25)
        new = [it for it in wl.list_windows()
               if it["hwnd"] not in owned_before
               and ("记事本" in it["title"] or "notepad" in it["title"].lower())]
        if new:
            hwnd = new[0]["hwnd"]
            break
    if hwnd is None:
        return 0
    OWNED.add(hwnd)
    win32gui.MoveWindow(hwnd, x, y, w, h, True)
    wl.wait_stable(hwnd, timeout=4.0)
    win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                          win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
    time.sleep(0.4)
    return hwnd


def activate_via_click(hwnd: int, retries: int = 5) -> bool:
    """点击窗口中心（编辑区）激活——绝不点标签条（恢复标签有悬停关闭 ×）。
    每次落点不归属就重抬 TOPMOST；仪表打印便于取证。"""
    for i in range(retries):
        rect = wl.get_rect(hwnd)
        if rect is None:
            print(f"  (activate retry {i}: 窗口已消失)", flush=True)
            return False
        pt = (rect[0] + 400, rect[1] + 300)
        ours = point_is_ours(pt, hwnd)
        fg_now = win32gui.GetForegroundWindow()
        print(f"  (activate retry {i}: point_ours={ours} "
              f"fg={win32gui.GetWindowText(fg_now)[:30]!r})", flush=True)
        if not ours:
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
            time.sleep(0.35)
            continue
        r = action({"action": "click", "x": pt[0], "y": pt[1], "settle_seconds": 0.1})
        time.sleep(0.3)
        if r.status_code == 200 and win32gui.GetForegroundWindow() == hwnd:
            return True
    return win32gui.GetForegroundWindow() == hwnd


def close_owned(hwnd: int) -> bool:
    if not hwnd:
        return False
    if wl.get_rect(hwnd) is None:
        OWNED.discard(hwnd)
        return True
    try:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    except Exception:  # noqa: BLE001
        return False
    for _ in range(8):
        time.sleep(0.4)
        if wl.get_rect(hwnd) is None:
            OWNED.discard(hwnd)
            return True
        try:
            pyautogui.hotkey("alt", "n")
        finally:
            release_all_modifiers()
    gone = wl.get_rect(hwnd) is None
    if gone:
        OWNED.discard(hwnd)
    return gone


def main() -> int:
    ok_all = True
    hwnds_snapshot = {w["hwnd"] for w in wl.list_windows()}

    # ---------- M2：/environment 解锁态准确性 ----------
    r = requests.get("http://127.0.0.1:8030/environment", timeout=15)
    ok_all &= check("M2-1a: GET /environment 无 token → 401", r.status_code == 401)
    r = requests.get("http://127.0.0.1:8030/environment", headers=H, timeout=30)
    env = r.json()
    print(f"  /environment: {env}", flush=True)
    ok_all &= check("M2-1b: 解锁态准确性（readable/Default/无锁屏/无UAC/允许输入）",
                    r.status_code == 200
                    and env.get("desktop_readable") is True
                    and env.get("input_desktop_name") == "Default"
                    and env.get("lock_screen") is False
                    and env.get("uac_secure_desktop") is False
                    and env.get("input_allowed") is True
                    and env.get("foreground_present") is True,
                    f"grab={env.get('grab')} modifiers_down={env.get('modifiers_down')}")

    # M2 门禁逻辑单测（进程内合成快照，不碰真实桌面状态）
    import app.api.routes as rt  # noqa: E402
    import app.core.environment as envmod  # noqa: E402

    orig_snapshot = envmod.snapshot

    def _fake_snapshot(probe_grab: bool = True):
        return {"input_allowed": False, "desktop_readable": False,
                "uac_secure_desktop": True, "lock_screen": False,
                "foreground_present": False,
                "probe_error": "simulated BitBlt failure",
                "input_desktop_name": None}

    envmod.snapshot = _fake_snapshot
    resp = rt.execute_action(rt.ExecuteRequest(action="move", x=100, y=100))
    envmod.snapshot = orig_snapshot
    body_text = resp.body.decode("utf-8") if hasattr(resp, "body") else str(resp)
    ok_all &= check("M2-1c: 合成不可读快照 → execute_action 503 SYSTEM_NOT_READABLE",
                    resp.status_code == 503 and b"SYSTEM_NOT_READABLE" in resp.body,
                    f"status={resp.status_code} body={body_text[:120]}")

    # ---------- M4：录制 → heal relocate → 重放 ----------
    hwnd1 = open_notepad(hwnds_snapshot, 200, 200)
    ok_all &= check("M4-2a: 自建记事本#1 已定位", hwnd1 in OWNED, f"hwnd1={hwnd1}")
    if not hwnd1:
        print("== v0.4 verify: HAS FAILURES ==", flush=True)
        return 1
    ok_all &= check("M4-2b: 激活+前台门禁",
                    activate_via_click(hwnd1)
                    and win32gui.GetForegroundWindow() == hwnd1)
    rs = requests.post("http://127.0.0.1:8030/macro/record/start", headers=H,
                       json={"task_id": "t_v04"}, timeout=15)
    rect1 = wl.get_rect(hwnd1)
    btn1 = [rect1[0] + 60, rect1[1] + 86]
    r = action({"action": "click", "x": btn1[0], "y": btn1[1],
                "hwnd": hwnd1, "settle_seconds": 0.4})
    ok_all &= check("M4-2c: 录制 click 文件按钮", r.status_code == 200,
                    f"resolved={r.json().get('resolved_physical')}")
    r = requests.post("http://127.0.0.1:8030/macro/record/stop", headers=H,
                      json={"task_id": "t_v04",
                            "name": "test_macro_v04_" + str(int(time.time()))},
                      timeout=15)
    body = r.json()
    macro_id = body.get("macro_id")
    ok_all &= check("M4-2d: record/stop → macro_id（1 步）",
                    r.status_code == 200 and body.get("steps") == 1, str(body)[:100])
    action({"action": "hotkey", "text": "esc", "settle_seconds": 0.1})  # 关菜单
    ok_all &= check("M4-2e: 关闭记事本#1", close_owned(hwnd1), f"hwnd1={hwnd1}")

    hwnd2 = open_notepad(set(hwnds_snapshot) | OWNED, 900, 500)
    ok_all &= check("M4-3a: 自建记事本#2 已定位（新位置 900,500）", hwnd2 in OWNED,
                    f"hwnd2={hwnd2}")
    def fresh_button() -> list:
        """使用瞬间重取 rect（用户可能拖窗口），按钮相对偏移固定 (60,86)。"""
        rect = wl.get_rect(hwnd2)
        return [rect[0] + 60, rect[1] + 86]

    btn2 = fresh_button()
    # heal relocate：主仓 Iris 传入新中心
    r = heal(macro_id, {"task_id": "t_v04", "step_id": 1, "mode": "relocate",
                        "base_version": 1,
                        "reason": "MACRO_NOT_FOUND",
                        "vision": {"found": True, "center": btn2}})
    hb = r.json()
    ok_all &= check("M4-3b: heal relocate → 200 version=2",
                    r.status_code == 200 and hb.get("version") == 2
                    and hb.get("healed") is True, str(hb)[:140])
    r = requests.get(
        f"http://127.0.0.1:8030/macro/{macro_id}", headers=H, timeout=15)
    mrow = r.json().get("macro", {})
    new_center = (mrow.get("steps") or [{}])[0].get("target", {}).get("recorded_center")
    ok_all &= check("M4-3c: steps_json 已更新（recorded_center=新中心）",
                    new_center == btn2, f"recorded_center={new_center}")

    r = replay(macro_id, {"dry_run": True, "hwnd": hwnd2})
    bd = r.json()
    ok_all &= check("M4-3d: dry_run 用新锚点命中新位置",
                    bd.get("steps_ok") == 1
                    and abs(bd["steps"][0]["center"][0] - btn2[0]) <= 2
                    and abs(bd["steps"][0]["center"][1] - btn2[1]) <= 2,
                    f"center={bd['steps'][0].get('center') if bd.get('steps') else None}")
    force_foreground(hwnd2) or activate_via_click(hwnd2)  # Mica 失活会拉低锚点分数
    r = replay(macro_id, {"dry_run": False, "hwnd": hwnd2})
    br = r.json()
    print(f"  (真实重放响应) {r.status_code} {br}", flush=True)
    ok_all &= check("M4-3e: 真实重放 → 200（新锚点跑通）",
                    r.status_code == 200 and br.get("status") == "success",
                    f"run_id={br.get('run_id')}")
    action({"action": "hotkey", "text": "esc", "settle_seconds": 0.1})

    # heal reanchor：先往自建记事本 #2 打一行字（高纹理、稳定的真实锚点），
    # 主仓传该文本区作为新锚点图。标签条/Mica 低纹理模板实测匹配不稳，弃用。
    force_foreground(hwnd2) or activate_via_click(hwnd2)
    rect2t = wl.get_rect(hwnd2)
    txt_pt = [rect2t[0] + 400, rect2t[1] + 300]
    if win32gui.GetForegroundWindow() != hwnd2:
        force_foreground(hwnd2) or activate_via_click(hwnd2)
    r = action({"action": "click", "x": txt_pt[0], "y": txt_pt[1],
                "hwnd": hwnd2, "settle_seconds": 0.1})  # 先点击放光标（type 只移鼠标不放光标）
    r = action({"action": "type", "x": txt_pt[0], "y": txt_pt[1],
                "text": "hello v04", "hwnd": hwnd2, "settle_seconds": 0.2})
    ok_all &= check("M4-4pre0: 打字产生高纹理锚点区（前台=自建窗口才打的）",
                    r.status_code == 200
                    and win32gui.GetForegroundWindow() == hwnd2,
                    f"error={r.json().get('error')}")
    img2 = None
    rect2g = None
    for _ in range(3):
        rect2a = wl.get_rect(hwnd2)
        img2, _ = grab(1)
        rect2g = wl.get_rect(hwnd2)
        if rect2a == rect2g:
            break  # 抓屏与 rect 同相（窗口没在两次采样间被拖动）
        time.sleep(0.3)
    rel_x, rel_y = 400, 300  # 文本/光标固定在窗口相对 (400,300)（click 预置光标处）
    crop_b64 = None
    expect_rel = None
    for cand in [(rel_x, rel_y), (150, 20)]:
        cand_b64, cand_c = crop_around(
            img2, rect2g[0] + cand[0], rect2g[1] + cand[1], 64)
        arr_c = cv2.imdecode(
            np.frombuffer(base64.b64decode(cand_b64), dtype=np.uint8), cv2.IMREAD_COLOR)
        if float(arr_c.std()) > 2.0:
            crop_b64, expect_center = cand_b64, list(cand_c)
            break
    ok_all &= check("M4-4pre: reanchor 模板有纹理（非退化）", crop_b64 is not None)
    r = heal(macro_id, {"task_id": "t_v04", "step_id": 1, "mode": "reanchor",
                        "base_version": 2,
                        "vision": {"found": True, "crop": crop_b64}})
    hb2 = r.json()
    ok_all &= check("M4-4a: heal reanchor → 200 version=3",
                    r.status_code == 200 and hb2.get("version") == 3, str(hb2)[:140])
    # M4-4b: DB 替换生效（确定性断言：image_b64 == 主仓所传新锚点）
    r = requests.get(
        f"http://127.0.0.1:8030/macro/{macro_id}", headers=H, timeout=15)
    steps_now = r.json().get("macro", {}).get("steps", [])
    ok_all &= check("M4-4b: steps_json 的 image_b64 已替换为主仓所传新锚点",
                    bool(steps_now)
                    and steps_now[0].get("target", {}).get("image_b64") == crop_b64)
    # M4-4c: dry_run 命中新锚点位置（期望随最新 rect 换算；重试对抗前台抖动）
    bd2 = None
    for attempt in range(3):
        force_foreground(hwnd2) or activate_via_click(hwnd2)
        time.sleep(0.4)
        rect2r = wl.get_rect(hwnd2)
        expect_now = [rect2r[0] + expect_rel[0], rect2r[1] + expect_rel[1]]
        r = replay(macro_id, {"dry_run": True, "hwnd": hwnd2})
        bd2 = r.json()
        print(f"  (reanchor 后 dry_run attempt {attempt}) {r.status_code} {bd2} "
              f"expect={expect_now}", flush=True)
        if (bd2.get("steps_ok") == 1 and bd2.get("steps")
                and abs(bd2["steps"][0]["center"][0] - expect_now[0]) <= 3
                and abs(bd2["steps"][0]["center"][1] - expect_now[1]) <= 3):
            break
        bd2 = None
        time.sleep(0.8)
    ok_all &= check("M4-4c: dry_run 命中新锚点位置（证明替换生效）",
                    bd2 is not None,
                    f"center={bd2['steps'][0].get('center') if bd2 and bd2.get('steps') else None}")

    # ---------- M4 负例 ----------
    btn2n = fresh_button()
    r = heal(macro_id, {"task_id": "t_v04", "step_id": 1, "mode": "relocate",
                        "base_version": 3,
                        "vision": {"found": True, "center": [99999, 99999]}})
    ok_all &= check("M4-5a: relocate 越界中心 → 400", r.status_code == 400,
                    str(r.json())[:100])
    r = heal(macro_id, {"task_id": "t_v04", "step_id": 1, "mode": "relocate",
                        "base_version": 2,  # 过期版本（当前已是 3）→ 409
                        "vision": {"found": True, "center": btn2n}})
    ok_all &= check("M4-5b: base_version 冲突 → 409", r.status_code == 409,
                    str(r.json().get("error")))
    white = np.full((64, 64, 3), 255, dtype=np.uint8)
    ok_e, buf_w = cv2.imencode(".png", white)
    r = heal(macro_id, {"task_id": "t_v04", "step_id": 1, "mode": "reanchor",
                        "base_version": 3,
                        "vision": {"found": True,
                                   "crop": base64.b64encode(buf_w.tobytes()).decode("ascii")}})
    ok_all &= check("M4-5c: 纯色退化锚点 → 400", r.status_code == 400,
                    str(r.json().get("detail"))[:80])
    r = heal("m_nonexistent", {"task_id": "t_v04", "step_id": 1, "mode": "relocate",
                               "base_version": 1,
                               "vision": {"found": True, "center": btn2n}})
    ok_all &= check("M4-5d: 不存在的宏 → 404", r.status_code == 404)
    r = heal(macro_id, {"task_id": "t_v04", "step_id": 99, "mode": "relocate",
                        "base_version": 3,
                        "vision": {"found": True, "center": btn2n}})
    ok_all &= check("M4-5e: 不存在的步骤 → 404", r.status_code == 404)

    # ---------- 清场 ----------
    ok_all &= check("M4-6a: 关闭记事本#2", close_owned(hwnd2), f"hwnd2={hwnd2}")
    con = sqlite3.connect(str(ROOT / "data" / "ghost.db"))
    log_rows = con.execute(
        "SELECT COUNT(*) FROM macro_heal_log WHERE macro_id = ?",
        (macro_id,)).fetchone()[0]
    con.execute("DELETE FROM macros WHERE name LIKE ?", ("test_macro_v04%",))
    con.commit()
    con.close()
    ok_all &= check("M4-6b: macro_heal_log 审计 2 条（成功 heal 记录，冲突不记）",
                    log_rows == 2, f"rows={log_rows}")
    leftover = OWNED & {w["hwnd"] for w in wl.list_windows()}
    ok_all &= check("M4-6c: 自建窗口全部销毁（证据链）", not leftover,
                    f"owned={sorted(OWNED)} leftover={sorted(leftover)}")

    passed = sum(1 for o in RESULTS if o)
    print(f"== v0.4 verify: {passed}/{len(RESULTS)} passed ==", flush=True)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
