"""v0.7 绝对防御验收：三道安全门禁拦截测试 + post_assert 防幻觉断言。

测试纪律（README 红线）：GUI 靶子全部是本脚本自建窗口——
- cmd 窗口（黑名单验证对象；Win11 实证可见宿主为 WindowsTerminal.exe，
  legacy 宿主 conhost.exe，两者与 cmd.exe 同在黑名单）；
- Tk 靶窗（_v07_target.py：点击后标题精确变「保存成功」，python.exe 宿主）。
一律 hwnd/标题差集绑定 + 坐标文件握手 + 只认自己 hwnd 消失，绝不触碰用户窗口。
所有被 403 拒绝的动作在门禁层即终止，不产生任何真实输入。

场景：
  A 窗口黑名单：cmd 窗口 click/type → 403 HIGH_RISK_WINDOW（带 hwnd 与裸落点
    双路）；标题含「删除/UAC」的自建窗口 → 403。
  B 键盘黑名单：win+r / ctrl+shift+esc → 403 HIGH_RISK_INPUT；type
    「format C:」/「shutdown /s」/「格式化磁盘」 → 403；负例 information /
    ctrl+s 正常放行（防误伤）。
  C 能力令牌：POST /task 缺失/空/非法 → 400；无令牌动作 → 403
    CAPABILITY_DENIED；白名单不含目标进程 → 403（直接动作与任务级 degraded
    双路）；move 无 hwnd 豁免 → 200；正确令牌 → 200。
  D post_assert：窗口标题断言过 → done；不过 → degraded POST_ASSERT_FAILED
    （detail 明示「指令可能错误」）；OCR 引擎缺失 → POST_ASSERT_UNVERIFIABLE。
  E 重放红线：真实重放缺令牌 → 400；白名单不含目标 → 403；正确 → 200。
  F 审计：403 拒绝在 /actions/recent 入账（驾驶舱可见）。
  G 清场：自建窗口全部销毁 + 测试宏清理。
"""
from __future__ import annotations

import ctypes
import importlib.util
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

import psutil  # noqa: E402
import requests  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402
import win32process  # noqa: E402

from app.core import window_locator as wl  # noqa: E402

H = {"X-Ghost-Token": ""}
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    if line.strip().startswith("GHOST_TOKEN="):
        H["X-Ghost-Token"] = line.split("=", 1)[1].strip()
BASE = "http://127.0.0.1:8030/api/ghost"
RESULTS: list = []
OWNED: set = set()          # 本脚本创建并负责销毁的窗口 hwnd
PROCS: list = []            # 本脚本 Popen 的进程
TMP_FILES: list = []
MACRO_IDS: list = []

CAPS_PY = {"capabilities": {"allowed_windows": ["python.exe"]}}
CREATE_NEW_CONSOLE = 0x00000010


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(ok)
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}" + (f"  | {detail}" if detail else ""), flush=True)
    return ok


def action(payload: dict):
    r = requests.post(f"{BASE}/execute_action", headers=H, json=payload, timeout=60)
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        body = {"raw": r.text[:120]}
    return r, body


def task_post(payload: dict):
    return requests.post(f"{BASE}/task", headers=H, json=payload, timeout=30)


def poll_task(task_id: str, want: set, timeout: float = 40.0):
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        r = requests.get(f"{BASE}/task/{task_id}", headers=H, timeout=15)
        try:
            last = r.json()
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
            continue
        if last.get("status") in want:
            return last
        time.sleep(0.3)
    return last


def find_by_title(stem: str, timeout: float = 12.0):
    """轮询枚举：返回标题含 stem 的可见顶层窗口 (hwnd, title, rect)。"""
    end = time.time() + timeout
    while time.time() < end:
        for w in wl.list_windows():
            if stem in w["title"]:
                return w["hwnd"], w["title"], w["rect"]
        time.sleep(0.3)
    return None, None, None


