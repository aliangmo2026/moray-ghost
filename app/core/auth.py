"""X-Ghost-Token 校验：静态 token + 可开关的 HMAC Intent Token（nonce 防重放）。"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional

from . import config, macro_store

HEADER = "X-Ghost-Token"


class Unauthorized(Exception):
    pass


def verify(value: Optional[str]) -> Dict[str, Any]:
    """校验 token。静态 token 永远可用；HMAC Intent Token 需 GHOST_INTENT_TOKEN_ENABLED=true。"""
    if not value:
        raise Unauthorized("missing X-Ghost-Token header")
    if hmac.compare_digest(str(value), config.ensure_token()):
        return {"kind": "static"}
    if config.INTENT_TOKEN_ENABLED and value.startswith("intent."):
        return _verify_intent(value)
    raise Unauthorized("invalid X-Ghost-Token")


def _b64decode_pad(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _verify_intent(value: str) -> Dict[str, Any]:
    """intent.<payload_b64>.<sig_b64>，payload={"exp","nonce","task_id"}，HMAC-SHA256。"""
    try:
        _, payload_b64, sig_b64 = value.split(".", 2)
        payload = json.loads(_b64decode_pad(payload_b64))
        sig = _b64decode_pad(sig_b64)
    except (ValueError, binascii.Error):
        raise Unauthorized("malformed intent token")
    key = (config.HMAC_KEY or config.ensure_token()).encode("utf-8")
    expected = hmac.new(key, payload_b64.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, sig):
        raise Unauthorized("bad intent token signature")
    try:
        exp = int(payload.get("exp", 0))
    except (TypeError, ValueError):
        raise Unauthorized("intent token has invalid exp")
    nonce = str(payload.get("nonce", ""))
    if exp < time.time():
        raise Unauthorized("intent token expired")
    if not nonce:
        raise Unauthorized("intent token missing nonce")
    try:
        macro_store.consume_nonce(nonce, exp)
    except macro_store.NonceReplayError:
        raise Unauthorized("intent token nonce replayed")
    return {"kind": "intent", "task_id": payload.get("task_id"), "nonce": nonce}
