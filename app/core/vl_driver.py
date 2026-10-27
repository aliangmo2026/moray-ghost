"""本地多模态感知驱动（v0.12 · MCP 底座的眼睛 + GUI RAG 做选择题）。

统一输出契约（坐标一律物理像素、全屏坐标系）：
    {"ok": true, "backend": "...", "window": {...},
     "controls": [{"text", "type", "bbox": [x, y, w, h], "center": [x, y]}],
     "elapsed_ms": ...}

v0.12 GUI RAG（extract_ui_elements / ground_target）：
- 检索层把客户区截图经 OCR 文本 + OpenCV 矩形 + win32 原生几何合并成
  带 id 的元素表；bbox 只来自像素/句柄，模型不生成坐标。
- 选择层把元素目录（必要时附编号截图）交给 VLM，只解析 {"target_id": N}，
  再由服务端映射回真实 bbox；文本精确命中走零模型快路径。
- 服务端 /vl/elements 与 /vl/ground 暴露给 MCP click_element 与主仓。

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

v0.15 System 1 微型反射脑（system1_reflex）：任务在挂起/降级边界先本地
"看一眼"——OCR 规则（safety_gate.system1_match）命中「弹窗特征词 + 关闭
按钮 同窗共现」就走 controller 安全管道点掉，任务继续；未命中/被拒/复检
未过则完全回退 v0.14 旧行为（挂起等主仓）。本层只负责编排，判定规则在
safety_gate，点击在 controller（唯一动桌面咽喉），绝不绕过任何门禁。
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import win32con
import win32gui

from . import config, safety_gate, window_locator

LOG = logging.getLogger("moray-ghost.vl_driver")

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


# ---- GUI RAG（v0.12.0）：OCR+CV 检索编号元素表 → VLM 只回 target_id -------
#
# 设计动机：让 VLM 直接回归一化坐标（0-1000）在 2B 档有坐标幻觉/精度瓶颈。
# 改为两段式：
#   1) 检索层（零模型）：客户端区截图 → OCR 文本 + OpenCV 矩形 + win32 原生
#      几何三方合并，产出带 id 的元素表；bbox 全部来自像素/句柄，不来自模型。
#   2) 选择层（VLM）：把元素表（+可选编号截图）交给模型，只要求
#      {"target_id": N}——模型永远不产生坐标，选错也不会"点到空白处"。
# 文本目标默认走纯文本目录（2B 实测 ~0.4s/次，无需视觉编码）；图标/无文本
# 候选才自动带编号截图。所有输出坐标仍是屏幕物理像素。


def _rect_iou(a: List[int], b: List[int]) -> float:
    ax, ay, aw, ah = [float(v) for v in a]
    bx, by, bw, bh = [float(v) for v in b]
    x1, y1 = max(ax, bx), max(ay, by)
    x2, y2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if x2 <= x1 or y2 <= y1:
        return 0.0
    inter = (x2 - x1) * (y2 - y1)
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _rect_center_in(rect: List[int], point: Tuple[float, float],
                    pad: float = 2.0) -> bool:
    x, y, w, h = [float(v) for v in rect]
    return (x - pad <= point[0] <= x + w + pad
            and y - pad <= point[1] <= y + h + pad)


def _rect_center(rect: List[int]) -> Tuple[float, float]:
    x, y, w, h = [float(v) for v in rect]
    return x + w / 2.0, y + h / 2.0


def _bbox_in(inner: List[int], outer: List[int], pad: int = 3) -> bool:
    ix, iy, iw, ih = inner
    ox, oy, ow, oh = outer
    return (ix >= ox - pad and iy >= oy - pad
            and ix + iw <= ox + ow + pad and iy + ih <= oy + oh + pad)


def _norm_key(value: Any) -> str:
    return "".join(ch for ch in str(value or "").lower()
                   if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")


def _grab_view(monitor: int, hwnd: Optional[int]):
    """抓屏并裁剪到窗口**客户区**（排除标题栏/边框）。

    返回 (crop_img, screen_origin[x,y], view)。客户区裁剪让 OCR 不会读到
    标题栏按钮（口/×），并且 bbox 与 win32 子件坐标天然同一坐标系。
    """
    from . import screen_mss
    img, origin = screen_mss.grab(monitor)
    off_x, off_y = int(origin["left"]), int(origin["top"])
    if not hwnd:
        return img, [off_x, off_y], {
            "window_rect": None,
            "client_rect": [off_x, off_y, img.width, img.height],
            "crop_origin_screen": [off_x, off_y],
            "crop_size": [img.width, img.height],
        }
    h = int(hwnd)
    try:
        window_rect = list(win32gui.GetWindowRect(h))
        cx, cy = win32gui.ClientToScreen(h, (0, 0))
        _, _, cw, ch = win32gui.GetClientRect(h)
    except Exception as exc:  # noqa: BLE001
        raise VLWindowNotFound("window " + str(h) + " is gone: "
                               + type(exc).__name__) from exc
    if cw <= 8 or ch <= 8:
        raise VLWindowNotFound("window client area is empty: " + repr(
            [cx, cy, cw, ch]))
    left = max(0, cx - off_x)
    top = max(0, cy - off_y)
    right = min(img.width, cx + cw - off_x)
    bottom = min(img.height, cy + ch - off_y)
    if right - left < 8 or bottom - top < 8:
        raise VLWindowNotFound("window client area is off-screen/empty: "
                               + repr([cx, cy, cw, ch]))
    crop = img.crop((left, top, right, bottom))
    screen_origin = [left + off_x, top + off_y]
    return crop, screen_origin, {
        "window_rect": window_rect,
        "client_rect": [int(cx), int(cy), int(cw), int(ch)],
        "crop_origin_screen": screen_origin,
        "crop_size": [crop.width, crop.height],
    }


def _has_visible_child(hwnd: int) -> bool:
    found = {"v": False}

    def _cb(ch: int, _acc: Any) -> None:
        if not win32gui.IsWindowVisible(ch):
            return
        r = win32gui.GetWindowRect(ch)
        if r[2] - r[0] > 2 and r[3] - r[1] > 2:
            found["v"] = True

    try:
        win32gui.EnumChildWindows(hwnd, _cb, None)
    except Exception:  # noqa: BLE001
        pass
    return found["v"]


def _native_seeds(hwnd: int, view: Dict[str, Any]) -> List[Dict[str, Any]]:
    """win32 子件 → 元素种子（角色/几何/原生文本）。

    关键本机实证（Tk）：Button/Entry 的 Win32 text 恒为空，但 class 与 rect
    准确——所以原生层只提供**角色先验与几何兜底**，标签文本由 OCR 合并。
    TkChild 需要尺寸+无可见子件双重过滤，否则窗体容器会被误当成输入框。
    """
    client = view.get("client_rect") or [0, 0, 10 ** 6, 10 ** 6]
    cx, cy, cw, ch = [int(v) for v in client]
    rows: List[Tuple[int, str, str, tuple]] = []

    def _cb(child: int, _acc: Any) -> None:
        if not win32gui.IsWindowVisible(child):
            return
        r = win32gui.GetWindowRect(child)
        if r[2] - r[0] <= 3 or r[3] - r[1] <= 3:
            return
        if r[2] <= cx or r[0] >= cx + cw or r[3] <= cy or r[1] >= cy + ch:
            return  # 完全在客户区外（被裁掉/最小化子件）
        rows.append((child, win32gui.GetClassName(child),
                     win32gui.GetWindowText(child) or "", r))

    try:
        win32gui.EnumChildWindows(hwnd, _cb, None)
    except Exception:  # noqa: BLE001  窗口枚举中途消失
        pass

    out: List[Dict[str, Any]] = []
    for child, cls, text, r in rows:
        low = cls.lower()
        w, h = r[2] - r[0], r[3] - r[1]
        role: Optional[str] = None
        if low in ("button", "tkbutton"):
            # 小尺寸 Button + 无居中大标签 → Tk Checkbutton（本机实测 91x41）
            role = "checkbox" if (h <= 48 and w <= 150) else "button"
        elif low in ("edit", "tkentry", "combobox", "tkcombo"):
            role = "input"
        elif low in ("static", "text"):
            role = "text" if text.strip() else None
        elif low.startswith("tk"):
            # Tk 的输入框是 TkChild（无子件、横向长条）；容器/画布高度更大。
            if (60 <= w <= max(80, int(cw * 0.92))
                    and 18 <= h <= 80
                    and not _has_visible_child(child)):
                role = "input"
        if role is None:
            continue
        if role == "text" and not text.strip():
            continue
        out.append({"bbox": [int(r[0]), int(r[1]), int(w), int(h)],
                    "role": role, "text": text.strip(),
                    "class": cls, "hwnd": int(child)})
    return out


def _detect_cv_boxes(crop_img) -> List[Dict[str, Any]]:
    """OpenCV 边缘矩形 → 按钮/输入框候选（crop 坐标）。

    本机 Tk 实测：Canny(40,120)+闭运算+approxPolyDP 对按钮/输入框边框的
    bbox 与 Tk 自报几何误差 ≤1px；fill_ratio≥0.8 可滤掉文字笔画轮廓。
    """
    import cv2
    import numpy as np
    rgb = np.array(crop_img.convert("RGB"))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 40, 120)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=2)
    try:
        contours, _ = cv2.findContours(edges, cv2.RETR_LIST,
                                       cv2.CHAIN_APPROX_SIMPLE)
    except cv2.error:
        return []
    img_w, img_h = crop_img.width, crop_img.height
    raw: List[Tuple[int, int, int, int, float]] = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w < 24 or h < 24:
            continue
        if w > 0.85 * img_w or h > 140 or h > 0.5 * img_h:
            continue
        if w * h > 0.25 * img_w * img_h:
            continue
        aspect = w / float(max(1, h))
        if aspect < 1.0 or aspect > 16.0:
            continue
        peri = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, 0.02 * peri, True)
        if not (4 <= len(approx) <= 12):
            continue
        area = cv2.contourArea(contour)
        if area < 250:
            continue
        fill = area / float(max(1, w * h))
        if fill < 0.85:
            continue
        raw.append((x, y, w, h, round(float(fill), 3)))
    # 去掉被更大矩形包住的内部轮廓（文字/图标），同尺寸去重
    raw.sort(key=lambda b: b[2] * b[3], reverse=True)
    kept: List[Dict[str, Any]] = []
    for x, y, w, h, fill in raw:
        rect = [x, y, w, h]
        if any(_rect_iou(rect, k["bbox"]) > 0.72 for k in kept):
            continue
        if any(_bbox_in(rect, k["bbox"], pad=2) for k in kept):
            continue
        kept.append({"bbox": rect, "fill": fill})
        if len(kept) >= 80:
            break
    return kept


_OCR_GLYPHS = frozenset("×✕✖□口◻◼◾▪▫●○◎—–-_|/\\·•")


def _split_label_value(text: str) -> Tuple[str, str]:
    """把 OCR 连读的「标签+值」拆开（用户名moray_user → 用户名 / moray_user）。

    只在输入框 OCR 行左右跨界时使用。ASCII 标签内部的空格（Email Address）
    不会被拆；CJK→ASCII 边界与 CJK 后单空格才是可靠的分界。
    """
    s = str(text or "").strip()
    if not s:
        return "", ""
    m = re.search(r"\s{2,}", s)
    if m:
        left, right = s[:m.start()].strip(), s[m.end():].strip()
        if left and right:
            return left, right
    for i in range(1, len(s)):
        a, b = s[i - 1], s[i]
        a_cjk = "\u4e00" <= a <= "\u9fff"
        b_cjk = "\u4e00" <= b <= "\u9fff"
        if a_cjk and not b_cjk and (b.isalnum() or b in "_@.-"):
            left, right = s[:i].strip(), s[i:].strip()
            if left and right:
                return left, right
        if (not a_cjk and (a.isalnum() or a in "_@.-")) and b_cjk:
            left, right = s[:i].strip(), s[i:].strip()
            if left and right:
                return left, right
    if " " in s:
        left, right = s.split(" ", 1)
        if left and right and all("\u4e00" <= ch <= "\u9fff" for ch in left):
            return left.strip(), right.strip()
    return s, ""


def _ocr_read(crop_img, min_score: float = 0.30) -> List[Dict[str, Any]]:
    """OCR → [{text,bbox(crop),score}]；过滤标题栏残留与窗口控制字形。"""
    engine = _get_ocr()
    try:
        result, _elapse = engine(crop_img)
    except Exception as exc:  # noqa: BLE001
        raise VLError("ocr inference failed: " + type(exc).__name__
                      + ": " + str(exc)) from exc
    out: List[Dict[str, Any]] = []
    for item in result or []:
        try:
            box, text, score = item[0], str(item[1]).strip(), float(item[2])
        except (IndexError, TypeError, ValueError):
            continue
        if not text or score < min_score:
            continue
        if len(text) == 1 and text in _OCR_GLYPHS:
            continue
        try:
            xs = [int(p[0]) for p in box]
            ys = [int(p[1]) for p in box]
        except (TypeError, ValueError, IndexError):
            continue
        x, y = min(xs), min(ys)
        w, h = max(1, max(xs) - x), max(1, max(ys) - y)
        out.append({"text": text, "bbox": [x, y, w, h],
                    "score": round(score, 4)})
    return out


def _ocr_text_inside(ocr_items: List[Dict[str, Any]],
                     box: List[int]) -> List[int]:
    return [i for i, item in enumerate(ocr_items)
            if _rect_center_in(box, _rect_center(item["bbox"]))]


def ocr_control_at(img, origin, x: int, y: int,
                   half_w: int = 220, half_h: int = 90
                   ) -> Tuple[List[str], Dict[str, Any]]:
    """点击落点局部 OCR（Ghost Guard 用）：CV 找到包含落点的控件框，只 OCR 框内。

    直接对 520x180 大区域 OCR 会把相邻按钮标签一起读进来（实测「取消」旁边
    就是「删除账户」），造成误杀。这里先用 OpenCV 在局部找矩形，取**包含落点
    的最小矩形**再 OCR——按钮/输入框的边框是可靠边界；无边框控件回退 180x72
    紧裁剪。返回 (texts, meta)，meta 含实际 OCR 区域（屏幕坐标）供审计。
    """
    img_w, img_h = img.size
    ox, oy = int(origin[0]), int(origin[1])
    left = max(0, int(x) - half_w - ox)
    top = max(0, int(y) - half_h - oy)
    right = min(img_w, int(x) + half_w - ox)
    bottom = min(img_h, int(y) + half_h - oy)
    if right - left < 8 or bottom - top < 8:
        raise VLError("click point " + repr([int(x), int(y)])
                      + " is outside the captured screen")
    search = img.crop((left, top, right, bottom))
    px, py = int(x) - ox - left, int(y) - oy - top
    try:
        boxes = _detect_cv_boxes(search)
    except Exception:  # noqa: BLE001  CV 失败→回退紧裁剪
        boxes = []
    containing = []
    for cb in boxes:
        bx, by, bw, bh = cb["bbox"]
        if bx - 3 <= px <= bx + bw + 3 and by - 3 <= py <= by + bh + 3:
            containing.append(cb)
    if containing:
        cb = min(containing,
                 key=lambda c: int(c["bbox"][2]) * int(c["bbox"][3]))
        bx, by, bw, bh = cb["bbox"]
        pad = 3
        box = (max(0, bx - pad), max(0, by - pad),
               min(search.width, bx + bw + pad),
               min(search.height, by + bh + pad))
        source = "cv_box"
    else:
        fw, fh = 180, 72
        box = (max(0, px - fw // 2), max(0, py - fh // 2),
               min(search.width, px + fw // 2),
               min(search.height, py + fh // 2))
        source = "fallback"
    if box[2] - box[0] < 4 or box[3] - box[1] < 4:
        return [], {"source": source, "box_screen": None, "texts": []}
    sub = search.crop(box)
    items = _ocr_read(sub)
    texts = [item["text"] for item in items]
    meta = {
        "source": source,
        "box_screen": [box[0] + left + ox, box[1] + top + oy,
                       box[2] - box[0], box[3] - box[1]],
        "texts": texts[:20],
    }
    return texts, meta


def _classify_cv_box(box: List[int],
                     ocr_items: List[Dict[str, Any]]) -> str:
    """CV 矩形 → button / input / checkbox（文本对齐 + 长宽比启发式）。"""
    x, y, w, h = box
    aspect = w / float(max(1, h))
    inside = [ocr_items[i]["bbox"] for i in _ocr_text_inside(ocr_items, box)]
    centered = False
    if inside:
        tx1 = min(b[0] for b in inside)
        tx2 = max(b[0] + b[2] for b in inside)
        tcx = (tx1 + tx2) / 2.0
        centered = abs(tcx - (x + w / 2.0)) <= max(10.0, w * 0.16)
    if h <= 48 and w <= 160 and inside and not centered and aspect < 4.0:
        return "checkbox"
    if aspect >= 3.2 and h <= 110:
        if not inside or not centered:
            return "input"
    return "button"


def _merge_element_text(seed: Dict[str, Any],
                        ocr_items: List[Dict[str, Any]],
                        consumed: set) -> Dict[str, Any]:
    """把 OCR 文本贴到元素种子上：输入框取左侧标签 + 框内值，按钮取框内标签。"""
    bbox = seed["bbox"]
    bx, by, bw, bh = bbox
    inside = [i for i in _ocr_text_inside(ocr_items, bbox)
              if i not in consumed]
    role = seed.get("role")
    text = ""
    value = ""
    label = ""
    if role == "input":
        # 标签：与输入框同一水平带、右边缘在框左侧 260px 内的最近 OCR 行
        label_idx: Optional[int] = None
        best_right = -10 ** 9
        for i, item in enumerate(ocr_items):
            if i in consumed:
                continue
            ix, iy, iw, ih = item["bbox"]
            iy_c = iy + ih / 2.0
            if not (by - 8 <= iy_c <= by + bh + 8):
                continue
            right = ix + iw
            if right <= bx + 8 and right >= bx - 260 and right > best_right:
                label_idx, best_right = i, right
        # OCR 把「标签 + 框内值」读成一行：以框左边界为界，整行作为元素名
        merged = [i for i in inside
                  if ocr_items[i]["bbox"][0] < bx - 4
                  and ocr_items[i]["bbox"][0] + ocr_items[i]["bbox"][2] > bx + 8]
        if merged:
            i = merged[0]
            whole = ocr_items[i]["text"]
            item_bbox = ocr_items[i]["bbox"]
            split_label, split_value = _split_label_value(whole)
            if not split_value and item_bbox[2] > 0 \
                    and all("\u4e00" <= ch <= "\u9fff" for ch in whole):
                # 纯中文「标签+值」连读（备注像素级自动化）：按输入框左边界
                # 占整行像素比例估切分点（CJK 字宽近似均匀，实测准确）。
                label_px = max(0, bx - item_bbox[0])
                k = int(round(len(whole) * label_px
                              / float(max(1, item_bbox[2]))))
                if 1 <= k < len(whole):
                    split_label, split_value = whole[:k], whole[k:]
            text = split_label or whole
            label = text
            value = split_value
            consumed.add(i)
            for j in inside:
                consumed.add(j)
        elif label_idx is not None:
            label = ocr_items[label_idx]["text"]
            text = label
            value = " ".join(ocr_items[j]["text"] for j in inside
                             if j != label_idx).strip()
            consumed.add(label_idx)
            consumed.update(inside)
        else:
            value = " ".join(ocr_items[j]["text"] for j in inside).strip()
            text = value
            consumed.update(inside)
    elif role in ("button", "checkbox", "text", "icon"):
        text = " ".join(ocr_items[j]["text"] for j in inside).strip()
        consumed.update(inside)
        if not text:
            text = str(seed.get("text") or "").strip()
        label = text
    out = dict(seed)
    out.update({"text": text, "label": label, "value": value})
    return out


def _assemble_elements(crop_img, screen_origin: List[int],
                       view: Dict[str, Any],
                       native: List[Dict[str, Any]],
                       cv_boxes: List[Dict[str, Any]],
                       ocr_items: List[Dict[str, Any]],
                       include_icons: bool = False,
                       max_elements: int = 200) -> List[Dict[str, Any]]:
    origin_x, origin_y = int(screen_origin[0]), int(screen_origin[1])
    crop_w, crop_h = crop_img.width, crop_img.height

    def _to_crop(bbox: List[int]) -> List[int]:
        return [int(bbox[0]) - origin_x, int(bbox[1]) - origin_y,
                int(bbox[2]), int(bbox[3])]

    # 1) 原生几何种子 + CV 精修 bbox（CV 的边框 bbox 比 TkChild 更贴边）
    seeds: List[Dict[str, Any]] = []
    used_cv: set = set()
    for n in native:
        nb = _to_crop(n["bbox"])
        if nb[0] + nb[2] <= 0 or nb[1] + nb[3] <= 0 \
                or nb[0] >= crop_w or nb[1] >= crop_h:
            continue
        best_i, best_iou = -1, 0.0
        for i, cb in enumerate(cv_boxes):
            iou = _rect_iou(nb, cb["bbox"])
            if iou > best_iou:
                best_i, best_iou = i, iou
        if best_i >= 0 and best_iou >= 0.45:
            bbox = list(cv_boxes[best_i]["bbox"])
            used_cv.add(best_i)
            sources = ["native", "cv"]
        else:
            bbox = nb
            sources = ["native"]
        seeds.append({"bbox": bbox, "role": n["role"],
                      "text": n["text"], "sources": sources,
                      "native_class": n["class"]})

    # 2) CV 独立矩形 → 分类后作为新种子
    for i, cb in enumerate(cv_boxes):
        if i in used_cv:
            continue
        role = _classify_cv_box(cb["bbox"], ocr_items)
        seeds.append({"bbox": list(cb["bbox"]), "role": role, "text": "",
                      "sources": ["cv"], "fill": cb.get("fill")})

    # 3) OCR 文本贴附（输入框标签/值、按钮标签），未消费的成为独立 text
    consumed: set = set()
    elements: List[Dict[str, Any]] = []
    for seed in seeds:
        elements.append(_merge_element_text(seed, ocr_items, consumed))

    for i, item in enumerate(ocr_items):
        if i in consumed:
            continue
        t = item["text"]
        if len(t) == 1 and t in _OCR_GLYPHS:
            continue
        elements.append({"bbox": list(item["bbox"]), "role": "text",
                         "text": t, "label": t, "value": "",
                         "sources": ["ocr"], "score": item["score"]})

    # 4) 可选图标候选（无文本工具条/画图软件）
    if include_icons:
        try:
            from . import icon_grid
            icons = icon_grid.detect_icon_candidates(
                crop_img, max_candidates=min(24, max_elements))
        except Exception:  # noqa: BLE001
            icons = []
        for cand in icons:
            cb = [int(v) for v in cand["bbox"]]
            if any(_rect_iou(cb, e["bbox"]) > 0.25 for e in elements):
                continue
            if any(_rect_center_in(e["bbox"], _rect_center(cb))
                   for e in elements):
                continue
            elements.append({"bbox": cb, "role": "icon", "text": "",
                             "label": "", "value": "",
                             "sources": ["cv_icon"],
                             "score": cand.get("score")})

    # 5) 去重 + 过滤：text 元素若被真实控件严格包住则丢弃（防标签重复）
    controls = [e for e in elements if e["role"] != "text"]
    kept: List[Dict[str, Any]] = []
    for e in elements:
        x, y, w, h = e["bbox"]
        if w < 4 or h < 4:
            continue
        if e["role"] == "text" and not e["text"]:
            continue
        if e["role"] == "text" and any(
                _bbox_in(e["bbox"], c["bbox"], pad=2) for c in controls):
            continue
        dup = next((k for k in kept
                    if k["role"] == e["role"]
                    and _rect_iou(k["bbox"], e["bbox"]) > 0.72), None)
        if dup is not None:
            for src in e.get("sources") or []:
                if src not in dup["sources"]:
                    dup["sources"].append(src)
            if not dup.get("text") and e.get("text"):
                dup["text"] = e["text"]
                dup["label"] = e.get("label") or e["text"]
            continue
        kept.append(e)

    # 6) 阅读顺序编号（行带 = 中位高度 0.6；同行按 x）
    import statistics
    heights = [e["bbox"][3] for e in kept if e["bbox"][3] > 0] or [24]
    band = max(14, int(statistics.median(heights) * 0.6))
    kept.sort(key=lambda e: (int(e["bbox"][1]) // band, int(e["bbox"][0])))
    kept = kept[:max(1, int(max_elements))]

    out: List[Dict[str, Any]] = []
    for i, e in enumerate(kept, 1):
        x, y, w, h = [int(v) for v in e["bbox"]]
        sx, sy = x + origin_x, y + origin_y
        score = e.get("score")
        if score is None and e.get("fill") is not None:
            score = e["fill"]
        item: Dict[str, Any] = {
            "id": i,
            "text": str(e.get("text") or ""),
            "type": e.get("role") or "unknown",
            "bbox": [sx, sy, w, h],
            "center": [sx + w // 2, sy + h // 2],
            "sources": sorted(set(e.get("sources") or [])),
        }
        if e.get("role") == "input":
            item["label"] = str(e.get("label") or "")
            item["value"] = str(e.get("value") or "")
        if score is not None:
            item["score"] = round(float(score), 4)
        if e.get("native_class"):
            item["native_class"] = e["native_class"]
        out.append(item)
    return out


_rag_cache: Dict[Any, Tuple[float, Dict[str, Any], Any, List[int]]] = {}
_rag_cache_lock = threading.Lock()


def invalidate_rag_cache() -> None:
    """动作执行后清空元素检索缓存（绝不把旧界面元素用于新动作）。"""
    with _rag_cache_lock:
        _rag_cache.clear()


def _rag_cache_key(real_hwnd: int, info: Optional[Dict[str, Any]],
                   backend: str, include_icons: bool,
                   max_elements: int) -> Tuple[Any, ...]:
    return (int(real_hwnd), str((info or {}).get("title") or ""),
            str(backend), bool(include_icons), int(max_elements))


def _annotate_elements(crop_img, screen_origin: List[int],
                       elements: List[Dict[str, Any]],
                       dangerous_ids: Optional[set] = None):
    """在缩放后的客户区截图上画编号框；返回 (b64, image_size, scale)。"""
    import base64
    import io
    from PIL import ImageDraw
    from . import icon_grid
    fitted, scale = _fit_for_vl(crop_img, config.VL_MAX_SIDE)
    draw = ImageDraw.Draw(fitted)
    font_size = max(12, min(18, fitted.width // 60))
    font = icon_grid._font(font_size)
    dangerous_ids = dangerous_ids or set()
    ox, oy = int(screen_origin[0]), int(screen_origin[1])
    for e in elements:
        x, y, w, h = [int(v) for v in e["bbox"]]
        x1 = max(0, int(round((x - ox) * scale)))
        y1 = max(0, int(round((y - oy) * scale)))
        x2 = min(fitted.width, int(round((x - ox + w) * scale)))
        y2 = min(fitted.height, int(round((y - oy + h) * scale)))
        if x2 - x1 < 3 or y2 - y1 < 3:
            continue
        color = (220, 40, 40) if int(e.get("id", 0)) in dangerous_ids \
            else (0, 150, 60)
        draw.rectangle([x1, y1, x2, y2], outline=color, width=2)
        label = " " + str(e.get("id")) + " "
        tw = draw.textlength(label, font=font)
        th = font_size + 3
        lx = x1
        ly = y1 - th if y1 - th >= 0 else y1 + 1
        draw.rectangle([lx, ly, lx + tw, ly + th], fill=color)
        draw.text((lx + 2, ly - 1), label, fill=(255, 255, 255), font=font)
    buf = io.BytesIO()
    fitted.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii"), list(fitted.size), scale


def _extract_elements_core(monitor: int = 1, window: Optional[str] = None,
                           hwnd: Optional[int] = None,
                           backend: Optional[str] = None,
                           include_icons: bool = False,
                           max_elements: int = 200,
                           annotate: bool = False,
                           use_cache: bool = False
                           ) -> Tuple[Dict[str, Any], Any, List[int]]:
    """extract_ui_elements 的内部实现；额外返回 (crop_img, screen_origin)。"""
    t0 = time.perf_counter()
    channel, resolved = resolve_window(window, hwnd)
    if channel == "cdp":
        raise VLError("extract_ui_elements is native-only; target "
                      + repr(resolved) + " routes to the CDP channel "
                      "(use browser tools for web)")
    real_hwnd = int(resolved)
    info = safety_gate.window_info(real_hwnd)
    requested = (backend or config.VL_BACKEND or "auto").strip().lower()
    timings: Dict[str, Any] = {}
    cache_key = _rag_cache_key(real_hwnd, info, requested,
                               include_icons, max_elements)
    if use_cache and config.RAG_CACHE_SECONDS > 0:
        now = time.monotonic()
        with _rag_cache_lock:
            hit = _rag_cache.get(cache_key)
        if hit is not None and now - hit[0] <= config.RAG_CACHE_SECONDS:
            cached = copy.deepcopy(hit[1])
            cached["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
            ct = dict(cached.get("timings") or {})
            ct["cache_hit"] = True
            ct["cache_age_ms"] = int((now - hit[0]) * 1000)
            cached["timings"] = ct
            return cached, hit[2], list(hit[3])

    if requested == "mock":
        raw = _analyze_mock()
        elements: List[Dict[str, Any]] = []
        for i, c in enumerate(raw, 1):
            item = dict(c)
            item["id"] = i
            item["sources"] = ["mock"]
            elements.append(item)
        crop_img, origin, view = _grab_view(monitor, real_hwnd)
        chosen = "mock"
        ocr_ok = False
    else:
        crop_img, origin, view = _grab_view(monitor, real_hwnd)
        native: List[Dict[str, Any]] = []
        ocr_items: List[Dict[str, Any]] = []
        cv_boxes: List[Dict[str, Any]] = []
        t = time.perf_counter()
        try:
            native = _native_seeds(real_hwnd, view)
        except Exception as exc:  # noqa: BLE001
            timings["native_error"] = type(exc).__name__ + ": " + str(exc)
        timings["native_ms"] = int((time.perf_counter() - t) * 1000)

        ocr_ok = False
        if requested != "native":
            t = time.perf_counter()
            try:
                ocr_items = _ocr_read(crop_img)
                ocr_ok = True
            except VLBackendUnavailable as exc:
                timings["ocr_error"] = str(exc)
            timings["ocr_ms"] = int((time.perf_counter() - t) * 1000)
            t = time.perf_counter()
            try:
                cv_boxes = _detect_cv_boxes(crop_img)
            except Exception as exc:  # noqa: BLE001
                timings["cv_error"] = type(exc).__name__ + ": " + str(exc)
            timings["cv_ms"] = int((time.perf_counter() - t) * 1000)

        t = time.perf_counter()
        elements = _assemble_elements(
            crop_img, origin, view, native, cv_boxes, ocr_items,
            include_icons=include_icons, max_elements=max_elements)
        timings["assemble_ms"] = int((time.perf_counter() - t) * 1000)
        if requested == "native":
            chosen = "native"
        elif ocr_ok:
            chosen = "rag_ocr_cv"
        elif native:
            chosen = "native"
        else:
            chosen = "none"

    # 元素级动态门禁（只扫可见标签，不扫输入值——输入框里的"删除"只是文字）
    hits_all: List[Dict[str, Any]] = []
    dangerous_ids: List[int] = []
    for e in elements:
        g = safety_gate.dynamic_guard([e.get("text") or ""])
        if g["requires_approval"]:
            e["dangerous"] = True
            e["guard_hits"] = g["hits"]
            dangerous_ids.append(int(e["id"]))
            for h in g["hits"]:
                h = dict(h)
                h["element_id"] = int(e["id"])
                hits_all.append(h)
    guard = {
        "verdict": "pending_approval" if hits_all else "allow",
        "requires_approval": bool(hits_all),
        "hits": hits_all[:40],
        "dangerous_ids": dangerous_ids,
        "categories": sorted({h["category"] for h in hits_all}),
        "version": safety_gate.dynamic_guard([])["version"],
        "approval_token_configured": safety_gate.approval_configured(),
    }

    result: Dict[str, Any] = {
        "ok": True,
        "backend": chosen,
        "window": {"hwnd": real_hwnd,
                   "title": (info or {}).get("title"),
                   "process": (info or {}).get("process")},
        "view": view,
        "elements": elements,
        "count": len(elements),
        "guard": guard,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        "timings": timings,
    }
    if annotate and elements:
        b64, size, scale = _annotate_elements(
            crop_img, origin, elements, set(dangerous_ids))
        result["annotated_b64"] = b64
        result["annotated_size"] = size
        result["annotated_scale"] = round(scale, 4)
    if use_cache and config.RAG_CACHE_SECONDS > 0:
        with _rag_cache_lock:
            now = time.monotonic()
            if len(_rag_cache) >= 8:
                for k, v in list(_rag_cache.items()):
                    if now - v[0] > config.RAG_CACHE_SECONDS:
                        _rag_cache.pop(k, None)
                while len(_rag_cache) >= 16:
                    _rag_cache.pop(next(iter(_rag_cache)), None)
            _rag_cache[cache_key] = (now, copy.deepcopy(result),
                                     crop_img, list(origin))
    return result, crop_img, origin


def extract_ui_elements(monitor: int = 1, window: Optional[str] = None,
                        hwnd: Optional[int] = None,
                        backend: Optional[str] = None,
                        include_icons: bool = False,
                        max_elements: int = 200,
                        annotate: bool = False) -> Dict[str, Any]:
    """GUI RAG 检索层：屏幕 → 带编号的元素表（按钮/输入框/文字/图标）。

    与 analyze() 的区别：analyze 是"模型直接看图出控件表"；本函数是
    **确定性的 OCR+CV+win32 检索**，bbox 全部来自像素/句柄，模型不参与
    坐标生成。返回元素 id 供 ground_target()/MCP click_element 选号。
    """
    result, _crop, _origin = _extract_elements_core(
        monitor=monitor, window=window, hwnd=hwnd, backend=backend,
        include_icons=include_icons, max_elements=max_elements,
        annotate=annotate)
    return result


_RAG_SYSTEM = (
    "You are a precise GUI element matcher. Match the target string to the "
    "element with the same label. Reply ONLY "
    '{"target_id": <number>}. If no element matches, reply '
    '{"target_id": 0}. Never invent an id that is not in the list.')


def _rag_catalog(elements: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for e in elements:
        kind = str(e.get("type") or "element")
        label = str(e.get("text") or e.get("label") or "").strip()
        if not label:
            label = "(unlabeled " + kind + ")"
        extra = ""
        if e.get("value"):
            extra = ' value="' + str(e["value"])[:80] + '"'
        lines.append(str(e["id"]) + ". [" + kind + "] " + label + extra)
    return "\n".join(lines)


def _rag_candidates(elements: List[Dict[str, Any]], query: str,
                    max_candidates: int) -> List[Dict[str, Any]]:
    """文本相关度预筛（RAG 的 retrieval）：精确/包含/词重叠/类型提示。"""
    q = _norm_key(query)
    q_low = str(query or "").lower()
    type_hint = None
    for hint, kind in (("输入框", "input"), ("输入", "input"),
                       ("按钮", "button"), ("勾选", "checkbox"),
                       ("复选框", "checkbox"), ("图标", "icon"),
                       ("文字", "text")):
        if hint in q_low:
            type_hint = kind
            break
    scored: List[Tuple[float, int, Dict[str, Any]]] = []
    for e in elements:
        keys = [_norm_key(e.get("text")), _norm_key(e.get("label")),
                _norm_key(e.get("value"))]
        text = keys[0]
        score = 0.0
        if q and q in keys:
            score = 100.0
        elif q and text and text == q:
            score = 95.0
        elif q and any(k and q in k for k in keys):
            score = 85.0
        elif q and any(k and len(k) >= 2 and k in q for k in keys):
            score = 65.0
        else:
            # 英文/数字词重叠（中文子串已覆盖）
            q_tokens = {t for t in re.split(r"\W+", q_low) if len(t) >= 2}
            e_tokens = {t for t in re.split(r"\W+", " ".join(keys).lower())
                        if len(t) >= 2}
            if q_tokens and e_tokens:
                overlap = len(q_tokens & e_tokens) / float(len(q_tokens))
                if overlap:
                    score = 40.0 * overlap
        if type_hint and e.get("type") == type_hint:
            score += 20.0
        if not str(e.get("text") or "").strip():
            score += 1.0  # 图标/无标签控件保留极低权重，避免被全部滤掉
        scored.append((score, int(e.get("id") or 0), e))
    scored.sort(key=lambda t: (-t[0], t[1]))
    positive = [e for s, _i, e in scored if s > 1.0]
    pool = positive or [e for _s, _i, e in scored]
    return pool[:max(1, int(max_candidates))]


def _parse_json_loose(content: str) -> Any:
    try:
        return json.loads(content)
    except (TypeError, ValueError):
        pass
    for left, right in (("{", "}"), ("[", "]")):
        a, b = content.find(left), content.rfind(right)
        if a >= 0 and b > a:
            try:
                return json.loads(content[a:b + 1])
            except ValueError:
                continue
    return None


def _rag_select(query: str, candidates: List[Dict[str, Any]],
                img_b64: Optional[str] = None
                ) -> Tuple[int, Optional[float], str, Dict[str, Any]]:
    """VLM 选号：候选目录（可选编号截图）→ target_id。绝不解析坐标。"""
    llm = _get_llama()
    catalog = _rag_catalog(candidates)
    user_text = ("Elements:\n" + catalog + "\nTarget label: "
                 + str(query) + "\nAnswer JSON:")
    if img_b64:
        messages = [
            {"role": "system", "content": _RAG_SYSTEM},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {
                    "url": "data:image/png;base64," + img_b64}},
                {"type": "text", "text": user_text}]},
        ]
    else:
        messages = [
            {"role": "system", "content": _RAG_SYSTEM},
            {"role": "user", "content": user_text},
        ]
    t0 = time.perf_counter()
    try:
        resp = llm.create_chat_completion(
            messages=messages, temperature=0.0, max_tokens=48,
            response_format={"type": "json_object"})
    except Exception as exc:  # noqa: BLE001
        raise VLError("rag selection failed: " + type(exc).__name__
                      + ": " + str(exc)) from exc
    elapsed = time.perf_counter() - t0
    content = str(((resp.get("choices") or [{}])[0].get("message") or {})
                  .get("content") or "")
    data = _parse_json_loose(content)
    target_id = 0
    confidence: Optional[float] = None
    if isinstance(data, dict):
        if data.get("found") is False:
            target_id = 0
        for key in ("target_id", "element_id", "index", "number", "id"):
            raw = data.get(key)
            if raw is None:
                continue
            if isinstance(raw, bool):
                continue
            if isinstance(raw, (int, float)):
                target_id = int(raw)
                break
            digits = re.findall(r"\d+", str(raw))
            if digits:
                target_id = int(digits[-1])
                break
        for key in ("confidence", "score", "probability"):
            try:
                confidence = float(data.get(key))
                break
            except (TypeError, ValueError):
                continue
    timings = {
        "vlm_ms": int(elapsed * 1000),
        "completion_chars": len(content),
        "raw_head": content[:200],
        "image": bool(img_b64),
        "candidates": len(candidates),
    }
    return target_id, confidence, content, timings


def _quick_text_match(elements: List[Dict[str, Any]], query: str,
                      match: str = "auto") -> List[Dict[str, Any]]:
    q = _norm_key(query)
    if not q:
        return []
    mode = (match or "auto").strip().lower()

    def _exact(e: Dict[str, Any]) -> bool:
        return any(_norm_key(e.get(k)) == q
                   for k in ("text", "label", "value"))

    def _contains(e: Dict[str, Any]) -> bool:
        return any(q in _norm_key(e.get(k))
                   for k in ("text", "label", "value"))

    exact = [e for e in elements if _exact(e)]
    if exact:
        return exact
    if mode == "exact":
        return []
    return [e for e in elements if _contains(e)]


def ground_target(query: str, monitor: int = 1, window: Optional[str] = None,
                  hwnd: Optional[int] = None, backend: Optional[str] = None,
                  strategy: str = "auto", match: str = "auto",
                  use_image: Optional[bool] = None,
                  min_conf: Optional[float] = None,
                  max_candidates: Optional[int] = None,
                  include_icons: bool = False,
                  elements: Optional[List[Dict[str, Any]]] = None,
                  annotate: bool = False) -> Dict[str, Any]:
    """GUI RAG 选择层：文本目标 → 编号元素 → VLM 只选 id → 返回真实 bbox。

    strategy:
      auto   （默认）唯一精确/包含文本命中直接返回（零模型）；否则 VLM 选号。
      vlm    强制 VLM 选号（准确率验收用；失败不回落文本匹配）。
      text   只用确定性文本匹配（零模型；多命中返回 409 而非猜测）。
      hybrid VLM 选号；低置信/无效时回落唯一文本匹配。
    use_image: None=auto（图标/无文本候选才带编号截图）；True/False 强制。
    """
    q = str(query or "").strip()
    if not q:
        raise VLError("ground_target requires a non-empty query")
    t0 = time.perf_counter()
    crop_img = None
    origin: Optional[List[int]] = None
    if elements is None:
        extracted, crop_img, origin = _extract_elements_core(
            monitor=monitor, window=window, hwnd=hwnd, backend=backend,
            include_icons=include_icons,
            max_elements=max(40, int(max_candidates or 200) * 4),
            annotate=False, use_cache=True)
        all_elements = extracted.get("elements") or []
    else:
        extracted = {"backend": "provided", "window": {},
                     "elements": elements, "count": len(elements),
                     "guard": {"verdict": "allow", "requires_approval": False,
                               "hits": [], "dangerous_ids": [],
                               "categories": [], "version": "v0.12",
                               "approval_token_configured":
                                   safety_gate.approval_configured()},
                     "timings": {}, "view": {}}
        all_elements = elements

    strat = (strategy or "auto").strip().lower()
    quick = _quick_text_match(all_elements, q, match=match)
    best = quick[0] if len(quick) == 1 else None
    if strat == "text":
        if len(quick) > 1:
            raise VLAmbiguous(
                repr(q) + " matches " + str(len(quick))
                + " elements; use strategy=vlm to disambiguate: "
                + repr([e.get("id") for e in quick[:8]]),
                candidates=quick[:12])
        if not quick:
            raise VLElementNotFound("no element matches " + repr(q))
        best = quick[0]
        method = "text_exact" if any(
            _norm_key(best.get(k)) == _norm_key(q)
            for k in ("text", "label", "value")) else "text_contains"
        return _ground_result(extracted, q, strat, method, best,
                              all_elements, [], None, None, annotate,
                              crop_img, origin, t0)
    if strat == "auto" and best is not None:
        method = "text_exact" if any(
            _norm_key(best.get(k)) == _norm_key(q)
            for k in ("text", "label", "value")) else "text_contains"
        return _ground_result(extracted, q, strat, method, best,
                              all_elements, [], None, None, annotate,
                              crop_img, origin, t0)

    # ---- VLM 选号 ----
    limit = int(max_candidates if max_candidates is not None
                else config.RAG_MAX_CANDIDATES)
    candidates = _rag_candidates(all_elements, q, limit)
    if not candidates:
        raise VLElementNotFound("no candidates extracted from window")
    if use_image is None:
        mode = config.RAG_IMAGE_MODE
        if mode == "always":
            use_image = True
        elif mode == "never":
            use_image = False
        else:
            use_image = any(str(e.get("type")) == "icon" for e in candidates) \
                or not any(str(e.get("text") or "").strip()
                           for e in candidates)
    if not llama_available():
        if strat in ("auto", "hybrid") and best is not None:
            return _ground_result(extracted, q, strat, "text_fallback", best,
                                  all_elements, candidates, None, None,
                                  annotate, crop_img, origin, t0)
        raise VLBackendUnavailable(
            "GUI RAG selection needs the llama backend "
            "(GHOST_VL_MODEL_PATH); strategy=" + strat)

    attempts: List[Dict[str, Any]] = []
    chosen: Optional[Dict[str, Any]] = None
    confidence: Optional[float] = None
    method = ""
    img_b64: Optional[str] = None
    if use_image:
        if crop_img is None:
            crop_img, origin, _view = _grab_view(monitor, hwnd)
        img_b64, _size, _scale = _annotate_elements(
            crop_img, origin or [0, 0], candidates, set())

    plans = [bool(use_image)]
    if not use_image and config.RAG_IMAGE_MODE != "never":
        plans.append(True)  # 文本选号无效/低置信 → 带编号截图重试一次
    for with_img in plans:
        if with_img and img_b64 is None:
            if crop_img is None:
                crop_img, origin, _view = _grab_view(monitor, hwnd)
            img_b64, _size, _scale = _annotate_elements(
                crop_img, origin or [0, 0], candidates, set())
        try:
            target_id, conf, raw, tm = _rag_select(
                q, candidates, img_b64 if with_img else None)
        except VLBackendUnavailable:
            if strat in ("auto", "hybrid") and best is not None:
                return _ground_result(
                    extracted, q, strat, "text_fallback", best,
                    all_elements, candidates, None, attempts,
                    annotate, crop_img, origin, t0)
            raise
        attempts.append(dict(tm, target_id=target_id, confidence=conf,
                             with_image=bool(with_img)))
        found = next((e for e in candidates
                      if int(e.get("id") or 0) == int(target_id)), None)
        if found is None:
            continue
        threshold = (config.RAG_MIN_CONF if min_conf is None
                     else float(min_conf))
        if conf is not None and conf < threshold:
            continue
        chosen = found
        confidence = conf
        method = "vlm_image" if with_img else "vlm_text"
        break
    if chosen is None:
        if strat == "hybrid" and best is not None:
            return _ground_result(extracted, q, strat, "text_fallback", best,
                                  all_elements, candidates, confidence,
                                  attempts, annotate, crop_img, origin, t0)
        raise VLElementNotFound(
            "GUI RAG: VLM selected no valid target_id for " + repr(q)
            + " (attempts=" + repr(attempts[-2:]) + ")",
            candidates=[{"id": e.get("id"), "text": e.get("text"),
                         "type": e.get("type")} for e in candidates],
            attempts=attempts[-2:])
    return _ground_result(extracted, q, strat, method, chosen,
                          all_elements, candidates, confidence, attempts,
                          annotate, crop_img, origin, t0)


def _ground_result(extracted: Dict[str, Any], query: str, strategy: str,
                   method: str, chosen: Dict[str, Any],
                   all_elements: List[Dict[str, Any]],
                   candidates: List[Dict[str, Any]],
                   confidence: Optional[float],
                   attempts: Optional[List[Dict[str, Any]]],
                   annotate: bool, crop_img, origin: Optional[List[int]],
                   t0: float) -> Dict[str, Any]:
    guard = extracted.get("guard") or {}
    target_hits = chosen.get("guard_hits") or []
    result: Dict[str, Any] = {
        "ok": True,
        "backend": extracted.get("backend"),
        "window": extracted.get("window") or {},
        "query": query,
        "strategy": strategy,
        "method": method,
        "target_id": int(chosen.get("id") or 0),
        "confidence": confidence,
        "element": chosen,
        "elements": all_elements,
        "count": len(all_elements),
        "candidates": [{"id": e.get("id"), "text": e.get("text"),
                        "type": e.get("type")} for e in candidates],
        "guard": guard,
        "target_guard": {
            "requires_approval": bool(target_hits),
            "hits": target_hits,
            "dangerous": bool(chosen.get("dangerous")),
        },
        "requires_approval": bool(target_hits),
        "vm_attempts": attempts or [],
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
    }
    if annotate and crop_img is not None:
        b64, size, scale = _annotate_elements(
            crop_img, origin or [0, 0], candidates or all_elements,
            set(guard.get("dangerous_ids") or []))
        result["annotated_b64"] = b64
        result["annotated_size"] = size
        result["annotated_scale"] = round(scale, 4)
    return result


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


def guard_status() -> Dict[str, Any]:
    """Ghost Guard / GUI RAG / System1 配置回显（供 /health；绝不泄露审批令牌）。"""
    return {
        "guard": {
            "enabled": config.GUARD_ENABLED,
            "mode": config.GUARD_MODE,
            "fail_closed": config.GUARD_FAIL_CLOSED,
            "approval_token_configured": safety_gate.approval_configured(),
            "version": "v0.12",
        },
        "rag": {
            "max_candidates": config.RAG_MAX_CANDIDATES,
            "min_conf": config.RAG_MIN_CONF,
            "image_mode": config.RAG_IMAGE_MODE,
            "cache_seconds": config.RAG_CACHE_SECONDS,
        },
        "system1": {
            "auto_dismiss": config.SYSTEM1_AUTO_DISMISS,
            "trigger_tokens": len(safety_gate.SYSTEM1_TRIGGER_TOKENS),
            "dismiss_buttons": len(safety_gate.SYSTEM1_DISMISS_BUTTONS),
            "version": "v0.15",
        },
    }


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


# ---- v0.15 System 1 微型反射脑（快思考） ----------------------------------
#
# 与慢系统（VL analyze → 主仓视觉 → degraded 挂起）互补：任务在挂起边界
# （require_vision / 遮挡类失败）先本地"看一眼"——OCR 规则命中「弹窗特征词
# + 关闭按钮 同窗共现」则走 controller 安全管道本地点击关闭并复检；任何一步
# 不满足就放弃并回退旧行为（挂起等主仓）。判定规则在 safety_gate.system1_match
# （纯函数）；本层只负责编排与安全收口，绝不绕过任何门禁：
# - UAC 提权窗 / 窗口黑名单：命中即整体放弃（零输入）；
# - 窗口特征：可见顶层窗、面积 ≤45% 屏（对话框体量），控制台宿主类排除；
# - 点击走 controller.run_action：能力令牌只授予"被识别弹窗自身进程"
#   （授权来源 = GHOST_SYSTEM1_AUTO_DISMISS 开关 + 窗口自身验证通过），
#   Ghost Guard / Fail-Safe / 熔断 / 前后截图审计一道不少；
# - 点击后必须复检（窗口消失 或 规则不再命中）才算 dismissed，否则如实报
#   still_present，调用方回退挂起——宁可挂起，绝不谎报消解。
SYSTEM1_MAX_CANDIDATES = 3       # 候选窗上限（控单次反射延迟）
SYSTEM1_MAX_ATTEMPTS = 2         # 单窗最多尝试的关闭按钮数（控风险）
SYSTEM1_VERIFY_WAIT = 0.35       # 点击后等待复检的秒数


def _system1_candidates(limit: int = SYSTEM1_MAX_CANDIDATES) -> List[int]:
    """弹窗候选窗：前台窗优先 + 顶层置顶且**标题含触发词**的窗（零 OCR 预筛）。

    标题预筛用 system1_title_hit（win32 直读标题，零 OCR 成本）——既控单次
    反射延迟，也控误伤面：绝不为"标题无关"的窗口付出 OCR，更不乱点用户的
    真实窗口（反射只扫：前台 + 置顶+标题命中 的对话框体量窗）。
    """
    out: List[int] = []
    seen: set = set()

    def _add(h: Any) -> None:
        try:
            v = int(h)
        except (TypeError, ValueError):
            return
        if v and v not in seen and len(out) < limit:
            seen.add(v)
            out.append(v)

    _add(window_locator.foreground_hwnd())

    def _cb(hwnd: int, _acc: Any) -> bool:
        if len(out) >= limit:
            return False
        try:
            if not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
                return True
            ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            if not (ex & win32con.WS_EX_TOPMOST):
                return True
            if ex & win32con.WS_EX_TOOLWINDOW:
                return True
            title = win32gui.GetWindowText(hwnd)
            if not title or not safety_gate.system1_title_hit(title):
                return True  # 标题无触发词的置顶窗不进候选（零 OCR、零误伤）
            _add(hwnd)
        except Exception:  # noqa: BLE001 窗口枚举竞态
            pass
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:  # noqa: BLE001
        pass
    return out[:limit]


def _system1_window_geometry(hwnd: int) -> Optional[Tuple[Tuple[int, ...], Dict[str, Any]]]:
    """窗口特征预筛：可见未最小化 + 对话框体量（≤45% 屏）。返回 (rect, monitor) 或 None。"""
    from . import screen_mss  # 延迟：DPI 顺序红线
    try:
        if not win32gui.IsWindow(hwnd) or win32gui.IsIconic(hwnd):
            return None
        rect = win32gui.GetWindowRect(hwnd)
    except Exception:  # noqa: BLE001
        return None
    w, h = int(rect[2]) - int(rect[0]), int(rect[3]) - int(rect[1])
    if w <= 4 or h <= 4:
        return None
    cx, cy = int(rect[0]) + w // 2, int(rect[1]) + h // 2
    mon = screen_mss.monitor_at(cx, cy)
    if not mon:
        return None
    if w * h > safety_gate.SYSTEM1_MAX_WINDOW_AREA_RATIO * mon["width"] * mon["height"]:
        return None  # 非对话框体量（最大化窗/主窗口里乱点的风险面）
    return rect, mon


def _system1_verify(hwnd: int, monitor: int) -> bool:
    """点击后复检：窗口消失/隐藏 → True；仍可见则再 OCR，规则不再命中 → True。

    复检失败（窗口在但弹窗规则仍命中 / OCR 故障无法证实）一律 False——
    宁可让调用方回退挂起，绝不把"点了按钮"说成"弹窗已关"。
    """
    try:
        if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd) or ""
        elements = _analyze_ocr(monitor, hwnd)
    except Exception:  # noqa: BLE001 窗口刚销毁/OCR 故障 → 无法证实
        return False
    return not safety_gate.system1_match(elements, title=title)["matched"]


def system1_reflex(hwnd: Optional[int] = None, reason: str = "") -> Dict[str, Any]:
    """System 1 微型反射脑：本地识别弹窗并点击关闭（v0.15 快思考）。

    调用点（task_runner）：require_vision 挂起前 / 遮挡类失败降级前。
    命中（dismissed=True）→ 调用方继续任务（不返回 degraded）；
    未命中/被拒/复检未过 → dismissed=False，调用方回退 v0.14 旧行为。
    任何异常由调用方兜底捕获——反射绝不允许打断任务主流程。

    返回 {"ok","enabled","dismissed","reason","trigger","button","window",
          "attempts","source","elapsed_ms"}；reason 关键取值：
    disabled | no_candidate | refused_uac | refused_high_risk |
    ocr_unavailable | click_refused:<err> | dismissed_window_closed |
    dismissed_content_cleared | still_present | no_trigger | no_button | ...
    """
    t0 = time.perf_counter()
    out: Dict[str, Any] = {
        "ok": True, "enabled": bool(config.SYSTEM1_AUTO_DISMISS),
        "dismissed": False, "reason": "", "trigger": None, "button": None,
        "window": None, "attempts": 0, "source": reason, "elapsed_ms": 0,
    }

    def _done(code: str) -> Dict[str, Any]:
        out["reason"] = code
        out["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        return out

    if not config.SYSTEM1_AUTO_DISMISS:
        return _done("disabled")

    from . import audit_sink, screen_mss  # 延迟：audit 无环；screen_mss 有 DPI 顺序红线
    from .controller import ActionError, run_action  # 延迟：controller 依赖本模块（环）

    candidates = [int(hwnd)] if hwnd else _system1_candidates()
    if not candidates:
        return _done("no_candidate")
    last = "no_match"
    for cand in candidates:
        info = safety_gate.window_info(cand)
        if info is None:
            last = "window_gone"
            continue
        # ---- 安全先验（先于一切 OCR/点击）：UAC 与黑名单零容忍 ----
        uac = safety_gate.uac_fatal_reason(info)
        if uac:
            audit_sink.write("system1_refused", reason="uac_fatal", detail=uac,
                             hwnd=cand, process=info.get("process"),
                             title=info.get("title"), source=reason)
            LOG.warning("SYSTEM1_REFLEX refused reason=uac hwnd=%s title=%r",
                        cand, info.get("title"))
            return _done("refused_uac")  # UAC 在场 → 整体放弃，绝不再碰任何窗
        red = safety_gate.redline_window(info)
        if red:
            audit_sink.write("system1_refused", reason="high_risk_window",
                             detail=red, hwnd=cand, process=info.get("process"),
                             title=info.get("title"), source=reason)
            LOG.warning("SYSTEM1_REFLEX refused reason=high_risk hwnd=%s title=%r",
                        cand, info.get("title"))
            return _done("refused_high_risk")
        # ---- 窗口特征：控制台宿主类 / 非对话框体量 → 跳过 ----
        try:
            cls = win32gui.GetClassName(cand)
        except Exception:  # noqa: BLE001
            cls = ""
        if cls in safety_gate.SYSTEM1_EXCLUDED_WINDOW_CLASSES:
            last = "excluded_class"
            continue
        geo = _system1_window_geometry(cand)
        if geo is None:
            last = "not_dialog_like"
            continue
        rect, mon = geo
        monitor_i = int(mon["index"])
        # ---- OCR + 规则判定（唯一重的一步） ----
        try:
            elements = _analyze_ocr(monitor_i, cand)
        except VLBackendUnavailable as exc:
            return _done("ocr_unavailable:" + str(exc)[:80])
        except VLError:
            last = "ocr_failed"
            continue
        match = safety_gate.system1_match(elements, title=str(info.get("title") or ""))
        if not match.get("matched"):
            last = str(match.get("reason") or "no_match")
            continue
        out["trigger"] = (match.get("trigger") or {}).get("token")
        out["window"] = {"hwnd": int(cand), "process": info.get("process"),
                         "title": info.get("title")}
        out["monitor"] = monitor_i
        # ---- 点击关闭（能力令牌只授予该弹窗自身进程；完整安全管道） ----
        process = str(info.get("process") or "")
        if not process:
            last = "unknown_process"
            continue
        caps = {"allowed_windows": [process]}
        dismissed_code = None
        res: Dict[str, Any] = {}
        bx = by = 0
        bmon: Optional[Dict[str, Any]] = None
        # 逐个按钮尝试：每轮点击前重新 OCR 拿最新布局（防按钮位移后点空）
        for _round in range(SYSTEM1_MAX_ATTEMPTS):
            buttons = list(match.get("buttons") or [])
            if not buttons:
                break
            btn = buttons[0]
            out["attempts"] = _round + 1
            out["button"] = str(btn.get("text") or "")
            out["button_bbox"] = list(btn.get("bbox") or [])
            bx, by = int(btn["center"][0]), int(btn["center"][1])
            bmon = screen_mss.monitor_at(bx, by) or mon
            try:
                if not win32gui.IsWindow(cand):  # 窗口在扫描后已消解
                    dismissed_code = "dismissed_window_gone"
                    break
            except Exception:  # noqa: BLE001
                dismissed_code = "dismissed_window_gone"
                break
            try:
                res = run_action({
                    "action": "click", "x": bx, "y": by,
                    "monitor": int(bmon["index"]), "hwnd": int(cand),
                    "capabilities": caps, "settle_seconds": 0.1,
                })
            except ActionError as exc:
                # 点击被安全管道拒绝（Guard/覆盖/Fail-Safe…）→ 如实上报，绝不绕过
                try:
                    if not win32gui.IsWindow(cand):
                        dismissed_code = "dismissed_window_gone"
                        break
                except Exception:  # noqa: BLE001
                    dismissed_code = "dismissed_window_gone"
                    break
                audit_sink.write("system1_refused",
                                 reason="click_" + str(getattr(exc, "error", "")),
                                 detail=str(exc)[:300], hwnd=int(cand),
                                 process=process, button=out["button"], x=bx, y=by,
                                 source=reason)
                LOG.warning("SYSTEM1_REFLEX click_refused error=%s hwnd=%s button=%r "
                            "detail=%s", getattr(exc, "error", "?"), cand,
                            out["button"], str(exc)[:160])
                return _done("click_refused:" + str(getattr(exc, "error", "")))
            time.sleep(SYSTEM1_VERIFY_WAIT)
            if _system1_verify(cand, monitor_i):
                dismissed_code = "dismissed_window_closed"
                break
            # 未消解 → 重新 OCR 拿最新布局，轮下一个按钮
            try:
                elements = _analyze_ocr(monitor_i, cand)
                match = safety_gate.system1_match(
                    elements, title=win32gui.GetWindowText(cand) or "")
            except Exception:  # noqa: BLE001
                break
            if not match.get("matched"):
                dismissed_code = "dismissed_content_cleared"
                break
        if dismissed_code:
            out["dismissed"] = True
            audit_sink.write(
                "system1_dismiss",
                trigger=out["trigger"], button=out["button"],
                button_bbox=out.get("button_bbox"), x=bx, y=by,
                monitor=int((bmon or mon)["index"]), hwnd=int(cand),
                process=process, title=info.get("title"),
                attempts=out["attempts"], verification=dismissed_code,
                source=reason,
                before_screenshot_path=(res or {}).get("before_screenshot_path"),
                after_screenshot_path=(res or {}).get("after_screenshot_path"))
            LOG.info("SYSTEM1_REFLEX dismissed trigger=%s button=%r hwnd=%s "
                     "process=%s at=(%d,%d) monitor=%d attempts=%d verify=%s "
                     "source=%s", out["trigger"], out["button"], cand, process,
                     bx, by, int((bmon or mon)["index"]), out["attempts"],
                     dismissed_code, reason)
            return _done(dismissed_code)
        last = "still_present"
        break  # 该窗点过了仍未消解 → 不再换窗乱点，如实回退
    return _done(last)