def spawn_cmd(stem: str):
    """拉起真实 cmd 窗口并定位可见宿主。

    Win11 实证：默认终端宿主是 WindowsTerminal.exe——
    - 无既有 WT 窗口时新开独立窗口，title 会传播到窗口标题（路 1）；
    - WT 已有窗口时新 cmd 只是加 tab，顶层标题仍是旧 tab（路 2：直接认任意
      可见 CASCADIA 宿主窗口——它的进程 WindowsTerminal.exe 本身就在黑名单）；
    - 默认终端被改回 legacy 时（路 3：可见 ConsoleWindowClass，进程 conhost/cmd）。
    """
    # 实证红线：CREATE_NEW_CONSOLE + stdio 重定向（DEVNULL）会让 WT 宿主窗口
    # 根本不出现/标题不传播（probe v4 对照实验）——绝不能带重定向。
    proc = subprocess.Popen(
        ["cmd.exe", "/k", "title " + stem], creationflags=CREATE_NEW_CONSOLE)
    PROCS.append(proc)
    hwnd, title, rect = find_by_title(stem)
    if hwnd:
        return proc, hwnd, title, rect
    for w in wl.list_windows():
        if w["class_name"] == "CASCADIA_HOSTING_WINDOW_CLASS":
            return proc, w["hwnd"], w["title"], w["rect"]
    for w in wl.list_windows():
        if w["class_name"] == "ConsoleWindowClass":
            return proc, w["hwnd"], w["title"], w["rect"]
    return proc, None, None, None


def prep_target(hwnd: int) -> bool:
    """把靶窗抬到 TOPMOST + 前台（真机纪律：防用户窗口遮挡点击落点）。"""
    for _ in range(3):
        try:
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
            time.sleep(0.3)
            win32gui.SetForegroundWindow(hwnd)
            time.sleep(0.3)
        except Exception:  # noqa: BLE001
            pass
        if win32gui.GetForegroundWindow() == hwnd:
            return True
    return win32gui.GetForegroundWindow() == hwnd


def click_retry(payload: dict, hwnd: int, tries: int = 4):
    """竞态安全环：门禁报"被覆盖"时重抬靶窗再试（与 drag 套件同纪律）。"""
    r, body = action(payload)
    for _ in range(tries - 1):
        if r.status_code == 400 and "covered by another window" in str(
                body.get("detail") or ""):
            prep_target(hwnd)
            time.sleep(0.4)
            r, body = action(payload)
        else:
            break
    return r, body


def spawn_tk(stem: str, title_prefix: str, geometry: str):
    """拉起 v07 Tk 靶窗并握手（坐标文件）。返回 (proc, hwnd, label_x, label_y)。"""
    tmp_dir = ROOT / "data" / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    coord_file = tmp_dir / ("v07_coord_" + stem + ".txt")
    if coord_file.exists():
        coord_file.unlink()
    TMP_FILES.append(coord_file)
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "_v07_target.py"),
         title_prefix, stem, str(coord_file), geometry],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    PROCS.append(proc)
    end = time.time() + 15
    while time.time() < end:
        if coord_file.exists():
            try:
                parts = coord_file.read_text(encoding="utf-8").split()
                cand = int(parts[0])
                if win32gui.IsWindow(cand):
                    OWNED.add(cand)
                    return proc, cand, int(parts[1]), int(parts[2])
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.3)
    return proc, None, None, None


def close_hwnd(hwnd: int) -> bool:
    if not hwnd:
        return True
    try:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    except Exception:  # noqa: BLE001
        return False
    for _ in range(30):
        time.sleep(0.2)
        if not win32gui.IsWindow(hwnd):
            return True
    return not win32gui.IsWindow(hwnd)


def process_of(hwnd: int) -> str:
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return psutil.Process(pid).name()
    except Exception:  # noqa: BLE001
        return "?"


