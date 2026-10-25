"""sdk_moray_integration_demo.py —— 主仓 Agent 视角：用官方 SDK 接管桌面完成任务。

角色设定：你是主仓大模型，Ghost 是你的"手"。本演示用 GhostClient 完成
"打开记事本 → 输入文字 → 保存"，并在宏重放被遮挡降级时展示完整处置闭环：
读取异常快照（TaskDegradedError.task）→ 视觉处置 → resume → heal 回填。

测试纪律（README 红线）：唯一 GUI 靶子是本脚本 Popen 的记事本（空临时文件
启动绕过会话恢复 + hwnd 差集 + 唯一文件名绑定）；遮挡层是本进程 tkinter
覆盖层（绝不抢焦点）；保存写的是 data/tmp 下自己的临时文件，不碰用户文档。

GUI 纪律助手复用已验收的 test_macro.py 实现，演示聚焦 SDK 链路本身。
"""
from __future__ import annotations

import ctypes
import sqlite3
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except OSError:
        pass

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from sdk import GhostClient, TaskDegradedError  # noqa: E402
from app.core import window_locator as wl  # noqa: E402  主仓侧窗口管理用本机 API
from test_macro import (OWNED, TMP_FILES, activate_via_click, button_center,  # noqa: E402
                        close_owned, close_overlay, force_foreground,
                        open_notepad, open_overlay, raise_overlay)

RESULTS: list = []
DEMO_MARK = "moray-ghost v0.5 SDK 接管成功"


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(ok)
    print(("[%s] %s" % ("PASS" if ok else "FAIL", name))
          + (("  | " + detail) if detail else ""), flush=True)
    return ok


