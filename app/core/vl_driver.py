"""本地多模态感知驱动（v0.8.0 · MCP 底座的眼睛）：截图/窗口 → 控件 JSON。

统一输出契约（坐标一律物理像素、全屏坐标系）：
    {"ok": true, "backend": "...", "window": {...},
     "controls": [{"text", "type", "bbox": [x, y, w, h], "center": [x, y]}],
     "elapsed_ms": ...}

backend 策略（GHOST_VL_BACKEND = auto | native | ocr | llama | mock）：
- native：win32 EnumChildWindows 几何+类名+窗口文本（零依赖最快；Tk 等自绘控件
  拿不到标签文本——实证 Tk Button 的 Win32 text 为空）。
- ocr    ：rapidocr-onnxruntime（PP-OCR ONNX，轻量级本地视觉模型，~百 MB 内）
  直接"看像素"取文本+bbox——无模型权重管理、任意可见界面通用。
- llama  ：llama-cpp-python + GHOST_VL_MODEL_PATH（GGUF 多模态模型，
  如 Qwen2-VL-2B / MiniCPM-V）——语义级理解（接入真实模型阶段的主通道）。
- mock   ：GHOST_VL_MOCK_JSON 夹具（纯协议联调）。
- auto   ：llama 可用 → llama；否则 ocr 可用 → ocr；否则 native（如实降级，
  backend 字段回显真实出处，绝不冒充）。

路由分流 resolve_channel()：URL 样式 / Edge·Chrome 窗口 → "cdp"（走既有
browser_driver），原生窗口 → "native"（走本驱动 + 安全管道点击）。

匹配铁律 find_control()：0 命中 → 404 VL_ELEMENT_NOT_FOUND；多命中 →
409 VL_AMBIGUOUS —— 与 CDP ELEMENT_AMBIGUOUS 同哲学，绝不猜。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import win32gui

from . import config, safety_gate, window_locator

# ---- 错误语义（HTTP 层由 vl_routes 原样透传 status/error） -----------------


class VLError(Exception):
    status = 400
    error = "VL_ERROR"

    def __init__(self, detail: str, **data: Any):
        super().__init__(detail)
        self.detail = detail
        self.data = data


class VLElementNotFound(VLError):
    status = 404
    error = "VL_ELEMENT_NOT_FOUND"


class VLAmbiguous(VLError):
    status = 409
    error = "VL_AMBIGUOUS"


class VLWindowNotFound(VLError):
    status = 409
    error = "WINDOW_NOT_FOUND"


class VLBackendUnavailable(VLError):
    status = 503
    error = "VL_BACKEND_UNAVAILABLE"


# ---- 通道/窗口路由 ---------------------------------------------------------

_EDGE_PROCS = frozenset({"msedge.exe", "chrome.exe", "msedgewebview2.exe"})


def _looks_like_url(s: str) -> bool:
    low = s.strip().lower()
    return low.startswith("http://") or low.startswith("https://")


def resolve_window(window: Optional[str] = None,
                   hwnd: Optional[int] = None) -> Tuple[str, Any]:
    """→ ("native", hwnd) | ("cdp", url)。

    - window=None & hwnd=None → 前台窗口（确定性默认，无前台 → 400）；
    - http(s):// 开头 → cdp + URL；
    - 其余 → 可见顶层窗标题子串（0 命中 → 409 WINDOW_NOT_FOUND，
      多命中 → 409 VL_AMBIGUOUS——绝不随便挑一个）。
    """
    if hwnd:
        info = safety_gate.window_info(int(hwnd))
        if info is None:
            raise VLWindowNotFound("window hwnd " + str(int(hwnd)) + " is gone")
        return "native", int(hwnd)
    if not window:
        fg = window_locator.foreground_hwnd()
        if not fg:
            raise VLError("no foreground window and no window/hwnd given")
        return "native", int(fg)
    text = str(window).strip()
    if _looks_like_url(text):
        return "cdp", text
    hits = [w for w in window_locator.list_windows() if text in w["title"]]
    if not hits:
        raise VLWindowNotFound("no visible window title contains "
                               + repr(text))
    if len(hits) > 1:
        raise VLAmbiguous("window substring matches " + str(len(hits))
                          + " windows: "
                          + repr([h["title"] for h in hits][:5]))
    return "native", int(hits[0]["hwnd"])


def resolve_channel(window: Optional[str] = None,
                    hwnd: Optional[int] = None) -> str:
    """轻量通道判定（不做唯一性校验的快速版，供 click_element 预分流）。"""
    if hwnd:
        return "native"
    if not window:
        return "native"
    text = str(window).strip()
    if _looks_like_url(text):
        return "cdp"
    hits = [w for w in window_locator.list_windows() if text in w["title"]]
    if len(hits) == 1:
        info = safety_gate.window_info(hits[0]["hwnd"])
        proc = str((info or {}).get("process") or "").lower()
        if proc in _EDGE_PROCS:
            return "cdp"
    return "native"


# ---- 控件匹配（纯函数，mcp_server 也直接复用） ------------------------------


def find_control(controls: List[Dict[str, Any]], text: str,
                 match: str = "exact") -> Dict[str, Any]:
    """按文本定位控件。0 → 404；多命中 → 409（绝不猜，绝不随便取第一个）。"""
    want = str(text)
    mode = (match or "exact").strip().lower()
    if mode == "contains":
        hits = [c for c in controls if want in str(c.get("text") or "")]
    else:
        hits = [c for c in controls if str(c.get("text") or "") == want]
    if not hits:
        raise VLElementNotFound(
            "no control matches " + repr(want) + " (match=" + mode
            + ", scanned " + str(len(controls)) + " controls)")
    if len(hits) > 1:
        raise VLAmbiguous(
            repr(want) + " matches " + str(len(hits)) + " controls; "
            "disambiguate with window= or match=contains")
    return hits[0]


def _norm(text: str, kind: str, bbox: List[int],
          score: Optional[float] = None) -> Dict[str, Any]:
    x, y, w, h = (int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3]))
    w = max(1, w)
    h = max(1, h)
    item: Dict[str, Any] = {"text": str(text), "type": kind,
                            "bbox": [x, y, w, h],
                            "center": [x + w // 2, y + h // 2]}
    if score is not None:
        item["score"] = round(float(score), 4)
    return item


# ---- backend: native（win32 几何+类名+窗口文本） ---------------------------

_NATIVE_TYPES = {
    "button": ("button", "sysbutton", "tkbutton"),
    "edit": ("edit", "tkentry"),
    "static": ("text", "tkchild_static"),
    "sstabctl32": ("tab",),
    "toolbarwindow32": ("toolbar",),
    "menu": ("#32768",),
    "combobox": ("combobox", "tkcombo"),
}


def _native_kind(cls: str) -> str:
    low = cls.lower()
    for kind, names in _NATIVE_TYPES.items():
        if kind == "tkchild_static":
            continue
        if low in names:
            return {"button": "button", "edit": "input", "static": "text",
                    "sstabctl32": "tab", "toolbarwindow32": "toolbar",
                    "menu": "menu", "combobox": "input"}[kind]
    if low.startswith("tk"):
        return "tk-child"
    return "unknown"


def _analyze_native(hwnd: int) -> List[Dict[str, Any]]:
    rect = win32gui.GetWindowRect(hwnd)
    out: List[Dict[str, Any]] = []
    title = win32gui.GetWindowText(hwnd) or ""
    if title:
        out.append(_norm(title, "window",
                         [rect[0], rect[1], rect[2] - rect[0],
                          rect[3] - rect[1]]))
    rows: List[Tuple[int, str, str, tuple]] = []

    def _cb(ch: int, _acc: Any) -> None:
        if not win32gui.IsWindowVisible(ch):
            return
        r = win32gui.GetWindowRect(ch)
        if r[2] - r[0] <= 2 or r[3] - r[1] <= 2:
            return
        rows.append((ch, win32gui.GetClassName(ch),
                     win32gui.GetWindowText(ch) or "", r))
    try:
        win32gui.EnumChildWindows(hwnd, _cb, None)
    except Exception:  # noqa: BLE001  窗口在枚举中途消失等竞态
        pass
    for _ch, cls, text, r in rows:
        kind = _native_kind(cls)
        if not text and kind not in ("button", "input", "tab", "toolbar"):
            continue  # 无文本且非控件类（如 TkChild 面板）→ 不列
        out.append(_norm(text or (cls + " (unlabeled)"), kind,
                         [r[0], r[1], r[2] - r[0], r[3] - r[1]]))
    return out


# ---- backend: ocr（rapidocr 轻量级本地视觉模型，看像素取文本） -------------

_ocr_engine = None
_ocr_lock = __import__("threading").Lock()


def _get_ocr():
    global _ocr_engine
    with _ocr_lock:
        if _ocr_engine is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
            except ImportError as exc:
                raise VLBackendUnavailable(
                    "ocr backend unavailable: rapidocr-onnxruntime not "
                    "installed (" + str(exc) + ")") from exc
            try:
                _ocr_engine = RapidOCR()
            except Exception as exc:  # noqa: BLE001
                raise VLBackendUnavailable(
                    "ocr backend init failed: " + str(exc)) from exc
    return _ocr_engine


def _analyze_ocr(monitor: int, hwnd: Optional[int]) -> List[Dict[str, Any]]:
    from . import screen_mss
    img, origin = screen_mss.grab(monitor)   # 全尺度物理像素（单 capture 线程）
    offset = [int(origin["left"]), int(origin["top"])]
    crop_origin = [0, 0]
    target = img
    if hwnd:
        rect = win32gui.GetWindowRect(int(hwnd))
        left = max(0, rect[0] - offset[0])
        top = max(0, rect[1] - offset[1])
        right = min(img.width, rect[2] - offset[0])
        bottom = min(img.height, rect[3] - offset[1])
        if right - left < 4 or bottom - top < 4:
            raise VLWindowNotFound("window rect is off-screen/empty: "
                                   + repr(rect))
        crop_origin = [left, top]
        target = img.crop((left, top, right, bottom))
    engine = _get_ocr()
    try:
        result, _elapse = engine(target)
    except Exception as exc:  # noqa: BLE001
        raise VLError("ocr inference failed: " + type(exc).__name__
                      + ": " + str(exc)) from exc
    out: List[Dict[str, Any]] = []
    for item in result or []:
        # rapidocr 单条 = [box(4 点), text, score]
        box, text, score = item[0], str(item[1]), float(item[2])
        xs = [int(p[0]) for p in box]
        ys = [int(p[1]) for p in box]
        x = min(xs) + crop_origin[0] + offset[0]
        y = min(ys) + crop_origin[1] + offset[1]
        w = max(1, max(xs) - min(xs))
        h = max(1, max(ys) - min(ys))
        if not text.strip():
            continue
        out.append(_norm(text.strip(), "text", [x, y, w, h], score))
    return out


# ---- backend: llama（真实本地 VL 模型，llama-cpp-python） ------------------

_llama_model = None
_llama_lock = __import__("threading").Lock()

_LLAMA_SYSTEM = (
    "You are a GUI parser. List every visible UI control in the screenshot. "
    'Reply with ONLY a JSON array: [{"text": "...", "type": '
    '"button|input|text|menu|image|checkbox|link", "bbox": [x, y, w, h]}] '
    "where bbox is in pixels of the given image. No prose.")


def llama_available() -> bool:
    if not config.VL_MODEL_PATH or not os.path.isfile(config.VL_MODEL_PATH):
        return False
    try:
        import llama_cpp  # noqa: F401
    except ImportError:
        return False
    return True


def _get_llama():
    global _llama_model
    with _llama_lock:
        if _llama_model is None:
            try:
                from llama_cpp import Llama
            except ImportError as exc:
                raise VLBackendUnavailable(
                    "llama backend unavailable: llama-cpp-python not "
                    "installed") from exc
            try:
                _llama_model = Llama(
                    model_path=config.VL_MODEL_PATH,
                    # v0.8.1：多模态投影器（Qwen2-VL 等视觉模型必需）；
                    # 未配置时交给 llama.cpp 自动发现（模型同目录 mmproj-*.gguf）。
                    clip_model_path=config.VL_MMPROJ_PATH or None,
                    n_ctx=4096, n_threads=os.cpu_count() or 4,
                    n_gpu_layers=config.VL_N_GPU_LAYERS,
                    verbose=False)
            except Exception as exc:  # noqa: BLE001
                raise VLBackendUnavailable(
                    "llama model load failed: " + str(exc)) from exc
    return _llama_model


def _analyze_llama(monitor: int, hwnd: Optional[int]) -> List[Dict[str, Any]]:
    import base64
    from . import screen_mss
    img, origin = screen_mss.grab(monitor)
    offset = [int(origin["left"]), int(origin["top"])]
    crop_origin = [0, 0]
    target = img
    if hwnd:
        rect = win32gui.GetWindowRect(int(hwnd))
        left = max(0, rect[0] - offset[0])
        top = max(0, rect[1] - offset[1])
        right = min(img.width, rect[2] - offset[0])
        bottom = min(img.height, rect[3] - offset[1])
        if right - left < 4 or bottom - top < 4:
            raise VLWindowNotFound("window rect is off-screen/empty")
        crop_origin = [left, top]
        target = img.crop((left, top, right, bottom))
    buf = __import__("io").BytesIO()
    target.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    llm = _get_llama()
    try:
        resp = llm.create_chat_completion(
            messages=[{"role": "system", "content": _LLAMA_SYSTEM},
                      {"role": "user", "content": [
                          {"type": "image_url", "image_url": {
                              "url": "data:image/png;base64," + b64}},
                          {"type": "text",
                           "text": "Parse the UI controls as JSON array."}]}],
            temperature=0.0, max_tokens=1024)
        content = resp["choices"][0]["message"]["content"] or ""
    except Exception as exc:  # noqa: BLE001
        raise VLError("llama inference failed: " + type(exc).__name__
                      + ": " + str(exc)) from exc
    m = content.find("[")
    e = content.rfind("]")
    if m < 0 or e <= m:
        raise VLError("llama reply is not a JSON array: "
                      + repr(content[:200]))
    try:
        items = json.loads(content[m:e + 1])
    except ValueError as exc:
        raise VLError("llama reply JSON parse failed: "
                      + repr(content[:200])) from exc
    out: List[Dict[str, Any]] = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        bbox = it.get("bbox")
        if (not isinstance(bbox, (list, tuple)) or len(bbox) != 4):
            continue
        x = int(bbox[0]) + crop_origin[0] + offset[0]
        y = int(bbox[1]) + crop_origin[1] + offset[1]
        out.append(_norm(str(it.get("text") or ""),
                         str(it.get("type") or "unknown"),
                         [x, y, int(bbox[2]), int(bbox[3])]))
    return out


# ---- backend: mock（协议联调夹具） ------------------------------------------


def _analyze_mock() -> List[Dict[str, Any]]:
    raw = config.VL_MOCK_JSON.strip()
    if not raw:
        raise VLBackendUnavailable(
            "mock backend needs GHOST_VL_MOCK_JSON fixture")
    try:
        items = json.loads(raw)
    except ValueError as exc:
        raise VLError("GHOST_VL_MOCK_JSON is not valid JSON") from exc
    out = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict) and isinstance(it.get("bbox"), list):
            out.append(_norm(str(it.get("text") or ""),
                             str(it.get("type") or "text"),
                             it["bbox"], it.get("score")))
    return out


# ---- 统一入口 --------------------------------------------------------------


def pick_backend(explicit: Optional[str] = None) -> str:
    name = (explicit or config.VL_BACKEND or "auto").strip().lower()
    if name == "auto":
        if llama_available():
            return "llama"
        try:
            _get_ocr()
            return "ocr"
        except VLBackendUnavailable:
            return "native"
    return name


def analyze(monitor: int = 1, window: Optional[str] = None,
            hwnd: Optional[int] = None,
            backend: Optional[str] = None) -> Dict[str, Any]:
    """感知入口：解析目标 → 分派 backend → 统一契约输出。"""
    t0 = time.perf_counter()
    channel, target = resolve_window(window, hwnd)
    if channel == "cdp":
        raise VLError("analyze is native-only; target "
                      + repr(target) + " routes to the CDP channel "
                      "(use browser tools for web)")
    real_hwnd = int(target)
    info = safety_gate.window_info(real_hwnd)
    chosen = pick_backend(backend)
    if chosen == "native":
        controls = _analyze_native(real_hwnd)
    elif chosen == "ocr":
        controls = _analyze_ocr(monitor, real_hwnd)
    elif chosen == "llama":
        controls = _analyze_llama(monitor, real_hwnd)
    elif chosen == "mock":
        controls = _analyze_mock()
    else:
        raise VLError("unknown VL backend " + repr(chosen)
                      + " (valid: auto|native|ocr|llama|mock)")
    return {"ok": True, "backend": chosen,
            "window": {"hwnd": real_hwnd,
                       "title": (info or {}).get("title"),
                       "process": (info or {}).get("process")},
            "controls": controls,
            "count": len(controls),
            "elapsed_ms": int((time.perf_counter() - t0) * 1000)}
