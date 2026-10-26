"""v0.7.2 极限安全验收：UAC 提权免疫 / Watchdog 物理自救 / 频率熔断 / 硬超时+健康解耦。

测试纪律（README 红线）：GUI 靶子全部自建（uac 假窗 / type 文本靶窗，stdout 握手 +
hwnd 差集）；不触碰用户窗口。唯一物理副作用：拖拽测试在自建靶窗内移动 120px、
打字测试打在自建靶窗里；受害者进程会短暂按住 Ctrl+Shift（<3s）后被强杀。
所有请求 URL 均为调用点内联字面量（127.0.0.1:8030，本机被测服务）。

场景（按执行顺序）：
  A UAC 提权免疫：
    A-1 单元判定：consent.exe 进程 / 「用户账户控制」标题 → FATAL；良性窗 → None。
    A-2 集成：点击/打字自建「用户账户控制」假窗 → 403 FATAL_UAC_ELEVATION，
      光标零位移（物理切断），审计落 uac_fatal 事件。
  B Watchdog：
    B-1 物理自救：受害者按住 Ctrl+Shift → 强杀 → 看门狗数秒内释放全部修饰键
      并自行退出。
    B-2 真服务接线：Ghost 拉起看门狗子进程；Ctrl+Alt+Q 杀服务 → 看门狗退出。
  C 频率熔断（防 AI 死循环）：
    C-1 单元：同指纹 1 秒内 11 次 → rule=window 熔断；reset 后重新武装。
    C-2 集成：51 次同坐标连续动作 → 429 RATE_CIRCUIT_BREAK(rule=consecutive)，
      服务强制冻结（frozen_reason=rate_circuit_break），后续动作 503，
      审计落 rate_circuit_break；release 后恢复。
  D 硬超时 + 健康解耦（GHOST_ACTION_TIMEOUT=2）：
    D-1 6 秒拖拽进行中，/health 连打全部 <1.5s（async 健康检查不排队）。
    D-2 拖拽占锁期间并发点击 → 408 ACTION_TIMEOUT(phase=lock_wait，零输入)。
    D-3 长打字超执行预算 → 408(phase=exec_deadline, released=true)，
      物理释放按键、服务冻结(frozen_reason=action_hard_timeout)、健康检查仍 200、
      release 后恢复；审计落 action_hard_timeout。
"""
from __future__ import annotations

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

import psutil  # noqa: E402
import requests  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402

from app.core import config, safety_gate  # noqa: E402

TOKEN = config.ensure_token()
AUDIT_PATH = config.AUDIT_PATH
H = {"X-Ghost-Token": TOKEN}
CAPS_PY = {"capabilities": {"allowed_windows": ["python.exe"]}}
RESULTS: list = []
PROCS: list = []
START_OFFSET = 0
VK_CONTROL, VK_SHIFT, VK_MENU = 0x11, 0x10, 0x12
USER32 = ctypes.windll.user32


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}"
          + (f"  | {detail}" if detail else ""), flush=True)
    return ok


def port_free(port: int = 8030) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def spawn(args: list, env_extra: dict | None = None,
          drain: bool = False) -> subprocess.Popen:
    env = dict(os.environ)
    env["GHOST_ENV"] = "dev"
    env["GHOST_STACKDUMP_INTERVAL"] = "15"  # 卡死取证：周期线程栈→logs/stacks.log
    if env_extra:
        env.update(env_extra)
    proc = subprocess.Popen(args, cwd=str(ROOT), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env,
                            text=True, encoding="utf-8", errors="replace")
    PROCS.append(proc)
    if drain:
        # 服务器输出必须持续排干：4KB 管道缓冲一旦写满，服务端日志 flush 会
        # 持 logging 锁级联卡死（v0.7.2 栈转储实证的整机假死根因）。
        def _drain():
            try:
                for _ in proc.stdout:
                    pass
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=_drain, daemon=True).start()
    return proc


def spawn_server(env_extra: dict | None = None) -> subprocess.Popen:
    return spawn([sys.executable, str(ROOT / "run.py")], env_extra, drain=True)


def spawn_script(name: str, *args: str,
                 env_extra: dict | None = None) -> subprocess.Popen:
    return spawn([sys.executable, str(ROOT / "scripts" / name), *args],
                 env_extra)


