"""任务生命周期执行器（v0.5 平台化；v0.7 绝对防御强化）。

状态机：created → queued → running → done | degraded | canceled
（start/resume 先入 FIFO 队列（task_queue），单调度线程串行派发；
 degraded ⇄ running 经 resume 重新排队）。
- 步骤类型：macro（走 runner 完整重放管道）/ action（走 controller 安全管道）/
  wait（可被 cancel 打断）/ require_vision（degraded 挂起，等主仓视觉后 resume 跳过）。
- v0.7 能力令牌（最小权限）：create_task 强制要求 capabilities.allowed_windows
  （含 action/macro 输入步骤时必须非空）；执行每个输入步骤时注入动作 payload，
  由 controller 的锁前/锁内门禁核对目标窗口进程。
- v0.7 动作后置断言（防幻觉）：步骤可带 post_assert（窗口标题 / 锚点模板 /
  OCR 字样）；执行完毕断言未过 → 任务立刻 degraded 挂起，fail.detail 明示
  "指令可能错误"，等主仓复核后 resume（从该步重试）。
- 任一步骤失败 → 任务 degraded 挂起（线程退出），fail 信息（fail_class/现场截图/
  run_id）挂在任务上等主仓处置；resume 从失败步重试（重新排队）。
- cancel 在步骤边界安全点生效，绝不切断进行中的输入；created/queued/degraded 下直接取消。
- Agent 隔离：create_task 记录归属 agent（来自 X-Agent-Id 请求头），跨 Agent 访问
  由路由层拒绝（403）；不带头 = 本机运维方，全量可见。
- 所有输入仍走 controller/runner 的完整安全管道与环境门禁（start/resume 前置校验，
  派发时复检）。
- 注册表为内存态：服务重启即失（任务持久化列入 backlog）。
"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import audit_sink, environment, janitor, safety_gate, task_queue
from . import window_locator
from .controller import ActionError, run_action
from .macro_runner import ReplayError, replay as macro_replay

VALID_KINDS = ("macro", "action", "wait", "require_vision")
TERMINAL_STATES = ("done", "failed", "canceled")
# v0.7 post_assert 类型：窗口标题族（win32 只读）/ 锚点模板族（cv2 匹配，与宏
# 锚点同一受信原语）/ OCR 字样族（需本机 OCR 引擎，缺失时如实报不可验证）。
VALID_POST_ASSERTS = ("window_title_contains", "window_title_is",
                      "window_title_changed", "anchor_present", "anchor_absent",
                      "ocr_text_present")


class TaskError(Exception):
    """带 HTTP 语义的任务操作错误。"""

    def __init__(self, status: int, error: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.error = error
        self.detail = detail


class StepFailure(Exception):
    """步骤失败（含 require_vision 的挂起语义）；data 并入步骤状态。"""

    def __init__(self, fail_class: str, detail: str, **data: Any):
        super().__init__(detail)
        self.fail_class = fail_class
        self.detail = detail
        self.data = data


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _validate_post_assert(spec: Any, label: str) -> Dict[str, Any]:
    """规范化 post_assert 规格；非法抛 TaskError(400)。"""
    if not isinstance(spec, dict):
        raise TaskError(400, "BAD_REQUEST",
                        label + ": post_assert must be an object")
    kind = str(spec.get("type", "")).strip().lower()
    if kind not in VALID_POST_ASSERTS:
        raise TaskError(400, "BAD_REQUEST",
                        label + ": unknown post_assert type " + repr(kind)
                        + " (valid: " + ", ".join(VALID_POST_ASSERTS) + ")")
    out: Dict[str, Any] = {"type": kind}
    if kind in ("window_title_contains", "window_title_is", "ocr_text_present"):
        value = spec.get("value")
        if not isinstance(value, str) or not value.strip():
            raise TaskError(400, "BAD_REQUEST",
                            label + ": post_assert " + kind + " requires non-empty value")
        out["value"] = value
    elif kind in ("anchor_present", "anchor_absent"):
        tpl = spec.get("template_b64")
        if not isinstance(tpl, str) or not tpl.strip():
            raise TaskError(400, "BAD_REQUEST",
                            label + ": post_assert " + kind
                            + " requires template_b64 (base64 PNG crop)")
        out["template_b64"] = tpl
        thr = spec.get("threshold", 0.80)
        try:
            thr = float(thr)
        except (TypeError, ValueError):
            raise TaskError(400, "BAD_REQUEST",
                            label + ": post_assert threshold must be a number") from None
        if not 0.0 <= thr <= 1.0:
            raise TaskError(400, "BAD_REQUEST",
                            label + ": post_assert threshold must be in 0..1")
        out["threshold"] = thr
    if "monitor" in spec:
        mon = spec.get("monitor")
        if not isinstance(mon, int) or mon < 1:
            raise TaskError(400, "BAD_REQUEST",
                            label + ": post_assert monitor must be int >= 1")
        out["monitor"] = mon
    return out


def validate_steps(steps: Any) -> List[Dict[str, Any]]:
    """POST /task 的步骤规范化与校验；非法抛 TaskError(400)。"""
    if not isinstance(steps, list) or not steps:
        raise TaskError(400, "BAD_REQUEST", "steps must be a non-empty array")
    out: List[Dict[str, Any]] = []
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            raise TaskError(400, "BAD_REQUEST",
                            "step " + str(i + 1) + " must be an object")
        kind = str(s.get("kind", "")).strip().lower()
        if kind not in VALID_KINDS:
            raise TaskError(400, "BAD_REQUEST",
                            "step " + str(i + 1) + ": unknown kind " + repr(kind)
                            + " (valid: " + ", ".join(VALID_KINDS) + ")")
        step: Dict[str, Any] = {"step_id": i + 1, "kind": kind}
        if kind == "macro":
            mid = s.get("macro_id")
            if not mid:
                raise TaskError(400, "BAD_REQUEST",
                                "step " + str(i + 1) + ": macro step requires macro_id")
            hwnd = s.get("hwnd")
            if not isinstance(hwnd, int) or hwnd <= 0:
                raise TaskError(400, "BAD_REQUEST",
                                "step " + str(i + 1)
                                + ": macro step requires a positive hwnd "
                                  "(主仓重开应用后传新 hwnd)")
            step["macro_id"] = str(mid)
            step["hwnd"] = hwnd
        elif kind == "action":
            payload = s.get("payload")
            if not isinstance(payload, dict) or not payload.get("action"):
                raise TaskError(400, "BAD_REQUEST",
                                "step " + str(i + 1)
                                + ": action step requires payload.action")
            step["payload"] = payload
        elif kind == "wait":
            ms = s.get("ms", 0)
            if not isinstance(ms, int) or ms < 0 or ms > 60000:
                raise TaskError(400, "BAD_REQUEST",
                                "step " + str(i + 1) + ": wait.ms must be int 0..60000")
            step["ms"] = ms
        elif kind == "require_vision":
            step["prompt"] = s.get("prompt")
        if s.get("post_assert") is not None:
            step["post_assert"] = _validate_post_assert(
                s.get("post_assert"), "step " + str(i + 1))
        out.append(step)
    return out


class TaskRun:
    """一个任务的生命周期状态。所有读写经 lock；view() 输出快照。"""

    def __init__(self, task_id: str, steps: List[Dict[str, Any]],
                 goal: Optional[str], agent_id: Optional[str] = None,
                 capabilities: Optional[Dict[str, Any]] = None):
        self.task_id = task_id
        self.goal = goal
        self.agent_id = agent_id      # 归属 Agent（X-Agent-Id）；None=本机运维方
        self.capabilities = capabilities or {"allowed_windows": []}
        self.steps = steps
        self.status = "created"
        self.step_states: List[Dict[str, Any]] = [
            {"step_id": s["step_id"], "kind": s["kind"], "status": "pending"}
            for s in steps
        ]
        self.current_index = 0          # 下一个要执行的步骤下标
        self.cancel_event = threading.Event()
        self.lock = threading.RLock()
        self.fail: Optional[Dict[str, Any]] = None
        # v0.7.1 准入身份快照：start/resume 请求的审计上下文（谁、哪个 token
        # 启动了本任务）；派发线程回放它，任务步骤的审计可归因到准入请求。
        self.admission: Dict[str, Any] = {}
        self.created_at = _now()
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None

    def view(self) -> Dict[str, Any]:
        with self.lock:
            current = (self.steps[self.current_index]["step_id"]
                       if self.current_index < len(self.steps) else None)
            return {
                "ok": True,
                "task_id": self.task_id,
                "agent_id": self.agent_id,
                "goal": self.goal,
                "status": self.status,
                "capabilities": dict(self.capabilities),
                "queue_position": task_queue.position(self.task_id),
                "steps_total": len(self.steps),
                "current_step_id": current,
                "step_states": [dict(s) for s in self.step_states],
                "fail": dict(self.fail) if self.fail else None,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            }

    def brief(self) -> Dict[str, Any]:
        """列表用摘要（不含 step_states/fail 大字段）。"""
        with self.lock:
            current = (self.steps[self.current_index]["step_id"]
                       if self.current_index < len(self.steps) else None)
            return {
                "task_id": self.task_id,
                "agent_id": self.agent_id,
                "goal": self.goal,
                "status": self.status,
                "queue_position": task_queue.position(self.task_id),
                "steps_total": len(self.steps),
                "current_step_id": current,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            }


_tasks: Dict[str, TaskRun] = {}
_reg_lock = threading.Lock()


def create_task(steps: List[Dict[str, Any]], goal: Optional[str],
                task_id: Optional[str], agent_id: Optional[str] = None,
                capabilities: Any = None) -> TaskRun:
    tid = str(task_id) if task_id else "task_" + uuid.uuid4().hex[:12]
    with _reg_lock:
        if tid in _tasks:
            # 冲突检查先行（既有 409 语义不变），随后才做 v0.7 令牌校验
            raise TaskError(409, "TASK_ID_CONFLICT",
                            "task " + repr(tid) + " already exists")
        if capabilities is None:
            raise TaskError(
                400, "CAPABILITIES_REQUIRED",
                'task creation requires capabilities, e.g. '
                '{"capabilities": {"allowed_windows": ["notepad.exe"]}} '
                '(最小权限原则：任务必须声明可触碰的窗口白名单)')
        try:
            caps = safety_gate.parse_capabilities(capabilities)
        except ValueError as exc:
            raise TaskError(400, "BAD_REQUEST", str(exc)) from exc
        if safety_gate.has_input_steps(steps) and not caps["allowed_windows"]:
            raise TaskError(
                400, "CAPABILITIES_REQUIRED",
                "tasks with action/macro steps require a non-empty "
                "capabilities.allowed_windows (最小权限原则)")
        task = TaskRun(tid, steps, goal, agent_id=agent_id, capabilities=caps)
        _tasks[tid] = task
        return task


def get_task(task_id: str) -> Optional[TaskRun]:
    with _reg_lock:
        return _tasks.get(task_id)


def list_tasks(agent_id: Optional[str] = None,
               limit: int = 50) -> List[Dict[str, Any]]:
    """任务列表摘要，按创建时间倒序。

    agent_id 给出时（带 X-Agent-Id 的 Agent 视角）只返回归属该 Agent 的任务；
    None（本机运维方）返回全部。
    """
    with _reg_lock:
        items = [t for t in _tasks.values()
                 if agent_id is None or t.agent_id == agent_id]
    items.sort(key=lambda t: t.created_at, reverse=True)
    return [t.brief() for t in items[:limit]]


def _spawn(task: TaskRun) -> None:
    """（v0.5 已废弃独立线程）保留占位防止外部误用；执行统一走 task_queue 调度。"""
    raise NotImplementedError("v0.5: tasks are dispatched via task_queue (start_task enqueues)")


def start_task(task: TaskRun) -> None:
    try:
        environment.assert_input_allowed()
    except environment.SystemNotReadable as exc:
        raise TaskError(503, "SYSTEM_NOT_READABLE", exc.detail)
    with task.lock:
        if task.status != "created":
            raise TaskError(409, "INVALID_STATE",
                            "start requires status=created, got " + task.status)
        task.status = "queued"      # 派发线程把它排队到 running（FIFO）
    task_queue.enqueue(task)


def resume_task(task: TaskRun) -> None:
    try:
        environment.assert_input_allowed()
    except environment.SystemNotReadable as exc:
        raise TaskError(503, "SYSTEM_NOT_READABLE", exc.detail)
    with task.lock:
        if task.status != "degraded":
            raise TaskError(409, "INVALID_STATE",
                            "resume requires status=degraded, got " + task.status)
        # require_vision 挂起点：主仓已完成视觉处置，标记该步完成并继续
        if task.fail and task.fail.get("await_vision"):
            idx = task.current_index
            task.step_states[idx]["status"] = "done"
            task.step_states[idx]["result"] = {
                "skipped": "vision handled by main repo"}
            task.current_index = idx + 1
        task.fail = None
        task.status = "queued"      # 重新排队（队尾），由调度线程串行派发
    task_queue.enqueue(task)


def cancel_task(task: TaskRun) -> str:
    with task.lock:
        if task.status in TERMINAL_STATES:
            raise TaskError(409, "INVALID_STATE",
                            "task already terminal: " + task.status)
        task.cancel_event.set()
        if task.status in ("created", "degraded", "queued"):
            # 线程未在跑（或挂起/排队）：出队并直接落终态
            if task.status == "queued":
                task_queue.remove(task.task_id)
            task.status = "canceled"
            task.finished_at = _now()
        # running：线程在下一个步骤边界安全点自行落 canceled 状态
        return task.status


def _execute(task: TaskRun) -> None:
    """顺序跑步骤直至终态/degraded 挂起；由 task_queue 调度线程同步调用（v0.5 起不再独立开线程）。

    v0.7.1 清道夫收尾：任何非 done 终局（失败/degraded/取消/未预期崩溃）
    都强制释放按键鼠标 + 线程盘点入审计——绝不把"按住"状态留给下一个任务。
    """
    try:
        _execute_steps(task)
    finally:
        with task.lock:
            final_status = task.status
            final_fail = dict(task.fail) if task.fail else None
        if final_status in ("done", "degraded", "canceled"):
            if final_status == "done":
                audit_sink.write("task_end", task_id=task.task_id,
                                 status=final_status)
            else:
                janitor.sweep("task_" + final_status, task_id=task.task_id,
                              extra={"fail": final_fail})
        else:
            # 未预期崩溃：状态未落终态就异常逃逸——按崩溃清场并留痕
            janitor.sweep("task_crashed", task_id=task.task_id,
                          extra={"status": final_status, "fail": final_fail})


def _execute_steps(task: TaskRun) -> None:
    while True:
        with task.lock:
            if task.cancel_event.is_set():
                task.status = "canceled"
                task.finished_at = _now()
                return
            idx = task.current_index
            if idx >= len(task.steps):
                task.status = "done"
                task.finished_at = _now()
                return
            task.step_states[idx]["status"] = "executing"
        step = task.steps[idx]
        # v0.7 window_title_changed 需要步骤执行前的标题基线（此刻窗口还没被碰）
        baseline_title: Optional[str] = None
        if (step.get("post_assert") or {}).get("type") == "window_title_changed":
            base_hwnd = _assert_target_hwnd(step)
            base_info = safety_gate.window_info(base_hwnd) if base_hwnd else None
            baseline_title = (base_info or {}).get("title")
        try:
            result = _run_step(task, step)
            assert_out = _eval_post_assert(task, step,
                                           baseline_title=baseline_title)  # v0.7 防幻觉
        except StepFailure as f:
            with task.lock:
                macro_step = (f.data or {}).get("step_id")
                task.step_states[idx]["status"] = "failed"
                task.step_states[idx].update(f.data or {})
                task.step_states[idx]["step_id"] = idx + 1  # 任务级 step_id 不被宏内部步号覆盖
                if f.fail_class == "CANCELED":
                    task.status = "canceled"
                    task.finished_at = _now()
                else:
                    task.fail = {"step_id": idx + 1, "fail_class": f.fail_class,
                                 "detail": f.detail}
                    task.fail.update(f.data or {})
                    if macro_step is not None:
                        task.fail["macro_step_id"] = macro_step
                    task.fail["step_id"] = idx + 1
                    task.status = "degraded"
            return
        except Exception as exc:  # noqa: BLE001
            with task.lock:
                task.step_states[idx]["status"] = "failed"
                task.fail = {"step_id": idx + 1, "fail_class": "INTERNAL_ERROR",
                             "detail": type(exc).__name__ + ": " + str(exc)}
                task.status = "degraded"
            return
        with task.lock:
            task.step_states[idx]["status"] = "done"
            task.step_states[idx]["result"] = result
            if assert_out.get("checked"):
                task.step_states[idx]["post_assert"] = assert_out
            task.current_index = idx + 1


def _run_step(task: TaskRun, step: Dict[str, Any]) -> Dict[str, Any]:
    kind = step["kind"]
    if kind == "macro":
        try:
            out = macro_replay(step["macro_id"],
                               hwnd_override=step.get("hwnd"),
                               task_id=task.task_id,
                               capabilities=task.capabilities)
        except ReplayError as exc:
            data = dict(exc.data or {})
            raise StepFailure(fail_class=exc.error, detail=exc.detail,
                              **data) from exc
        return {"run_id": out.get("run_id"), "steps_ok": out.get("steps_ok")}
    if kind == "action":
        payload = dict(step.get("payload") or {})
        # v0.7 能力令牌随任务注入（任务级令牌覆盖 payload 自带的，防步骤级越权）
        payload["capabilities"] = task.capabilities
        try:
            out = run_action(payload)
        except ActionError as exc:
            raise StepFailure(fail_class=exc.error, detail=exc.detail,
                              **dict(exc.data)) from exc
        return {"before_screenshot_path": out.get("before_screenshot_path"),
                "after_screenshot_path": out.get("after_screenshot_path")}
    if kind == "wait":
        ms = int(step.get("ms") or 0)
        end = time.time() + ms / 1000.0
        while time.time() < end:
            if task.cancel_event.is_set():
                raise StepFailure(fail_class="CANCELED",
                                  detail="canceled during wait",
                                  canceled=True)
            time.sleep(0.15)
        return {"waited_ms": ms}
    if kind == "require_vision":
        raise StepFailure(fail_class="REQUIRE_VISION",
                          detail="awaiting main-repo vision",
                          await_vision=True, prompt=step.get("prompt"))
    raise StepFailure(fail_class="INTERNAL_ERROR",
                      detail="unknown kind " + repr(kind))


def _assert_target_hwnd(step: Dict[str, Any]) -> Optional[int]:
    """post_assert 的目标窗口：步骤显式 hwnd（macro/action payload）> 前台窗口。"""
    if step.get("kind") == "macro" and step.get("hwnd"):
        return int(step["hwnd"])
    if step.get("kind") == "action":
        hwnd = (step.get("payload") or {}).get("hwnd")
        if hwnd:
            return int(hwnd)
    return window_locator.foreground_hwnd()


def _eval_post_assert(task: TaskRun, step: Dict[str, Any],
                      baseline_title: Optional[str] = None) -> Dict[str, Any]:
    """v0.7 动作后置断言（防幻觉）：步骤执行成功后核对"期望的结果真的发生了"。

    未配置 post_assert → {"checked": False}（绝大多数步骤零开销）。
    断言失败 → StepFailure(POST_ASSERT_FAILED)：任务立刻 degraded 挂起，detail
    明示"指令可能错误"，等主仓复核后 resume 从该步重试。
    断言本身无法验证（OCR 引擎缺失 / 抓屏失败 / 窗口消失 / changed 缺基线）→
    StepFailure(POST_ASSERT_UNVERIFIABLE)：同样挂起，但如实报告"无法验证"
    而不是谎称指令错误。
    """
    spec = step.get("post_assert")
    if not spec:
        return {"checked": False}
    kind = spec["type"]
    monitor = int(spec.get("monitor") or 1)
    hwnd = _assert_target_hwnd(step)

    if kind in ("window_title_contains", "window_title_is",
                "window_title_changed"):
        info = safety_gate.window_info(hwnd) if hwnd else None
        if info is None:
            raise StepFailure(
                fail_class="POST_ASSERT_UNVERIFIABLE",
                detail="post_assert " + kind + " cannot verify: target window "
                       + repr(hwnd) + " is gone",
                post_assert=dict(spec), target_hwnd=hwnd)
        title = str(info.get("title") or "")
        want = str(spec.get("value") or "")
        if kind == "window_title_contains":
            ok = want in title
        elif kind == "window_title_is":
            ok = title == want
        else:  # window_title_changed：与 _execute 在步骤执行前抓取的基线比对
            if baseline_title is None:
                raise StepFailure(
                    fail_class="POST_ASSERT_UNVERIFIABLE",
                    detail="post_assert window_title_changed cannot verify: "
                           "baseline title was not captured before the step "
                           "(target window may not have existed)",
                    post_assert=dict(spec), target_hwnd=hwnd)
            ok = title != baseline_title
            want = "title changed from " + repr(baseline_title)
        if ok:
            return {"checked": True, "type": kind, "passed": True,
                    "title": title, "target_hwnd": hwnd}
        raise StepFailure(
            fail_class="POST_ASSERT_FAILED",
            detail="post_assert " + kind + " failed (expected "
                   + repr(want) + ", actual title " + repr(title)
                   + ") —— 指令可能错误：Ghost 已挂起任务等主仓复核",
            post_assert=dict(spec), actual_title=title, target_hwnd=hwnd)

    if kind in ("anchor_present", "anchor_absent"):
        from . import macro_matcher, screen_mss  # 延迟 import：cv2 较重
        try:
            img, _origin = screen_mss.grab(monitor)
        except Exception as exc:  # noqa: BLE001
            raise StepFailure(
                fail_class="POST_ASSERT_UNVERIFIABLE",
                detail="post_assert " + kind + " cannot verify: screen capture "
                       "failed: " + type(exc).__name__ + ": " + str(exc),
                post_assert=dict(spec)) from exc
        try:
            m = macro_matcher.find(img, spec["template_b64"],
                                   float(spec.get("threshold") or 0.80),
                                   hwnd=hwnd)
        except ValueError as exc:
            raise StepFailure(
                fail_class="POST_ASSERT_FAILED",
                detail="post_assert " + kind + " failed: bad template_b64: "
                       + str(exc) + " —— 指令可能错误：Ghost 已挂起任务等主仓复核",
                post_assert=dict(spec)) from exc
        status = m.get("status")
        if status == "window_not_found":
            raise StepFailure(
                fail_class="POST_ASSERT_UNVERIFIABLE",
                detail="post_assert " + kind + " cannot verify: target window "
                       + repr(hwnd) + " is gone",
                post_assert=dict(spec), target_hwnd=hwnd)
        if status == "capture_failed":
            raise StepFailure(
                fail_class="POST_ASSERT_UNVERIFIABLE",
                detail="post_assert " + kind + " cannot verify: capture failed",
                post_assert=dict(spec))
        matched = status == "matched"
        passed = matched if kind == "anchor_present" else not matched
        score = float(m.get("best_score") or m.get("score") or 0.0)
        if passed:
            return {"checked": True, "type": kind, "passed": True,
                    "score": score, "target_hwnd": hwnd}
        raise StepFailure(
            fail_class="POST_ASSERT_FAILED",
            detail="post_assert " + kind + " failed (anchor "
                   + ("appeared on screen" if matched else "is absent")
                   + ", best_score=" + format(score, ".4f")
                   + ") —— 指令可能错误：Ghost 已挂起任务等主仓复核",
            post_assert=dict(spec), score=score, target_hwnd=hwnd)

    if kind == "ocr_text_present":
        try:
            import pytesseract  # type: ignore[import-not-found]
        except ImportError as exc:
            raise StepFailure(
                fail_class="POST_ASSERT_UNVERIFIABLE",
                detail="post_assert ocr_text_present cannot verify: no OCR "
                       "engine on ghost host (install tesseract + pytesseract, "
                       "or use anchor_present with a text crop instead)",
                post_assert=dict(spec)) from exc
        from . import screen_mss
        try:
            img, _origin = screen_mss.grab(monitor)
            text = pytesseract.image_to_string(img, lang="chi_sim+eng")
        except Exception as exc:  # noqa: BLE001
            raise StepFailure(
                fail_class="POST_ASSERT_UNVERIFIABLE",
                detail="post_assert ocr_text_present cannot verify: "
                       + type(exc).__name__ + ": " + str(exc),
                post_assert=dict(spec)) from exc
        want = str(spec.get("value") or "")
        if want in text:
            return {"checked": True, "type": kind, "passed": True,
                    "target_hwnd": hwnd}
        raise StepFailure(
            fail_class="POST_ASSERT_FAILED",
            detail="post_assert ocr_text_present failed (expected OCR text "
                   + repr(want) + " not found on screen) —— 指令可能错误："
                   "Ghost 已挂起任务等主仓复核",
            post_assert=dict(spec), target_hwnd=hwnd)

    raise StepFailure(fail_class="INTERNAL_ERROR",
                      detail="unknown post_assert type " + repr(kind))