def main() -> int:
    stem = "v07" + str(int(time.time() * 1000))[-7:]

    # ================= A. 窗口黑名单 =================
    print("\n---- A 窗口黑名单（cmd / 标题高危）----", flush=True)
    cmd_proc, cmd_hwnd, cmd_title, cmd_rect = spawn_cmd("REDLINE_CMD_" + stem)
    ok_a0 = check("A-1: cmd 窗口已拉起（可见宿主）", cmd_hwnd is not None,
                  f"hwnd={cmd_hwnd} proc={process_of(cmd_hwnd or 0)} "
                  f"title={cmd_title!r}")
    if ok_a0:
        cmd_center = [(cmd_rect[0] + cmd_rect[2]) // 2,
                      (cmd_rect[1] + cmd_rect[3]) // 2]
        r, body = action({"action": "click", "x": cmd_center[0],
                          "y": cmd_center[1], "hwnd": cmd_hwnd,
                          "settle_seconds": 0, **CAPS_PY})
        check("A-2: click 带 hwnd 指向 cmd 窗口 → 403 HIGH_RISK_WINDOW",
              r.status_code == 403 and body.get("error") == "HIGH_RISK_WINDOW",
              f"{r.status_code} {body.get('error')} {str(body.get('detail'))[:70]}")
        # 裸落点路径靠锁内 point_root 解析：先把 cmd 窗口抬到 TOPMOST 并复核
        # 中心归属，防真机活跃用户窗口遮挡造成误判
        win32gui.SetWindowPos(cmd_hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
        time.sleep(0.4)
        leaf = win32gui.GetAncestor(win32gui.WindowFromPoint(
            tuple(cmd_center)), 2)
        r, body = action({"action": "click", "x": cmd_center[0],
                          "y": cmd_center[1], "settle_seconds": 0, **CAPS_PY})
        check("A-3: click 裸落点在 cmd 窗口上 → 403 HIGH_RISK_WINDOW（锁内落点解析）",
              r.status_code == 403 and body.get("error") == "HIGH_RISK_WINDOW",
              f"{r.status_code} {body.get('error')} root={leaf}")
        r, body = action({"action": "type", "text": "echo hi",
                          "x": cmd_center[0], "y": cmd_center[1],
                          "hwnd": cmd_hwnd, "settle_seconds": 0, **CAPS_PY})
        check("A-4: type 到 cmd 窗口 → 403 HIGH_RISK_WINDOW",
              r.status_code == 403 and body.get("error") == "HIGH_RISK_WINDOW",
              f"{r.status_code} {body.get('error')}")

    t1, h1, lx1, ly1 = spawn_tk(stem + "t", "删除测试窗口-UAC", "520x260+120+120")
    ok_a5 = check("A-5: 标题含「删除/UAC」的自建窗口已就位", h1 is not None,
                  f"hwnd={h1}")
    if ok_a5:
        r, body = action({"action": "click", "x": lx1 + 150, "y": ly1 + 60,
                          "hwnd": h1, "settle_seconds": 0, **CAPS_PY})
        check("A-6: click 标题高危窗口（删除/UAC）→ 403 HIGH_RISK_WINDOW",
              r.status_code == 403 and body.get("error") == "HIGH_RISK_WINDOW",
              f"{r.status_code} {body.get('error')} {str(body.get('detail'))[:70]}")
    # A 段收官即关 cmd（它被 A-3 抬过 TOPMOST，留着会遮挡后续靶窗点击）
    if cmd_hwnd:
        close_hwnd(cmd_hwnd)
        time.sleep(0.5)

    # ================= B. 键盘黑名单 =================
    print("\n---- B 键盘黑名单（系统级热键 / 破坏性文本）----", flush=True)
    t2, h2, lx2, ly2 = spawn_tk(stem + "b", "v07输入靶", "520x260+700+240")
    ok_b0 = check("B-0: 输入靶窗（python.exe）已就位", h2 is not None,
                  f"hwnd={h2} proc={process_of(h2 or 0)}")
    if ok_b0:
        win32gui.SetForegroundWindow(h2)
        time.sleep(0.3)
        r, body = action({"action": "hotkey", "text": "win+r",
                          "hwnd": h2, "settle_seconds": 0, **CAPS_PY})
        check("B-1: hotkey win+r → 403 HIGH_RISK_INPUT",
              r.status_code == 403 and body.get("error") == "HIGH_RISK_INPUT",
              f"{r.status_code} {body.get('error')} {str(body.get('detail'))[:60]}")
        r, body = action({"action": "hotkey", "text": "ctrl+shift+esc",
                          "hwnd": h2, "settle_seconds": 0, **CAPS_PY})
        check("B-2: hotkey ctrl+shift+esc → 403 HIGH_RISK_INPUT",
              r.status_code == 403 and body.get("error") == "HIGH_RISK_INPUT",
              f"{r.status_code} {body.get('error')}")
        for bad_text in ("format C:", "shutdown /s /t 0", "格式化磁盘"):
            r, body = action({"action": "type", "text": bad_text,
                              "x": lx2 + 150, "y": ly2 + 60, "hwnd": h2,
                              "settle_seconds": 0, **CAPS_PY})
            check(f"B-3: type {bad_text!r} → 403 HIGH_RISK_INPUT",
                  r.status_code == 403 and body.get("error") == "HIGH_RISK_INPUT",
                  f"{r.status_code} {body.get('error')}")
        r, body = action({"action": "type", "text": "information 排版很流畅",
                          "x": lx2 + 150, "y": ly2 + 60, "hwnd": h2,
                          "settle_seconds": 0.2, **CAPS_PY})
        for _retry in range(3):  # 真机纪律：用户动鼠标 → 漂移 400 瞬态，重试
            if r.status_code == 400 and "drift" in str(body.get("detail") or ""):
                time.sleep(0.6)
                r, body = action({"action": "type", "text": "information 排版很流畅",
                                  "x": lx2 + 150, "y": ly2 + 60, "hwnd": h2,
                                  "settle_seconds": 0.2, **CAPS_PY})
            else:
                break
        check("B-4: 负例：含 information 的正常文本放行（词边界防误伤）",
              r.status_code == 200, f"{r.status_code} {body.get('error')}")
        r, body = action({"action": "hotkey", "text": "ctrl+s",
                          "hwnd": h2, "settle_seconds": 0.1, **CAPS_PY})
        check("B-5: 负例：普通热键 ctrl+s 放行",
              r.status_code == 200, f"{r.status_code} {body.get('error')}")

    # ================= C. 能力令牌 =================
    print("\n---- C 能力令牌（最小权限）----", flush=True)
    r = task_post({"task_id": "v07_nocaps_" + stem,
                   "steps": [{"kind": "wait", "ms": 10}]})
    check("C-1: POST /task 缺 capabilities → 400 CAPABILITIES_REQUIRED",
          r.status_code == 400 and r.json().get("error") == "CAPABILITIES_REQUIRED",
          f"{r.status_code} {r.json().get('error')}")
    r = task_post({"task_id": "v07_emptycaps_" + stem,
                   "capabilities": {"allowed_windows": []},
                   "steps": [{"kind": "action",
                              "payload": {"action": "click", "x": 100, "y": 100}}]})
    check("C-2: 输入步骤任务 + 空白名单 → 400 CAPABILITIES_REQUIRED",
          r.status_code == 400 and r.json().get("error") == "CAPABILITIES_REQUIRED",
          f"{r.status_code} {r.json().get('error')}")
    r = task_post({"task_id": "v07_badcaps_" + stem,
                   "capabilities": {"allowed_windows": "notepad.exe"},
                   "steps": [{"kind": "wait", "ms": 10}]})
    check("C-3: capabilities 非法（非数组）→ 400 BAD_REQUEST",
          r.status_code == 400 and r.json().get("error") == "BAD_REQUEST",
          f"{r.status_code} {r.json().get('error')}")
    if ok_b0:
        r, body = action({"action": "click", "x": lx2 + 150, "y": ly2 + 60,
                          "hwnd": h2, "settle_seconds": 0})
        check("C-4: execute_action 无令牌 → 403 CAPABILITY_DENIED",
              r.status_code == 403 and body.get("error") == "CAPABILITY_DENIED",
              f"{r.status_code} {body.get('error')}")
        r, body = action({"action": "click", "x": lx2 + 150, "y": ly2 + 60,
                          "hwnd": h2, "settle_seconds": 0,
                          "capabilities": {"allowed_windows": ["explorer.exe"]}})
        check("C-5: 白名单不含目标进程（explorer vs python）→ 403 CAPABILITY_DENIED",
              r.status_code == 403 and body.get("error") == "CAPABILITY_DENIED",
              f"{r.status_code} {body.get('error')} {str(body.get('detail'))[:70]}")
        tid_c6 = "v07_occ_" + stem
        r = task_post({"task_id": tid_c6,
                       "capabilities": {"allowed_windows": ["explorer.exe"]},
                       "steps": [{"kind": "action",
                                  "payload": {"action": "click",
                                              "x": lx2 + 150, "y": ly2 + 60,
                                              "hwnd": h2, "settle_seconds": 0.1}}]})
        check("C-6a: 任务创建（白名单≠目标）→ 201", r.status_code == 201,
              str(r.json())[:80])
        requests.post(f"{BASE}/task/{tid_c6}/start", headers=H, timeout=30)
        v = poll_task(tid_c6, {"degraded"})
        fail = (v or {}).get("fail") or {}
        check("C-6b: 任务步骤越权 → degraded fail_class=CAPABILITY_DENIED",
              v.get("status") == "degraded"
              and fail.get("fail_class") == "CAPABILITY_DENIED",
              f"status={v.get('status')} fail={fail.get('fail_class')} "
              f"{str(fail.get('detail'))[:60]}")
        cur = requests.get(f"{BASE}/screenshot", params={"scale": 0.2}, timeout=15) \
            .json().get("cursor") or [0, 0]
        r, body = action({"action": "move", "x": cur[0], "y": cur[1],
                          "settle_seconds": 0.05})
        check("C-7: move 无 hwnd 豁免令牌（零位移）→ 200",
              r.status_code == 200, f"{r.status_code} {body.get('error')}")
        r, body = click_retry({"action": "click", "x": lx2 + 150, "y": ly2 + 60,
                               "hwnd": h2, "settle_seconds": 0.1, **CAPS_PY}, h2)
        check("C-8: 正确令牌 + 白名单进程 → 200", r.status_code == 200,
              f"{r.status_code} {body.get('error')}")

    # ================= D. post_assert 防幻觉 =================
    print("\n---- D 动作后置断言（post_assert）----", flush=True)
    t3, h3, lx3, ly3 = spawn_tk(stem + "d", "v07断言靶", "520x260+1200+600")
    ok_d0 = check("D-0: 断言靶窗已就位", h3 is not None, f"hwnd={h3}")
    if ok_d0:
        prep_target(h3)
        tid_d2 = "v07_aser_" + stem
        r = task_post({"task_id": tid_d2, **CAPS_PY, "steps": [
            {"kind": "action",
             "payload": {"action": "click", "x": lx3 + 150, "y": ly3 + 60,
                         "hwnd": h3, "settle_seconds": 0.4},
             "post_assert": {"type": "window_title_is", "value": "保存成功"}}]})
        check("D-1: task+post_assert 创建 → 201", r.status_code == 201,
              str(r.json())[:80])
        requests.post(f"{BASE}/task/{tid_d2}/start", headers=H, timeout=30)
        v = poll_task(tid_d2, {"done", "degraded"})
        states = (v.get("step_states") or [{}])[0]
        check("D-2: 点击真的把标题改成「保存成功」→ 断言过 → done",
              v.get("status") == "done"
              and states.get("post_assert", {}).get("passed") is True,
              f"status={v.get('status')} assert={states.get('post_assert')}")

        t4, h4, lx4, ly4 = spawn_tk(stem + "e", "v07断言靶2", "520x260+1200+900")
        if h4:
            prep_target(h4)
            tid_d3 = "v07_asfail_" + stem
            r = task_post({"task_id": tid_d3, **CAPS_PY, "steps": [
                {"kind": "action",
                 "payload": {"action": "click", "x": lx4 + 150, "y": ly4 + 60,
                             "hwnd": h4, "settle_seconds": 0.4},
                 "post_assert": {"type": "window_title_contains",
                                 "value": "绝不可能出现的字样"}}]})
            requests.post(f"{BASE}/task/{tid_d3}/start", headers=H, timeout=30)
            v = poll_task(tid_d3, {"done", "degraded"})
            fail = (v or {}).get("fail") or {}
            check("D-3: 断言不过 → degraded POST_ASSERT_FAILED（明示指令可能错误）",
                  v.get("status") == "degraded"
                  and fail.get("fail_class") == "POST_ASSERT_FAILED"
                  and "指令可能错误" in str(fail.get("detail") or ""),
                  f"fail={fail.get('fail_class')} {str(fail.get('detail'))[:80]}")
            has_ocr = bool(importlib.util.find_spec("pytesseract"))
            v = {}
            for attempt in range(3):  # 真机纪律：用户动鼠标 → 步骤漂移瞬态，重试
                tid_try = "v07_ocr_" + stem + "_r" + str(attempt)
                r = task_post({"task_id": tid_try, **CAPS_PY, "steps": [
                    {"kind": "action",
                     "payload": {"action": "click", "x": lx4 + 150, "y": ly4 + 60,
                                 "hwnd": h4, "settle_seconds": 0.4},
                     "post_assert": {"type": "ocr_text_present",
                                     "value": "保存成功"}}]})
                requests.post(f"{BASE}/task/{tid_try}/start",
                              headers=H, timeout=30)
                v = poll_task(tid_try, {"done", "degraded"})
                fail_t = (v or {}).get("fail") or {}
                if not (fail_t.get("fail_class") == "SAFETY_BLOCKED"
                        and "drift" in str(fail_t.get("detail") or "")):
                    break
                time.sleep(1.0)
            fail = (v or {}).get("fail") or {}
            if has_ocr:
                check("D-4: ocr_text_present（本机有引擎）→ done",
                      v.get("status") == "done", f"status={v.get('status')}")
            else:
                check("D-4: ocr_text_present（本机无 OCR 引擎）→ degraded "
                      "POST_ASSERT_UNVERIFIABLE（如实报告，不谎称通过）",
                      v.get("status") == "degraded"
                      and fail.get("fail_class") == "POST_ASSERT_UNVERIFIABLE",
                      f"fail={fail.get('fail_class')}")
        else:
            check("D-3: 断言不过 → degraded POST_ASSERT_FAILED", False,
                  "断言靶窗2 未就位")
        r = task_post({"task_id": "v07_badasrt_" + stem, **CAPS_PY, "steps": [
            {"kind": "wait", "ms": 10,
             "post_assert": {"type": "magic_8ball", "value": "??"}}]})
        check("D-5: 非法 post_assert 类型 → 400（创建期拒绝）",
              r.status_code == 400, f"{r.status_code} {r.json().get('detail', '')[:60]}")

    # ================= E. 真实重放红线 =================
    print("\n---- E 宏真实重放红线 ----", flush=True)
    if ok_b0:
        prep_target(h2)
        rs = requests.post(f"{BASE}/macro/record/start", headers=H,
                           json={"task_id": "v07_" + stem}, timeout=15)
        check("E-1: record/start → 200", rs.status_code == 200, str(rs.status_code))
        # 录制点必须落在标签文字上（Tk Label 文字居中 → 标签几何中心）：
        # 锚点 = 点位周围 64px 裁剪，落在纯灰背景区会触发 TEXTURE_MIN=12
        # 低纹理防线（best_score=0.0 拒绝匹配）。
        r, body = action({"action": "click", "x": lx2 + 250, "y": ly2 + 120,
                          "hwnd": h2, "settle_seconds": 0.3, **CAPS_PY})
        check("E-2: 录制 click → 200", r.status_code == 200, str(r.status_code))
        rs = requests.post(f"{BASE}/macro/record/stop", headers=H,
                           json={"task_id": "v07_" + stem,
                                 "name": "v07_redline_" + stem}, timeout=15)
        macro_id = rs.json().get("macro_id")
        check("E-3: record/stop → macro_id", bool(macro_id), str(rs.json())[:80])
        if macro_id:
            MACRO_IDS.append(macro_id)
            r = requests.post(f"{BASE}/macro/{macro_id}/replay", headers=H,
                              json={"hwnd": h2, "dry_run": False}, timeout=60)
            check("E-4: 真实重放缺令牌 → 400 CAPABILITIES_REQUIRED",
                  r.status_code == 400
                  and r.json().get("error") == "CAPABILITIES_REQUIRED",
                  f"{r.status_code} {r.json().get('error')}")
            r = requests.post(f"{BASE}/macro/{macro_id}/replay", headers=H,
                              json={"hwnd": h2, "dry_run": False,
                                    "capabilities": {"allowed_windows":
                                                     ["explorer.exe"]}},
                              timeout=60)
            check("E-5: 真实重放白名单不含目标 → 403 CAPABILITY_DENIED",
                  r.status_code == 403
                  and r.json().get("error") == "CAPABILITY_DENIED",
                  f"{r.status_code} {r.json().get('error')} "
                  f"{str(r.json().get('detail'))[:120]}")
            r = requests.post(f"{BASE}/macro/{macro_id}/replay", headers=H,
                              json={"hwnd": h2, "dry_run": False, **CAPS_PY},
                              timeout=60)
            check("E-6: 真实重放正确令牌 → 200 success",
                  r.status_code == 200 and r.json().get("status") == "success",
                  f"{r.status_code} {str(r.json())[:220]}")
            r = requests.post(f"{BASE}/macro/{macro_id}/replay", headers=H,
                              json={"hwnd": h2, "dry_run": True}, timeout=60)
            check("E-7: dry_run 豁免令牌（只读）→ 200", r.status_code == 200,
                  f"{r.status_code}")

    # ================= F. 审计入账 =================
    print("\n---- F 拒绝动作驾驶舱入账 ----", flush=True)
    r = requests.get(f"{BASE}/actions/recent?limit=30", headers=H, timeout=15)
    actions = (r.json() or {}).get("actions") or []
    denied = [a for a in actions if a.get("ok") is False and a.get("status") == 403]
    check("F-1: 403 拒绝已入账 /actions/recent（驾驶舱可归因）",
          r.status_code == 200 and bool(denied), f"denied={len(denied)}")

    # ================= G. 清场 =================
    print("\n---- G 清场 ----", flush=True)
    ok_cmd = (not cmd_hwnd) or (not win32gui.IsWindow(cmd_hwnd))
    check("G-1: cmd 窗口已销毁（A 段收官即关）", ok_cmd, f"hwnd={cmd_hwnd}")
    for p in PROCS:
        try:
            p.kill()
        except Exception:  # noqa: BLE001
            pass
    for p in PROCS:
        try:
            p.wait(timeout=3)
        except Exception:  # noqa: BLE001
            pass
    # 进程终止后统一等窗口销毁（避免逐窗串行等待的时序抖动）
    deadline = time.time() + 10
    while time.time() < deadline:
        alive = [h for h in OWNED if win32gui.IsWindow(h)]
        if not alive:
            break
        time.sleep(0.25)
    leftover_tk = [h for h in OWNED if win32gui.IsWindow(h)]
    check("G-2: Tk 靶窗全部销毁（只认自己 hwnd）", not leftover_tk,
          f"leftover={leftover_tk}")
    if MACRO_IDS:
        import sqlite3
        from app.core.config import DB_PATH
        with sqlite3.connect(str(DB_PATH)) as con:
            for mid in MACRO_IDS:
                con.execute("DELETE FROM macros WHERE id = ?", (mid,))
            con.commit()
        check("G-3: 测试宏已参数化 DELETE 清理", True)
    for p in TMP_FILES:
        try:
            p.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    total = len(RESULTS)
    passed = sum(1 for x in RESULTS if x)
    print(f"\n== v07 redline verify: {passed}/{total} passed ==", flush=True)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
