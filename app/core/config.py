"""集中配置：.env、常量、路径。token 只从环境/.env 读取，缺失时生成随机值写回 .env。"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"
SCREENSHOT_DIR = DATA_DIR / "screenshots"
LOG_DIR = BASE_DIR / "logs"
DB_PATH = DATA_DIR / "ghost.db"
ENV_PATH = BASE_DIR / ".env"

load_dotenv(ENV_PATH)

SERVICE = "moray-ghost"
VERSION = "0.14.0"

HOST = os.getenv("GHOST_HOST", "127.0.0.1")
PORT = int(os.getenv("GHOST_PORT", "8030"))

# 运行环境标识（v0.7.1）：dev（默认，宽松，本地联调/旧测试行为不变）/
# production（生产：动作必须来自带 Token 的合法 HTTP 调用链，否则 403 SOURCE_DENIED）。
ENV_MODE = os.getenv("GHOST_ENV", "dev").strip().lower() or "dev"
IS_PRODUCTION = ENV_MODE in ("production", "prod")

# 全局急停热键（v0.7.1）：系统级、按下一键进程退出；留空禁用。
ESTOP_HOTKEY = os.getenv("GHOST_ESTOP_HOTKEY", "ctrl+alt+q").strip().lower()

# 结构化审计落盘（v0.7.1）：JSON Lines，一行一事件，供安全审计追溯。
AUDIT_PATH = DATA_DIR / "ghost_audit.jsonl"

# ---- v0.7.2 极限安全 ------------------------------------------------------

# 动作硬超时（秒）：锁等待 + 执行两段各受此预算约束（拖拽等声明时长另加补偿）。
# 超时 → 强制释放按键鼠标 + 冻结服务 + 408 ACTION_TIMEOUT。
ACTION_TIMEOUT = float(os.getenv("GHOST_ACTION_TIMEOUT", "10"))

# 动作频率熔断（防 AI 死循环）：同一指纹（action+x+y+…）在窗口内超限，或
# 连续重复超限 → 429 RATE_CIRCUIT_BREAK + 强制冻结，通知主仓。
RATE_LIMIT_COUNT = int(os.getenv("GHOST_RATE_LIMIT_COUNT", "10"))
RATE_LIMIT_WINDOW = float(os.getenv("GHOST_RATE_LIMIT_WINDOW", "1.0"))
RATE_LIMIT_CONSECUTIVE = int(os.getenv("GHOST_RATE_LIMIT_CONSECUTIVE", "50"))

# 独立看门狗（v0.7.2）：主进程被强杀后释放系统修饰键（防按键卡死）。
WATCHDOG_ENABLED = os.getenv("GHOST_WATCHDOG", "true").strip().lower() in (
    "1", "true", "yes")

# ---- v0.14 长时运行保障 ----------------------------------------------------

# 审计截图保留上限（按 mtime 只保留最新 N 张；0 = 不限制）。
# 实测挂机期间截图无界累积到 13,594 张 / 3.4GB——挂机数周必然撑爆磁盘。
SHOT_MAX_FILES = int(os.getenv("GHOST_SHOT_MAX_FILES", "2000"))

# ---- v0.8.0 MCP 多模态感知（VL Driver） ------------------------------------

# 感知后端：auto（默认：llama 可用→llama，否则 ocr，否则 native）|
# native（win32 几何枚举）| ocr（rapidocr 轻量本地视觉模型）|
# llama（llama-cpp-python + GGUF）| mock（夹具）。
VL_BACKEND = os.getenv("GHOST_VL_BACKEND", "auto").strip().lower() or "auto"
# 模型档位：2b（默认，当前已验证）/ 7b（下载完成后只改这一行）。
# 路径按档位读取 GHOST_VL_MODEL_PATH_<PROFILE>；2b 档兼容旧的
# GHOST_VL_MODEL_PATH（向后兼容）。
VL_PROFILE = (os.getenv("GHOST_VL_MODEL_PROFILE", "2b").strip().lower()
              or "2b")
_VL_KEY = VL_PROFILE.upper().replace("-", "_")
_VL_MODEL_SPECIFIC = os.getenv("GHOST_VL_MODEL_PATH_" + _VL_KEY, "").strip()
_VL_MMPROJ_SPECIFIC = os.getenv("GHOST_VL_MMPROJ_PATH_" + _VL_KEY,
                                "").strip()
# 真实 VL 模型权重（GGUF，如 Qwen2-VL-2B/7B）；空 = 未接入（auto 降级）。
VL_MODEL_PATH = _VL_MODEL_SPECIFIC or (
    os.getenv("GHOST_VL_MODEL_PATH", "").strip()
    if VL_PROFILE == "2b" else "")
# 多模态投影器（mmproj GGUF，llama.cpp 视觉必需，与模型配套下载）；空=未配视觉。
VL_MMPROJ_PATH = _VL_MMPROJ_SPECIFIC or (
    os.getenv("GHOST_VL_MMPROJ_PATH", "").strip()
    if VL_PROFILE == "2b" else "")
# GPU offload 层数（-1=全部；CPU 版 llama-cpp 轮子忽略此值，装 CUDA 轮子后自动生效）。
VL_N_GPU_LAYERS = int(os.getenv("GHOST_VL_N_GPU_LAYERS", "-1"))
# 送模型的图像最长边（CPU 上视觉编码按像素平方增长：640≈20s/次，
# 1280≈140s/次）。CUDA 可上调到 1024/1280。小窗口允许最多 2x 放大。
VL_MAX_SIDE = int(os.getenv("GHOST_VL_MAX_SIDE", "640"))
# llama.cpp 0.4.5+：Qwen-VL grounding 建议至少 1024 个图像 token
# （0.3.36 无此参数，驱动会自动忽略）。
VL_IMAGE_MIN_TOKENS = int(os.getenv("GHOST_VL_IMAGE_MIN_TOKENS", "1024"))
# 混合图标定位（CV 粗筛 + 编号网格 + VL 选号）：
# auto（默认）= 仅 7b 档启用；1/true=强制启用；0/false=关闭。
VL_HYBRID = os.getenv("GHOST_VL_HYBRID", "auto").strip().lower() or "auto"
VL_HYBRID_MIN_CONF = float(os.getenv("GHOST_VL_HYBRID_MIN_CONF", "0.55"))
# mock 后端夹具 JSON：[{"text","type","bbox":[x,y,w,h]}]
VL_MOCK_JSON = os.getenv("GHOST_VL_MOCK_JSON", "").strip()

# ---- v0.12.0 GUI RAG + Ghost Guard ---------------------------------------
# GUI RAG：OCR+CV 检索出的编号元素表 → VLM 只回 {"target_id": N}（无坐标）。
# 送给 VLM 的候选上限（大屏/密集界面按文本相关度预筛，避免长目录拖慢 2B）。
RAG_MAX_CANDIDATES = int(os.getenv("GHOST_RAG_MAX_CANDIDATES", "16"))
# 元素检索短 TTL 缓存（秒）：连续 ground_target 复用同一张元素表，避免
# 每次重新 OCR（本机 660x540 暖机 ~1.4s）。动作执行后立即失效，绝不缓存
# 跨动作的旧界面。0 = 关闭缓存。
RAG_CACHE_SECONDS = float(os.getenv("GHOST_RAG_CACHE_SECONDS", "2.0"))
# VLM 选号置信度下限（模型未返回 confidence 时不施加门槛）。
RAG_MIN_CONF = float(os.getenv("GHOST_RAG_MIN_CONF", "0.0"))
# 是否把编号截图一起送 VLM：auto（图标候选时才带图）/ always / never。
RAG_IMAGE_MODE = (os.getenv("GHOST_RAG_IMAGE_MODE", "auto").strip().lower()
                  or "auto")

# 动态视觉门禁（Ghost Guard）：点击前对落点附近 OCR 文本做高危词检测。
# block（默认）= 命中即 409 PENDING_APPROVAL；off = 关闭（其余结构门禁不变）。
GUARD_MODE = (os.getenv("GHOST_GUARD", "block").strip().lower() or "block")
GUARD_ENABLED = GUARD_MODE not in ("0", "false", "no", "off", "disable")
# OCR 不可用时：true（默认）= 拒绝点击（fail-closed）；false = 放行并审计。
GUARD_FAIL_CLOSED = os.getenv("GHOST_GUARD_FAIL_CLOSED", "true").strip().lower() \
    in ("1", "true", "yes", "on")
# 人类审批令牌：配置后，动作 payload 带 approval_token 且匹配才放行
# PENDING_APPROVAL。Agent 侧环境里不应持有它（只给人工控制台）。
APPROVAL_TOKEN = os.getenv("GHOST_APPROVAL_TOKEN", "").strip()

# CDP 浏览器接管（v0.6 M2）：仅允许 loopback 调用点（browser_driver 使用前再校验一次）
BROWSER_CDP_URL = os.getenv("GHOST_BROWSER_CDP_URL", "http://127.0.0.1:9222").strip()

TOKEN = os.getenv("GHOST_TOKEN", "").strip()
INTENT_TOKEN_ENABLED = os.getenv("GHOST_INTENT_TOKEN_ENABLED", "false").strip().lower() in ("1", "true", "yes")
HMAC_KEY = os.getenv("GHOST_HMAC_KEY", "").strip()

JPEG_QUALITY = int(os.getenv("GHOST_JPEG_QUALITY", "70"))

# Fail-Safe 永远开启，不提供任何关闭开关。
FAIL_SAFE_ENABLED = True


def ensure_dirs() -> None:
    for d in (DATA_DIR, SCREENSHOT_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)


def ensure_token() -> str:
    """保证存在可用 token：缺失时生成随机值并写回 .env（.env 已 gitignore）。"""
    global TOKEN
    if TOKEN:
        return TOKEN
    TOKEN = secrets.token_urlsafe(24)
    lines: list[str] = []
    if ENV_PATH.exists():
        lines = [line for line in ENV_PATH.read_text(encoding="utf-8").splitlines()
                 if not line.strip().startswith("GHOST_TOKEN=")]
    lines.insert(0, f"GHOST_TOKEN={TOKEN}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return TOKEN
