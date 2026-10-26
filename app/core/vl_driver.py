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

_cuda_dirs_added = False
_gpu_offload: Optional[bool] = None


def _add_cuda_dll_dirs() -> None:
    """把 pip 的 NVIDIA CUDA 12.8 runtime/cuBLAS DLL 目录加入搜索路径。

    llama-cpp-python 的 CUDA wheel 只带 ggml-cuda.dll，cudart64_12/
    cublas64_12 由 nvidia-*-cu12 包提供；必须在首次 import llama_cpp 前
    注册，否则 CUDA 后端会静默不加载（gpu_offload=False）。
    """
    global _cuda_dirs_added
    if _cuda_dirs_added:
        return
    _cuda_dirs_added = True
    bases: List[str] = []
    try:
        import site
        bases.extend(site.getsitepackages())
        user = site.getusersitepackages()
        if user:
            bases.append(user)
    except Exception:  # noqa: BLE001
        pass
    bases.append(os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(__file__))), ".venv", "Lib", "site-packages"))
    import glob
    for base in bases:
        for d in glob.glob(os.path.join(base, "nvidia", "*", "bin")):
            if not os.path.isdir(d):
                continue
            try:
                os.add_dll_directory(d)
            except OSError:
                pass
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")

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
    "You are a GUI parser. Return ONLY a JSON object: "
    '{"controls":[{"text":"label or icon tool name","type":'
    '"button|input|text|icon|menu|link","bbox":[x1,y1,x2,y2]}]}. '
    "bbox is normalized to 0-1000 relative to the image: x1,y1 is the "
    "top-left corner, x2,y2 the bottom-right corner. Include every visible "
    "control, including icon-only toolbar buttons; put the icon's tool name "
    "(e.g. Rectangle, Pencil, Eraser) in text. Do not stop after the first "
    "control.")


def _target_system(target: str) -> str:
    """定向 grounding 提示词：只找一个控件，允许诚实返回 found=false。"""
    return (
        "You are a GUI visual grounding model. Locate the control whose "
        "visible label contains " + json.dumps(str(target), ensure_ascii=False)
        + ". Return ONLY JSON: {\"found\":true,\"text\":\"...\","
        "\"type\":\"button|input|text|icon\",\"bbox\":[x1,y1,x2,y2]} "
        "normalized 0-1000, or {\"found\":false} if no such control is "
        "visible. Do not guess; bbox must tightly enclose the control.")


def llama_available() -> bool:
    """真视觉后端可用 = 权重 + mmproj 都在 + llama-cpp-python 已装。"""
    if not config.VL_MODEL_PATH or not os.path.isfile(config.VL_MODEL_PATH):
        return False
    if not config.VL_MMPROJ_PATH or not os.path.isfile(
            config.VL_MMPROJ_PATH):
        return False
    _add_cuda_dll_dirs()
    try:
        import llama_cpp  # noqa: F401
    except ImportError:
        return False
    return True


def gpu_offload_supported() -> bool:
    """GPU offload 能力（带缓存）。

    0.4.5+ 的 CUDA ggml 后端在**首次构造 Llama 时**才注册，直接调
    llama_supports_gpu_offload() 会假阴性；这里用 vocab_only 探针把
    后端注册一次（不加载权重），再回读。
    """
    global _gpu_offload
    if _gpu_offload is not None:
        return _gpu_offload
    _add_cuda_dll_dirs()
    try:
        import llama_cpp
    except Exception:  # noqa: BLE001
        _gpu_offload = False
        return False
    try:
        if llama_cpp.llama_supports_gpu_offload():
            _gpu_offload = True
            return True
    except Exception:  # noqa: BLE001
        pass
    if not config.VL_MODEL_PATH or not os.path.isfile(config.VL_MODEL_PATH):
        _gpu_offload = False
        return False
    try:
        from llama_cpp import Llama
        probe = Llama(model_path=config.VL_MODEL_PATH, vocab_only=True,
                      n_ctx=64, n_gpu_layers=0, verbose=False)
        try:
            probe.close()
        except Exception:  # noqa: BLE001
            pass
        _gpu_offload = bool(llama_cpp.llama_supports_gpu_offload())
    except Exception:  # noqa: BLE001
        _gpu_offload = False
    return _gpu_offload


