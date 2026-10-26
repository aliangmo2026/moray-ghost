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
VERSION = "0.8.0"

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

# ---- v0.8.0 MCP 多模态感知（VL Driver） ------------------------------------

# 感知后端：auto（默认：llama 可用→llama，否则 ocr，否则 native）|
# native（win32 几何枚举）| ocr（rapidocr 轻量本地视觉模型）|
# llama（llama-cpp-python + GGUF）| mock（夹具）。
VL_BACKEND = os.getenv("GHOST_VL_BACKEND", "auto").strip().lower() or "auto"
# 真实 VL 模型权重（GGUF，如 Qwen2-VL-2B / MiniCPM-V）；空 = 未接入（auto 降级）。
VL_MODEL_PATH = os.getenv("GHOST_VL_MODEL_PATH", "").strip()
# mock 后端夹具 JSON：[{"text","type","bbox":[x,y,w,h]}]
VL_MOCK_JSON = os.getenv("GHOST_VL_MOCK_JSON", "").strip()

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