def read_ready(proc: subprocess.Popen, timeout: float = 15.0):
    """从子进程 stdout 读一行 `READY ...`（后台线程 + 队列，绝不永久阻塞）。"""
    import queue
    q: queue.Queue = queue.Queue()

    def _reader():
        try:
            q.put(proc.stdout.readline())
        except Exception:  # noqa: BLE001
            q.put("")
    threading.Thread(target=_reader, daemon=True).start()
    try:
        line = q.get(timeout=timeout)
    except queue.Empty:
        return []
    parts = line.strip().split()
    if not parts or parts[0] != "READY":
        return []
    try:
        return [int(p) for p in parts[1:]]
    except ValueError:
        return []


def wait_healthy(timeout: float = 30.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = requests.get("http://127.0.0.1:8030/api/ghost/health",
                             timeout=2)
            if r.status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.4)
    return False


def wait_exit(proc: subprocess.Popen, timeout: float = 15.0) -> int | None:
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def kill_by_port(port: int) -> None:
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.laddr and c.laddr.port == port and c.pid:
                psutil.Process(c.pid).kill()
                time.sleep(1)
    except Exception as exc:  # noqa: BLE001
        print("  (kill_by_port: " + type(exc).__name__ + ")", flush=True)


def stop_proc(proc: subprocess.Popen, port: int | None = 8030) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    if port and not port_free(port):
        kill_by_port(port)


def cleanup() -> None:
    for proc in PROCS:
        try:
            stop_proc(proc, port=8030)
        except Exception:  # noqa: BLE001
            pass


def read_audit() -> list:
    if not AUDIT_PATH.exists():
        return []
    out = []
    for line in AUDIT_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def cursor_pos() -> tuple:
    import ctypes.wintypes
    pt = ctypes.wintypes.POINT()
    USER32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def modifier_down(vk: int) -> bool:
    return bool(USER32.GetAsyncKeyState(vk) & 0x8000)


def force_foreground(hwnd: int) -> bool:
    for _ in range(4):
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
        except Exception:  # noqa: BLE001
            try:  # ALT 技巧：先发 ALT 释放前台锁
                USER32.keybd_event(0x12, 0, 0, 0)
                USER32.keybd_event(0x12, 0, 2, 0)
                win32gui.SetForegroundWindow(hwnd)
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.3)
        if win32gui.GetForegroundWindow() == hwnd:
            return True
    return False


def close_hwnd(hwnd: int) -> bool:
    if not hwnd or not win32gui.IsWindow(hwnd):
        return True
    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    for _ in range(20):
        time.sleep(0.2)
        if not win32gui.IsWindow(hwnd):
            return True
    return False


# ================= A. UAC 提权免疫 =================
def phase_uac(server: subprocess.Popen) -> None:
    print("\n---- A UAC 提权免疫 ----", flush=True)
    check("A-1 consent.exe 进程 → FATAL 判定",
          safety_gate.uac_fatal_reason(
              {"process": "consent.exe", "title": "User Account Control"})
          is not None)
    check("A-1 「用户账户控制」标题 → FATAL 判定",
          safety_gate.uac_fatal_reason(
              {"process": "python.exe", "title": "用户账户控制"})
          is not None)
    check("A-1 良性窗口 → None",
          safety_gate.uac_fatal_reason(
              {"process": "notepad.exe", "title": "无标题 - 记事本"}) is None)

    before_n = len(read_audit())
    tgt = spawn_script("_v072_targets.py", "uac")
    ready = read_ready(tgt)
    check("A-2 UAC 假窗就位（自建）", len(ready) == 3, f"ready={ready}")
    if len(ready) != 3:
        return
    hwnd, cx, cy = ready
    x0, y0 = cursor_pos()
    r = requests.post("http://127.0.0.1:8030/api/ghost/execute_action",
                      headers=H, json={
                          "action": "click", "x": cx, "y": cy, "hwnd": hwnd,
                          "settle_seconds": 0, **CAPS_PY}, timeout=30)
    body = {}
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        pass
    x1, y1 = cursor_pos()
    check("A-2 点击 UAC 假窗 → 403 FATAL_UAC_ELEVATION",
          r.status_code == 403 and body.get("error") == "FATAL_UAC_ELEVATION",
          f"status={r.status_code} error={body.get('error')}")
    check("A-2 物理切断：光标零位移", (x0, y0) == (x1, y1),
          f"{(x0, y0)} -> {(x1, y1)}")
    r2 = requests.post("http://127.0.0.1:8030/api/ghost/execute_action",
                       headers=H, json={
                           "action": "type", "text": "x", "x": cx, "y": cy,
                           "hwnd": hwnd, "settle_seconds": 0, **CAPS_PY},
                       timeout=30)
    check("A-2 打字进 UAC 假窗同样 FATAL",
          r2.status_code == 403
          and r2.json().get("error") == "FATAL_UAC_ELEVATION")
    uac_events = [e for e in read_audit()[before_n:]
                  if e.get("event") == "uac_fatal"]
    check("A-2 审计落 uac_fatal 事件", len(uac_events) >= 1,
          json.dumps(uac_events[-1], ensure_ascii=False)[:140]
          if uac_events else "missing")
    check("A-2 UAC 假窗已销毁", close_hwnd(hwnd))
    stop_proc(tgt, port=None)


