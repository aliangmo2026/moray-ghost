"""统一安全管道：细粒度窗口锁 + 坐标校验 + Fail-Safe + 前后截图审计 + v0.7 绝对防御。

所有真实动桌面的调用（包括 v0.3 的宏重放）都必须走 run_action()，
不得绕过本模块直接调用 backend。

v0.17 并发与双通道
------------------
- ``channel="physical"``（默认）：真实鼠标/键盘，行为与 v0.16 完全一致。
  物理鼠标全系统只有一套，仍由 ``ACTION_LOCK`` 全局串行；窗口锁只用于
  同窗口动作的细粒度互斥。
- ``channel="virtual"``：PostMessage 投递到指定 hwnd，不移动物理鼠标、
  不抢前台。不同 hwnd 的动作只受各自窗口锁约束，可真正并行。
- 虚拟通道执行后用 128px 窗口截图（或 WM_GETTEXT 文本）校验；无可观测
  变化且 ``virtual_fallback`` 未关闭 → 自动降级 physical，并写
  ``virtual_fallback`` 审计事件。

v0.7 三道门禁（safety_gate，全部在此咽喉强制）：
1. 窗口黑名单：目标窗口进程/标题高危 → 403 HIGH_RISK_WINDOW（锁前早杀 + 锁内
   落点复核，绝不产生输入）。
2. 键盘黑名单：系统级热键（win 系 / ctrl+shift+esc / ctrl+alt+delete）与破坏性
   文本 → 403 HIGH_RISK_INPUT（纯字符串、最早校验）。
3. 能力令牌：capabilities.allowed_windows 缺失或目标进程不在名单 →
   403 CAPABILITY_DENIED（move 无 hwnd 不与窗口交互，豁免令牌）。

v0.12 第四道门禁（Ghost Guard）：点击前对落点控件做局部 OCR，命中
转账/删除/格式化/退出登录等高危词 → 409 PENDING_APPROVAL（零输入）；
配置 GHOST_APPROVAL_TOKEN 后，payload 携带匹配的 approval_token 才放行。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import win32gui

from . import backend, geometry, safety_gate, screen_mss, virtual_input
from . import action_locks, action_log, audit_sink, authz, breaker, janitor
from . import config, vl_driver
from .backend import FailSafeException

LOG = logging.getLogger("moray-ghost.controller")

# v0.17：物理输入仍全局串行（全系统只有一套鼠标键盘）；虚拟通道不获取本锁。
# 保留原名 ACTION_LOCK 是为了 CDP 路由与既有测试的零回归（同一把锁）。
ACTION_LOCK = threading.Lock()
VALID_CHANNELS = ("physical", "virtual")
DRIFT_LIMIT_PX = 3                  # moveTo 后实际落点允许的最大偏差
CORNER_GUARD_PX = 2                 # 请求坐标距 Fail-Safe 角落点的保护半径
MAX_TRAJECTORY_POINTS = 2000        # drag 单段轨迹点上限（超限等距降采样保端点）
MAX_SUBPATHS = 200                  # v0.16 多段轨迹（subpaths）段数上限
MAX_SUBPATH_TOTAL_POINTS = 8000     # v0.16 多段轨迹全部段合计点数上限
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


class FatalUACError(ActionError):
    """v0.7.2 FATAL 级：目标是 UAC 提权弹窗（consent.exe / 用户账户控制标题）。

    比普通黑名单更高一级：拒绝前先物理释放全部按键鼠标（零输入、零按住态），
    并落 uac_fatal 审计事件供主仓告警。
    """
    status = 403
    error = "FATAL_UAC_ELEVATION"


class PendingApprovalError(ActionError):
    """v0.12 Ghost Guard：点击目标 OCR 文本命中高危词，等待人类审批。

    这不是失败重试语义（409），动作**完全未执行**：在 moveTo 之前就返回，
    鼠标不会移动到危险按钮上。配置 GHOST_APPROVAL_TOKEN 且 payload 带匹配
    approval_token 时才放行；agent 环境不应持有该令牌。
    """
    status = 409
    error = "PENDING_APPROVAL"


class VisualGuardUnavailableError(ActionError):
    """v0.12 Ghost Guard：OCR 引擎不可用且 fail-closed（默认）→ 拒绝点击。"""
    status = 503
    error = "VISUAL_GUARD_UNAVAILABLE"


class RateCircuitBreakError(ActionError):
    """v0.7.2：动作频率熔断（AI 死循环）——服务已同时被强制冻结。"""
    status = 429
    error = "RATE_CIRCUIT_BREAK"


class ActionTimeoutError(ActionError):
    """v0.7.2：动作硬超时（锁等待或执行超预算）——已强制释放按键鼠标并冻结。"""
    status = 408
    error = "ACTION_TIMEOUT"


class InternalActionError(ActionError):
    status = 500
    error = "INTERNAL_ERROR"


class BadSourceSizeError(ActionError):
    status = 400
    error = "BAD_SOURCE_SIZE"


class VirtualInputUnsupportedError(ActionError):
    """v0.17：virtual 通道无法表达该动作，或目标平台不支持。"""
    status = 400
    error = "VIRTUAL_UNSUPPORTED"


class VirtualInputFailedError(ActionError):
    """v0.17：虚拟投递失败（窗口消失/坐标越界/PostMessage 失败）。"""
    status = 400
    error = "VIRTUAL_INPUT_FAILED"


class VirtualInputUnverifiedError(ActionError):
    """v0.17：虚拟投递后无可观测变化，且调用方关闭了自动降级。"""
    status = 409
    error = "VIRTUAL_INPUT_UNVERIFIED"


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


def _raise_fatal_uac(reason: str, info: Optional[Dict[str, Any]],
                     label: str = "") -> None:
    """UAC 提权弹窗 = FATAL：物理切断（先释放按键鼠标）+ 审计留痕 + 拒绝。

    绝不进入动作执行链（本函数只在门禁层被调，任何物理输入之前）。
    """
    janitor.release_input("uac_fatal_guard")
    try:
        audit_sink.write("uac_fatal", detail=label + reason,
                         hwnd=(info or {}).get("hwnd"),
                         process=(info or {}).get("process"),
                         title=(info or {}).get("title"))
    except Exception:  # noqa: BLE001  审计失败不阻断拒绝本身
        pass
    LOG.error("FATAL_UAC_ELEVATION %s%s process=%s title=%r",
              label, reason, (info or {}).get("process"),
              (info or {}).get("title"))
    raise FatalUACError(
        label + reason,
        hwnd=(info or {}).get("hwnd"), process=(info or {}).get("process"),
        title=(info or {}).get("title"))


def run_action(payload: Dict[str, Any]) -> Dict[str, Any]:
    """安全管道唯一入口（v0.6 起同时是驾驶舱动作日志的入账点）。

    成功 / ActionError（含进锁前校验拒绝）/ 未预期异常三路径都入账——
    宏重放、任务步骤、直接 /execute_action 全部经过这里，无一遗漏。
    """
    action = str(payload.get("action", "")).strip().lower()
    channel = str(payload.get("channel") or "physical").strip().lower()
    audit_channel = "native" if channel not in ("virtual",) else "virtual"
    detail = {"x": payload.get("x"), "y": payload.get("y"),
              "monitor": payload.get("monitor") or 1,
              "channel": channel if channel in VALID_CHANNELS else "physical"}
    try:
        result = _run_action_impl(payload)
    except ActionError as exc:
        err_data = exc.data or {}
        action_log.record(audit_channel, action, detail, False, exc.status,
                          thumb=(err_data.get("after_screenshot_path")
                                 or err_data.get("scene_screenshot_path")))
        raise
    except Exception:
        action_log.record(audit_channel, action, detail, False, 500)
        raise
    finally:
        # v0.12：任何动作之后界面都可能变化，元素检索缓存立即失效。
        vl_driver.invalidate_rag_cache()
    action_log.record(audit_channel, action,
                      {"x": result.get("x"), "y": result.get("y"),
                       "monitor": result.get("monitor"),
                       "channel": result.get("channel") or channel},
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

    # v0.17 通道选择：physical（默认，真实鼠标/键盘）| virtual（PostMessage）
    channel = str(payload.get("channel") or "physical").strip().lower()
    if channel not in VALID_CHANNELS:
        raise SafetyError(
            "channel must be one of " + repr(VALID_CHANNELS)
            + ", got " + repr(payload.get("channel")))
    if channel == "virtual" and not config.VIRTUAL_INPUT_ENABLED:
        raise VirtualInputUnsupportedError(
            "virtual input channel is disabled by GHOST_VIRTUAL_INPUT=false")
    vf_raw = payload.get("virtual_fallback")
    virtual_fallback = (bool(config.VIRTUAL_FALLBACK) if vf_raw is None
                        else bool(vf_raw))
    verify_raw = payload.get("verify")
    virtual_verify = (bool(config.VIRTUAL_VERIFY) if verify_raw is None
                      else bool(verify_raw))

    x = payload.get("x")
    y = payload.get("y")
    text = payload.get("text")
    monitor = int(payload.get("monitor") or 1)
    settle = min(max(float(payload.get("settle_seconds") or 0.0), 0.0), 5.0)
    fmt = str(payload.get("fmt") or "jpeg").lower()
    quality = min(max(int(payload.get("quality") or 70), 10), 95)
    scale = min(max(float(payload.get("scale") or 0.5), 0.1), 1.0)
    include_before_b64 = bool(payload.get("include_before_b64"))
    # v0.16：after_image_b64 默认仍返回（兼容 v0.2 契约），但绘图类调用可
    # 关掉以省 CPU/带宽——draw_trajectory 默认走极简响应（路由层传 False）。
    include_after_raw = payload.get("include_after_b64")
    include_after_b64 = True if include_after_raw is None else bool(include_after_raw)
    approval_token = str(payload.get("approval_token") or "").strip() or None
    source_width = payload.get("source_width")
    source_height = payload.get("source_height")

    if fmt not in ("jpeg", "png"):
        raise SafetyError(f"fmt must be jpeg or png, got {fmt!r}")
    if action == "type" and not text:
        raise SafetyError("action 'type' requires non-empty text")
    if action == "hotkey" and not text:
        raise SafetyError("action 'hotkey' requires a combo like ctrl+s")
    if action in COORD_ACTIONS and (x is None or y is None) \
            and not (action == "drag"
                     and (payload.get("points") or payload.get("subpaths")
                          or payload.get("primitives"))) \
            and not (channel == "virtual" and action == "type"):
        # drag 带显式轨迹/多段/原语时 x/y 可省略（起点取首点）；
        # virtual type 不需要屏幕坐标（消息直接投给目标窗口），其余动作必须给 x/y
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
    # v0.16：drag 原生支持多段轨迹 subpaths（段间物理抬笔，绝不连线）与
    # 几何原语 primitives（本地展开成平滑点列——大模型不传坐标传语义）。
    trajectory: Optional[List[List[int]]] = None
    trajectory_t: Optional[List[int]] = None
    subpaths: Optional[List[List[List[int]]]] = None
    subpaths_t: Optional[List[List[int]]] = None
    scroll_dy: Optional[int] = None
    scroll_smooth = False
    if action == "drag":
        duration_ms = min(max(int(payload.get("duration_ms") or 600), 100), 10000)
        raw_points = payload.get("points")
        raw_subpaths = payload.get("subpaths")
        raw_primitives = payload.get("primitives")
        if raw_points is not None and raw_subpaths is not None:
            raise SafetyError(
                "drag accepts either points or subpaths, not both")
        segments: List[List[List[int]]] = []
        legacy_points_t: Optional[List[int]] = None
        if raw_subpaths is not None or raw_points is not None:
            if raw_subpaths is not None:
                if not isinstance(raw_subpaths, list) or not raw_subpaths:
                    raise SafetyError(
                        "drag subpaths must be a non-empty list of point lists")
                if len(raw_subpaths) > MAX_SUBPATHS:
                    raise SafetyError(
                        f"drag subpaths count {len(raw_subpaths)} exceeds "
                        f"max {MAX_SUBPATHS}")
                for sp in raw_subpaths:
                    if not isinstance(sp, list) or len(sp) < 2:
                        raise SafetyError(
                            "each drag subpath must be a list of >=2 [x,y] "
                            "entries")
                    pts_seg: List[Tuple[int, int]] = []
                    for p in sp:
                        if (not isinstance(p, (list, tuple)) or len(p) < 2
                                or not isinstance(p[0], (int, float))
                                or not isinstance(p[1], (int, float))):
                            raise SafetyError(
                                "drag subpath entries must be [x,y] numbers")
                        pts_seg.append((int(p[0]), int(p[1])))
                    if len(pts_seg) > MAX_TRAJECTORY_POINTS:
                        keep = MAX_TRAJECTORY_POINTS
                        step = (len(pts_seg) - 1) / (keep - 1)
                        pts_seg = [pts_seg[round(i * step)]
                                   for i in range(keep)]
                    segments.append([[px, py] for px, py in pts_seg])
                total_pts = sum(len(s) for s in segments)
                if total_pts > MAX_SUBPATH_TOTAL_POINTS:
                    raise SafetyError(
                        f"drag subpaths total points {total_pts} exceeds max "
                        f"{MAX_SUBPATH_TOTAL_POINTS}")
            else:
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
                segments.append([[px, py] for px, py in pts])
                raw_t = payload.get("points_t")
                if isinstance(raw_t, list) \
                        and len(raw_t) == len(segments[0]):
                    legacy_points_t = [min(max(int(t), 0), 10000) for t in raw_t]
                    if legacy_points_t != sorted(legacy_points_t):
                        raise SafetyError("drag points_t must be non-decreasing")
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
            segments.append([
                [int(round(sx + (x2i - sx) * i / (n - 1))),
                 int(round(sy + (y2i - sy) * i / (n - 1)))]
                for i in range(n)])

        # 几何原语：本地展开成平滑点列（frac 坐标相对目标窗口/显示器）
        if raw_primitives is not None:
            if not isinstance(raw_primitives, list):
                raise SafetyError("drag primitives must be a list of objects")
            hwnd_hint = payload.get("hwnd")
            if hwnd_hint:
                try:
                    wr = win32gui.GetWindowRect(int(hwnd_hint))
                    frame = (wr[0], wr[1], wr[2] - wr[0], wr[3] - wr[1])
                except Exception as exc:  # noqa: BLE001
                    raise SafetyError(
                        "cannot resolve primitives frame from hwnd: "
                        + type(exc).__name__) from exc
            else:
                frame = (mon["left"], mon["top"], mon["width"], mon["height"])
            try:
                prim_subs, _metas = geometry.primitives_to_subpaths(
                    raw_primitives, frame,
                    str(payload.get("primitive_units") or "frac"))
            except geometry.PrimitiveError as exc:
                raise SafetyError(str(exc)) from exc
            segments.extend([[[int(round(px)), int(round(py))] for px, py in seg]
                             for seg in prim_subs])
            if len(segments) > MAX_SUBPATHS:
                raise SafetyError(
                    f"drag subpaths count {len(segments)} (with primitives) "
                    f"exceeds max {MAX_SUBPATHS}")
            total_pts = sum(len(s) for s in segments)
            if total_pts > MAX_SUBPATH_TOTAL_POINTS:
                raise SafetyError(
                    f"drag subpaths total points {total_pts} exceeds max "
                    f"{MAX_SUBPATH_TOTAL_POINTS}")

        if len(segments) > 1 or raw_subpaths is not None:
            subpaths = segments
            total_units = max(1, sum(len(s) - 1 for s in segments))
            elapsed = 0
            subpaths_t = []
            for s in segments:
                seg_ms = duration_ms * (len(s) - 1) / total_units
                ts = [int(round(j * seg_ms / max(1, len(s) - 1)))
                      for j in range(len(s))]
                subpaths_t.append(ts)
                elapsed += ts[-1]
            trajectory = [p for s in segments for p in s]
            flat: List[int] = []
            offset = 0
            for s, ts in zip(segments, subpaths_t):
                flat.extend(offset + t for t in ts)
                offset += ts[-1]
            trajectory_t = flat
        else:
            trajectory = segments[0]
            if legacy_points_t is not None:
                trajectory_t = legacy_points_t
            else:
                step_ms = duration_ms / (len(trajectory) - 1)
                trajectory_t = [int(round(i * step_ms))
                                for i in range(len(trajectory))]
        # 轨迹起点为权威：改写 x/y，让下方通用 bounds/换算/回显全部以它为准
        x, y = trajectory[0][0], trajectory[0][1]
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
    if action in COORD_ACTIONS and x is not None and y is not None:
        x_req, y_req = int(x), int(y)  # 已在上方校验非空（virtual type 豁免）
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
    if channel == "virtual":
        # 虚拟通道的语义就是「操作指定窗口」：没有 hwnd 就无从投递，绝不
        # 猜前台窗口（那会把消息投给用户正在用的应用）。
        if not want_hwnd:
            raise SafetyError(
                "channel='virtual' requires an explicit hwnd (target window)")
        if not virtual_input.available():
            raise VirtualInputUnsupportedError(
                "virtual input channel is not available on this platform")
        if action in ("move", "hotkey"):
            raise VirtualInputUnsupportedError(
                "action " + repr(action) + " is not supported on the virtual "
                "channel (supported: click/double_click/right_click/type/"
                "drag/scroll)", action=action)
    # 会打进窗口的输入一律要求令牌；move 无 hwnd 不与任何窗口交互 → 豁免
    needs_caps = action != "move" or want_hwnd is not None
    if needs_caps and caps is None:
        raise CapabilityDeniedError(
            "no capability token: declare capabilities.allowed_windows "
            "(最小权限原则：未声明白名单的输入一律拒绝)", action=action)
    if want_hwnd:
        info = safety_gate.window_info(int(want_hwnd))
        if info is not None:
            # v0.7.2 UAC 提权弹窗 = FATAL，先于一切黑名单判定（宁拒不放）
            uac_reason = safety_gate.uac_fatal_reason(info)
            if uac_reason:
                _raise_fatal_uac(uac_reason, info)
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
        if subpaths is not None and len(subpaths) > 1:
            audit_extra += " subpaths=" + str(len(subpaths))
    elif action == "scroll" and scroll_dy is not None:
        audit_extra = " dy=" + str(scroll_dy) + (" smooth" if scroll_smooth else "")
    if payload.get("is_painting"):
        audit_extra += " is_painting"
    audit_channel = "virtual" if channel == "virtual" else "native"
    LOG.info("input_audit channel=%s action=%s resolved=%s monitor=%s%s",
             audit_channel, action, resolved_physical, monitor, audit_extra)

    # ---- v0.7.2 硬超时执行 + 频率熔断 -------------------------------------
    # 两段预算：取锁等待 budget_lock；执行 budget_exec = 硬超时 + 声明时长补偿
    # （拖拽 duration_ms / settle 是契约内合法时长，不计入超时预算）+ 固定余量
    # （前后审计抓屏 ~0.3s + 逐点/逐字开销——实测 6s 拖拽真实耗时 ~7.5s）。
    # 执行在工作线程里进行：请求线程 join 超预算 → 置 abort（未开始的执行在
    # 进锁后立即放弃，绝不产生输入）+ 物理释放按键鼠标 + 冻结服务 + 408。
    declared = float(settle)
    if action == "drag":
        declared += min(max(int(payload.get("duration_ms") or 600),
                            100), 10000) / 1000.0
    budget_lock = float(config.ACTION_TIMEOUT)
    budget_exec = float(config.ACTION_TIMEOUT) + declared + 2.0  # 固定开销余量

    abort = threading.Event()
    box: Dict[str, Any] = {}

    def _exec() -> None:
        # v0.17：先取目标窗口的细粒度锁。不同 hwnd 的 virtual 动作从此可以
        # 真正并行；同一窗口仍然串行。无法解析目标窗口 → 全局锁兜底。
        lock_target = _resolve_lock_target(action, want_hwnd, xi, yi)
        lock_key = action_locks.window_key(lock_target) or action_locks.GLOBAL_KEY
        wait_deadline = time.monotonic() + budget_lock
        lease = action_locks.acquire_window(lock_target, budget_lock)
        if lease is None:
            box["e"] = ActionTimeoutError(
                "window lock busy for " + format(budget_lock, ".1f")
                + "s on " + action_locks.describe_key(lock_key)
                + " (another action on the same target); request aborted, "
                "no input sent",
                phase="lock_wait", released=False, frozen=False,
                channel=channel, lock_target=lock_target)
            return
        physical_locked = False
        try:
            if abort.is_set():
                box["e"] = ActionTimeoutError(
                    "request timed out before execution; aborted, no input sent",
                    phase="aborted", released=True, frozen=True)
                return
            if channel == "physical":
                # 物理鼠标/键盘全系统只有一套：窗口锁之外还必须拿物理锁。
                # virtual 绝不获取本锁——它不碰真实设备。
                remaining = max(0.0, wait_deadline - time.monotonic())
                got = ACTION_LOCK.acquire(timeout=remaining)
                if not got:
                    box["e"] = ActionTimeoutError(
                        "physical input lock busy for "
                        + format(budget_lock, ".1f")
                        + "s (another physical action in flight); request "
                        "aborted, no input sent",
                        phase="lock_wait", released=False, frozen=False,
                        channel=channel)
                    return
                physical_locked = True
            # 频率熔断：只登记真正要上桌面的动作（窗口锁内登记，同窗口串行，
            # 不同窗口并发安全——breaker.hit 内部自有互斥）。
            # is_painting（draw_trajectory 长时绘图任务）豁免指纹计数：合法
            # 绘图坐标天然离散、不构成死循环；冻结/归属/前台/Fail-Safe/硬超时
            # 等其余门禁一道不少，审计照记（input_audit 行带 is_painting 标记）。
            if not payload.get("is_painting"):
                fp = breaker.make_fingerprint(
                    "virtual" if channel == "virtual" else "native",
                    action, x_req, y_req,
                    text if action in ("type", "hotkey") else None)
                trip = breaker.hit(fp)
                if trip is not None:
                    box["trip"] = trip
                    box["fp"] = fp
                    return
            try:
                box["r"] = _run_locked_action(
                    action, xi, yi, text, keys, monitor, settle,
                    fmt, quality, scale, include_before_b64,
                    x_req, y_req, coord_source, resolved_physical,
                    want_hwnd, caps, trajectory, trajectory_t,
                    scroll_dy, scroll_smooth, abort,
                    include_after_b64=include_after_b64,
                    subpaths=subpaths, subpaths_t=subpaths_t,
                    approval_token=approval_token,
                    channel=channel,
                    virtual_fallback=virtual_fallback,
                    virtual_verify=virtual_verify)
            except BaseException as exc:  # noqa: BLE001  在请求线程原样重抛
                box["e"] = exc
        finally:
            if physical_locked:
                ACTION_LOCK.release()
            lease.release()

    worker = threading.Thread(target=_exec, name="ghost-action-exec",
                              daemon=True)
    worker.start()
    worker.join(timeout=budget_lock + budget_exec)
    if worker.is_alive():
        # 硬超时：中止尚未开始的执行 + 物理释放 + 熔断式冻结，通知主仓。
        # 正在执行中的线程无法被 Python 强杀——冻结保证它不再有后继输入，
        # 释放保证系统不遗留"按住"状态（残余风险 = 该原子动作自身收尾）。
        abort.set()
        janitor.release_input("action_hard_timeout")
        trip = breaker.trip_now({
            "rule": "hard_timeout", "action": action,
            "budget_seconds": round(budget_lock + budget_exec, 3)})
        frozen = breaker.engage("action_hard_timeout", trip)
        raise ActionTimeoutError(
            "action exceeded hard timeout ("
            + format(budget_lock + budget_exec, ".1f")
            + "s); input forcibly released and service frozen",
            phase="exec_deadline", released=True, frozen=True,
            frozen_at=frozen.get("frozen_at"), trip_info=trip)
    if "trip" in box:
        frozen = breaker.engage("rate_circuit_break", box["trip"])
        LOG.error("RATE_CIRCUIT_BREAK %s", box["trip"])
        raise RateCircuitBreakError(
            "action rate circuit breaker tripped (AI loop suspected); "
            "service force-frozen - release via POST /api/ghost/"
            "emergency_stop/release after review",
            frozen=True, frozen_at=frozen.get("frozen_at"),
            frozen_reason="rate_circuit_break", **box["trip"])
    if "e" in box:
        raise box["e"]
    return box["r"]


def _root_hwnd(hwnd: Any) -> Optional[int]:
    """hwnd → 顶层根窗口（锁隔离维度必须用根，子控件/顶层不能各拿一把锁）。"""
    try:
        h = int(hwnd)
    except (TypeError, ValueError):
        return None
    if h <= 0:
        return None
    try:
        root = win32gui.GetAncestor(h, 2)  # GA_ROOT
        return int(root) if root else h
    except Exception:  # noqa: BLE001
        return h


def _resolve_lock_target(action: str, want_hwnd: Optional[int],
                         xi: Optional[int], yi: Optional[int]) -> Optional[int]:
    """v0.17 锁隔离维度：本动作会作用到哪个窗口（None → 全局锁）。

    与 ``_resolve_input_target`` 语义一致，但只用只读解析（WindowFromPoint /
    GetForegroundWindow），绝不产生输入；解析失败/无窗口 → None，退化到
    GLOBAL_KEY，串行化所有「目标窗口未知」的动作。
    """
    if want_hwnd:
        return _root_hwnd(want_hwnd)
    if action in ("click", "double_click", "right_click", "drag", "scroll") \
            and xi is not None and yi is not None:
        return backend.point_root((xi, yi))
    if action in ("type", "hotkey"):
        return backend.foreground_hwnd()
    return None


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
    """锁内门禁：UAC FATAL → 窗口黑名单 → 能力白名单，命中即拒（带审计截图）。"""
    if not target_hwnd:
        return
    info = safety_gate.window_info(target_hwnd)
    # v0.7.2 UAC 提权弹窗：FATAL 级，先于普通黑名单（锁内实时解析同样覆盖）
    uac_reason = safety_gate.uac_fatal_reason(info)
    if uac_reason:
        _raise_fatal_uac(uac_reason, info, label=label)
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


def _visual_guard_click(action: str, before_img, origin: Dict[str, int],
                        xi: int, yi: int, approval_token: Optional[str],
                        before_path: Optional[str],
                        target_hwnd: Optional[int]) -> Dict[str, Any]:
    """v0.12 Ghost Guard：点击前对落点控件做局部 OCR + 高危词动态检测。

    用 before 截图（零额外抓屏）先由 CV 找包含落点的控件矩形，只 OCR 控件
    内部文本——绝不把相邻按钮的「删除」读成当前点击目标。命中 → 抛
    409 PENDING_APPROVAL（此时尚未 moveTo，物理鼠标不动）。fail-closed
    默认开启：OCR 引擎不可用则 503，绝不在"看不见"时盲点。
    """
    if not config.GUARD_ENABLED:
        return {"enabled": False, "verdict": "off"}
    ox = int(origin.get("left") or 0)
    oy = int(origin.get("top") or 0)
    try:
        texts, meta = vl_driver.ocr_control_at(before_img, [ox, oy], xi, yi)
    except Exception as exc:  # noqa: BLE001
        detail = type(exc).__name__ + ": " + str(exc)
        audit_sink.write("guard_unavailable", action=action, x=xi, y=yi,
                         hwnd=target_hwnd, error=detail[:300],
                         fail_closed=config.GUARD_FAIL_CLOSED,
                         before_screenshot_path=before_path)
        if config.GUARD_FAIL_CLOSED:
            raise VisualGuardUnavailableError(
                "Ghost Guard cannot OCR the click target (" + detail
                + "); click refused (fail-closed, set "
                "GHOST_GUARD_FAIL_CLOSED=false to opt out)",
                action=action, x=xi, y=yi, guard_backend="unavailable",
                before_screenshot_path=before_path) from exc
        meta = {"source": "unavailable", "texts": [], "error": detail[:300]}
        meta.update({"enabled": True, "verdict": "unavailable"})
        return meta
    verdict = safety_gate.dynamic_guard(texts)
    meta.update({"enabled": True,
                 "verdict": verdict["verdict"],
                 "categories": verdict["categories"],
                 "hits": verdict["hits"]})
    if not verdict["requires_approval"]:
        return meta
    if safety_gate.approval_matches(approval_token):
        audit_sink.write("guard_approved", action=action, x=xi, y=yi,
                         hwnd=target_hwnd, hits=verdict["hits"],
                         ocr_box=meta.get("box_screen"),
                         before_screenshot_path=before_path)
        meta["verdict"] = "approved"
        meta["approved"] = True
        return meta
    audit_sink.write("guard_pending", action=action, x=xi, y=yi,
                     hwnd=target_hwnd, hits=verdict["hits"],
                     categories=verdict["categories"],
                     ocr_box=meta.get("box_screen"),
                     texts=meta.get("texts"),
                     approval_token_configured=
                     safety_gate.approval_configured(),
                     before_screenshot_path=before_path)
    raise PendingApprovalError(
        "Ghost Guard: click target reads "
        + repr([h.get("text") for h in verdict["hits"]][:3])
        + " (" + ", ".join(verdict["categories"])
        + "); human approval required - action NOT executed",
        action=action, x=xi, y=yi,
        hits=verdict["hits"], categories=verdict["categories"],
        requires_approval=True,
        approval_token_configured=safety_gate.approval_configured(),
        visual_guard=meta,
        before_screenshot_path=before_path)


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
                       scroll_smooth: bool = False,
                       abort: Optional[threading.Event] = None,
                       include_after_b64: bool = True,
                       subpaths: Optional[List[List[List[int]]]] = None,
                       subpaths_t: Optional[List[List[int]]] = None,
                       approval_token: Optional[str] = None,
                       channel: str = "physical",
                       virtual_fallback: bool = True,
                       virtual_verify: bool = True
                       ) -> Dict[str, Any]:
    if channel == "virtual":
        return _run_virtual_action(
            action, xi, yi, text, monitor, settle, fmt, quality, scale,
            include_before_b64, x_req, y_req, coord_source, resolved_physical,
            want_hwnd, caps, trajectory, trajectory_t, scroll_dy,
            scroll_smooth, abort, include_after_b64, subpaths, subpaths_t,
            approval_token, virtual_fallback, virtual_verify)
    t0 = time.perf_counter()
    # v0.7.2 硬超时中止点：请求线程已放弃本动作——此刻尚未产生任何输入，直接退出
    if abort is not None and abort.is_set():
        raise ActionTimeoutError(
            "request timed out before execution; aborted, no input sent",
            phase="aborted", released=True, frozen=True)
    before_path: Optional[str] = None
    after_path: Optional[str] = None
    visual_guard: Optional[Dict[str, Any]] = None
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
        # v0.12 Ghost Guard：点击类动作在物理移动之前做局部 OCR 动态门禁。
        # 用 before 截图（无额外抓屏）；CV 定位落点控件边界后只 OCR 控件内部。
        if action in ("click", "double_click", "right_click") \
                and xi is not None and yi is not None:
            visual_guard = _visual_guard_click(
                action, before_img, origin, xi, yi, approval_token,
                before_path, target_hwnd)
        if action == "drag" and trajectory is not None and not want_hwnd:
            # 无 hwnd 拖拽：终点根窗口若与起点不同（跨窗口拖拽），两端都必须
            # 过黑名单 + 白名单；带 hwnd 的拖拽已有起点+终点归属门禁兜底。
            end_root = backend.point_root((trajectory[-1][0], trajectory[-1][1]))
            if end_root and end_root != target_hwnd:
                _gate_target_window(action, caps, end_root, monitor, fmt, quality,
                                    "drag end point: ", before_path)

        # 移动 + 漂移校验（type 前也必须把光标移到 (x,y) 并校验）
        if xi is not None and yi is not None:
            if abort is not None and abort.is_set():
                raise ActionTimeoutError(
                    "action aborted before physical move (hard timeout)",
                    phase="mid_action", released=True, frozen=True)
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
            # 拖拽（v0.6 M1 → v0.16 多段）：起点已 moveTo+漂移校验；执行前先
            # 逐段做起/终点落点归属门禁（带 hwnd 时），执行中逐点 Fail-Safe，
            # 每段结束物理抬笔（mouse_up）→ 移动到下一段起点 → 落笔，
            # finally 强制释放鼠标键——段间绝不连线。
            if trajectory is None:
                raise InternalActionError("drag trajectory missing after validation")
            segments = subpaths if subpaths else [trajectory]
            if want_hwnd:
                # 每段起点+终点双门禁：起点被覆盖时拖拽会落进错误窗口（可能
                # 拖动别的窗口本体），终点被覆盖同 click 门禁语义——都在
                # mouse_down 前查（多段在第一次落笔前全部查完，快速失败）。
                for seg in segments:
                    for gate_pt, gate_name in ((seg[0], "start"),
                                               (seg[-1], "end")):
                        root = backend.point_root((gate_pt[0], gate_pt[1]))
                        if root != int(want_hwnd):
                            after_img, _ = screen_mss.grab(monitor)
                            after_path = str(screen_mss.save_audit(
                                after_img, "after", fmt, quality))
                            raise SafetyError(
                                f"drag {gate_name} point {tuple(gate_pt)} is "
                                f"covered by another window (root={root}, "
                                f"expected {int(want_hwnd)})",
                                x=gate_pt[0], y=gate_pt[1],
                                expected_root=int(want_hwnd),
                                actual_root=root,
                                before_screenshot_path=before_path,
                                after_screenshot_path=after_path)
            # 轨迹节奏完全由各段 points_t 自控；pyautogui 全局 PAUSE(0.05s)
            # 逐点施加会把 6s 拖拽拖成 ~25s（v0.7.2 实测），循环期间归零、
            # 出环即还原。
            prev_pause = getattr(backend.pyautogui, "PAUSE", 0)
            backend.pyautogui.PAUSE = 0
            try:
                for si, seg in enumerate(segments):
                    # 单段（legacy points/x2y2）用整段 trajectory_t；多段用
                    # 各段自带的相对节奏——漏掉前者会把 6s 拖拽跑成 1.5s。
                    seg_ts = subpaths_t[si] if subpaths_t is not None \
                        and si < len(subpaths_t) else trajectory_t
                    if si > 0:
                        # ---- 抬笔移段（v0.16）：键已松开，滑到下一段起点 ----
                        if abort is not None and abort.is_set():
                            after_img, _ = screen_mss.grab(monitor)
                            after_path = str(screen_mss.save_audit(
                                after_img, "after", fmt, quality))
                            raise ActionTimeoutError(
                                "drag aborted between subpaths (hard "
                                "timeout); pointer button released",
                                phase="mid_action", released=True,
                                frozen=True, segment=si,
                                before_screenshot_path=before_path,
                                after_screenshot_path=after_path)
                        backend.move_to(seg[0][0], seg[0][1])
                        landed = backend.position()
                        if _in_corner(landed):
                            after_img, _ = screen_mss.grab(monitor)
                            after_path = str(screen_mss.save_audit(
                                after_img, "after", fmt, quality))
                            raise FailSafeTriggered(
                                "cursor entered a Fail-Safe corner while "
                                "traveling between subpaths at "
                                + str(list(landed)) + "; released",
                                before_screenshot_path=before_path,
                                after_screenshot_path=after_path)
                    backend.mouse_down("left")
                    seg_t0 = time.perf_counter()
                    try:
                        for i in range(1, len(seg)):
                            px, py = seg[i]
                            if abort is not None and abort.is_set():
                                # v0.7.2 硬超时中止：立即停轨，finally 负责释放鼠标键
                                after_img, _ = screen_mss.grab(monitor)
                                after_path = str(screen_mss.save_audit(
                                    after_img, "after", fmt, quality))
                                raise ActionTimeoutError(
                                    "drag aborted mid-trajectory (hard timeout); "
                                    "pointer button released",
                                    phase="mid_action", released=True, frozen=True,
                                    point=i, segment=si,
                                    before_screenshot_path=before_path,
                                    after_screenshot_path=after_path)
                            if _in_corner((px, py)):
                                after_img, _ = screen_mss.grab(monitor)
                                after_path = str(screen_mss.save_audit(
                                    after_img, "after", fmt, quality))
                                raise FailSafeTriggered(
                                    "trajectory point (" + str(px) + "," + str(py)
                                    + ") is inside a Fail-Safe corner mid-drag; released",
                                    before_screenshot_path=before_path,
                                    after_screenshot_path=after_path)
                            backend.move_to(px, py)
                            landed = backend.position()
                            if _in_corner(landed):
                                after_img, _ = screen_mss.grab(monitor)
                                after_path = str(screen_mss.save_audit(
                                    after_img, "after", fmt, quality))
                                raise FailSafeTriggered(
                                    "cursor entered a Fail-Safe corner mid-drag at "
                                    + str(list(landed)) + "; released",
                                    before_screenshot_path=before_path,
                                    after_screenshot_path=after_path)
                            if seg_ts is not None and i < len(seg_ts):
                                ahead = seg_ts[i] / 1000.0 \
                                    - (time.perf_counter() - seg_t0)
                                if ahead > 0:
                                    time.sleep(min(ahead, 0.25))
                    finally:
                        backend.release_pointer_buttons()  # 抬笔：段间绝不连线
            finally:
                backend.release_pointer_buttons()  # 绝不把"按住"状态泄漏到系统
                backend.pyautogui.PAUSE = prev_pause
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
                if abort is not None and abort.is_set():
                    raise ActionTimeoutError(
                        "scroll aborted mid-sequence (hard timeout)",
                        phase="mid_action", released=True, frozen=True,
                        chunks_done=i)
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
    }
    if include_after_b64:
        body["after_image_b64"] = screen_mss.b64_of(after_img, fmt, quality, scale)
    if action == "drag":
        body["trajectory"] = trajectory          # 宏录制取这份（执行轨迹）
        body["trajectory_t"] = trajectory_t      # 毫秒节奏（相对起点）
        if subpaths is not None:
            # v0.16 多段回显：段列表 + 段在展平轨迹里的下标区间
            spans: List[List[int]] = []
            cursor = 0
            for seg in subpaths:
                spans.append([cursor, cursor + len(seg) - 1])
                cursor += len(seg)
            body["subpaths"] = subpaths
            body["subpath_spans"] = spans
    if action == "scroll":
        body["scroll"] = {"dy": scroll_dy, "smooth": scroll_smooth}
    if include_before_b64 and before_img is not None:
        body["before_image_b64"] = screen_mss.b64_of(before_img, fmt, quality, scale)
    if visual_guard is not None:
        body["visual_guard"] = visual_guard
    return body


def _verify_virtual(hwnd: int, action: str,
                    text_before: Optional[str],
                    sig_before: Optional[Any],
                    abort: Optional[threading.Event]) -> Dict[str, Any]:
    """虚拟投递后的可观测性校验：WM_GETTEXT 文本优先，128px 像素兜底。

    轮询到 VIRTUAL_VERIFY_TIMEOUT；期间任一判据成立即 verified。绝不把
    「不知道有没有成功」写成成功——调用方据此决定是否降级或报错。
    """
    deadline = time.monotonic() + float(config.VIRTUAL_VERIFY_TIMEOUT)
    attempts = 0
    last: Dict[str, Any] = {}
    while True:
        attempts += 1
        if action == "type" and text_before is not None:
            now_text = virtual_input.read_text(hwnd, timeout_ms=300)
            if now_text is not None and now_text != text_before:
                return {"enabled": True, "verified": True,
                        "method": "wm_gettext", "attempts": attempts,
                        "text_before_len": len(text_before),
                        "text_after_len": len(now_text)}
        if sig_before is not None:
            sig_after = virtual_input.capture_signature(
                hwnd, config.VIRTUAL_VERIFY_MAX_SIDE)
            last = virtual_input.compare_signatures(
                sig_before, sig_after,
                pixel_tol=config.VIRTUAL_VERIFY_PIXEL_TOL,
                min_ratio=config.VIRTUAL_VERIFY_MIN_RATIO,
                min_changed_px=config.VIRTUAL_VERIFY_MIN_CHANGED_PX,
                min_mean_delta=config.VIRTUAL_VERIFY_MIN_MEAN)
            last.update({"enabled": True, "method": "pixels",
                         "attempts": attempts})
            if last.get("verified"):
                return last
        if time.monotonic() >= deadline:
            break
        if abort is not None and abort.is_set():
            last = dict(last or {})
            last["aborted"] = True
            break
        time.sleep(max(0.0, float(config.VIRTUAL_VERIFY_INTERVAL)))
    if not last:
        last = {"enabled": True, "verified": False, "method": "none",
                "reason": "capture_unavailable", "attempts": attempts}
    last.setdefault("verified", False)
    return last


def _try_virtual_pointer(action: str, hwnd: int, xi: Optional[int],
                         yi: Optional[int],
                         trajectory: Optional[List[List[int]]],
                         subpaths: Optional[List[List[List[int]]]],
                         trajectory_t: Optional[List[int]],
                         subpaths_t: Optional[List[List[int]]],
                         abort: Optional[threading.Event]
                         ) -> Optional[Dict[str, Any]]:
    """PostMessage 无可观测效果时的第二层虚拟通道：合成笔注入。

    仍然不移动物理鼠标；但坐标投递要求目标窗口在该点是最顶层（否则会打到
    遮挡者），因此先逐点校验。支持 click / drag；type 仍走 WM_CHAR，
    不支持的动作直接返回 None 交给 physical 降级。
    """
    if action not in ("click", "drag"):
        return None
    if not config.VIRTUAL_POINTER_ENABLED:
        return None
    if not virtual_input.synthetic_pointer_available():
        return None
    if action == "click":
        if xi is None or yi is None:
            return None
        segments: List[List[List[int]]] = [[[int(xi), int(yi)]]]
        flat: List[Tuple[int, int]] = [(int(xi), int(yi))]
    else:
        if trajectory is None:
            return None
        segments = subpaths if subpaths else [trajectory]
        flat = [(int(p[0]), int(p[1])) for seg in segments for p in seg]
    ok, bad, blocker = virtual_input.points_target_window(hwnd, flat)
    if not ok:
        try:
            audit_sink.write("virtual_pointer_skipped", action=action,
                             hwnd=hwnd, reason="target_not_topmost_or_invisible",
                             point=bad, blocker=blocker)
        except Exception:  # noqa: BLE001
            pass
        return {"attempted": False,
                "reason": "target_not_topmost_or_invisible",
                "point": bad, "blocker": blocker}
    sig_before = virtual_input.stable_signature(
        hwnd, config.VIRTUAL_VERIFY_MAX_SIDE)
    try:
        if action == "click":
            meta: Dict[str, Any] = virtual_input.virtual_pointer_click(
                hwnd, int(xi), int(yi),
                restore_focus=config.VIRTUAL_POINTER_RESTORE_FOCUS)
        else:
            metas: List[Dict[str, Any]] = []
            total_ms = int(trajectory_t[-1]) if trajectory_t else 300
            for si, seg in enumerate(segments):
                seg_ms = (int(subpaths_t[si][-1])
                          if subpaths_t is not None and si < len(subpaths_t)
                          else total_ms)
                metas.append(virtual_input.virtual_pointer_drag(
                    hwnd, seg, duration_ms=seg_ms,
                    restore_focus=config.VIRTUAL_POINTER_RESTORE_FOCUS))
            meta = {"channel": "virtual", "mode": "synthetic_pointer",
                    "op": "drag", "segments": metas,
                    "messages_posted": sum(int(m.get("messages_posted") or 0)
                                           for m in metas),
                    "points": sum(int(m.get("points") or 0)
                                  for m in metas),
                    "foreground_before": metas[0].get("foreground_before")
                    if metas else None,
                    "focus_restored": all(bool(m.get("focus_restored"))
                                          for m in metas) if metas else False}
    except virtual_input.VirtualInputError as exc:
        try:
            audit_sink.write("virtual_pointer", action=action, hwnd=hwnd,
                             ok=False, code=exc.code, detail=exc.detail[:300],
                             **{k: v for k, v in exc.data.items()
                                if isinstance(v, (str, int, float, type(None)))})
        except Exception:  # noqa: BLE001
            pass
        return {"attempted": True, "ok": False, "code": exc.code,
                "detail": exc.detail, **exc.data}
    verify = _verify_virtual(hwnd, action, None, sig_before, abort)
    try:
        audit_sink.write("virtual_pointer", action=action, hwnd=hwnd,
                         ok=bool(verify.get("verified")), verify=verify,
                         pointer=meta)
    except Exception:  # noqa: BLE001
        pass
    return {"attempted": True, "ok": True, "meta": meta, "verify": verify}


def _run_virtual_action(action: str, xi: Optional[int], yi: Optional[int],
                        text: Optional[str], monitor: int, settle: float,
                        fmt: str, quality: int, scale: float,
                        include_before_b64: bool, x_req: Optional[int],
                        y_req: Optional[int], coord_source: str,
                        resolved_physical: Optional[List[int]],
                        want_hwnd: Optional[int],
                        caps: Optional[Dict[str, Any]] = None,
                        trajectory: Optional[List[List[int]]] = None,
                        trajectory_t: Optional[List[int]] = None,
                        scroll_dy: Optional[int] = None,
                        scroll_smooth: bool = False,
                        abort: Optional[threading.Event] = None,
                        include_after_b64: bool = True,
                        subpaths: Optional[List[List[List[int]]]] = None,
                        subpaths_t: Optional[List[List[int]]] = None,
                        approval_token: Optional[str] = None,
                        virtual_fallback: bool = True,
                        virtual_verify: bool = True) -> Dict[str, Any]:
    """v0.17 虚拟通道执行：不移动物理鼠标、不抢前台、不碰剪贴板。

    与 physical 共用：Fail-Safe 角检查、窗口黑名单/能力令牌、Ghost Guard、
    前后审计截图、频率熔断（在锁层）、硬超时 abort。差异只在「怎么送输入」
    以及「用 128px 窗口截图/ WM_GETTEXT 验证是否有可观测效果」。
    """
    t0 = time.perf_counter()
    if abort is not None and abort.is_set():
        raise ActionTimeoutError(
            "request timed out before execution; aborted, no input sent",
            phase="aborted", released=True, frozen=True)
    if not want_hwnd:
        raise VirtualInputUnsupportedError(
            "virtual input requires an explicit hwnd")
    hwnd = int(want_hwnd)
    before_path: Optional[str] = None
    after_path: Optional[str] = None
    visual_guard: Optional[Dict[str, Any]] = None
    origin: Dict[str, int] = {"left": 0, "top": 0}
    cursor_before: List[int] = []
    cursor_after: List[int] = []
    before_img = None
    after_img = None
    sig_before = None
    text_before: Optional[str] = None
    virtual_meta: Dict[str, Any] = {}

    cursor_now = backend.position()
    if _in_corner(cursor_now):
        scene_path, scene_b64 = _scene_shot(fmt, quality)
        raise FailSafeTriggered(
            "mouse is parked in a Fail-Safe corner; whole action aborted",
            cursor=list(cursor_now), scene_screenshot_path=scene_path,
            scene_image_b64=scene_b64)

    try:
        before_img, origin = screen_mss.grab(monitor)
        before_path = str(screen_mss.save_audit(before_img, "before", fmt, quality))
        cursor_before = list(backend.position())

        # 与 physical 完全相同的窗口门禁（UAC FATAL → 黑名单 → 能力白名单）。
        target_hwnd = _resolve_input_target(action, hwnd, xi, yi)
        _gate_target_window(action, caps, target_hwnd, monitor, fmt, quality,
                            "virtual: ", before_path)
        if action in ("click", "double_click", "right_click") \
                and xi is not None and yi is not None:
            visual_guard = _visual_guard_click(
                action, before_img, origin, xi, yi, approval_token,
                before_path, target_hwnd)

        if virtual_verify:
            sig_before = virtual_input.stable_signature(
                hwnd, config.VIRTUAL_VERIFY_MAX_SIDE)
            if action == "type":
                text_before = virtual_input.read_text(hwnd, timeout_ms=400)

        # ---- 投递（唯一发生副作用的地方）----
        if action == "click":
            virtual_meta = virtual_input.virtual_click(
                hwnd, int(xi), int(yi), button="left")
        elif action == "double_click":
            virtual_meta = virtual_input.virtual_click(
                hwnd, int(xi), int(yi), button="left", double=True)
        elif action == "right_click":
            virtual_meta = virtual_input.virtual_click(
                hwnd, int(xi), int(yi), button="right")
        elif action == "type":
            virtual_meta = virtual_input.virtual_type(
                hwnd, str(text), interval_ms=config.VIRTUAL_TYPE_INTERVAL_MS)
        elif action == "drag":
            if trajectory is None:
                raise InternalActionError(
                    "drag trajectory missing after validation")
            segments = subpaths if subpaths else [trajectory]
            metas: List[Dict[str, Any]] = []
            total_ms = int(trajectory_t[-1]) if trajectory_t else 300
            for si, seg in enumerate(segments):
                seg_ms = (int(subpaths_t[si][-1])
                          if subpaths_t is not None and si < len(subpaths_t)
                          else total_ms)
                metas.append(virtual_input.virtual_drag(
                    hwnd, seg, duration_ms=seg_ms))
            virtual_meta = {
                "channel": "virtual", "op": "drag",
                "segments": metas,
                "messages_posted": sum(int(m.get("messages_posted") or 0)
                                       for m in metas),
                "points": sum(int(m.get("points") or 0) for m in metas),
                "duration_ms": sum(int(m.get("duration_ms") or 0)
                                   for m in metas),
                "target_hwnd": (metas[0].get("target_hwnd") if metas else hwnd),
            }
        elif action == "scroll":
            if scroll_dy is None:
                raise InternalActionError("scroll dy missing after validation")
            virtual_meta = virtual_input.virtual_scroll(
                hwnd, int(xi), int(yi), int(scroll_dy))
        else:  # 理论上在参数校验阶段已被拒
            raise VirtualInputUnsupportedError(
                "action " + repr(action) + " is not supported on virtual channel")

        if settle > 0:
            time.sleep(settle)

        verify: Dict[str, Any]
        if virtual_verify:
            verify = _verify_virtual(hwnd, action, text_before, sig_before, abort)
        else:
            verify = {"enabled": False, "verified": True,
                      "method": "disabled"}
        virtual_meta["mode"] = "postmessage"

        # v0.17 第二层：WinUI/内容岛忽略 PostMessage → 合成笔注入（仍不碰
        # 物理鼠标）。只对 click/drag 有意义；type 的 WM_CHAR 已足够。
        if not verify.get("verified") and action in ("click", "drag"):
            pointer = _try_virtual_pointer(
                action, hwnd, xi, yi, trajectory, subpaths, trajectory_t,
                subpaths_t, abort)
            if pointer is not None:
                virtual_meta["pointer"] = pointer
                pv = pointer.get("verify") or {}
                if pv.get("verified"):
                    verify = pv
                    virtual_meta["mode"] = "synthetic_pointer"
                else:
                    virtual_meta["mode"] = "postmessage+pointer_attempt"

        cursor_after = list(backend.position())
        after_img, _ = screen_mss.grab(monitor)
        after_path = str(screen_mss.save_audit(after_img, "after", fmt, quality))
    except virtual_input.VirtualInputError as exc:
        janitor.release_input("virtual_error")
        try:
            if after_path is None:
                after_img, _ = screen_mss.grab(monitor)
                after_path = str(screen_mss.save_audit(
                    after_img, "error", fmt, quality))
        except Exception:  # noqa: BLE001
            pass
        raise VirtualInputFailedError(
            exc.detail, code=exc.code,
            before_screenshot_path=before_path,
            after_screenshot_path=after_path,
            **exc.data) from exc
    except FailSafeException:
        janitor.release_input("action_failsafe")
        scene_path, scene_b64 = _scene_shot(fmt, quality)
        raise FailSafeTriggered(
            "pyautogui Fail-Safe triggered; whole action aborted",
            scene_screenshot_path=scene_path, scene_image_b64=scene_b64,
            before_screenshot_path=before_path, after_screenshot_path=after_path)
    except ActionError:
        janitor.release_input("action_error")
        raise
    except Exception as exc:  # noqa: BLE001
        janitor.release_input("virtual_crash")
        try:
            if after_path is None:
                after_img, _ = screen_mss.grab(monitor)
                after_path = str(screen_mss.save_audit(
                    after_img, "error", fmt, quality))
        except Exception:
            pass
        detail = type(exc).__name__ + ": " + str(exc)
        raise InternalActionError(
            detail, before_screenshot_path=before_path,
            after_screenshot_path=after_path) from exc

    cursor_moved = (max(abs(cursor_after[0] - cursor_before[0]),
                        abs(cursor_after[1] - cursor_before[1]))
                    if cursor_after and cursor_before else 0)
    body: Dict[str, Any] = {
        "ok": True,
        "action": action,
        "channel": "virtual",
        "physical_pointer_used": False,
        "executed_at": _now_iso(),
        "cursor_before": cursor_before,
        "cursor_after": cursor_after,
        "cursor_moved_px": cursor_moved,
        "x": x_req,
        "y": y_req,
        "coord_source": coord_source,
        "resolved_physical": resolved_physical,
        "monitor": monitor,
        "monitor_origin": [origin["left"], origin["top"]],
        "before_screenshot_path": before_path,
        "after_screenshot_path": after_path,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        "virtual": {**virtual_meta, "verify": verify,
                    "fallback": False},
    }
    if include_after_b64:
        body["after_image_b64"] = screen_mss.b64_of(after_img, fmt, quality, scale)
    if action == "drag":
        body["trajectory"] = trajectory
        body["trajectory_t"] = trajectory_t
        if subpaths is not None:
            spans: List[List[int]] = []
            cursor = 0
            for seg in subpaths:
                spans.append([cursor, cursor + len(seg) - 1])
                cursor += len(seg)
            body["subpaths"] = subpaths
            body["subpath_spans"] = spans
    if action == "scroll":
        body["scroll"] = {"dy": scroll_dy, "smooth": scroll_smooth}
    if include_before_b64 and before_img is not None:
        body["before_image_b64"] = screen_mss.b64_of(before_img, fmt, quality, scale)
    if visual_guard is not None:
        body["visual_guard"] = visual_guard

    if verify.get("verified") or not virtual_fallback:
        if not verify.get("verified"):
            raise VirtualInputUnverifiedError(
                "virtual input produced no observable change and automatic "
                "fallback is disabled (set virtual_fallback=true to retry "
                "through the physical channel)",
                channel="virtual", physical_pointer_used=False,
                verify=verify,
                virtual=body.get("virtual"),
                before_screenshot_path=before_path,
                after_screenshot_path=after_path)
        return body

    # ---- v0.17 自动降级：虚拟无可观测效果 → physical 真实输入 ----
    try:
        audit_sink.write("virtual_fallback", action=action, hwnd=hwnd,
                         x=xi, y=yi, verify=verify,
                         virtual=body.get("virtual"),
                         before_screenshot_path=before_path,
                         after_screenshot_path=after_path)
    except Exception:  # noqa: BLE001  审计失败不阻断降级
        pass
    LOG.warning("virtual_fallback action=%s hwnd=%s reason=%s -> physical",
                action, hwnd, verify.get("reason") or verify.get("method"))

    activated: Optional[bool] = None
    if action in ("type", "hotkey"):
        # physical type 的前台门禁要求目标窗口在前台；把窗口带到前台（不移动
        # 鼠标）。这一步会改变用户焦点，审计事件已在上方落盘。
        activated = virtual_input.activate_window(hwnd, timeout=1.5)
        LOG.warning("virtual_fallback activate_window hwnd=%s ok=%s",
                    hwnd, activated)
    got = ACTION_LOCK.acquire(timeout=float(config.VIRTUAL_FALLBACK_LOCK_TIMEOUT))
    if not got:
        raise ActionTimeoutError(
            "virtual input was not verified and the physical fallback lock "
            "could not be acquired within "
            + format(config.VIRTUAL_FALLBACK_LOCK_TIMEOUT, ".1f") + "s",
            phase="fallback_lock_wait", released=False, frozen=False,
            channel="virtual", verify=verify)
    try:
        physical = _run_locked_action(
            action, xi, yi, text, None, monitor, settle, fmt, quality, scale,
            include_before_b64, x_req, y_req, coord_source, resolved_physical,
            want_hwnd, caps, trajectory, trajectory_t, scroll_dy,
            scroll_smooth, abort,
            include_after_b64=include_after_b64,
            subpaths=subpaths, subpaths_t=subpaths_t,
            approval_token=approval_token,
            channel="physical", virtual_fallback=False, virtual_verify=False)
    finally:
        ACTION_LOCK.release()

    physical["channel"] = "physical"
    physical["physical_pointer_used"] = True
    physical["fallback_from_virtual"] = True
    physical["audit_tag"] = "virtual_fallback"
    physical["virtual"] = {**body.get("virtual", {}),
                           "verify": verify, "fallback": True,
                           "fallback_activated": activated,
                           "fallback_reason": (verify.get("reason")
                                               or verify.get("method"))}
    physical["virtual_before_screenshot_path"] = before_path
    physical["virtual_after_screenshot_path"] = after_path
    return physical


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