def main() -> int:
    ok_all = True
    client = GhostClient(agent_id="main-repo-demo")   # 主仓 Agent 的专属身份
    hwnds_snapshot = {w["hwnd"] for w in wl.list_windows()}

    print("== 主仓 Agent 接管演示（SDK only，Ghost 是唯一输入通道）==", flush=True)

    # ---------- 1. 环境体检：桌面可读才允许开工 ----------
    env = client.environment.check()
    ok_all &= check("1. environment 体检（input_allowed）",
                    env.get("input_allowed") is True,
                    "readable=%s lock=%s" % (env.get("desktop_readable"),
                                             env.get("lock_screen")))

    # ---------- 2. 打开自己的记事本，经 /window/list 发现目标 ----------
    hwnd = open_notepad(hwnds_snapshot, 200, 200)
    ok_all &= check("2a. 自建记事本已启动（hwnd 差集 + 临时文件绑定）",
                    hwnd in OWNED, f"hwnd={hwnd}")
    if not hwnd:
        print("== demo ABORT ==", flush=True)
        return 1
    found = None
    for _ in range(10):
        for it in client.window.list().get("windows", []):
            if it["hwnd"] == hwnd:
                found = it
                break
        if found:
            break
        time.sleep(0.3)
    ok_all &= check("2b. SDK window.list() 发现目标窗口",
                    found is not None and found.get("is_foreground") is not None,
                    str((found or {}).get("title", "")[:40]))

    # ---------- 3. 激活（SDK 单步动作点击编辑区） ----------
    ok_all &= check("3. action.click 激活到前台",
                    activate_via_click(hwnd) and True, f"hwnd={hwnd}")

    # ---------- 4. 输入文字：走任务队列（建+启+轮询一步到位） ----------
    rect = None
    for _ in range(3):
        rect = wl.get_rect(hwnd)
        if rect:
            break
        time.sleep(0.3)
    editor = [rect[0] + 400, rect[1] + 300]
    view = client.task.run(
        steps=[{"kind": "action",
                "payload": {"action": "type", "text": DEMO_MARK,
                            "x": editor[0], "y": editor[1], "hwnd": hwnd,
                            "settle_seconds": 0.2}}],
        goal="demo: 输入文字", timeout=60)
    ok_all &= check("4. task.run 输入文字 → done（队列串行）",
                    view.get("status") == "done", str(view.get("status")))

    # ---------- 5. 保存：Ctrl+S 原地静默保存（文件是我们自己的临时文件） ----------
    view = client.task.run(
        steps=[{"kind": "action",
                "payload": {"action": "hotkey", "text": "ctrl+s", "hwnd": hwnd,
                            "settle_seconds": 0.5}}],
        goal="demo: 保存", timeout=60)
    tmp_path = TMP_FILES[-1]
    time.sleep(0.6)
    saved = tmp_path.read_text(encoding="utf-8", errors="replace") if tmp_path.exists() else ""
    ok_all &= check("5. task.run Ctrl+S 保存 → 磁盘内容校验",
                    view.get("status") == "done" and DEMO_MARK in saved,
                    "file=%s bytes=%d" % (tmp_path.name, len(saved)))

    # ---------- 6. 录制"打开文件菜单"宏（保存路径的第一步） ----------
    rs = client.macro.record_start("sdk_demo")
    btn = button_center(wl.get_rect(hwnd))
    r = client.action.click(btn[0], btn[1], hwnd=hwnd, settle_seconds=0.4)
    ok_all &= check("6a. 录制 click 文件菜单按钮", r.get("ok") is True,
                    str(r.get("resolved_physical")))
    stop = client.macro.record_stop("sdk_demo", name="sdk_demo_menu_"
                                    + str(int(time.time())))
    macro_id = stop.get("macro_id")
    ok_all &= check("6b. record_stop → macro_id", bool(macro_id), str(stop)[:80])
    client.action.hotkey("esc", settle_seconds=0.1)  # 关掉刚打开的菜单

    # ---------- 7. 宏重放作为任务步骤：文件菜单再次打开 ----------
    view = client.task.run(
        steps=[{"kind": "macro", "macro_id": macro_id, "hwnd": hwnd}],
        goal="demo: 重放打开文件菜单", timeout=60)
    ok_all &= check("7. task.run 含 macro 步 → done（锚点顺窗口匹配）",
                    view.get("status") == "done", str(view.get("status")))
    client.action.hotkey("esc", settle_seconds=0.1)

    # ---------- 8. 遮挡 → 任务降级 → 读取异常快照 ----------
    ov = open_overlay(btn[0] - 200, btn[1] - 200, 400, 300)
    force_foreground(hwnd)
    raise_overlay(ov)  # 激活会把窗口抬到覆盖层之上，必须重抬
    time.sleep(0.4)
    tid = "sdk_demo_occ_" + str(int(time.time()))
    degraded_snapshot = None
    try:
        client.task.run(
            steps=[{"kind": "macro", "macro_id": macro_id, "hwnd": hwnd}],
            task_id=tid, goal="demo: 遮挡下的重放", timeout=60)
        check("8. 遮挡下重放 → TaskDegradedError", False)
    except TaskDegradedError as exc:
        fail = exc.task.get("fail") or {}
        degraded_snapshot = fail
        ok_all &= check("8. 遮挡下重放 → TaskDegradedError（读取异常快照）",
                        fail.get("fail_class") in ("MACRO_NOT_FOUND",
                                                   "WINDOW_OCCLUDED")
                        and fail.get("step_id") == 1,
                        "fail_class=%s best_score=%s shot=%s" % (
                            fail.get("fail_class"), fail.get("best_score"),
                            Path(fail.get("fail_screenshot_path", "x")).name))
    print("  (主仓读取快照) 主仓此刻应看 fail_screenshot 截图自行识图；"
          "本演示以'移开遮挡物'作为视觉处置结论", flush=True)

    # ---------- 9. 视觉处置（移开遮挡）→ resume 重排队 → done ----------
    close_overlay(ov)
    time.sleep(0.6)
    force_foreground(hwnd) or activate_via_click(hwnd)
    client.task.resume(tid)
    done = client.task.wait(tid, timeout=60)
    ok_all &= check("9. 处置后 resume → done（失败步重试成功）",
                    done.get("status") == "done", str(done.get("status")))
    client.action.hotkey("esc", settle_seconds=0.1)

    # ---------- 10. 视觉反哺 heal：主仓把"按钮新位置"回填锚点 ----------
    ver_row = client.macro.get(macro_id).get("macro", {})
    base_version = int(ver_row.get("version") or 1)
    btn_now = button_center(wl.get_rect(hwnd))
    heal = client.macro.heal(macro_id, step_id=1, mode="relocate",
                             base_version=base_version,
                             vision={"found": True, "center": btn_now},
                             task_id="sdk_demo")
    ok_all &= check("10a. heal relocate → version+1",
                    heal.get("healed") is True
                    and heal.get("version") == base_version + 1,
                    "v%s center=%s" % (heal.get("version"),
                                       heal.get("recorded_center")))
    dry = client.macro.replay(macro_id, dry_run=True, hwnd=hwnd)
    ok_all &= check("10b. dry_run 用 heal 后锚点命中",
                    dry.get("steps_ok") == 1
                    and abs(dry["steps"][0]["center"][0] - btn_now[0]) <= 2,
                    "center=%s expect=%s" % (dry["steps"][0].get("center"), btn_now))
    client.action.hotkey("esc", settle_seconds=0.1)

    # ---------- 11. 清场 ----------
    ok_all &= check("11a. 关闭记事本（只认自己的 hwnd 消失）", close_owned(hwnd))
    con = sqlite3.connect(str(ROOT / "data" / "ghost.db"))
    con.execute("DELETE FROM macros WHERE name LIKE ?", ("sdk_demo%",))
    con.commit()
    con.close()
    for f in TMP_FILES:
        try:
            f.unlink(missing_ok=True)
        except OSError:
            pass
    leftover = OWNED & {w["hwnd"] for w in wl.list_windows()}
    ok_all &= check("11b. 自建窗口全部销毁（证据链）", not leftover,
                    "owned=%s" % sorted(OWNED))

    passed = sum(1 for o in RESULTS if o)
    print("\n== SDK integration demo: %d/%d passed ==" % (passed, len(RESULTS)),
          flush=True)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