# ================= B-1 Watchdog 物理自救 =================
def phase_watchdog_physical() -> None:
    print("\n---- B-1 Watchdog 物理自救（强杀释放修饰键）----", flush=True)
    vic = spawn_script("_v072_victim.py")
    deadline = time.time() + 15
    while time.time() < deadline and not (modifier_down(VK_CONTROL)
                                          and modifier_down(VK_SHIFT)):
        time.sleep(0.2)
    down_ok = modifier_down(VK_CONTROL) and modifier_down(VK_SHIFT)
    check("B-1 受害者已物理按住 Ctrl+Shift", down_ok)
    if not down_ok:
        stop_proc(vic, port=None)
        return
    wd_pids = [p.pid for p in psutil.process_iter(["cmdline"])
               if p.info.get("cmdline")
               and "ghost_watchdog" in " ".join(p.info["cmdline"])]
    check("B-1 看门狗进程已独立拉起", len(wd_pids) >= 1, f"pids={wd_pids}")
    psutil.Process(vic.pid).kill()   # 任务管理器式强杀
    wait_exit(vic, timeout=5)
    deadline = time.time() + 5
    while time.time() < deadline and (modifier_down(VK_CONTROL)
                                      or modifier_down(VK_SHIFT)):
        time.sleep(0.2)
    up_ok = not modifier_down(VK_CONTROL) and not modifier_down(VK_SHIFT)
    check("B-1 强杀后修饰键被物理释放（键盘恢复自由）", up_ok)
    deadline = time.time() + 6
    while time.time() < deadline and any(psutil.pid_exists(p)
                                         for p in wd_pids):
        time.sleep(0.3)
    gone = not any(psutil.pid_exists(p) for p in wd_pids)
    check("B-1 看门狗释放后自行退出", gone)


# ================= C. 频率熔断 =================
def phase_breaker(server: subprocess.Popen) -> None:
    print("\n---- C 动作频率熔断（防 AI 死循环）----", flush=True)
    from app.core import breaker
    breaker.reset()
    fp = breaker.make_fingerprint("unit", "click", 1, 2)
    trip = None
    for _ in range(12):
        trip = breaker.hit(fp)
        if trip:
            break
    check("C-1 单元：窗口规则 11 连发熔断(rule=window)",
          trip is not None and trip.get("rule") == "window", str(trip))
    breaker.reset()
    check("C-1 reset 后重新武装", breaker.hit(fp) is None
          and not breaker.is_tripped())

    # 集成：同坐标连续动作（动作锁串行 ~4/s，触发的是连续重复规则）
    x, y = cursor_pos()
    # 光标若停在 Fail-Safe 角落，先同区域微移出角落（否则 move 全部 409）
    corners = [(0, 0), (2559, 0), (0, 1599), (2559, 1599)]
    if any(abs(x - fx) <= 2 and abs(y - fy) <= 2 for fx, fy in corners):
        USER32.SetCursorPos(min(max(x, 40), 2500), min(max(y, 40), 1500))
        x, y = cursor_pos()
    n_ok = 0
    trip_body = None
    unexpected_frozen: list = []
    for _ in range(70):
        try:
            r = requests.post("http://127.0.0.1:8030/api/ghost/execute_action",
                              headers=H, json={
                                  "action": "move", "x": x, "y": y,
                                  "settle_seconds": 0}, timeout=30)
        except requests.RequestException:
            time.sleep(0.2)
            continue
        if r.status_code == 429:
            trip_body = r.json()
            break
        if r.status_code == 503:  # 未预期的提前冻结：立即暴露（不再烧超时）
            unexpected_frozen.append(r.json())
            break
        if r.status_code == 200:
            n_ok += 1
        time.sleep(0.05)
    check("C-2 集成：连续重复 → 429 RATE_CIRCUIT_BREAK",
          trip_body is not None
          and trip_body.get("error") == "RATE_CIRCUIT_BREAK"
          and trip_body.get("rule") == "consecutive",
          f"n_ok={n_ok} frozen={json.dumps(unexpected_frozen, ensure_ascii=False)[:120]} "
          f"body={json.dumps(trip_body, ensure_ascii=False)[:160]}")
    st = requests.get("http://127.0.0.1:8030/api/ghost/dashboard/state",
                      timeout=10).json()
    check("C-2 服务被强制冻结且带原因通知",
          st.get("frozen") is True
          and st.get("frozen_reason") == "rate_circuit_break",
          json.dumps({k: st.get(k) for k in ("frozen", "frozen_reason")},
                     ensure_ascii=False))
    r = requests.post("http://127.0.0.1:8030/api/ghost/execute_action",
                      headers=H, json={"action": "move", "x": x, "y": y,
                                       "settle_seconds": 0}, timeout=15)
    check("C-2 冻结后后续动作 503 且带 frozen_reason",
          r.status_code == 503
          and r.json().get("frozen_reason") == "rate_circuit_break")
    ev = [e for e in read_audit()
          if e.get("event") == "rate_circuit_break"]
    check("C-2 审计落 rate_circuit_break 事件", len(ev) >= 1)
    r = requests.post("http://127.0.0.1:8030/api/ghost/emergency_stop/release",
                      headers=H, timeout=15)
    check("C-2 release 解冻并重新武装", r.status_code == 200)
    r = requests.post("http://127.0.0.1:8030/api/ghost/execute_action",
                      headers=H, json={"action": "move", "x": x, "y": y,
                                       "settle_seconds": 0}, timeout=30)
    check("C-2 解冻后动作恢复 200", r.status_code == 200,
          f"status={r.status_code}")


