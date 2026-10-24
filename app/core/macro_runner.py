"""宏重放（v0.3）：锚点匹配驱动的安全重放。

铁律：
- 重放坐标一律来自 cv2.matchTemplate 的匹配结果（全屏物理像素），绝不使用
  recorded_center 推算点击位置（它只是录制时的参考，仅 type 步沿用原光标位）。
- 每一步的输入必须走 controller.run_action 的同一安全管道（全局串行锁、
  Fail-Safe、前后审计截图），不得绕过。
- 状态三分开：capture_failed（抓屏失败，503）≠ not_found（锚点不存在，409
  MACRO_NOT_FOUND）≠ window_not_found（窗口不存在，409 WINDOW_NOT_FOUND）。
  只有 not_found 才触发降级让主仓重新识图。
- allow_fallback 目前为保留字段：not_found 一律 409 终止整个宏，绝不自行
  用 recorded_center 兜底（拿云端调用去修本地问题不如让主仓重新看屏幕）。
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

import win32gui

from . import environment, macro_store, screen_mss, window_locator
from .controller import ActionError, run_action
from .macro_matcher import find

GA_ROOT = 2

CAPTURE_RETRIES = 2  # 抓屏失败后最多重试 2 次（共 3 次）
MATCH_THRESHOLD = 0.80


class ReplayError(Exception):
    """带 HTTP 语义的重放失败；data 会并入响应体。"""

    def __init__(self, status: int, error: str, detail: str, **data: Any):
        super().__init__(detail)
        self.status = status
        self.error = error
        self.detail = detail
        self.data = data


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _grab_with_retry(monitor: int = 1) -> Any:
    last_exc: Optional[Exception] = None
    for _ in range(CAPTURE_RETRIES + 1):
        try:
            img, _origin = screen_mss.grab(monitor)
            return img
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(0.1)
    raise ReplayError(503, "CAPTURE_FAILED",
                      "screen capture failed after "
                      + str(CAPTURE_RETRIES + 1) + " attempts: " + str(last_exc),
                      inner=type(last_exc).__name__ + ": " + str(last_exc))


def _save_fail_shot(img: Any, step_id: Any) -> Optional[str]:
    try:
        return str(screen_mss.save_audit(img, "macro_fail_step" + str(step_id),
                                         "jpeg", 80))
    except Exception:  # noqa: BLE001
        return None


def replay(macro_id: str, dry_run: bool = False, allow_fallback: bool = False,
           task_id: Optional[str] = None,
           hwnd_override: Optional[int] = None) -> Dict[str, Any]:
    # 环境门禁（v0.4 M2）：锁屏 / 安全桌面 / 桌面不可读时拒绝盲操作
    try:
        environment.assert_input_allowed()
    except environment.SystemNotReadable as exc:
        raise ReplayError(503, "SYSTEM_NOT_READABLE", exc.detail,
                          environment=exc.snapshot)
    macro = macro_store.get_macro(macro_id)
    if macro is None:
        raise ReplayError(404, "NOT_FOUND", "macro " + repr(macro_id) + " not found")
    try:
        steps: List[Dict[str, Any]] = json.loads(macro.get("steps_json") or "[]")
    except json.JSONDecodeError:
        raise ReplayError(500, "INTERNAL_ERROR", "macro steps_json is corrupted")
    if not steps:
        raise ReplayError(400, "BAD_REQUEST", "macro has no steps")

    run_id = "run_" + uuid.uuid4().hex[:16]
    started = _now()
    macro_store.create_run(run_id, macro_id, task_id, started, len(steps))

    results: List[Dict[str, Any]] = []
    steps_ok = 0
    failure: Optional[ReplayError] = None
    fail_shot: Optional[str] = None
    t0 = time.perf_counter()

    for step in steps:
        step_id = step.get("step_id")
        action = str(step.get("action", "")).lower()
        # 目标窗口：主仓可通过 hwnd_override 统一覆盖（重开应用后的新 hwnd）；
        # 缺省用各步录制时的 hwnd。窗口 rect 每步实时获取，不缓存。
        hwnd = int(hwnd_override) if hwnd_override else step.get("hwnd")
        result: Dict[str, Any] = {"step_id": step_id, "action": action,
                                  "hwnd": hwnd}

        # 1) 窗口存活检查（每次实时取 rect，不缓存）
        rect = window_locator.get_rect(hwnd) if hwnd else None
        if hwnd and rect is None:
            failure = ReplayError(409, "WINDOW_NOT_FOUND",
                                  "step " + repr(step_id) + ": hwnd "
                                  + repr(hwnd) + " invalid/minimized/gone",
                                  step_id=step_id, hwnd=hwnd)
            result["status"] = "window_not_found"
            results.append(result)
            break

        # 1.5) type/hotkey 前台门禁：打字只会进前台窗口——目标不在前台就拒绝，
        #      绝不把宏文本打进无关应用（纪律红线）。
        if action in ("type", "hotkey") and hwnd:
            fg = window_locator.foreground_hwnd()
            if fg != int(hwnd):
                failure = ReplayError(
                    409, "WINDOW_NOT_FOREGROUND",
                    "step " + repr(step_id) + ": target hwnd " + repr(hwnd)
                    + " is not foreground (fg=" + repr(fg) + "); typing aborted",
                    step_id=step_id, hwnd=hwnd, foreground=fg)
                result["status"] = "window_not_foreground"
                results.append(result)
                break

        # 2) click 系：ROI 内锚点匹配（重放坐标只来自匹配结果）
        if action in ("click", "double_click", "right_click"):
            target = step.get("target") or {}
            anchor_b64 = target.get("image_b64")
            if target.get("type") != "anchor" or not anchor_b64:
                failure = ReplayError(500, "INTERNAL_ERROR",
                                      "step " + repr(step_id)
                                      + ": click step has no anchor")
                result["status"] = "bad_step"
                results.append(result)
                break
            try:
                img = _grab_with_retry()
            except ReplayError as exc:
                failure = exc
                result["status"] = "capture_failed"
                results.append(result)
                break
            m = find(img, anchor_b64, MATCH_THRESHOLD, hwnd=hwnd)
            if m.get("status") == "window_not_found":
                failure = ReplayError(409, "WINDOW_NOT_FOUND",
                                      "step " + repr(step_id)
                                      + ": window gone during match",
                                      step_id=step_id, hwnd=hwnd)
                result["status"] = "window_not_found"
                results.append(result)
                break
            result["score"] = m.get("score")
            result["find_ms"] = m.get("find_ms")
            if m.get("status") != "matched":
                fail_shot = _save_fail_shot(img, step_id)
                best = float(m.get("best_score") or 0.0)
                failure = ReplayError(
                    409, "MACRO_NOT_FOUND",
                    "step " + repr(step_id) + ": anchor not found (best_score="
                    + format(best, ".4f") + "); degrade to main-repo re-vision",
                    step_id=step_id, best_score=best,
                    fail_screenshot_path=fail_shot, allow_fallback=allow_fallback)
                result["status"] = "not_found"
                result["best_score"] = best
                results.append(result)
                break
            result["status"] = "matched"
            result["center"] = m["center"]
            if dry_run:
                result["dry_run"] = True
                results.append(result)
                steps_ok += 1
                continue
            payload: Dict[str, Any] = {"action": action, "x": m["center"][0],
                                       "y": m["center"][1]}
        elif action in ("type", "hotkey"):
            result["status"] = "matched" if dry_run else "pending"
            if dry_run:
                result["dry_run"] = True
                results.append(result)
                steps_ok += 1
                continue
            payload = {"action": action, "text": step.get("text")}
            if action == "type":
                # type 需要 x/y（moveTo+漂移校验是安全管道的一部分）；
                # 沿用录制时的光标位（无推算），打字落入前台窗口
                rc = (step.get("target") or {}).get("recorded_center")
                if rc:
                    payload["x"], payload["y"] = int(rc[0]), int(rc[1])
        else:
            failure = ReplayError(500, "INTERNAL_ERROR",
                                  "step " + repr(step_id) + ": unsupported action "
                                  + repr(action))
            result["status"] = "bad_step"
            results.append(result)
            break

        # 3) 执行（同一安全管道；wait_after_ms 即动作后 settle）
        wait_ms = int(step.get("wait_after_ms") or 0)
        payload["settle_seconds"] = min(max(wait_ms / 1000.0, 0.0), 5.0)
        # 3) 点击落点归属门禁：匹配与点击之间 z 序可能变化（遮挡/用户切窗），
        #    落点被别的窗口覆盖就拒绝——绝不把点击打进别的窗口（纪律红线）。
        if action in ("click", "double_click", "right_click"):
            pt = (int(payload["x"]), int(payload["y"]))
            try:
                root = win32gui.GetAncestor(win32gui.WindowFromPoint(pt), GA_ROOT)
            except Exception:  # noqa: BLE001
                root = None
            if root != int(hwnd):
                fail_shot = _save_fail_shot(img, step_id)
                failure = ReplayError(
                    409, "WINDOW_OCCLUDED",
                    "step " + repr(step_id) + ": click point " + str(pt)
                    + " is covered by another window (root=" + repr(root) + ")",
                    step_id=step_id, hwnd=hwnd, occluder=root,
                    fail_screenshot_path=fail_shot)
                result["status"] = "window_occluded"
                results.append(result)
                break
        try:
            out = run_action(payload)
        except ActionError as exc:
            fail_shot = (exc.data.get("after_screenshot_path")
                         or exc.data.get("scene_screenshot_path") or fail_shot)
            failure = ReplayError(exc.status, exc.error,
                                  "step " + repr(step_id) + ": " + exc.detail,
                                  step_id=step_id, controller=exc.data)
            result["status"] = "action_error"
            result["error"] = exc.error
            results.append(result)
            break
        except Exception as exc:  # noqa: BLE001
            failure = ReplayError(500, "INTERNAL_ERROR",
                                  "step " + repr(step_id) + ": "
                                  + type(exc).__name__ + ": " + str(exc),
                                  step_id=step_id)
            result["status"] = "action_error"
            results.append(result)
            break
        result["status"] = "executed"
        result["before_screenshot_path"] = out.get("before_screenshot_path")
        result["after_screenshot_path"] = out.get("after_screenshot_path")
        result["cursor_after"] = out.get("cursor_after")
        results.append(result)
        steps_ok += 1

    elapsed = int((time.perf_counter() - t0) * 1000)
    finished = _now()
    if failure is None:
        status = "dry_run" if dry_run else "success"
        macro_store.finish_run(run_id, status, finished, steps_ok, None, None, None)
        macro_store.bump_stats(macro_id, success=not dry_run)
        return {"ok": True, "run_id": run_id, "macro_id": macro_id,
                "dry_run": dry_run, "status": status,
                "steps_total": len(steps), "steps_ok": steps_ok,
                "steps": results, "elapsed_ms": elapsed}
    macro_store.finish_run(run_id, "failed", finished, steps_ok,
                           failure.data.get("step_id"), failure.detail, fail_shot)
    macro_store.bump_stats(macro_id, success=False)
    failure.data["run_id"] = run_id  # 失败响应也带 run_id，便于主仓核对 macro_runs
    raise failure
