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
VERSION = "0.5.0"

HOST = os.getenv("GHOST_HOST", "127.0.0.1")
PORT = int(os.getenv("GHOST_PORT", "8030"))

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