# ================= B-2 Watchdog 真服务接线 =================
def phase_watchdog_wired(server: subprocess.Popen) -> None:
    print("\n---- B-2 Watchdog 真服务接线 ----", flush=True)
    kids = []
    deadline = time.time() + 4
    while time.time() < deadline and not kids:
        try:
            # venv python.exe 是启动器：真服务是其子进程，看门狗是孙进程
            kids = [p for p in psutil.Process(server.pid).children(
                recursive=True)
                if any("ghost_watchdog" in str(a) for a in
                       (p.cmdline() or []))]
        except psutil.NoSuchProcess:
            kids = []
        if not kids:
            time.sleep(0.4)
    # venv python.exe 是启动器：每个"看门狗"实为 启动器+真解释器 两进程，
    # 断言按"至少一个"计，退出判定要求全部消失。
    check("B-2 服务进程的看门狗子进程存在", len(kids) >= 1,
          f"kids={[k.pid for k in kids]} server={server.pid}")
    import keyboard
    keyboard.press_and_release("ctrl+alt+q")
    rc = wait_exit(server, timeout=12)
    check("B-2 服务被急停杀死（exit=1）", rc == 1, f"exit={rc}")
    deadline = time.time() + 6
    while time.time() < deadline and any(psutil.pid_exists(k.pid)
                                         for k in kids):
        time.sleep(0.3)
    check("B-2 主死后看门狗退出（无残留）",
          bool(kids) and not any(psutil.pid_exists(k.pid) for k in kids))
    PHASE_SERVER["proc"] = None


PHASE_SERVER: dict = {"proc": None}


