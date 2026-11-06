"""等解锁（绝不绕锁、绝不模拟输入）→ 解锁后自动跑 v16 全量 + 回归链。

红线：解锁需要用户凭据——本脚本只轮询等待，绝不尝试绕过/模拟任何输入。
触发条件（连续 2 次确认）：前台不再是锁屏（LockApp/「锁屏」标题）且
environment.assert_input_allowed() 通过。解锁后再静置 30s（给用户缓冲，
此期间可终止本任务）。输出全走 stdout（外层 tee 落盘），本脚本不写文件。

跑法：.venv\\Scripts\\python.exe scripts/_v16_unlock_chain.py 2>&1 | tee logs/chain_all.log
"""
from __future__ import annotations

import subprocess
import sys
import time

ROOT = __file__.rsplit("\\scripts\\", 1)[0] if "\\scripts\\" in __file__ \
    else __file__.rsplit("/scripts/", 1)[0]
sys.path.insert(0, ROOT)

WAIT_UP_TO_H = 24.0
POLL_S = 20.0
GRACE_S = 30.0

# （标签, 相对脚本路径）——字面量，cwd=ROOT 下相对定位；输出直接透传
CHAIN = [
    ("v16_industrial", "scripts/test_v16_industrial_draw.py"),
    ("smoke", "scripts/smoke.py"),
    ("v09_draw", "scripts/test_v09_draw_trajectory.py"),
    ("v072_extreme", "scripts/test_v072_extreme.py"),
    ("v15_platforms", "scripts/test_v15_platforms.py"),
    ("v15_skills", "scripts/test_v15_skills.py"),
]


def desktop_ready() -> bool:
    """前台窗口优先于体检字段（OpenInputDesktop 对锁屏有漏报史）。"""
    try:
        import win32gui
        import win32process
        import psutil
        fg = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(fg)
        if "锁屏" in title or "lock" in title.lower():
            return False
        _t, pid = win32process.GetWindowThreadProcessId(fg)
        if psutil.Process(pid).name().lower() == "lockapp.exe":
            return False
        from app.core import environment
        environment.assert_input_allowed()
        return True
    except Exception:
        return False


def _service_up() -> bool:
    import requests
    try:
        return requests.get("http://127.0.0.1:8030/api/ghost/health",
                            timeout=2).status_code == 200
    except Exception:
        return False


def ensure_service_for_smoke():
    """smoke 探测的是「已在运行的服务」——链里为它先拉起一个。"""
    for _ in range(50):
        if _service_up():
            return None
        time.sleep(0.5)
    proc = subprocess.Popen(
        [sys.executable, "run.py"], cwd=ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        if _service_up():
            return proc
        time.sleep(0.5)
    try:
        proc.terminate()
    except Exception:  # noqa: BLE001
        pass
    return None


def main() -> int:
    skip_wait = "--now" in sys.argv
    if skip_wait:
        print("[chain] --now: skipping unlock wait", flush=True)
    else:
        print(f"[chain] waiting for desktop unlock (up to {WAIT_UP_TO_H}h; "
              "poll 20s; never bypasses the lock)", flush=True)
        deadline = time.time() + WAIT_UP_TO_H * 3600
        streak = 0
        while time.time() < deadline:
            if desktop_ready():
                streak += 1
                if streak >= 2:
                    break
            else:
                streak = 0
            time.sleep(POLL_S)
        else:
            # 睡眠/休眠会暂停本进程导致 deadline 在锁屏期间溜走——超时前
            # 再做一次终检，桌面若已可用仍照常执行。
            if desktop_ready():
                print("[chain] deadline reached but desktop is ready now",
                      flush=True)
            else:
                print("[chain] unlock wait timed out — nothing was executed",
                      flush=True)
                return 2
    print(f"[chain] desktop unlocked; grace {GRACE_S}s (kill now to abort)",
          flush=True)
    if not skip_wait:
        time.sleep(GRACE_S)
    if not desktop_ready():
        print("[chain] desktop re-locked during grace — aborting cleanly",
              flush=True)
        return 2
    failed = []
    for tag, script_rel in CHAIN:
        svc = None
        if tag == "smoke":
            svc = ensure_service_for_smoke()
            if svc is None and not _service_up():
                print(f"===== [{tag}] service unavailable, skipped =====",
                      flush=True)
                failed.append(tag)
                continue
        print(f"\n===== [{tag}] {script_rel} =====", flush=True)
        try:
            proc = subprocess.run(
                [sys.executable, script_rel], cwd=ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", timeout=1800)
            out = proc.stdout or ""
            for line in out.strip().splitlines()[-16:]:
                print(line, flush=True)
            print(f"===== [{tag}] exit={proc.returncode} =====", flush=True)
            if proc.returncode != 0:
                failed.append(tag)
        except Exception as exc:  # noqa: BLE001
            print(f"[chain] [{tag}] crashed: {exc}", flush=True)
            failed.append(tag)
        finally:
            if svc is not None:
                try:
                    svc.terminate()
                except Exception:  # noqa: BLE001
                    pass
    print("\n[chain] DONE failed=" + (",".join(failed) or "none"),
          flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
