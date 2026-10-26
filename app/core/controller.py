"""统一安全管道：全局串行锁 + 坐标校验 + Fail-Safe + 前后截图审计 + v0.7 绝对防御。

所有真实动桌面的调用（包括 v0.3 的宏重放）都必须走 run_action()，
不得绕过本模块直接调用 backend。

v0.7 三道门禁（safety_gate，全部在此咽喉强制）：
1. 窗口黑名单：目标窗口进程/标题高危 → 403 HIGH_RISK_WINDOW（锁前早杀 + 锁内
   落点复核，绝不产生输入）。
2. 键盘黑名单：系统级热键（win 系 / ctrl+shift+esc / ctrl+alt+delete）与破坏性
   文本 → 403 HIGH_RISK_INPUT（纯字符串、最早校验）。
3. 能力令牌：capabilities.allowed_windows 缺失或目标进程不在名单 →
   403 CAPABILITY_DENIED（move 无 hwnd 不与窗口交互，豁免令牌）。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from . import backend, safety_gate, screen_mss
from . import action_log, authz, janitor
from .backend import FailSafeException

LOG = logging.getLogger("moray-ghost.controller")

ACTION_LOCK = threading.Lock()      # 桌面动作全局串行（进程内锁）
DRIFT_LIMIT_PX = 3                  # moveTo 后实际落点允许的最大偏差
CORNER_GUARD_PX = 2                 # 请求坐标距 Fail-Safe 角落点的保护半径
MAX_TRAJECTORY_POINTS = 2000        # drag 轨迹点上限（超限等距降采样保端点）
MAX_SCROLL_DY = 100000              # 单次滚轮幅度上限
VALID_ACTIONS = ("click", "double_click", "right_click", "move", "type",
                 "hotkey", "drag", "scroll")
COORD_ACTIONS = ("click", "double_click", "right_click", "move", "type",
                 "drag", "scroll")
VALID_ACTIONS_TXT = ("click / double_click / right_click / move / type / "
                     "hotkey / drag / scroll")


class ActionError(Exception):
    status = 500
    error = "INTERNAL_ERROR"

    def __init__(self, detail: str, **data: Any):
        super().__init__(detail)
        self.detail = detail
        self.data = data


class SafetyError(ActionError):
    status = 400
    error = "SAFETY_BLOCKED"


class FailSafeTriggered(ActionError):
    status = 409
    error = "FAIL_SAFE_TRIGGERED"


class ForegroundError(ActionError):
    status = 409
    error = "WINDOW_NOT_FOREGROUND"


class HighRiskWindowError(ActionError):
    """v0.7 红线：目标窗口在黑名单（cmd/powershell/regedit/Terminal/UAC/…）。"""
    status = 403
    error = "HIGH_RISK_WINDOW"


class HighRiskInputError(ActionError):
    """v0.7 红线：系统级热键 / 破坏性文本。"""
    status = 403
    error = "HIGH_RISK_INPUT"


class CapabilityDeniedError(ActionError):
    """v0.7 最小权限：未携带能力令牌，或目标进程不在 allowed_windows。"""
    status = 403
    error = "CAPABILITY_DENIED"


class SourceDeniedError(ActionError):
    """v0.7.1 生产环境：动作不在带 Token 的合法 HTTP 调用链上（旁路裸调）。"""
    status = 403
    error = "SOURCE_DENIED"


class InternalActionError(ActionError):
    status = 500
    error = "INTERNAL_ERROR"


class BadSourceSizeError(ActionError):
    status = 400
    error = "BAD_SOURCE_SIZE"


def _failsafe_points() -> List[Tuple[int, int]]:
    pts = getattr(backend.pyautogui, "FAILSAFE_POINTS", [(0, 0)])
    return [(int(p[0]), int(p[1])) for p in pts]


def _in_corner(pt: Tuple[int, int], tol: int = CORNER_GUARD_PX) -> bool:
    return any(abs(pt[0] - fx) <= tol and abs(pt[1] - fy) <= tol for fx, fy in _failsafe_points())


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _parse_hotkey(combo: str) -> List[str]:
    parts = [p.strip().lower() for p in str(combo).split("+") if p.strip()]
    if not parts:
        raise SafetyError("hotkey combo is empty")
    valid = set(backend.key_names())
    out: List[str] = []
    for p in parts:
        if p == "win" and "win" not in valid and "winleft" in valid:
            p = "winleft"
        if p not in valid:
            raise SafetyError(f"unknown key in hotkey combo: {p!r}")
        out.append(p)
    return out


def run_action(payload: Dict[str, Any]) -> Dict[str, Any]:
    """安全管道唯一入口（v0.6 起同时是驾驶舱动作日志的入账点）。

    成功 / ActionError（含进锁前校验拒绝）/ 未预期异常三路径都入账——
    宏重放、任务步骤、直接 /execute_action 全部经过这里，无一遗漏。
    """
    action = str(payload.get("action", "")).strip().lower()
    detail = {"x": payload.get("x"), "y": payload.get("y"),
              "monitor": payload.get("monitor") or 1}
    try:
        result = _run_action_impl(payload)
    except ActionError as exc:
        err_data = exc.data or {}
        action_log.record("native", action, detail, False, exc.status,
                          thumb=(err_data.get("after_screenshot_path")
                                 or err_data.get("scene_screenshot_path")))
        raise
    except Exception:
        action_log.record("native", action, detail, False, 500)
        raise
    action_log.record("native", action,
                      {"x": result.get("x"), "y": result.get("y"),
                       "monitor": result.get("monitor")},
                      True, 200, thumb=result.get("after_screenshot_path"))
    return result


def _run_action_impl(payload: Dict[str, Any]) -> Dict[str, Any]:
    # v0.7.1 生产环境输入源过滤（最高优先级、零副作用，先于一切校验）：
    # 动作必须来自带 Token 的合法 HTTP 调用链（http_api）或其派发的任务
    # （task_queue）；进程内裸调 run_action 等旁路一律 403 SOURCE_DENIED。
    try:
        authz.assert_action_source()
    except authz.SourceDenied as exc:
        raise SourceDeniedError(str(exc)) from exc
    action = str(payload.get("action", "")).strip().lower()
    if action not in VALID_ACTIONS:
        raise SafetyError(f"unsupported action: {action!r} (valid: {VALID_ACTIONS_TXT})")

    x = payload.get("x")
    y = payload.get("y")
    text = payload.get("text")
    monitor = int(payload.get("monitor") or 1)
    settle = min(max(float(payload.get("settle_seconds") or 0.0), 0.0), 5.0)
    fmt = str(payload.get("fmt") or "jpeg").lower()
    quality = min(max(int(payload.get("quality") or 70), 10), 95)
    scale = min(max(float(payload.get("scale") or 0.5), 0.1), 1.0)
    include_before_b64 = bool(payload.get("include_before_b64"))
    source_width = payload.get("source_width")
    source_height = payload.get("source_height")

    if fmt not in ("jpeg", "png"):
        raise SafetyError(f"fmt must be jpeg or png, got {fmt!r}")
    if action == "type" and not text:
        raise SafetyError("action 'type' requires non-empty text")
    if action == "hotkey" and not text:
        raise SafetyError("action 'hotkey' requires a combo like ctrl+s")
    if action in COORD_ACTIONS and (x is None or y is None) \
            and not (action == "drag" and payload.get("points")):
        # drag 带显式轨迹时 x/y 可省略（起点取 points[0]），其余动作必须给 x/y
        raise SafetyError(f"action {action!r} requires x/y")

    # v0.7 键盘黑名单（最高优先级、纯字符串零副作用）：系统级热键与破坏性文本
    # 在一切参数/坐标校验之前拒绝——绝不让危险输入走完整条管道。
    keys: Optional[List[str]] = None
    if action == "hotkey":
        keys = _parse_hotkey(str(text))
        reason = safety_gate.redline_hotkey(keys)
        if reason:
            raise HighRiskInputError(reason, combo=str(text), keys=keys)
    if action == "type":
        reason = safety_gate.redline_text(str(text))
        if reason:
            raise HighRiskInputError(reason, action="type")

    mon_count = len(screen_mss.monitors())  # 含 index 0 = 全屏联合区
    max_mon = mon_count - 1
    if monitor < 1 or monitor > max_mon:
        raise SafetyError(f"monitor out of range: {monitor} (1..{max_mon})")
    mon = screen_mss.monitor_rect(monitor)

    # drag/scroll 专属解析（v0.6 M1）：轨迹/滚轮在通用坐标校验之后、进锁之前完成
    trajectory: Optional[List[List[int]]] = None
    trajectory_t: Optional[List[int]] = None
    scroll_dy: Optional[int] = None
    scroll_smooth = False
    if action == "drag":
        duration_ms = min(max(int(payload.get("duration_ms") or 600), 100), 10000)
        raw_points = payload.get("points")
        if raw_points is not None:
            if not isinstance(raw_points, list) or len(raw_points) < 2:
                raise SafetyError("drag points must be a list of >=2 [x,y] entries")
            pts: List[Tuple[int, int]] = []
            for p in raw_points:
                if (not isinstance(p, (list, tuple)) or len(p) < 2
                        or not isinstance(p[0], (int, float))
                        or not isinstance(p[1], (int, float))):
                    raise SafetyError("drag points entries must be [x,y] numbers")
                pts.append((int(p[0]), int(p[1])))
            if len(pts) > MAX_TRAJECTORY_POINTS:  # 等距降采样，端点保留
                keep = MAX_TRAJECTORY_POINTS
                step = (len(pts) - 1) / (keep - 1)
                pts = [pts[round(i * step)] for i in range(keep)]
            trajectory = [[px, py] for px, py in pts]
            raw_t = payload.get("points_t")
            if isinstance(raw_t, list) and len(raw_t) == len(trajectory):
                trajectory_t = [min(max(int(t), 0), 10000) for t in raw_t]
                if trajectory_t != sorted(trajectory_t):
                    raise SafetyError("drag points_t must be non-decreasing")
            else:
                step_ms = duration_ms / (len(trajectory) - 1)
                trajectory_t = [int(round(i * step_ms)) for i in range(len(trajectory))]
            # 轨迹起点为权威：改写 x/y，让下方通用 bounds/换算/回显全部以它为准
            x, y = trajectory[0][0], trajectory[0][1]
        else:
            if x is None or y is None:
                raise SafetyError("drag requires x/y (or points)")
            sx, sy = int(x), int(y)
            x2 = payload.get("x2")
            y2 = payload.get("y2")
            if x2 is None or y2 is None:
                raise SafetyError("drag requires x2/y2 or points")
            x2i, y2i = int(x2), int(y2)
            n = max(2, min(duration_ms // 16, 1000))
            trajectory = [
                [int(round(sx + (x2i - sx) * i / (n - 1))),
                 int(round(sy + (y2i - sy) * i / (n - 1)))]
                for i in range(n)]
            step_ms = duration_ms / (n - 1)
            trajectory_t = [int(round(i * step_ms)) for i in range(n)]
    if action == "scroll":
        if payload.get("dy") is None:
            raise SafetyError("scroll requires dy")
        scroll_dy = int(payload.get("dy"))
        if scroll_dy == 0 or abs(scroll_dy) > MAX_SCROLL_DY:
            raise SafetyError(
                "scroll dy must be a non-zero int within +/-" + str(MAX_SCROLL_DY))
        scroll_smooth = bool(payload.get("smooth"))

    # 轨迹全点校验：每个轨迹点都必须在屏内且不落在 Fail-Safe 角落
    if trajectory is not None:
        for px, py in trajectory:
            inside = (mon["left"] <= px < mon["left"] + mon["width"]
                      and mon["top"] <= py < mon["top"] + mon["height"])
            if not inside:
                raise SafetyError(
                    f"trajectory point ({px},{py}) out of bounds for monitor "
                    + str(monitor), x=px, y=py)
            if _in_corner((px, py)):
                raise SafetyError(
                    f"trajectory point ({px},{py}) fall inside a Fail-Safe corner",
                    x=px, y=py)

    coord_source = "physical"
    resolved_physical: Optional[List[int]] = None
    x_req: Optional[int] = None
    y_req: Optional[int] = None
    xi: Optional[int] = None
    yi: Optional[int] = None
    if action in COORD_ACTIONS:
        x_req, y_req = int(x), int(y)  # 已在上方校验非空
        xi, yi = x_req, y_req
        # 源图坐标 → 物理像素换算（source_width/height 必须同时给出；缺省走原路径，
        # 行为完全不变）。换算后的物理坐标仍走下方原有的越界 / Fail-Safe 角落 /
        # 漂移检查，不绕过任何安全防线。monitor_origin 取 mss 的 left/top（多屏
        # 主屏原点可能不是 (0,0)）。
        if source_width is not None and source_height is not None:
            if source_width <= 0 or source_height <= 0:
                raise BadSourceSizeError("source_width/source_height must be positive integers")
            real_w = mon["width"]
            real_h = mon["height"]
            if source_width > real_w or source_height > real_h:
                raise BadSourceSizeError(
                    f"source image {source_width}x{source_height} is larger than monitor {monitor} ({real_w}x{real_h})")
            xi = round(x_req * real_w / source_width) + mon["left"]
            yi = round(y_req * real_h / source_height) + mon["top"]
            coord_source = "scaled"
        resolved_physical = [xi, yi]
        inside = (mon["left"] <= xi < mon["left"] + mon["width"]
                  and mon["top"] <= yi < mon["top"] + mon["height"])
        if not inside:
            mon_left = mon["left"]
            mon_top = mon["top"]
            mon_w = mon["width"]
            mon_h = mon["height"]
            raise SafetyError(
                f"coordinates ({xi},{yi}) out of bounds for monitor {monitor} "
                f"rect=({mon_left},{mon_top},{mon_w}x{mon_h})",
                x=xi, y=yi)
        if _in_corner((xi, yi)):
            raise SafetyError(
                f"coordinates ({xi},{yi}) fall inside a Fail-Safe corner", x=xi, y=yi)

    # ---- v0.7 能力令牌 + 窗口黑名单（锁前早杀） ------------------------
    # 纯参数校验（上面的 400 SAFETY_BLOCKED 族）先行，既有语义不变；此处开始
    # 涉及"输入会打进哪个窗口"的门禁。锁内还会对 no-hwnd 动作的实时落点复核。
    caps: Optional[Dict[str, Any]] = None
    caps_raw = payload.get("capabilities")
    if caps_raw is not None:
        try:
            caps = safety_gate.parse_capabilities(caps_raw)
        except ValueError as exc:
            raise SafetyError(str(exc)) from exc
    want_hwnd = payload.get("hwnd")
    # 会打进窗口的输入一律要求令牌；move 无 hwnd 不与任何窗口交互 → 豁免
    needs_caps = action != "move" or want_hwnd is not None
    if needs_caps and caps is None:
        raise CapabilityDeniedError(
            "no capability token: declare capabilities.allowed_windows "
            "(最小权限原则：未声明白名单的输入一律拒绝)", action=action)
    if want_hwnd:
        info = safety_gate.window_info(int(want_hwnd))
        if info is not None:
            reason = safety_gate.redline_window(info)
            if reason:
                raise HighRiskWindowError(
                    reason, hwnd=info["hwnd"], process=info.get("process"),
                    title=info.get("title"))
            if not safety_gate.capability_allows(caps, info["process"]):
                raise CapabilityDeniedError(
                    "window process " + repr(info.get("process"))
                    + " is not in capabilities.allowed_windows "
                    + repr(caps.get("allowed_windows")),
                    hwnd=info["hwnd"], process=info.get("process"),
                    title=info.get("title"))

    # 输入审计：每一次真实桌面输入在此留一行账（本模块是全仓唯一动鼠标的咽喉，
    # 宏重放/任务/直接动作都汇到这里）；与 logs/access.log 的 HTTP 层记录互相印证，
    # "鼠标为什么动了"从此可事后归因。type 的文本内容不记账（只记坐标）。
    audit_extra = ""
    if action == "drag" and trajectory is not None:
        audit_extra = " points=" + str(len(trajectory))
    elif action == "scroll" and scroll_dy is not None:
        audit_extra = " dy=" + str(scroll_dy) + (" smooth" if scroll_smooth else "")
    LOG.info("input_audit channel=native action=%s resolved=%s monitor=%s%s",
             action, resolved_physical, monitor, audit_extra)

    with ACTION_LOCK:
        result = _run_locked_action(action, xi, yi, text, keys, monitor, settle,
                                    fmt, quality, scale, include_before_b64,
                                    x_req, y_req, coord_source, resolved_physical,
                                    want_hwnd, caps,
                                    trajectory, trajectory_t,
                                    scroll_dy, scroll_smooth)
    return result


def _resolve_input_target(action: str, want_hwnd: Optional[int],
                          xi: Optional[int], yi: Optional[int]) -> Optional[int]:
    """输入实际去向窗口（v0.7 锁内复核用）。

    显式 hwnd > 落点根窗口（click 系 / drag / scroll，取 point_root）>
    前台窗口（type / hotkey 的文本只会进前台）。move 无 hwnd 不与窗口交互 → None。
    """
    if want_hwnd:
        return int(want_hwnd)
    if action in ("click", "double_click", "right_click", "drag", "scroll") \
            and xi is not None and yi is not None:
        return backend.point_root((xi, yi))
    if action in ("type", "hotkey"):
        return backend.foreground_hwnd()
    return None


def _gate_target_window(action: str, caps: Optional[Dict[str, Any]],
                        target_hwnd: Optional[int], monitor: int,
                        fmt: str, quality: int, label: str,
                        before_path: Optional[str]) -> None:
    """锁内门禁：target_hwnd 过窗口黑名单 + 能力白名单，命中即 403（带审计截图）。"""
    if not target_hwnd:
        return
    info = safety_gate.window_info(target_hwnd)
    reason = safety_gate.redline_window(info)
    if reason:
        after_img, _ = screen_mss.grab(monitor)
        after_path = str(screen_mss.save_audit(after_img, "after", fmt, quality))
        raise HighRiskWindowError(
            label + reason, hwnd=int(target_hwnd),
            process=(info or {}).get("process"), title=(info or {}).get("title"),
            before_screenshot_path=before_path,
            after_screenshot_path=after_path)
    if not safety_gate.capability_allows(caps, (info or {}).get("process", "")):
        after_img, _ = screen_mss.grab(monitor)
        after_path = str(screen_mss.save_audit(after_img, "after", fmt, quality))
        raise CapabilityDeniedError(
            label + "input would land on process "
            + repr((info or {}).get("process"))
            + " which is not in capabilities.allowed_windows "
            + repr((caps or {}).get("allowed_windows")),
            hwnd=int(target_hwnd), process=(info or {}).get("process"),
            title=(info or {}).get("title"),
            before_screenshot_path=before_path,
            after_screenshot_path=after_path)


def _run_locked_action(action: str, xi: Optional[int], yi: Optional[int],
                       text: Optional[str], keys: Optional[List[str]], monitor: int,
                       settle: float, fmt: str, quality: int, scale: float,
                       include_before_b64: bool, x_req: Optional[int],
                       y_req: Optional[int], coord_source: str,
                       resolved_physical: Optional[List[int]],
                       want_hwnd: Optional[int],
                       caps: Optional[Dict[str, Any]] = None,
                       trajectory: Optional[List[List[int]]] = None,
                       trajectory_t: Optional[List[int]] = None,
                       scroll_dy: Optional[int] = None,
                       scroll_smooth: bool = False) -> Dict[str, Any]:
    t0 = time.perf_counter()
    before_path: Optional[str] = None
    after_path: Optional[str] = None
    origin: Dict[str, int] = {"left": 0, "top": 0}
    cursor_before: List[int] = []
    cursor_after: List[int] = []
    before_img = None
    after_img = None

    # 用户鼠标已停在角落：尚未执行任何动作就终止（整个动作级中止）
    cursor_now = backend.position()
    if _in_corner(cursor_now):
        scene_path, scene_b64 = _scene_shot(fmt, quality)
        raise FailSafeTriggered(
            "mouse is parked in a Fail-Safe corner; whole action aborted",
            cursor=list(cursor_now), scene_screenshot_path=scene_path,
            scene_image_b64=scene_b64)

    try:
        # 审计：执行前截图（任何动作都必须有，无盲点）
        before_img, origin = screen_mss.grab(monitor)
        before_path = str(screen_mss.save_audit(before_img, "before", fmt, quality))
        cursor_before = list(backend.position())

        # type/hotkey 前台门禁（v0.4.1）：带 hwnd 提示时，打字目标必须是前台
        # 窗口——绝不把文本打进别的应用（与 runner 的宏 type 门禁对齐）。
        if action in ("type", "hotkey") and want_hwnd:
            fg = backend.foreground_hwnd()
            if fg != int(want_hwnd):
                after_img, _ = screen_mss.grab(monitor)
                after_path = str(screen_mss.save_audit(after_img, "after", fmt, quality))
                raise ForegroundError(
                    f"target hwnd {int(want_hwnd)} is not foreground (fg={fg}); "
                    f"typing aborted",
                    expected_hwnd=int(want_hwnd), foreground=fg,
                    before_screenshot_path=before_path,
                    after_screenshot_path=after_path)

        # v0.7 锁内复核（任何物理移动之前）：解析输入实际去向窗口，再过一遍
        # 窗口黑名单 + 能力白名单。no-hwnd 动作的落点根窗口/前台窗口只有在
        # 这里实时解析才无 TOCTOU 空隙（锁前已查显式 hwnd，此处并集覆盖）。
        target_hwnd = _resolve_input_target(action, want_hwnd, xi, yi)
        _gate_target_window(action, caps, target_hwnd, monitor, fmt, quality,
                            "", before_path)
        if action == "drag" and trajectory is not None and not want_hwnd:
            # 无 hwnd 拖拽：终点根窗口若与起点不同（跨窗口拖拽），两端都必须
            # 过黑名单 + 白名单；带 hwnd 的拖拽已有起点+终点归属门禁兜底。
            end_root = backend.point_root((trajectory[-1][0], trajectory[-1][1]))
            if end_root and end_root != target_hwnd:
                _gate_target_window(action, caps, end_root, monitor, fmt, quality,
                                    "drag end point: ", before_path)

        # 移动 + 漂移校验（type 前也必须把光标移到 (x,y) 并校验）
        if xi is not None and yi is not None:
            backend.move_to(xi, yi)
            actual = backend.position()
            drift = max(abs(actual[0] - xi), abs(actual[1] - yi))
            if drift > DRIFT_LIMIT_PX:
                after_img, _ = screen_mss.grab(monitor)
                after_path = str(screen_mss.save_audit(after_img, "after", fmt, quality))
                raise SafetyError(
                    f"cursor drift {drift}px exceeds {DRIFT_LIMIT_PX}px after moveTo",
                    x=xi, y=yi, expected=[xi, yi], actual=list(actual),
                    before_screenshot_path=before_path,
                    after_screenshot_path=after_path)
            # 点击/滚轮落点归属门禁：payload 带 hwnd 的点击系与 scroll 动作，
            # 作用点必须仍属于目标窗口（moveTo 与执行之间窗口可能被拖走/遮挡）
            # ——否则拒绝，绝不把输入打进别的窗口。
            if want_hwnd and action in ("click", "double_click", "right_click",
                                        "scroll"):
                root = backend.point_root((xi, yi))
                if root != int(want_hwnd):
                    after_img, _ = screen_mss.grab(monitor)
                    after_path = str(screen_mss.save_audit(after_img, "after", fmt, quality))
                    raise SafetyError(
                        f"click point ({xi},{yi}) is covered by another window "
                        f"(root={root}, expected {int(want_hwnd)})",
                        x=xi, y=yi, expected_root=int(want_hwnd), actual_root=root,
                        before_screenshot_path=before_path,
                        after_screenshot_path=after_path)

        # 执行（FailSafeException 会从 pyautogui 冒出并中止整个动作）
        if action == "click":
            backend.click(clicks=1, button="left")
        elif action == "double_click":
            backend.click(clicks=2, button="left")
        elif action == "right_click":
            backend.click(clicks=1, button="right")
        elif action == "type":
            backend.write_text(str(text))
        elif action == "hotkey":
            backend.hotkey(*keys)
        elif action == "drag":
            # 拖拽（v0.6 M1）：起点已 moveTo+漂移校验；执行前先做终点落点归属
            # 门禁（带 hwnd 时），执行中逐点 Fail-Safe，finally 强制释放鼠标键。
            if trajectory is None:
                raise InternalActionError("drag trajectory missing after validation")
            if want_hwnd:
                # 起点+终点双门禁：起点被覆盖时拖拽会落进错误窗口（可能拖动别的
                # 窗口本体），终点被覆盖同 click 门禁语义——都在 mouse_down 前查。
                for gate_pt, gate_name in ((trajectory[0], "start"),
                                           (trajectory[-1], "end")):
                    root = backend.point_root((gate_pt[0], gate_pt[1]))
                    if root != int(want_hwnd):
                        after_img, _ = screen_mss.grab(monitor)
                        after_path = str(screen_mss.save_audit(after_img, "after", fmt, quality))
                        raise SafetyError(
                            f"drag {gate_name} point {tuple(gate_pt)} is covered by "
                            f"another window (root={root}, expected {int(want_hwnd)})",
                            x=gate_pt[0], y=gate_pt[1], expected_root=int(want_hwnd),
                            actual_root=root,
                            before_screenshot_path=before_path,
                            after_screenshot_path=after_path)
            drag_t0 = time.perf_counter()
            backend.mouse_down("left")
            try:
                for i in range(1, len(trajectory)):
                    px, py = trajectory[i]
                    if _in_corner((px, py)):
                        after_img, _ = screen_mss.grab(monitor)
                        after_path = str(screen_mss.save_audit(after_img, "after", fmt, quality))
                        raise FailSafeTriggered(
                            "trajectory point (" + str(px) + "," + str(py)
                            + ") is inside a Fail-Safe corner mid-drag; released",
                            before_screenshot_path=before_path,
                            after_screenshot_path=after_path)
                    backend.move_to(px, py)
                    landed = backend.position()
                    if _in_corner(landed):
                        after_img, _ = screen_mss.grab(monitor)
                        after_path = str(screen_mss.save_audit(after_img, "after", fmt, quality))
                        raise FailSafeTriggered(
                            "cursor entered a Fail-Safe corner mid-drag at "
                            + str(list(landed)) + "; released",
                            before_screenshot_path=before_path,
                            after_screenshot_path=after_path)
                    if trajectory_t is not None and i < len(trajectory_t):
                        ahead = trajectory_t[i] / 1000.0 - (time.perf_counter() - drag_t0)
                        if ahead > 0:
                            time.sleep(min(ahead, 0.25))
            finally:
                backend.release_pointer_buttons()  # 绝不把"按住"状态泄漏到系统
        elif action == "scroll":
            # 滚轮（v0.6 M1）：smooth 时分片滚动；带 hwnd 的归属门禁已在上方复核
            if scroll_dy is None:
                raise InternalActionError("scroll dy missing after validation")
            if scroll_smooth:
                sign = 1 if scroll_dy > 0 else -1
                chunks = max(2, min(abs(scroll_dy) // 40, 20))
                base = abs(scroll_dy) // chunks
                parts = [sign * base] * chunks
                parts[-1] += scroll_dy - sign * base * chunks
            else:
                parts = [scroll_dy]
            for i, part in enumerate(parts):
                if part:
                    backend.scroll(part)
                if len(parts) > 1 and i < len(parts) - 1:
                    time.sleep(0.015)
        # move：moveTo 本身即动作

        if settle > 0:
            time.sleep(settle)
        cursor_after = list(backend.position())
        after_img, _ = screen_mss.grab(monitor)
        after_path = str(screen_mss.save_audit(after_img, "after", fmt, quality))
    except FailSafeException:
        janitor.release_input("action_failsafe")  # v0.7.1 失败路径清场
        scene_path, scene_b64 = _scene_shot(fmt, quality)
        raise FailSafeTriggered(
            "pyautogui Fail-Safe triggered; whole action aborted",
            scene_screenshot_path=scene_path, scene_image_b64=scene_b64,
            before_screenshot_path=before_path, after_screenshot_path=after_path)
    except ActionError:
        # SafetyError / ForegroundError / BadSourceSizeError 原样上抛——
        # 语义化错误绝不能被兜底处理器吞掉转成 INTERNAL_ERROR
        janitor.release_input("action_error")  # v0.7.1 失败路径清场（幂等）
        raise
    except Exception as exc:
        janitor.release_input("action_crash")  # v0.7.1 崩溃路径清场
        try:
            if after_path is None:
                after_img, _ = screen_mss.grab(monitor)
                after_path = str(screen_mss.save_audit(after_img, "error", fmt, quality))
        except Exception:
            pass
        detail = type(exc).__name__ + ": " + str(exc)
        raise InternalActionError(
            detail,
            before_screenshot_path=before_path,
            after_screenshot_path=after_path)

    body: Dict[str, Any] = {
        "ok": True,
        "action": action,
        "executed_at": _now_iso(),
        "cursor_before": cursor_before,
        "cursor_after": cursor_after,
        "x": x_req,
        "y": y_req,
        "coord_source": coord_source,
        "resolved_physical": resolved_physical,
        "monitor": monitor,
        "monitor_origin": [origin["left"], origin["top"]],
        "before_screenshot_path": before_path,
        "after_screenshot_path": after_path,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        "after_image_b64": screen_mss.b64_of(after_img, fmt, quality, scale),
    }
    if action == "drag":
        body["trajectory"] = trajectory          # 宏录制取这份（执行轨迹）
        body["trajectory_t"] = trajectory_t      # 毫秒节奏（相对起点）
    if action == "scroll":
        body["scroll"] = {"dy": scroll_dy, "smooth": scroll_smooth}
    if include_before_b64 and before_img is not None:
        body["before_image_b64"] = screen_mss.b64_of(before_img, fmt, quality, scale)
    return body


def _scene_shot(fmt: str, quality: int) -> Tuple[Optional[str], Optional[str]]:
    """Fail-Safe 现场截图：全分辨率落盘 + 缩放 b64。

    失败安全：抓屏本身可能正在突发故障（此时恰恰是 Fail-Safe 触发的常见时刻），
    这里绝不能再抛异常，否则原始的 FailSafeTriggered 会被掩盖成裸 500。
    失败时返回 (None, None)，响应中相应字段为 null。
    """
    try:
        img, _ = screen_mss.grab(1)
        path = str(screen_mss.save_audit(img, "failsafe", fmt, quality))
        return path, screen_mss.b64_of(img, fmt, quality, 0.5)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("scene shot failed while handling fail-safe: %s", exc)
        return None, None