def llama_loaded() -> bool:
    return _llama_model is not None


def _get_llama():
    global _llama_model
    with _llama_lock:
        if _llama_model is None:
            _add_cuda_dll_dirs()
            try:
                from llama_cpp import Llama
                from llama_cpp.llama_chat_format import MTMDChatHandler
            except ImportError as exc:
                raise VLBackendUnavailable(
                    "llama backend unavailable: llama-cpp-python not "
                    "installed") from exc
            try:
                # v0.8.2：llama-cpp-python 0.3.36 无 clip_model_path 参数
                # （**kwargs 吞掉=视觉没挂），必须 MTMDChatHandler。
                # 0.4.5 参数改名为 mmproj_path，并支持 image_min_tokens。
                import inspect
                use_gpu = (gpu_offload_supported()
                           and config.VL_N_GPU_LAYERS != 0)
                params = inspect.signature(
                    MTMDChatHandler.__init__).parameters
                kwargs: Dict[str, Any] = {
                    "verbose": False, "use_gpu": use_gpu}
                if "mmproj_path" in params:
                    kwargs["mmproj_path"] = config.VL_MMPROJ_PATH
                else:
                    kwargs["clip_model_path"] = config.VL_MMPROJ_PATH
                if "image_min_tokens" in params:
                    kwargs["image_min_tokens"] = config.VL_IMAGE_MIN_TOKENS
                handler = MTMDChatHandler(**kwargs)
                _llama_model = Llama(
                    model_path=config.VL_MODEL_PATH,
                    chat_handler=handler,
                    n_ctx=4096,
                    n_threads=max(4, (os.cpu_count() or 8) // 2),
                    n_batch=256,
                    n_gpu_layers=config.VL_N_GPU_LAYERS,
                    verbose=False)
            except Exception as exc:  # noqa: BLE001
                raise VLBackendUnavailable(
                    "llama model load failed: " + type(exc).__name__ + ": "
                    + str(exc)) from exc
    return _llama_model


def _fit_for_vl(img, max_side: int):
    """等比缩放到最长边 max_side（小图最多放大 2x）；返回 (图, 缩放比)。"""
    from PIL import Image
    longest = max(img.width, img.height)
    if longest <= 0:
        return img, 1.0
    scale = min(max_side / float(longest), 2.0)
    if abs(scale - 1.0) < 0.01:
        return img, 1.0
    size = (max(1, round(img.width * scale)),
            max(1, round(img.height * scale)))
    resample = getattr(Image, "Resampling", Image).LANCZOS
    return img.resize(size, resample), scale


def _parse_llama_controls(content: str, img_w: int, img_h: int,
                          offset_x: int, offset_y: int,
                          target: Optional[str] = None,
                          scale: float = 1.0) -> List[Dict[str, Any]]:
    """解析模型 JSON 并把 bbox 映射回屏幕物理像素。

    img_w/img_h 是**未缩放**的窗口裁剪尺寸；归一化坐标 (0-1000) 直接乘
    原图尺寸即可（与缩放无关）。模型若给绝对像素（>1000），那是缩放图
    坐标，需除以 scale 再映射。兼容 {"controls":[...]}、单对象
    {"text","bbox"} 与裸数组。target 非空时执行合理性校验（标签包含目标
    + 面积/长宽比合理），拒绝“整图框/1px 框”这类模型幻觉——宁可 404，
    绝不乱点。
    """
    data: Any = None
    try:
        data = json.loads(content)
    except ValueError:
        m, e = content.find("{"), content.rfind("}")
        if m >= 0 and e > m:
            try:
                data = json.loads(content[m:e + 1])
            except ValueError:
                data = None
        if data is None:
            m, e = content.find("["), content.rfind("]")
            if m >= 0 and e > m:
                try:
                    data = json.loads(content[m:e + 1])
                except ValueError:
                    data = None
    if isinstance(data, dict):
        if data.get("found") is False:
            return []
        if isinstance(data.get("controls"), list):
            items = data["controls"]
        elif isinstance(data.get("elements"), list):
            items = data["elements"]
        elif "bbox" in data:
            items = [data]
        else:
            items = []
    elif isinstance(data, list):
        items = data
    else:
        items = []
    out: List[Dict[str, Any]] = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        bbox = it.get("bbox")
        if (not isinstance(bbox, (list, tuple)) or len(bbox) != 4):
            continue
        try:
            vals = [float(v) for v in bbox]
        except (TypeError, ValueError):
            continue
        if max(vals) <= 1000.0 and min(vals) >= 0.0:
            x1 = vals[0] / 1000.0 * img_w
            y1 = vals[1] / 1000.0 * img_h
            x2 = vals[2] / 1000.0 * img_w
            y2 = vals[3] / 1000.0 * img_h
        else:  # 模型给了缩放图的绝对像素 → 除回原图尺度
            inv = 1.0 / max(1e-9, float(scale))
            x1, y1, x2, y2 = [v * inv for v in vals]
        x1 = max(0.0, min(float(img_w), x1))
        y1 = max(0.0, min(float(img_h), y1))
        x2 = max(0.0, min(float(img_w), x2))
        y2 = max(0.0, min(float(img_h), y2))
        if x2 - x1 < 2 or y2 - y1 < 2:
            continue
        if target:
            label = str(it.get("text") or "")
            if str(target).lower() not in label.lower():
                continue
            area = ((x2 - x1) * (y2 - y1)) / float(max(1, img_w * img_h))
            if not (0.0001 <= area <= 0.35):
                continue
            aspect = (x2 - x1) / float(max(1.0, y2 - y1))
            if not (0.1 <= aspect <= 10.0):
                continue
        out.append(_norm(str(it.get("text") or ""),
                         str(it.get("type") or "unknown"),
                         [int(round(x1)) + offset_x,
                          int(round(y1)) + offset_y,
                          int(round(x2 - x1)), int(round(y2 - y1))]))
        if len(out) >= 200:
            break
    return out


def _grab_window(monitor: int, hwnd: Optional[int]):
    """抓屏并裁剪到目标窗口；返回 (crop_img, 屏幕物理原点[x,y])。"""
    from . import screen_mss
    img, origin = screen_mss.grab(monitor)
    offset = [int(origin["left"]), int(origin["top"])]
    crop_origin = [0, 0]
    crop_img = img
    if hwnd:
        rect = win32gui.GetWindowRect(int(hwnd))
        left = max(0, rect[0] - offset[0])
        top = max(0, rect[1] - offset[1])
        right = min(img.width, rect[2] - offset[0])
        bottom = min(img.height, rect[3] - offset[1])
        if right - left < 4 or bottom - top < 4:
            raise VLWindowNotFound("window rect is off-screen/empty")
        crop_origin = [left, top]
        crop_img = img.crop((left, top, right, bottom))
    return crop_img, [crop_origin[0] + offset[0],
                      crop_origin[1] + offset[1]]


def _ocr_label_match(crop_img, screen_origin: List[int],
                     control: Dict[str, Any], target: str
                     ) -> Tuple[bool, str]:
    """用本地 OCR 复核 VL 定向 bbox：裁剪框内必须真的出现目标文本。

    这是对 2B 定向 grounding 幻觉的安全闸：模型给一个“看起来合理”的
    bbox 但目标根本不存在时，OCR 读不到目标 → 拒绝，绝不盲点。
    """
    x, y, w, h = [int(v) for v in control["bbox"]]
    left = max(0, x - 4 - screen_origin[0])
    top = max(0, y - 4 - screen_origin[1])
    right = min(crop_img.width, x + w + 4 - screen_origin[0])
    bottom = min(crop_img.height, y + h + 4 - screen_origin[1])
    if right - left < 4 or bottom - top < 4:
        return False, ""
    crop = crop_img.crop((left, top, right, bottom)).convert("RGB")
    try:
        engine = _get_ocr()
        result, _elapse = engine(crop)
    except Exception:  # noqa: BLE001  引擎不可用→安全拒绝
        return False, ""
    seen = " ".join(str(item[1]) for item in (result or [])
                    if len(item) > 1)
    norm_seen = "".join(seen.split()).lower()
    norm_want = "".join(str(target).split()).lower()
    return bool(norm_want) and norm_want in norm_seen, seen


def _analyze_llama(monitor: int, hwnd: Optional[int],
                   target: Optional[str] = None) -> Tuple[
        List[Dict[str, Any]], Dict[str, Any]]:
    import base64
    import io
    crop_img, screen_origin = _grab_window(monitor, hwnd)
    # CPU 视觉编码按像素平方增长：先缩放再送模型，bbox 按比例映射回原图。
    fitted, scale = _fit_for_vl(crop_img, config.VL_MAX_SIDE)
    buf = io.BytesIO()
    fitted.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    llm = _get_llama()
    t0 = time.perf_counter()
    ttft: Optional[float] = None
    chunks: List[str] = []
    system = _target_system(target) if target else _LLAMA_SYSTEM
    user_text = ("Locate " + str(target)) if target \
        else "List all visible UI controls as JSON."
    # 2B 在 1024 分辨率下会试图列很多控件/生成超长 JSON（实测拖到分钟级）；
    # 2B 限 256 token，7B 留 768 给完整控件表。
    is_2b = "2b" in os.path.basename(config.VL_MODEL_PATH or "").lower()
    max_tokens = (256 if target else (256 if is_2b else 768))
    try:
        stream = llm.create_chat_completion(
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": [
                          {"type": "image_url", "image_url": {
                              "url": "data:image/png;base64," + b64}},
                          {"type": "text", "text": user_text}]}],
            temperature=0.0, max_tokens=max_tokens,
            response_format={"type": "json_object"}, stream=True)
        for chunk in stream:
            piece = ((chunk.get("choices") or [{}])[0].get("delta")
                     or {}).get("content")
            if piece:
                if ttft is None:
                    ttft = time.perf_counter() - t0
                chunks.append(piece)
    except Exception as exc:  # noqa: BLE001
        raise VLError("llama inference failed: " + type(exc).__name__
                      + ": " + str(exc)) from exc
    total = time.perf_counter() - t0
    content = "".join(chunks)
    controls = _parse_llama_controls(
        content, crop_img.width, crop_img.height,
        screen_origin[0], screen_origin[1],
        target=target, scale=scale)
    if target:
        verified = []
        for c in controls:
            ok, seen = _ocr_label_match(crop_img, screen_origin, c, target)
            if ok:
                c["ocr_text"] = seen[:80]
                verified.append(c)
        controls = verified
    timings = {
        "input_px": [fitted.width, fitted.height],
        "scale": round(scale, 4),
        "ttft_ms": int(ttft * 1000) if ttft is not None else None,
        "total_ms": int(total * 1000),
        "gen_ms": int((total - ttft) * 1000) if ttft is not None else None,
        "completion_chars": len(content),
        "raw_head": content[:200],
    }
    if target:
        timings["ocr_verified"] = len(controls)
    return controls, timings


