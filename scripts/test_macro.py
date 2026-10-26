"""任务 4 验收：宏录制与安全重放。

测试纪律（README 红线，本脚本强制执行）：
- 所有 GUI 靶子都是本脚本 subprocess 新开的记事本；启动前快照全部 hwnd，
  只允许操作差集内的自建窗口；绝不触碰任何既有窗口。
- 关闭只认"自己开的 hwnd 已消失"；带未保存文本时处理保存对话框（Alt+N）。
- 证据链：打印自建 hwnd 与销毁结果。不引用/展示任何用户窗口内容。

流程：录制（type hello + click 文件）→ 换位置重开 → dry_run → 真实重放 →
关窗降级 409 WINDOW_NOT_FOUND → 遮挡降级 409 MACRO_NOT_FOUND → 清场。
"""
from __future__ import annotations

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

import pyautogui  # noqa: E402
import psutil  # noqa: E402
import requests  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402
import win32process  # noqa: E402

from app.core.backend import release_all_modifiers  # noqa: E402
from app.core import window_locator as wl  # noqa: E402

token = ""
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    if line.strip().startswith("GHOST_TOKEN="):
        token = line.split("=", 1)[1].strip()
H = {"X-Ghost-Token": token}
RESULTS: list = []
OWNED: set = set()          # 本脚本创建并负责销毁的 hwnd
TMP_FILES: list = []
DX, DY, TW_B, TH_B = (12, 62, 96, 48)  # Win11 新记事本"文件"按钮相对窗口偏移
# v0.7 能力令牌（最小权限）：GUI 靶子全部是自建记事本（notepad.exe）
CAPS = {"capabilities": {"allowed_windows": ["notepad.exe"]}}


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(ok)
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}" + (f"  | {detail}" if detail else ""), flush=True)
    return ok


def action(payload: dict):
    return requests.post("http://127.0.0.1:8030/api/ghost/execute_action", headers=H,
                         json={**CAPS, **payload}, timeout=60)


def record_start(payload: dict):
    return requests.post("http://127.0.0.1:8030/api/ghost/macro/record/start", headers=H,
                         json=payload, timeout=30)


def record_stop(payload: dict):
    return requests.post("http://127.0.0.1:8030/api/ghost/macro/record/stop", headers=H,
                         json=payload, timeout=30)


def replay(macro_id: str, payload: dict):
    return requests.post(
        f"http://127.0.0.1:8030/api/ghost/macro/{macro_id}/replay", headers=H,
        json={**CAPS, **payload}, timeout=120)


def get_macro(macro_id: str):
    return requests.get(
        f"http://127.0.0.1:8030/api/ghost/macro/{macro_id}", headers=H, timeout=15)


def jbody(r):
    try:
        return r.json()
    except Exception:  # noqa: BLE001
        return {"_non_json": True, "status_code": r.status_code, "raw": r.text[:120]}


def point_is_ours(pt: tuple, hwnd: int) -> bool:
    """纪律级门禁：确认屏幕上 pt 像素的根窗口是自建窗口（点击前必查）。"""
    try:
        leaf = win32gui.WindowFromPoint(pt)
        return win32gui.GetAncestor(leaf, 2) == hwnd  # GA_ROOT
    except Exception:  # noqa: BLE001
        return False


