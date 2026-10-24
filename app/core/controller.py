"""统一安全管道：全局串行锁 + 坐标校验 + Fail-Safe + 前后截图审计。

所有真实动桌面的调用（包括 v0.3 的宏重放）都必须走 run_action()，
不得绕过本模块直接调用 backend。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from . import backend, screen_mss
from .backend import FailSafeException

ACTION_LOCK = threading.Lock()      # 桌面动作全局串行（进程内锁）
DRIFT_LIMIT_PX = 3                  # moveTo 后实际落点允许的最大偏差
CORNER_GUARD_PX = 2                 # 请求坐标距 Fail-Safe 角落点的保护半径
VALID_ACTIONS = ("click", "double_click", "right_click", "move", "type", "hotkey")
COORD_ACTIONS = ("click", "double_click", "right_click", "move", "type")
VALID_ACTIONS_TXT = "click / double_click / right_click / move / type / hotkey"


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
    if action in COORD_ACTIONS and (x is None or y is None):
        raise SafetyError(f"action {action!r} requires x/y")

    mon_count = len(screen_mss.monitors())  # 含 index 0 = 全屏联合区
    max_mon = mon_count - 1
    if monitor < 1 or monitor > max_mon:
        raise SafetyError(f"monitor out of range: {monitor} (1..{max_mon})")
    mon = screen_mss.monitor_rect(monitor)

    keys: Optional[List[str]] = None
    if action == "hotkey":
        keys = _parse_hotkey(str(text))

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

    with ACTION_LOCK:
        return _run_locked_action(action, xi, yi, text, keys, monitor, settle,
                                  fmt, quality, scale, include_before_b64,
                                  x_req, y_req, coord_source, resolved_physical)


def _run_locked_action(action: str, xi: Optional[int], yi: Optional[int],
                       text: Optional[str], keys: Optional[List[str]], monitor: int,
                       settle: float, fmt: str, quality: int, scale: float,
                       include_before_b64: bool, x_req: Optional[int],
                       y_req: Optional[int], coord_source: str,
                       resolved_physical: Optional[List[int]]) -> Dict[str, Any]:
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
        # move：moveTo 本身即动作

        if settle > 0:
            time.sleep(settle)
        cursor_after = list(backend.position())
        after_img, _ = screen_mss.grab(monitor)
        after_path = str(screen_mss.save_audit(after_img, "after", fmt, quality))
    except FailSafeException:
        scene_path, scene_b64 = _scene_shot(fmt, quality)
        raise FailSafeTriggered(
            "pyautogui Fail-Safe triggered; whole action aborted",
            scene_screenshot_path=scene_path, scene_image_b64=scene_b64,
            before_screenshot_path=before_path, after_screenshot_path=after_path)
    except SafetyError:
        raise
    except Exception as exc:
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
    if include_before_b64 and before_img is not None:
        body["before_image_b64"] = screen_mss.b64_of(before_img, fmt, quality, scale)
    return body


def _scene_shot(fmt: str, quality: int) -> Tuple[str, str]:
    """Fail-Safe 现场截图：全分辨率落盘 + 缩放 b64。"""
    img, _ = screen_mss.grab(1)
    path = str(screen_mss.save_audit(img, "failsafe", fmt, quality))
    return path, screen_mss.b64_of(img, fmt, quality, 0.5)