# ---- 自由问答 describe（v0.9.1）：让 VLM "读"图而非只做 grounding --------
#
# 与 analyze（控件 JSON 契约）不同：describe 不解析控件、不做 bbox 映射，
# 就是把图喂给本地多模态模型 + 一个开放问题，原文回传描述。主仓 Iris
# 可以直接传 image_b64 问"这张图里画了什么"，无需先落盘。

_DESCRIBE_SYSTEM = (
    "You are a helpful vision assistant. Look at the image and answer the "
    "user's question faithfully based only on what is visible. Answer in "
    "the same language as the question. Be concise and concrete.")


def describe(monitor: int = 1, window: Optional[str] = None,
             hwnd: Optional[int] = None, backend: Optional[str] = None,
             question: str = "", image_b64: Optional[str] = None,
             max_tokens: int = 512) -> Dict[str, Any]:
    """开放问答：对截图/窗口/外部图片问一个自由问题，返回模型原文。

    - image_b64 给出 → 直接喂该图（主仓自己的截图/裁剪图，不抓屏）；
    - 否则按 window/hwnd 抓屏裁剪（语义与 analyze 一致，都缺省=前台）。
    - 只支持 llama 后端（真正的多模态语义理解）；ocr/native 只会读字，
      对"画了什么"没有语义 → 503 如实拒绝，绝不冒充。
    """
    q = str(question or "").strip()
    if not q:
        raise VLError("describe requires a non-empty question")
    if not image_b64 and (backend or config.VL_BACKEND or "auto") != "llama" \
            and pick_backend(backend) != "llama":
        raise VLBackendUnavailable(
            "describe requires the llama backend (GHOST_VL_MODEL_PATH); "
            "current backend would be " + repr(pick_backend(backend))
            + " which cannot answer open questions about images")
    text, timings = _describe_llama(monitor, hwnd, q, image_b64, max_tokens)
    return {"ok": True, "backend": "llama", "question": q,
            "description": text, "timings": timings}