# ================= D. 硬超时 + 健康解耦 =================
def phase_timeout() -> None:
    print("---- D 硬超时 + 健康解耦（GHOST_ACTION_TIMEOUT=2）----", flush=True)
    srv = spawn_server({"GHOST_ACTION_TIMEOUT": "2"})
    PHASE_SERVER["proc"] = srv
    ok = wait_healthy()
    check("D-0 超时模式服务启动", ok)
    if not ok:
        stop_proc(srv)
        return
    tgt = spawn_script("_v072_targets.py", "type")
    ready = read_ready(tgt)
    check("D-0 文本靶窗就位", len(ready) == 3, f"ready={ready}")
    if len(ready) != 3:
        stop_proc(srv)
        return
    hwnd, cx, cy = ready
    try:
        results: list = []

        def _drag():
            r = requests.post(
                "http://127.0.0.1:8030/api/ghost/execute_action",
                headers=H, json={
                    "action": "drag", "x": cx - 60, "y": cy,
                    "x2": cx + 60, "y2": cy, "duration_ms": 6000,
                    "settle_seconds": 0, "hwnd": hwnd, **CAPS_PY},
                timeout=30)
            results.append(r.status_code)

        t = threading.Thread(target=_drag, daemon=True)
        t.start()
        time.sleep(0.6)
        lat = []
        for _ in range(6):
            t0 = time.perf_counter()
            try:
                r = requests.get("http://127.0.0.1:8030/api/ghost/health",
                                 timeout=3)
                lat.append((r.status_code,
                            int((time.perf_counter() - t0) * 1000)))
            except requests.RequestException:
                lat.append((0, 9999))
            time.sleep(0.35)
        check("D-1 长动作进行中健康检查全部即时（async 不排队）",
              all(code == 200 and ms < 1500 for code, ms in lat),
              str(lat[:6]))
        rb = requests.post("http://127.0.0.1:8030/api/ghost/execute_action",
                           headers=H, json={
                               "action": "click", "x": cx, "y": cy,
                               "hwnd": hwnd, "settle_seconds": 0, **CAPS_PY},
                           timeout=30)
        body = {}
        try:
            body = rb.json()
        except Exception:  # noqa: BLE001
            pass
        check("D-2 占锁期间并发点击 → 408 ACTION_TIMEOUT(lock_wait)",
              rb.status_code == 408 and body.get("phase") == "lock_wait",
              f"status={rb.status_code} phase={body.get('phase')}")
        t.join(timeout=20)
        check("D-1 6 秒拖拽自身完成 200（声明时长计入预算不误杀）",
              results == [200], str(results))

        check("D-3 靶窗前台就绪（打字前台门禁前提）", force_foreground(hwnd))
        before_n = len(read_audit())
        rt = requests.post("http://127.0.0.1:8030/api/ghost/execute_action",
                           headers=H, json={
                               "action": "type", "text": "a" * 500,
                               "x": cx, "y": cy, "hwnd": hwnd,
                               "settle_seconds": 0, **CAPS_PY}, timeout=60)
        tbody = {}
        try:
            tbody = rt.json()
        except Exception:  # noqa: BLE001
            pass
        check("D-3 长打字超硬超时 → 408(exec_deadline, released=true)",
              rt.status_code == 408
              and tbody.get("phase") in ("exec_deadline", "mid_action")
              and tbody.get("released") is True,
              f"status={rt.status_code} body="
              + json.dumps(tbody, ensure_ascii=False)[:180])
        time.sleep(6.5)  # 等残余打字线程（~6s）在自建靶窗内彻底收尾再关窗
        mods_up = not any(modifier_down(v) for v in (VK_CONTROL, VK_SHIFT,
                                                     VK_MENU))
        check("D-3 超时后系统修饰键全部释放", mods_up)
        st = requests.get("http://127.0.0.1:8030/api/ghost/dashboard/state",
                          timeout=10).json()
        check("D-3 服务冻结且原因=action_hard_timeout",
              st.get("frozen") is True
              and st.get("frozen_reason") == "action_hard_timeout",
              json.dumps({k: st.get(k) for k in ("frozen", "frozen_reason")},
                         ensure_ascii=False))
        rh = requests.get("http://127.0.0.1:8030/api/ghost/health", timeout=5)
        check("D-3 冻结期间健康检查仍 200", rh.status_code == 200)
        ev = [e for e in read_audit()[before_n:]
              if e.get("event") == "action_hard_timeout"]
        check("D-3 审计落 action_hard_timeout 事件", len(ev) >= 1)
        r = requests.post(
            "http://127.0.0.1:8030/api/ghost/emergency_stop/release",
            headers=H, timeout=15)
        x, y = cursor_pos()
        rr = requests.post("http://127.0.0.1:8030/api/ghost/execute_action",
                           headers=H, json={"action": "move", "x": x, "y": y,
                                            "settle_seconds": 0}, timeout=30)
        check("D-3 release 后恢复 200", r.status_code == 200
              and rr.status_code == 200,
              f"release={r.status_code} move={rr.status_code}")
    finally:
        close_hwnd(hwnd)
        stop_proc(tgt, port=None)
        stop_proc(srv)
        check("D-4 服务停止、端口释放", port_free())


def main() -> int:
    print("== moray-ghost v0.7.2 extreme-safety acceptance ==", flush=True)
    global START_OFFSET
    START_OFFSET = len(read_audit())
    try:
        phase_watchdog_physical()
        check("前提：端口 8030 空闲", port_free())
        srv = spawn_server()
        PHASE_SERVER["proc"] = srv
        check("前提：dev 服务启动", wait_healthy())
        phase_uac(srv)
        phase_breaker(srv)
        phase_watchdog_wired(srv)
        phase_timeout()
    finally:
        cleanup()
    check("收尾：全部子进程已收尸且端口空闲",
          all(p.poll() is not None for p in PROCS) and port_free())
    passed = sum(1 for x in RESULTS if x)
    print(f"\n== RESULT: {passed}/{len(RESULTS)} passed ==", flush=True)
    return len(RESULTS) - passed


if __name__ == "__main__":
    sys.exit(main())