def activate_via_click(hwnd: int, retries: int = 3) -> bool:
    """确定性前台：TOPMOST 抬升 → WindowFromPoint 验证落点归属 → 物理点击
    窗口正中（编辑区）。**绝不能点标题条/标签条**：恢复的标签页有悬停关闭 ×，
    点中即关标签（实测 3.5/任务 4 各翻车一次）。窗口中心 = 编辑区，零风险。"""
    for i in range(retries):
        rect = wl.get_rect(hwnd)
        if rect is None:
            print(f"  (activate retry {i}: 窗口已消失 rect=None)", flush=True)
            return False
        pt = (rect[0] + (rect[2] - rect[0]) // 2,
              rect[1] + (rect[3] - rect[1]) // 2)
        ours = point_is_ours(pt, hwnd)
        fg_now = win32gui.GetForegroundWindow()
        print(f"  (activate retry {i}: rect={rect} point_ours={ours} "
              f"fg={fg_now} fg_title={win32gui.GetWindowText(fg_now)[:30]!r})",
              flush=True)
        if not ours:
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
            time.sleep(0.35)
            continue
        r = action({"action": "click", "x": pt[0], "y": pt[1],
                    "settle_seconds": 0.1})
        time.sleep(0.3)
        if r.status_code == 200 and win32gui.GetForegroundWindow() == hwnd:
            return True
    return win32gui.GetForegroundWindow() == hwnd


def force_foreground(hwnd: int) -> bool:
    """多策略前台锁定；最后必须验证前台 == 自建窗口，否则返回 False（调用方必须
    中止任何 typing——纪律红线：绝不往未知前台窗口输入）。"""
    # 策略1：最小化→还原（还原会激活窗口）
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        time.sleep(0.25)
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        time.sleep(0.4)
        if win32gui.GetForegroundWindow() == hwnd:
            return True
    except Exception:  # noqa: BLE001
        pass
    # 策略2：ALT + SetForegroundWindow（alt 的 down/up 必须 try/finally 成对——
    # SetForegroundWindow 在前台锁下会抛异常，异常路径不释放 = 系统级按键挂起）
    try:
        pyautogui.keyDown("alt")
        try:
            win32gui.SetForegroundWindow(hwnd)
        finally:
            pyautogui.keyUp("alt")
            release_all_modifiers()
        time.sleep(0.2)
        if win32gui.GetForegroundWindow() == hwnd:
            return True
    except Exception:  # noqa: BLE001
        pass
    # 策略3：TOPMOST 闪烁
    try:
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
        time.sleep(0.1)
        win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
        time.sleep(0.3)
        if win32gui.GetForegroundWindow() == hwnd:
            return True
    except Exception:  # noqa: BLE001
        pass
    return win32gui.GetForegroundWindow() == hwnd


def open_notepad(owned_before: set, x: int, y: int, w: int = 800, h: int = 600) -> int:
    """纪律要求的新开窗口（v0.5 加固）：带空临时文件启动（绕过 Win11 会话恢复，
    绝不载入用户文档）→ hwnd 差集 + 唯一文件名双绑定 → 移到指定位置 → 等稳定。"""
    tmp_dir = ROOT / "data" / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_file = tmp_dir / ("moray_t_" + str(int(time.time() * 1000)) + ".txt")
    tmp_file.write_text("", encoding="utf-8")
    TMP_FILES.append(tmp_file)
    # 新记事本会把"带文件参数"的启动并进既有窗口的标签页（实测）：启动前必须
    # 无任何记事本窗口；本套脚本顺序使用记事本，发现残留即拒绝并明示
    existing = [it for it in wl.list_windows()
                if "记事本" in it["title"] or "notepad" in it["title"].lower()]
    if existing:
        print("  (open_notepad: 已有记事本窗口未关闭，拒绝启动以免并入标签页)",
              flush=True)
        return 0
    subprocess.Popen(["notepad.exe", str(tmp_file)])
    hwnd = None
    for _ in range(60):
        time.sleep(0.25)
        new = [it for it in wl.list_windows()
               if it["hwnd"] not in owned_before
               and tmp_file.stem in it["title"]]
        if new:
            hwnd = new[0]["hwnd"]
            break
    if hwnd is None:
        return 0
    OWNED.add(hwnd)
    win32gui.MoveWindow(hwnd, x, y, w, h, True)
    wl.wait_stable(hwnd, timeout=4.0)
    # TOPMOST 常驻：测试期间自建窗口保持在最上层（不被用户的全屏窗口遮挡；
    # 销毁即随窗口消失）
    win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                          win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
    time.sleep(0.4)
    return hwnd


def cleanup_tmp_files() -> None:
    for f in TMP_FILES:
        try:
            f.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


_OVERLAY_EX_NOACTIVATE = 0x08000000
_OVERLAY_GWL_EXSTYLE = -20


def open_overlay(x: int, y: int, w: int, h: int, color: str = "#3a3a3a") -> dict:
    """遮挡层（v0.5 加固）：本进程 tkinter 窗口，置顶 + WS_EX_NOACTIVATE——
    纯视觉遮挡、绝不抢焦点。取代记事本遮挡窗（绕开新记事本标签页合并与会话
    恢复两个风险面）。返回 {"tk": root, "hwnd": int}。"""
    import ctypes
    import tkinter as tk

    root = tk.Tk()
    root.overrideredirect(True)
    root.geometry(f"{w}x{h}+{x}+{y}")
    tk.Frame(root, background=color).pack(fill="both", expand=True)
    root.attributes("-topmost", True)
    root.update_idletasks()
    hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
    ex = ctypes.windll.user32.GetWindowLongW(hwnd, _OVERLAY_GWL_EXSTYLE)
    ctypes.windll.user32.SetWindowLongW(
        hwnd, _OVERLAY_GWL_EXSTYLE, ex | _OVERLAY_EX_NOACTIVATE)
    root.update()
    return {"tk": root, "hwnd": int(hwnd)}


def close_overlay(ov: dict) -> bool:
    try:
        ov["tk"].destroy()
        return True
    except Exception:  # noqa: BLE001
        return False


def raise_overlay(ov: dict) -> None:
    """重新把遮挡层抬到最上层：force_foreground(目标) 的最小化→还原会把目标
    抬到 topmost 带顶端（实测 0.99 干净命中=遮挡失效），必须在激活之后重抬。"""
    try:
        ov["tk"].attributes("-topmost", True)
        ov["tk"].lift()
        ov["tk"].update()
    except Exception:  # noqa: BLE001
        pass


def close_owned(hwnd: int) -> bool:
    """关闭自建窗口；带未保存文本时处理保存对话框（Alt+N = 不保存）。
    通过标准：本 hwnd 从窗口列表消失（不是"桌面上没有记事本"）。"""
    if not hwnd:
        return False
    if wl.get_rect(hwnd) is None:
        print(f"  (close_owned: hwnd {hwnd} 已经消失——窗口提前关闭/进程退出)",
              flush=True)
        OWNED.discard(hwnd)
        return True
    try:
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    except Exception as exc:  # noqa: BLE001
        print(f"  (close_owned: PostMessage failed: {exc})", flush=True)
        return False
    for _ in range(8):
        time.sleep(0.4)
        try:
            gone = wl.get_rect(hwnd) is None
        except Exception:  # noqa: BLE001
            gone = True
        if gone:
            OWNED.discard(hwnd)
            return True
        try:
            pyautogui.hotkey("alt", "n")  # 保存对话框：不保存
        finally:
            release_all_modifiers()
    try:
        gone = wl.get_rect(hwnd) is None
    except Exception:  # noqa: BLE001
        gone = True
    if gone:
        OWNED.discard(hwnd)
    return gone


def button_center(rect: list) -> list:
    return [rect[0] + DX + TW_B // 2, rect[1] + DY + TH_B // 2]


def main() -> int:
    ok_all = True
    hwnds_snapshot = {w["hwnd"] for w in wl.list_windows()}
    print(f"[纪律] 启动前窗口快照: {len(hwnds_snapshot)} 个 hwnd（只操作差集）", flush=True)

    # ---------- 验收 1：录制 ----------
    hwnd1 = open_notepad(hwnds_snapshot, 200, 200)
    ok_all &= check("1b: 自建记事本#1 已定位（hwnd 差集）", hwnd1 in OWNED, f"hwnd1={hwnd1}")
    if not hwnd1:
        print("== task4 verify: HAS FAILURES ==", flush=True)
        return 1
    pid1 = win32process.GetWindowThreadProcessId(hwnd1)[1]
    ok_all &= check("1c: 编辑区点击激活（record/start 之前，不进宏）",
                    activate_via_click(hwnd1), f"hwnd1={hwnd1}")
    ok_all &= check("1c2: 前台门禁（不是自建窗口绝不 typing）",
                    win32gui.GetForegroundWindow() == hwnd1, f"hwnd1={hwnd1}")
    if win32gui.GetForegroundWindow() != hwnd1:
        close_owned(hwnd1)
        print("== task4 verify: ABORT — 前台无法锁定，拒绝 typing（纪律红线）==", flush=True)
        return 1
    r = record_start({"task_id": "t_task4"})
    ok_all &= check("1a: record/start", r.status_code == 200 and r.json().get("recording"),
                    str(r.json())[:100])
    print(f"[证据] hwnd1={hwnd1} pid={pid1} 进程存活={psutil.pid_exists(pid1)}", flush=True)
    rect1 = wl.get_rect(hwnd1)
    editor = [rect1[0] + 400, rect1[1] + 300]
    btn1 = button_center(rect1)

    r = action({"action": "type", "x": editor[0], "y": editor[1], "text": "hello",
                "hwnd": hwnd1, "settle_seconds": 0.3})
    body_t = jbody(r)
    ok_all &= check("1d: 录制 type hello（走完整安全管道）",
                    r.status_code == 200
                    and Path(body_t.get("before_screenshot_path", "x")).is_file()
                    and Path(body_t.get("after_screenshot_path", "x")).is_file(),
                    f"elapsed={body_t.get('elapsed_ms')}ms error={body_t.get('error')}")
    r = action({"action": "click", "x": btn1[0], "y": btn1[1],
                "hwnd": hwnd1, "settle_seconds": 0.4})
    body_c = jbody(r)
    ok_all &= check("1e: 录制 click 文件按钮（走完整安全管道）",
                    r.status_code == 200
                    and Path(body_c.get("before_screenshot_path", "x")).is_file()
                    and Path(body_c.get("after_screenshot_path", "x")).is_file(),
                    f"resolved={body_c.get('resolved_physical')} "
                    f"error={body_c.get('error')}")

    r = record_stop({"task_id": "t_task4", "name": "test_macro_task4_"
                                                      + str(int(time.time())),
                     "app_signature": "Notepad"})
    body = jbody(r)
    macro_id = body.get("macro_id")
    ok_all &= check("1f: record/stop → macro_id", r.status_code == 200
                    and bool(macro_id), str(body)[:120])
    steps = []
    if macro_id:
        r = get_macro(macro_id)
        macro_row = jbody(r).get("macro", {})
        steps = macro_row.get("steps", [])
    ok_all &= check("1g: DB 写入成功（2 步，step2 为 anchor）",
                    len(steps) == 2
                    and steps[0].get("action") == "type"
                    and steps[1].get("action") == "click"
                    and (steps[1].get("target") or {}).get("type") == "anchor"
                    and bool((steps[1].get("target") or {}).get("image_b64")),
                    f"steps={[s.get('action') for s in steps]} "
                    f"recorded_center={steps[1].get('target', {}).get('recorded_center') if len(steps) > 1 else None}")

    # ---------- 清场 #1（关闭自建窗口，带未保存文本） ----------
    ok_all &= check("1h: 关闭记事本#1（只认自己的 hwnd 消失）", close_owned(hwnd1),
                    f"hwnd1={hwnd1} owned={sorted(OWNED)}")

    # ---------- 验收 2：换位置重开 + dry_run + 真实重放 ----------
    hwnd2 = open_notepad(set(hwnds_snapshot) | OWNED, 900, 500)
    ok_all &= check("2a: 自建记事本#2 已定位（新位置 900,500）", hwnd2 in OWNED,
                    f"hwnd2={hwnd2}")
    rect2 = wl.get_rect(hwnd2)
    btn2 = button_center(rect2)
    ok_all &= check("2b: force_foreground（含标题栏点击兜底）",
                    force_foreground(hwnd2) or activate_via_click(hwnd2),
                    f"hwnd2={hwnd2}")
    ok_all &= check("2b2: dry_run 前再次确认前台",
                    force_foreground(hwnd2) or activate_via_click(hwnd2),
                    f"fg={win32gui.GetForegroundWindow()} hwnd2={hwnd2}")

    r = replay(macro_id, {"dry_run": True, "hwnd": hwnd2})
    bd = jbody(r)
    ok_all &= check("2c: dry_run → 200 匹配报告（不产生输入，hwnd 覆盖到新窗口）",
                    r.status_code == 200 and bd.get("dry_run") is True
                    and bd.get("steps_ok") == 2,
                    f"steps={[(s.get('step_id'), s.get('status')) for s in bd.get('steps', [])]}")
    dr_click = (bd.get("steps") or [{}])[-1] if bd.get("steps") else {}
    ok_all &= check("2d: dry_run 锚点匹配到新位置",
                    dr_click.get("status") == "matched"
                    and abs(dr_click["center"][0] - btn2[0]) <= 2
                    and abs(dr_click["center"][1] - btn2[1]) <= 2,
                    f"center={dr_click.get('center')} expect={btn2}")

    r = replay(macro_id, {"dry_run": False, "hwnd": hwnd2})
    br = jbody(r)
    if br.get("error") == "MACRO_NOT_FOUND":
        # Mica 材质在窗口失活时会重混色导致锚点失配：重新激活后再试一次
        force_foreground(hwnd2) or activate_via_click(hwnd2)
        time.sleep(0.6)
        r = replay(macro_id, {"dry_run": False, "hwnd": hwnd2})
        br = jbody(r)
    ok_all &= check("2e: 真实重放 → 200 全步成功",
                    r.status_code == 200 and br.get("status") == "success"
                    and br.get("steps_ok") == 2,
                    f"run_id={br.get('run_id')} "
                    f"steps={[(s.get('step_id'), s.get('status')) for s in br.get('steps', [])]}")
    st_click = (br.get("steps") or [{}])[-1] if br.get("steps") else {}
    ok_all &= check("2f: 重放点击命中新位置（顺窗口移动找按钮）",
                    st_click.get("status") == "executed"
                    and abs(st_click["center"][0] - btn2[0]) <= 2
                    and abs(st_click["center"][1] - btn2[1]) <= 2,
                    f"center={st_click.get('center')} expect={btn2} "
                    f"find_ms={st_click.get('find_ms')}ms")
    ok_all &= check("2g: 每步前后审计截图落盘（不盲点贯穿宏）",
                    all(Path(s.get("before_screenshot_path", "x")).is_file()
                        and Path(s.get("after_screenshot_path", "x")).is_file()
                        for s in br.get("steps", [])
                        if s.get("status") == "executed"))
    con = sqlite3.connect(str(ROOT / "data" / "ghost.db"))
    row = con.execute(
        "SELECT status, steps_ok, fail_step_index FROM macro_runs WHERE run_id = ?",
        (br.get("run_id"),)).fetchone()
    con.close()
    ok_all &= check("2h: macro_runs 落库（success）",
                    row is not None and row[0] == "success" and row[1] == 2
                    and row[2] is None, f"row={row}")
    action({"action": "hotkey", "text": "esc", "settle_seconds": 0.2})  # 关菜单
    ok_all &= check("2i: 关闭记事本#2", close_owned(hwnd2), f"hwnd2={hwnd2}")

    # ---------- 验收 3：窗口不存在 → 立刻 409 WINDOW_NOT_FOUND ----------
    r = replay(macro_id, {"dry_run": False})
    b3 = r.json()
    ok_all &= check("3: 关窗后重放 → 409 WINDOW_NOT_FOUND",
                    r.status_code == 409 and b3.get("error") == "WINDOW_NOT_FOUND",
                    f"step_id={b3.get('step_id')}")

    # ---------- 验收 4：状态漂移（遮挡锚点）→ 409 MACRO_NOT_FOUND ----------
    # 编排：前台留给 #3（typing 去向合法），遮挡窗 #4 用 SWP_NOACTIVATE 抬到
    # TOPMOST 最上层（只造成视觉遮挡、不抢前台）→ step1 正常打字，step2 锚点
    # 被遮 → 409 MACRO_NOT_FOUND。
    hwnd3 = open_notepad(set(hwnds_snapshot) | OWNED, 200, 200)
    ok_all &= check("4a: 自建记事本#3 已定位", hwnd3 in OWNED, f"hwnd3={hwnd3}")
    force_foreground(hwnd3) or activate_via_click(hwnd3)
    ov4 = open_overlay(230, 240, 500, 400)
    hwnd4 = ov4["hwnd"]
    ok_all &= check("4b: 自建遮挡层#4 已就位（tkinter 覆盖层，绝不抢焦点）",
                    bool(hwnd4), f"hwnd4={hwnd4}")
    force_foreground(hwnd3)
    raise_overlay(ov4)  # 激活会把 #3 抬到覆盖层之上，必须重抬（0.99 命中教训）
    time.sleep(0.4)
    print(f"  (遮挡编排) fg={win32gui.GetForegroundWindow()} (期望 hwnd3={hwnd3})",
          flush=True)
    r = replay(macro_id, {"dry_run": False, "hwnd": hwnd3})
    b4 = jbody(r)
    print(f"  (漂移重放响应) {r.status_code} "
          f"{ {k: v for k, v in b4.items() if k != 'controller'} }", flush=True)
    fail_shot = b4.get("fail_screenshot_path")
    drifted = (r.status_code == 409
               and b4.get("error") in ("MACRO_NOT_FOUND", "WINDOW_OCCLUDED")
               and bool(b4.get("run_id"))
               and bool(fail_shot) and Path(fail_shot).is_file())
    ok_all &= check("4c: 遮挡/漂移 → 409（MACRO_NOT_FOUND 或 WINDOW_OCCLUDED）"
                    "+ run_id + 现场截图，绝不误点",
                    drifted,
                    f"error={b4.get('error')} best_score={b4.get('best_score')} "
                    f"occluder={b4.get('occluder')} fail_shot={fail_shot}")
    con = sqlite3.connect(str(ROOT / "data" / "ghost.db"))
    row4 = con.execute(
        "SELECT status, fail_step_index, fail_reason FROM macro_runs WHERE run_id = ?",
        (b4.get("run_id"),)).fetchone()
    con.close()
    ok_all &= check("4d: macro_runs 落库（failed + fail_step_index）",
                    row4 is not None and row4[0] == "failed"
                    and row4[1] is not None, f"row={row4}")

    # ---------- 清场（只关自己的 hwnd，逐个验证） ----------
    ok_all &= check("5a: 关闭记事本#3", close_owned(hwnd3), f"hwnd3={hwnd3}")
    ok_all &= check("5b: 遮挡层#4 已销毁", close_overlay(ov4), f"hwnd4={hwnd4}")
    leftover = OWNED & {w["hwnd"] for w in wl.list_windows()}
    cleanup_tmp_files()
    ok_all &= check("5c: 自建窗口全部销毁（证据链）", not leftover,
                    f"owned={sorted(OWNED)} leftover={sorted(leftover)}")

    passed = sum(1 for o in RESULTS if o)
    print(f"== task4 verify: {passed}/{len(RESULTS)} passed ==", flush=True)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