def _describe_llama(monitor: int, hwnd: Optional[int], question: str,
                    image_b64: Optional[str], max_tokens: int
                    ) -> Tuple[str, Dict[str, Any]]:
    import base64
    import io
    if image_b64:
        from PIL import Image
        try:
            crop_img = Image.open(io.BytesIO(base64.b64decode(image_b64)))
            crop_img = crop_img.convert("RGB")
        except Exception as exc:  # noqa: BLE001
            raise VLError("image_b64 is not a decodable image: "
                          + type(exc).__name__ + ": " + str(exc)) from exc
    else:
        crop_img, _origin = _grab_window(monitor, hwnd)
    fitted, scale = _fit_for_vl(crop_img, config.VL_MAX_SIDE)
    buf = io.BytesIO()
    fitted.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    llm = _get_llama()
    t0 = time.perf_counter()
    ttft: Optional[float] = None
    chunks: List[str] = []
    try:
        stream = llm.create_chat_completion(
            messages=[{"role": "system", "content": _DESCRIBE_SYSTEM},
                      {"role": "user", "content": [
                          {"type": "image_url", "image_url": {
                              "url": "data:image/png;base64," + b64}},
                          {"type": "text", "text": question}]}],
            temperature=0.0, max_tokens=max(32, int(max_tokens)),
            stream=True)
        for chunk in stream:
            piece = ((chunk.get("choices") or [{}])[0].get("delta")
                     or {}).get("content")
            if piece:
                if ttft is None:
                    ttft = time.perf_counter() - t0
                chunks.append(piece)
    except Exception as exc:  # noqa: BLE001
        raise VLError("llama describe failed: " + type(exc).__name__
                      + ": " + str(exc)) from exc
    total = time.perf_counter() - t0
    content = "".join(chunks).strip()
    timings = {
        "input_px": [fitted.width, fitted.height],
        "scale": round(scale, 4),
        "ttft_ms": int(ttft * 1000) if ttft is not None else None,
        "total_ms": int(total * 1000),
        "gen_ms": int((total - ttft) * 1000) if ttft is not None else None,
        "completion_chars": len(content),
    }
    return content, timings


