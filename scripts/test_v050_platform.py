"""v0.5 平台化验收：FIFO 队列 / X-Agent-Id 隔离 / emergency stop / dashboard / SDK。

测试纪律（README 红线）：本脚本零 GUI 靶子——只提交 wait 步骤的任务（不碰桌面）、
move 一律零位移，只操作自己创建的 task_id。

场景：
  S1 基线回归：无 agent 头的旧式 create+start → queued→running→done（向后兼容）。
  S2 FIFO 串行：blocker 占道期间，t2/t3(agentA) + t4(agentB) 依次入队，
     start 响应排位 1/2/3；finished_at/started_at 证明严格串行无重叠。
  S3 Agent 隔离：跨 agent GET → 403；无头 = 运维方全量；/tasks 按归属过滤。
  S4 emergency stop：清空队列（queued → canceled）+ 冻结（start/execute_action
     → 503 GHOST_FROZEN）+ running 任务步骤边界落 canceled；release 后恢复。
  S5 SDK 全链路：environment/task.run/degraded 处置/冻结异常映射。
  S6 dashboard：/dashboard 200 + /dashboard/state 免 token 快照。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from sdk import GhostClient, GhostFrozenError, TaskDegradedError  # noqa: E402

BASE = "http://127.0.0.1:8030"
token = ""
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    if line.strip().startswith("GHOST_TOKEN="):
        token = line.split("=", 1)[1].strip()

RESULTS: list = []
SFX = str(int(time.time()))[-6:]


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append(ok)
    print(("[%s] %s" % ("PASS" if ok else "FAIL", name))
          + (("  | " + detail) if detail else ""), flush=True)
    return ok


def H(agent: str = "") -> dict:
    h = {"X-Ghost-Token": token}
    if agent:
        h["X-Agent-Id"] = agent
    return h


def post(path: str, agent: str = "", body: dict = None):
    return requests.post(f"http://127.0.0.1:8030/api/ghost{path}",
                         headers=H(agent), json=body or {}, timeout=30)


def get(path: str, agent: str = ""):
    return requests.get(f"http://127.0.0.1:8030/api/ghost{path}",
                        headers=H(agent), timeout=15)


def make_task(name: str, agent: str, ms: int) -> str:
    r = post("/task", agent, {"task_id": name, "goal": "v05 " + name,
                              "steps": [{"kind": "wait", "ms": ms}]})
    assert r.status_code == 201, str(r.json())
    return name


def poll(task_id: str, want: set, agent: str = "", timeout: float = 40.0):
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = get(f"/task/{task_id}", agent).json()
        if last.get("status") in want:
            return last
        time.sleep(0.15)
    return last


def cleanup() -> None:
    """兜底解冻（幂等），保证脚本退出时 Ghost 不被冻结遗留。"""
    try:
        requests.post("http://127.0.0.1:8030/api/ghost/emergency_stop/release",
                      headers=H(), timeout=10)
    except Exception:  # noqa: BLE001
        pass


def s1_baseline() -> None:
    print("\n---- S1 基线回归（无 agent 头，兼容旧脚本）----")
    tid = f"v05_s1_{SFX}"
    make_task(tid, "", 300)
    r = post(f"/task/{tid}/start")
    ok = check("S1-1: start → 202（status=queued）", r.status_code == 202
               and r.json().get("status") == "queued", str(r.json())[:90])
    v = poll(tid, {"done"})
    ok &= check("S1-2: queued → running → done", v.get("status") == "done",
                str(v.get("status")))
    ok &= check("S1-3: view 带 agent_id=None 与 queue_position 字段",
                v.get("agent_id", "missing") is None and "queue_position" in v,
                "agent_id=%r pos=%r" % (v.get("agent_id"), v.get("queue_position")))
    check("S1x", ok)


def s2_fifo() -> None:
    print("\n---- S2 FIFO 串行 + 排位 ----")
    blocker = f"v05_s2_blk_{SFX}"
    t2 = f"v05_s2_t2_{SFX}"
    t3 = f"v05_s2_t3_{SFX}"
    t4 = f"v05_s2_t4_{SFX}"
    make_task(blocker, "agentA", 2500)
    make_task(t2, "agentA", 300)
    make_task(t3, "agentA", 300)
    make_task(t4, "agentB", 300)

    r = post(f"/task/{blocker}/start", "agentA")
    check("S2-1: blocker start → 202", r.status_code == 202)
    time.sleep(0.4)  # 让 blocker 进入 running

    starts = [post(f"/task/{t}/start", a) for t, a in
              ((t2, "agentA"), (t3, "agentA"), (t4, "agentB"))]
    pos = [s.json().get("queue_position") for s in starts]
    check("S2-2: start 响应排位 = 1/2/3", pos == [1, 2, 3], "pos=%s" % pos)
    v2 = get(f"/task/{t2}", "agentA").json()
    check("S2-3: 排队中状态可见 queued + 排位",
          v2.get("status") == "queued" and v2.get("queue_position") in (1, 2, 3),
          "status=%s pos=%s" % (v2.get("status"), v2.get("queue_position")))

    b = poll(blocker, {"done"}, "agentA")
    p2 = poll(t2, {"done"}, "agentA")
    p3 = poll(t3, {"done"}, "agentA")
    p4 = poll(t4, {"done"}, "agentB")
    order_ok = (b.get("finished_at", "") <= p2.get("started_at", "")
                and p2.get("finished_at", "") <= p3.get("started_at", "")
                and p3.get("finished_at", "") <= p4.get("started_at", ""))
    check("S2-4: FIFO 严格串行（前一任务 finished_at ≤ 后一任务 started_at）",
          order_ok and all(x.get("status") == "done" for x in (b, p2, p3, p4)),
          "b=%s t2=%s t3=%s t4=%s" % (b.get("finished_at"), p2.get("started_at"),
                                      p3.get("started_at"), p4.get("started_at")))


def s3_isolation() -> None:
    print("\n---- S3 Agent 隔离 ----")
    tid = f"v05_s3_{SFX}"
    make_task(tid, "agentB", 100)
    r = get(f"/task/{tid}", "agentA")
    check("S3-1: 跨 agent GET → 403 AGENT_FORBIDDEN",
          r.status_code == 403 and r.json().get("error") == "AGENT_FORBIDDEN",
          str(r.status_code) + " " + str(r.json().get("error")))
    check("S3-2: 归属 agent GET → 200", get(f"/task/{tid}", "agentB").status_code == 200)
    check("S3-3: 无请求头（运维方）GET → 200", get(f"/task/{tid}").status_code == 200)

    ls_a = get("/tasks", "agentA").json()
    ls_b = get("/tasks", "agentB").json()
    ls_op = get("/tasks").json()
    ids_a = [t["task_id"] for t in ls_a.get("tasks", [])]
    ids_b = [t["task_id"] for t in ls_b.get("tasks", [])]
    ids_op = [t["task_id"] for t in ls_op.get("tasks", [])]
    check("S3-4: agentA 列表不含 agentB 的任务", tid not in ids_a, str(ids_a[:3]))
    check("S3-5: agentB 列表含自己的任务", tid in ids_b)
    check("S3-6: 运维方列表全量可见", tid in ids_op)
    check("S3-7: 列表条目带 queue_position 字段",
          all("queue_position" in t for t in ls_op.get("tasks", [])))


def s4_emergency() -> None:
    print("\n---- S4 emergency stop ----")
    blocker = f"v05_s4_blk_{SFX}"
    queued = f"v05_s4_q_{SFX}"
    fresh = f"v05_s4_new_{SFX}"
    make_task(blocker, "agentA", 60000)
    make_task(queued, "agentB", 30000)
    post(f"/task/{blocker}/start", "agentA")
    time.sleep(0.4)
    post(f"/task/{queued}/start", "agentB")
    time.sleep(0.3)

    r = post("/emergency_stop")
    body = r.json()
    check("S4-1: POST /emergency_stop → frozen=true, 清队 1 个",
          r.status_code == 200 and body.get("frozen") is True
          and body.get("queue_cleared") == 1
          and body.get("canceled_tasks") == [queued], str(body)[:120])

    qb = poll(queued, {"canceled"}, "agentB", timeout=10)
    check("S4-2: 排队任务 → canceled（EMERGENCY_STOP）",
          qb.get("status") == "canceled"
          and (qb.get("fail") or {}).get("fail_class") == "EMERGENCY_STOP",
          str((qb.get("fail") or {}).get("fail_class")))
    bb = poll(blocker, {"canceled"}, "agentA", timeout=10)
    check("S4-3: running 任务在步骤边界落 canceled（wait 可打断）",
          bb.get("status") == "canceled", str(bb.get("status")))

    make_task(fresh, "agentA", 100)
    r = post(f"/task/{fresh}/start", "agentA")
    check("S4-4: 冻结期 start → 503 GHOST_FROZEN",
          r.status_code == 503 and r.json().get("error") == "GHOST_FROZEN",
          str(r.status_code) + " " + str(r.json().get("error")))
    r = requests.post("http://127.0.0.1:8030/api/ghost/execute_action",
                      headers=H("agentA"),
                      json={"action": "move", "x": 5, "y": 5}, timeout=15)
    check("S4-5: 冻结期 execute_action → 503 GHOST_FROZEN",
          r.status_code == 503 and r.json().get("error") == "GHOST_FROZEN",
          str(r.status_code))
    st = get("/dashboard/state").json()
    check("S4-6: dashboard/state 报告 frozen=true", st.get("frozen") is True)

    r = post("/emergency_stop/release")
    check("S4-7: release → 200 frozen=false",
          r.status_code == 200 and r.json().get("frozen") is False)
    r = post(f"/task/{fresh}/start", "agentA")
    v = poll(fresh, {"done"}, "agentA")
    check("S4-8: 解冻后 start 恢复并跑完", r.status_code == 202
          and v.get("status") == "done", str(v.get("status")))


def s5_sdk() -> None:
    print("\n---- S5 SDK 全链路 ----")
    client = GhostClient(base_url=BASE, token=token, agent_id="sdk-verify")
    env = client.environment.check()
    check("S5-1: client.environment.check()", env.get("ok") is True
          and "input_allowed" in env)
    view = client.task.run(
        steps=[{"kind": "wait", "ms": 300}, {"kind": "wait", "ms": 200}],
        goal="SDK verify run", timeout=30)
    check("S5-2: client.task.run() → done（自动建/启/轮询）",
          view.get("status") == "done", str(view.get("status")))

    tid = f"v05_s5_dg_{SFX}"
    try:
        client.task.run(steps=[{"kind": "require_vision", "prompt": "x"}],
                        task_id=tid, timeout=30)
        check("S5-3: require_vision → TaskDegradedError", False)
        return
    except TaskDegradedError as exc:
        ok = check("S5-3: require_vision → TaskDegradedError（带快照）",
                   exc.task.get("task_id") == tid
                   and (exc.task.get("fail") or {}).get("fail_class") == "REQUIRE_VISION",
                   str(exc)[:90])
    client.task.resume(tid)
    done = client.task.wait(tid, timeout=30)
    ok &= check("S5-4: resume + wait → done", done.get("status") == "done")

    # 冻结异常映射：emergency_stop → start 抛 GhostFrozenError → release
    client.system.emergency_stop()
    fz = f"v05_s5_fz_{SFX}"
    make_task(fz, "sdk-verify", 100)
    try:
        client.task.start(fz)
        check("S5-5: 冻结期 start → GhostFrozenError", False)
    except GhostFrozenError as exc:
        ok &= check("S5-5: 冻结期 start → GhostFrozenError", exc.status == 503,
                    str(exc)[:80])
    client.system.release()
    r = client.task.start(fz)
    ok &= check("S5-6: SDK release 后 start 恢复", r.get("ok") is True)
    client.task.wait(fz, timeout=30)
    check("S5x", ok)


def s6_dashboard() -> None:
    print("\n---- S6 dashboard ----")
    r = requests.get("http://127.0.0.1:8030/dashboard", timeout=15)
    check("S6-1: GET /dashboard → 200 HTML（含 EMERGENCY STOP）",
          r.status_code == 200 and "EMERGENCY STOP" in r.text
          and "text/html" in r.headers.get("content-type", ""))
    r = requests.get("http://127.0.0.1:8030/api/ghost/dashboard/state", timeout=15)
    st = r.json()
    check("S6-2: dashboard/state 免 token 快照（frozen/queue_depth/running/queued）",
          r.status_code == 200 and all(k in st for k in
                                       ("frozen", "queue_depth", "running", "queued")),
          str({k: st.get(k) for k in ("frozen", "queue_depth")}))
    r = requests.get("http://127.0.0.1:8030/api/ghost/screenshot?scale=0.3&quality=50",
                     timeout=15)
    check("S6-3: 截图探活放行（面板数据源可用）", r.status_code == 200)


def main() -> int:
    try:
        s1_baseline()
        s2_fifo()
        s3_isolation()
        s4_emergency()
        s5_sdk()
        s6_dashboard()
    finally:
        cleanup()
    passed = sum(1 for o in RESULTS if o)
    print("\n== platform v0.5 verify: %d/%d passed ==" % (passed, len(RESULTS)),
          flush=True)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
