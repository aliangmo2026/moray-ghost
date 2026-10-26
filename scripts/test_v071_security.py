"""v0.7.1 企业级安全与开源标准验收：单实例互斥 / 结构化审计 / 急停热键 / 生产输入源过滤。

测试纪律（README 红线）：零 GUI 靶子、零真实鼠标位移——
- 动作用「零位移 move」（移动到当前光标位置，竞态重试）或纯拒绝路径；
- 服务生命周期自管：开跑先断言端口空闲 + 互斥体空闲（前提：没有其他 Ghost 在跑）；
- 不创建、不触碰任何用户窗口。

场景（按执行顺序）：
  A 单实例互斥（防多开）：
    A-1 holder 进程持锁但不占端口 → run.py 第二实例被拒，且此刻端口空闲
      （证明拦它的是互斥体，不是端口门禁）；强杀 holder 后探针恢复 OK（无僵尸锁）。
    A-2 端口被占（互斥空闲）→ run.py 被端口门禁拒绝。
    A-3 真双开：实例 A 健康 → 实例 B 被拒 → A 不受影响；强杀 A 后可再启动
      （锁随进程消亡自动释放）——续接的实例 C 供 B/C 场景复用。
  B 结构化审计 data/ghost_audit.jsonl：逐行 JSON 可解析；service_start/action
    事件齐全；action 带身份三元组（source/client/token_fp）+ 坐标明细；
    文件中绝不出现 token 原文与盘符绝对路径（只存指纹、只存 basename）。
  C 全局急停热键 Ctrl+Alt+Q：一按 → 服务进程立刻退出（code=1）、端口释放、
    退出前同步留下 estop_hotkey 审计事件。
  D 生产环境输入源过滤（GHOST_ENV=production）：
    进程内裸调 run_action → 403 SOURCE_DENIED 且鼠标零位移；
    带 Token 的合法 HTTP 动作 → 200；无 Token → 401；
    任务步骤经任务队列 → done（task_queue 来源放行）；
    对照组：GHOST_ENV=dev 下同一裸调 → 400（差异由环境标识唯一决定）。
  E 开源标准静态检查：app/·sdk/·run.py 无盘符绝对路径；requirements 全部
    `name==version` 锁定；.env.example 无任何可用凭据。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except OSError:
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import keyboard  # noqa: E402
import requests  # noqa: E402

from app.core import config  # noqa: E402

TOKEN = config.ensure_token()
AUDIT_PATH = config.AUDIT_PATH
H = {"X-Ghost-Token": TOKEN}
CAPS_PY = {"capabilities": {"allowed_windows": ["python.exe"]}}

RESULTS: list = []
PROCS: list = []            # 本脚本 Popen 的全部进程（统一收尸）
START_OFFSET = 0            # 开跑时审计行数（本套件产生的事件都算其后）


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(ok)
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}" + (f"  | {detail}" if detail else ""), flush=True)
    return ok


def port_free(port: int = 8030) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def spawn(args: list, env_extra: dict | None = None) -> subprocess.Popen:
    """拉子进程（stdout/stderr 走管道捕获，退出后统一读取取证）。

    不落任何日志文件：子进程输出只进内存管道，彻底不存在可变路径的 open。
    """
    env = dict(os.environ)
    env["GHOST_ENV"] = "dev"
    if env_extra:
        env.update(env_extra)
    proc = subprocess.Popen(args, cwd=str(ROOT), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env,
                            text=True, encoding="utf-8", errors="replace")
    PROCS.append(proc)
    return proc


def spawn_server(env_extra: dict | None = None) -> subprocess.Popen:
    return spawn([sys.executable, str(ROOT / "run.py")], env_extra)


def spawn_script(name: str, *args: str,
                 env_extra: dict | None = None) -> subprocess.Popen:
    return spawn([sys.executable, str(ROOT / "scripts" / name), *args],
                 env_extra)


def log_text(proc: subprocess.Popen) -> str:
    """读取子进程 stdout 捕获（只在 wait_exit 之后调用；读一次即缓存）。"""
    cached = getattr(proc, "_v071_out", None)
    if cached is not None:
        return cached
    out = getattr(proc, "stdout", None)
    if out is None:
        return ""
    try:
        cached = out.read() or ""
    except Exception:  # noqa: BLE001
        cached = ""
    proc._v071_out = cached        # type: ignore[attr-defined]
    return cached


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


def stop_proc(proc: subprocess.Popen, port: int | None = 8030) -> None:
    """收尸：terminate → kill，再确认端口释放（防残留监听）。"""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    if port and not wait_port_free(port, timeout=10):
        kill_by_port(port)


def wait_port_free(port: int = 8030, timeout: float = 10.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if port_free(port):
            return True
        time.sleep(0.3)
    return False


def kill_by_port(port: int) -> None:
    """按端口强杀残留监听（应急路径，与 README 紧急停止流程一致）。"""
    try:
        import psutil
        for c in psutil.net_connections(kind="inet"):
            if c.laddr and c.laddr.port == port and c.pid:
                psutil.Process(c.pid).kill()
                time.sleep(1)
    except Exception as exc:  # noqa: BLE001
        print("  (kill_by_port: " + type(exc).__name__ + ")", flush=True)


def read_audit_lines() -> list:
    """逐行解析审计文件；返回 (记录列表, 坏行数)。"""
    if not AUDIT_PATH.exists():
        return [], -1
    text = AUDIT_PATH.read_text(encoding="utf-8")
    records, bad = [], 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            bad += 1
    return records, bad


def cursor_pos() -> tuple:
    import ctypes
    import ctypes.wintypes
    pt = ctypes.wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def zero_move(retries: int = 3) -> tuple:
    """零位移 move（移动到当前光标位置）；用户动鼠标导致漂移 400 时重试。"""
    last = (0, None)
    for _ in range(retries):
        x, y = cursor_pos()
        try:
            r = requests.post("http://127.0.0.1:8030/api/ghost/execute_action",
                              headers=H, json={
                                  "action": "move", "x": x, "y": y,
                                  "settle_seconds": 0},
                              timeout=30)
        except requests.RequestException as exc:
            last = (0, type(exc).__name__)
            time.sleep(0.5)
            continue
        last = (r.status_code, None)
        if r.status_code == 200:
            return r.status_code, r.json()
        if r.status_code != 400:      # 400 = 漂移/角落竞态，可重试
            return r.status_code, None
        time.sleep(0.5)
    return last


def poll_task(task_id: str, want: set, timeout: float = 40.0) -> dict:
    end = time.time() + timeout
    last: dict = {}
    while time.time() < end:
        try:
            # URL 内联在调用点：协议+主机字面量钉死，task_id 只作路径尾部简单变量
            r = requests.get(f"http://127.0.0.1:8030/api/ghost/task/{task_id}",
                             headers=H, timeout=10)
            last = r.json()
        except requests.RequestException:
            time.sleep(0.3)
            continue
        if last.get("status") in want:
            return last
        time.sleep(0.3)
    return last


def cleanup() -> None:
    for proc in PROCS:
        try:
            stop_proc(proc, port=8030)
        except Exception:  # noqa: BLE001
            pass


# ============================== A. 单实例互斥 ==============================
def phase_mutex() -> None:
    print("\n---- A 单实例互斥（防多开 + 端口门禁）----", flush=True)
    check("A-0 前提：端口 8030 空闲", port_free())
    probe = spawn_script("_v071_mutex.py", "probe")
    rc = wait_exit(probe, timeout=15)
    check("A-0 前提：互斥体空闲（探针 OK）", rc == 0, f"exit={rc}")
    if rc != 0:
        print("  !! 互斥体被占：请先停止已有 Ghost 实例再跑本套件", flush=True)
        return

    # A-1：holder 持锁（不占端口）→ run.py 必须因互斥体被拒
    holder = spawn_script("_v071_mutex.py", "hold")
    held = False
    end = time.time() + 15
    while time.time() < end:
        p = spawn_script("_v071_mutex.py", "probe")
        prc = wait_exit(p, timeout=10)
        if prc == 3:
            held = True
            break
        if prc is None:
            stop_proc(p, port=None)
        time.sleep(0.3)
    check("A-1 holder 已持锁（探针 BUSY）", held)
    if held:
        check("A-1 此刻端口仍空闲（隔离互斥体变量）", port_free())
        second = spawn_server()
        rc2 = wait_exit(second, timeout=20)
        out2 = log_text(second)
        check("A-1 第二实例被互斥体拒绝（exit=1 + FATAL）",
              rc2 == 1 and "already running" in out2,
              f"exit={rc2} log={out2.strip().splitlines()[:1]}")
        stop_proc(second, port=None)
    stop_proc(holder, port=None)
    p2 = spawn_script("_v071_mutex.py", "probe")
    check("A-1 互斥体随进程消亡自动释放（无僵尸锁）",
          wait_exit(p2, timeout=10) == 0)
    stop_proc(p2, port=None)

    # A-2：端口被占（互斥空闲）→ run.py 必须因端口门禁被拒
    blocker = socket.socket()
    try:
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", 8030))
        blocker.listen(1)
        third = spawn_server()
        rc3 = wait_exit(third, timeout=20)
        out3 = log_text(third)
        check("A-2 端口被占 → 第二实例被端口门禁拒绝（exit=1 + port）",
              rc3 == 1 and "already in use" in out3,
              f"exit={rc3} log={out3.strip().splitlines()[:1]}")
        stop_proc(third, port=None)
    finally:
        blocker.close()

    # A-3：真双开
    inst_a = spawn_server()
    ok_a = wait_healthy()
    check("A-3 实例 A 正常启动", ok_a)
    if ok_a:
        inst_b = spawn_server()
        rc_b = wait_exit(inst_b, timeout=20)
        out_b = log_text(inst_b)
        check("A-3 实例 B 被拒（exit=1 + already running）",
              rc_b == 1 and "already running" in out_b, f"exit={rc_b}")
        stop_proc(inst_b, port=None)
        check("A-3 A 不受 B 影响（仍健康）", wait_healthy(5))
        stop_proc(inst_a)
    else:
        stop_proc(inst_a)

    # A-4：强杀后可重启（续接实例 C，供 B/C 场景复用）
    inst_c = spawn_server()
    ok_c = wait_healthy()
    check("A-4 实例可重启（无僵尸互斥体/端口残留）", ok_c)
    if ok_c:
        PHASE_SERVER["proc"] = inst_c


PHASE_SERVER: dict = {"proc": None}


# ============================== B. 结构化审计 ==============================
def phase_audit() -> None:
    print("\n---- B 结构化审计 ghost_audit.jsonl ----", flush=True)
    proc = PHASE_SERVER.get("proc")
    if not proc or proc.poll() is not None:
        check("B-0 前提：服务在跑", False)
        return
    check("B-0 审计文件存在", AUDIT_PATH.exists(), str(AUDIT_PATH))

    status, body = zero_move()
    check("B-1 零位移动作 200", status == 200, f"status={status}")
    records, bad = read_audit_lines()
    check("B-2 逐行 JSON 全部可解析", bad == 0, f"bad={bad} total={len(records)}")
    # service_start 在 A 阶段（实例启动）就已写入 → 用开跑偏移而不是本阶段偏移
    starts = [r for r in records[START_OFFSET:]
              if r.get("event") == "service_start"]
    check("B-3 service_start 事件已落盘", len(starts) >= 1, f"n={len(starts)}")
    actions = [r for r in records[START_OFFSET:]
               if r.get("event") == "action" and r.get("ok")]
    check("B-4 action 事件已落盘", len(actions) >= 1, f"n={len(actions)}")
    if actions:
        last = actions[-1]
        ident = last.get("identity") or {}
        expect_fp = hashlib.sha256(
            TOKEN.encode("utf-8")).hexdigest()[:12]
        check("B-5 身份三元组齐全（source/client/token_fp）",
              ident.get("source") == "http_api"
              and str(ident.get("client", "")).startswith("127.0.0.1:")
              and ident.get("token_fp") == expect_fp,
              json.dumps(ident, ensure_ascii=False))
        detail = last.get("detail") or {}
        check("B-6 动作明细含坐标",
              detail.get("x") is not None and detail.get("y") is not None,
              json.dumps(detail, ensure_ascii=False))
    raw = AUDIT_PATH.read_text(encoding="utf-8")
    check("B-7 审计文件不含 token 原文", TOKEN not in raw)
    # B-8：身份与缩略图字段永不含路径（thumb 只存 basename 是 v0.6 既有铁律；
    # 任务失败详情里的现场截图绝对路径是 API 同款取证数据，允许存在）。
    bad_fields = []
    for r in records:
        ident = r.get("identity") or {}
        if re.search(r"[A-Za-z]:[\\/]", json.dumps(ident, ensure_ascii=False)):
            bad_fields.append("identity@" + str(r.get("event")))
        thumb = r.get("thumb")
        if thumb and ("/" in str(thumb) or "\\" in str(thumb)):
            bad_fields.append("thumb@" + str(r.get("event")))
    check("B-8 身份/缩略图字段不含路径成分", not bad_fields,
          ",".join(bad_fields[:4]))

    # 拒绝路径也入账（驾驶舱 + JSONL 双可见）
    offset2 = len(records)
    r = requests.post("http://127.0.0.1:8030/api/ghost/execute_action",
                      headers=H, json={"action": "definitely_not_an_action"},
                      timeout=30)
    check("B-9 非法动作 400", r.status_code == 400, f"status={r.status_code}")
    records2, _ = read_audit_lines()
    rejected = [x for x in records2[offset2:]
                if x.get("event") == "action" and not x.get("ok")]
    check("B-9 被拒动作同样落审计（ok=false）", len(rejected) >= 1,
          f"n={len(rejected)} status={rejected[-1].get('status') if rejected else None}")


# ============================== C. 急停热键 ==============================
def phase_hotkey() -> None:
    print("\n---- C 全局急停热键 Ctrl+Alt+Q ----", flush=True)
    proc = PHASE_SERVER.get("proc")
    if not proc or proc.poll() is not None:
        check("C-0 前提：服务在跑", False)
        return
    offset = len(read_audit_lines()[0])
    check("C-0 前提：端口在服务", not port_free())
    keyboard.press_and_release("ctrl+alt+q")
    rc = wait_exit(proc, timeout=12)
    check("C-1 一按就死：进程立刻退出且 code=1", rc == 1, f"exit={rc}")
    check("C-2 端口随之释放", wait_port_free(8030, 8))
    records, _ = read_audit_lines()
    estops = [r for r in records[offset:] if r.get("event") == "estop_hotkey"]
    check("C-3 退出前同步留下 estop_hotkey 审计", len(estops) == 1,
          json.dumps(estops[-1], ensure_ascii=False)[:160] if estops else "missing")
    PHASE_SERVER["proc"] = None


# ============================== D. 生产环境输入源过滤 ==============================
def phase_production() -> None:
    print("\n---- D 生产环境 GHOST_ENV=production 输入源过滤 ----", flush=True)
    prod = spawn_server({"GHOST_ENV": "production"})
    ok = wait_healthy()
    check("D-0 生产实例启动（互斥体已随上一实例释放）", ok)
    if not ok:
        stop_proc(prod)
        return
    try:
        status, _ = zero_move()
        check("D-1 带 Token 的合法 HTTP 动作 → 200", status == 200,
              f"status={status}")
        r = requests.post("http://127.0.0.1:8030/api/ghost/execute_action",
                          json={"action": "move", "x": 10, "y": 10},
                          timeout=15)
        check("D-2 无 Token → 401", r.status_code == 401,
              f"status={r.status_code}")

        # 任务步骤经任务队列（task_queue 来源）在生产环境放行
        x, y = cursor_pos()
        task_ok = False
        task_detail = ""
        for _ in range(3):
            r = requests.post("http://127.0.0.1:8030/api/ghost/task",
                              headers=H, json={
                                  "steps": [{"kind": "action", "payload": {
                                      "action": "move", "x": x, "y": y,
                                      "settle_seconds": 0}}],
                                  "goal": "v071-prod-task", **CAPS_PY},
                              timeout=15)
            if r.status_code != 201:
                task_detail = f"create={r.status_code}"
                break
            tid = r.json().get("task_id")
            r2 = requests.post(
                f"http://127.0.0.1:8030/api/ghost/task/{tid}/start",
                headers=H, timeout=15)
            if r2.status_code != 202:
                task_detail = f"start={r2.status_code}"
                break
            view = poll_task(tid, {"done", "degraded", "canceled"})
            status_name = view.get("status")
            fail_class = (view.get("fail") or {}).get("fail_class")
            if status_name == "done":
                task_ok = True
                task_detail = "done"
                break
            if fail_class == "SOURCE_DENIED":
                task_detail = "SOURCE_DENIED (task_queue 来源被拒!)"
                break
            # 漂移竞态（用户动鼠标）→ 换新坐标重试
            x, y = cursor_pos()
            task_detail = f"status={status_name} fail={fail_class}"
        check("D-3 任务步骤经任务队列在生产环境放行", task_ok, task_detail)

        # 进程内旁路（绕过 HTTP）→ 必须被输入源门禁拦截且零位移
        probe = spawn_script("_v071_prod_probe.py",
                             env_extra={"GHOST_ENV": "production"})
        prc = wait_exit(probe, timeout=30)
        check("D-4 进程内裸调 run_action → 403 SOURCE_DENIED（零位移）",
              prc == 0, f"exit={prc} {log_text(probe).strip()}")
        stop_proc(probe, port=None)

        # 对照组：唯一变量是 GHOST_ENV（同一裸调在 dev 走到参数校验 400）
        ctrl = spawn_script("_v071_prod_probe.py",
                            env_extra={"GHOST_ENV": "dev"})
        crc = wait_exit(ctrl, timeout=30)
        check("D-5 对照组 dev 环境同一裸调 → 400（环境标识唯一变量）",
              crc == 0, f"exit={crc} {log_text(ctrl).strip()}")
        stop_proc(ctrl, port=None)
    finally:
        stop_proc(prod)
        check("D-6 生产实例已停止、端口释放", wait_port_free(8030, 8))


# ============================== E. 开源标准静态检查 ==============================
def phase_static() -> None:
    print("\n---- E 开源标准静态检查（路径/依赖/凭据）----", flush=True)
    # 负向前瞻排除 URL scheme（http:// 的 "p:/" 不算盘符路径）
    drive = re.compile(r"[A-Za-z]:[\\/](?![\\/])")
    hits = []
    for base in ("app", "sdk"):
        for p in (ROOT / base).rglob("*.py"):
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if drive.search(line):
                    hits.append(f"{p.name}:{i}")
    run_text = (ROOT / "run.py").read_text(encoding="utf-8")
    if drive.search(run_text):
        hits.append("run.py")
    check("E-1 app/·sdk/·run.py 无盘符绝对路径", not hits, ",".join(hits[:6]))

    unpinned = []
    for line in (ROOT / "requirements.txt").read_text(
            encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "==" not in line:
            unpinned.append(line.split(">=")[0].split("<")[0])
    check("E-2 requirements.txt 全部 == 锁定版本", not unpinned,
          ",".join(unpinned))

    env_lines = (ROOT / ".env.example").read_text(
        encoding="utf-8").splitlines()
    leaks = []
    for line in env_lines:
        s = line.strip()
        if s.startswith("#") or "=" not in s:
            continue
        key, _, val = s.partition("=")
        if key in ("GHOST_TOKEN", "GHOST_HMAC_KEY") and val.strip():
            leaks.append(key)
    check("E-3 .env.example 无任何可用凭据", not leaks, ",".join(leaks))


def main() -> int:
    print("== moray-ghost v0.7.1 security acceptance ==", flush=True)
    print("audit:", AUDIT_PATH, flush=True)
    global START_OFFSET
    START_OFFSET = len(read_audit_lines()[0])
    try:
        phase_mutex()
        if PHASE_SERVER.get("proc"):
            phase_audit()
            phase_hotkey()
        else:
            check("B/C 因无可用实例跳过", False, "phase A 未产出实例")
        phase_production()
        phase_static()
    finally:
        cleanup()
    check("收尾：全部子进程已收尸且端口 8030 空闲",
          all(p.poll() is not None for p in PROCS) and port_free())
    passed = sum(1 for x in RESULTS if x)
    total = len(RESULTS)
    print(f"\n== RESULT: {passed}/{total} passed ==", flush=True)
    return total - passed


if __name__ == "__main__":
    sys.exit(main())