def hybrid_enabled() -> bool:
    """auto=仅 7b 档启用；1/true=强制；0/false=关闭。"""
    mode = (config.VL_HYBRID or "auto").strip().lower()
    if mode in ("1", "true", "yes", "on"):
        return True
    if mode in ("0", "false", "no", "off"):
        return False
    model = os.path.basename(config.VL_MODEL_PATH or "").lower()
    return "7b" in model


def _icon_grid_system(target: str) -> str:
    return (
        "You are a GUI icon classifier. The image is a numbered grid of "
        "icon candidates extracted from a toolbar. Each tile has an orange "
        "header with its number. Which numbered tile shows the "
        + json.dumps(str(target), ensure_ascii=False)
        + " tool/icon? Reply ONLY JSON: {\"index\": N, "
        "\"confidence\": 0.0-1.0} or {\"found\": false}. The number is the "
        "orange header label. Do not guess.")


def _analyze_llama_hybrid(monitor: int, hwnd: Optional[int],
                          target: str) -> Tuple[
        List[Dict[str, Any]], Dict[str, Any]]:
    """CV 粗筛小图标 → 编号 contact sheet → VL 只做“几号是目标”。"""
    import base64
    import io
    import statistics
    from . import icon_grid
    crop_img, screen_origin = _grab_window(monitor, hwnd)
    candidates = icon_grid.detect_icon_candidates(crop_img,
                                                  max_candidates=64)
    if not candidates:
        raise VLElementNotFound(
            "hybrid: no icon candidates found in window")
    # 编号按屏幕空间行优先排列（更像人看到的网格；CV 分数保留在 score）。
    band = max(12, int(statistics.median(
        [c["bbox"][3] for c in candidates]) * 0.6))
    candidates.sort(key=lambda c: (c["bbox"][1] // band, c["bbox"][0]))
    sheet, used = icon_grid.build_contact_sheet(crop_img, candidates)
    fitted, scale = _fit_for_vl(sheet, config.VL_MAX_SIDE)
    buf = io.BytesIO()
    fitted.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    llm = _get_llama()
    t0 = time.perf_counter()
    ttft: Optional[float] = None
    chunks: List[str] = []
    try:
        stream = llm.create_chat_completion(
            messages=[{"role": "system",
                       "content": _icon_grid_system(target)},
                      {"role": "user", "content": [
                          {"type": "image_url", "image_url": {
                              "url": "data:image/png;base64," + b64}},
                          {"type": "text",
                           "text": "Which number shows " + str(target)
                           + "?"}]}],
            temperature=0.0, max_tokens=64,
            response_format={"type": "json_object"}, stream=True)
        for chunk in stream:
            piece = ((chunk.get("choices") or [{}])[0].get("delta")
                     or {}).get("content")
            if piece:
                if ttft is None:
                    ttft = time.perf_counter() - t0
                chunks.append(piece)
    except Exception as exc:  # noqa: BLE001
        raise VLError("hybrid inference failed: " + type(exc).__name__
                      + ": " + str(exc)) from exc
    total = time.perf_counter() - t0
    content = "".join(chunks)
    try:
        data = json.loads(content)
    except ValueError as exc:
        raise VLError("hybrid reply is not JSON: "
                      + repr(content[:200])) from exc
    if not isinstance(data, dict) or data.get("found") is False:
        raise VLElementNotFound(
            "hybrid: model found no tile matching " + repr(target))
    try:
        idx = int(data.get("index"))
    except (TypeError, ValueError) as exc:
        raise VLElementNotFound(
            "hybrid: model returned no valid index: "
            + repr(content[:200])) from exc
    if idx < 1 or idx > len(used):
        raise VLElementNotFound(
            "hybrid: index " + str(idx) + " out of range 1.."
            + str(len(used)) + " (raw=" + repr(content[:160]) + ")")
    try:
        conf = float(data.get("confidence"))
    except (TypeError, ValueError):
        conf = 0.0
    if conf < config.VL_HYBRID_MIN_CONF:
        raise VLElementNotFound(
            "hybrid: low confidence " + str(conf) + " < "
            + str(config.VL_HYBRID_MIN_CONF)
            + " (index " + str(idx) + ", raw=" + repr(content[:160])
            + ")")
    c = used[idx - 1]
    x, y, w, h = [int(v) for v in c["bbox"]]
    control = _norm(str(target), "icon",
                    [x + screen_origin[0], y + screen_origin[1], w, h],
                    conf)
    control["source"] = "cv_vl_grid"
    control["icon_index"] = idx
    control["icon_total"] = len(used)
    control["cv_score"] = c.get("score")
    timings = {
        "input_px": [fitted.width, fitted.height],
        "scale": round(scale, 4),
        "ttft_ms": int(ttft * 1000) if ttft is not None else None,
        "total_ms": int(total * 1000),
        "gen_ms": int((total - ttft) * 1000) if ttft is not None else None,
        "completion_chars": len(content),
        "raw_head": content[:200],
        "hybrid": {
            "candidates": len(used),
            "index": idx,
            "confidence": conf,
            "sheet_px": [sheet.width, sheet.height],
        },
    }
    return [control], timings


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


def describe_backend() -> str:
    """快速回显当前会选中的后端，不加载 OCR/模型（供 /health 使用）。"""
    name = (config.VL_BACKEND or "auto").strip().lower()
    if name == "auto":
        if llama_available():
            return "llama"
        try:
            import rapidocr_onnxruntime  # noqa: F401
            return "ocr"
        except ImportError:
            return "native"
    return name


def analyze(monitor: int = 1, window: Optional[str] = None,
            hwnd: Optional[int] = None,
            backend: Optional[str] = None,
            target: Optional[str] = None,
            hybrid: bool = False) -> Dict[str, Any]:
    """感知入口：解析目标 → 分派 backend → 统一契约输出。

    target 非空 = 定向 grounding（只找该标签，含合理性校验）；用于
    click_element 在通用控件表未命中时的二次定位。
    hybrid=True 且 backend=llama：CV 粗筛图标 + 编号网格 + VL 选号
    （仅建议 7B；2B 的形状语义不可靠，默认 auto 只对 7b 档启用）。
    """
    t0 = time.perf_counter()
    channel, resolved = resolve_window(window, hwnd)
    if channel == "cdp":
        raise VLError("analyze is native-only; target "
                      + repr(resolved) + " routes to the CDP channel "
                      "(use browser tools for web)")
    real_hwnd = int(resolved)
    info = safety_gate.window_info(real_hwnd)
    chosen = pick_backend(backend)
    timings = None
    if chosen == "native":
        controls = _analyze_native(real_hwnd)
    elif chosen == "ocr":
        controls = _analyze_ocr(monitor, real_hwnd)
    elif chosen == "llama":
        if hybrid and target:
            controls, timings = _analyze_llama_hybrid(monitor, real_hwnd,
                                                      target)
        else:
            controls, timings = _analyze_llama(monitor, real_hwnd,
                                               target=target)
    elif chosen == "mock":
        controls = _analyze_mock()
    else:
        raise VLError("unknown VL backend " + repr(chosen)
                      + " (valid: auto|native|ocr|llama|mock)")
    auto_mode = (backend or config.VL_BACKEND or "auto").strip().lower() \
        in ("", "auto")
    fallback_from = None
    if (auto_mode and not target and not hybrid and chosen == "llama"
            and len(controls) <= 1):
        # 2B 在 1024 下常只回 1 个控件（实测漏按钮）；auto 用 OCR 补全
        # 控件表。backend 仍是 llama，timings 标注 ocr_merged，绝不冒充。
        try:
            ocr_controls = _analyze_ocr(monitor, real_hwnd)
        except VLBackendUnavailable:
            ocr_controls = []
        if ocr_controls:
            controls = controls + ocr_controls
            if timings is not None:
                timings["ocr_merged"] = len(ocr_controls)
    if not controls and auto_mode and not target and not hybrid:
        # auto 模式的“诚实降级”：llama 通用解析为空 → 立刻用 OCR 读像素
        # 文本；后端字段回显真实出处，绝不冒充 llama。
        if chosen in ("llama", "ocr"):
            try:
                controls = _analyze_ocr(monitor, real_hwnd)
                fallback_from = chosen
                chosen = "ocr"
            except VLBackendUnavailable:
                controls = []
        if not controls and chosen != "native":
            controls = _analyze_native(real_hwnd)
            if not fallback_from:
                fallback_from = "llama" if timings else chosen
            chosen = "native"
    if target and not controls:
        raise VLElementNotFound(
            "targeted grounding found no visible control matching "
            + repr(target) + " in window " + repr((info or {}).get("title")),
            backend=chosen)
    result = {"ok": True, "backend": chosen,
              "window": {"hwnd": real_hwnd,
                         "title": (info or {}).get("title"),
                         "process": (info or {}).get("process")},
              "controls": controls,
              "count": len(controls),
              "elapsed_ms": int((time.perf_counter() - t0) * 1000)}
    if timings is not None:
        if fallback_from:
            timings["fallback_from"] = fallback_from
        result["timings"] = timings
    return result
