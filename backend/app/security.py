from __future__ import annotations

import base64
import os
import re
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import get_settings

_SECRET_KEYS = {
    "authorization",
    "access_token",
    "refresh_token",
    "pass_code",
    "pass_code_token",
    "password",
    "captcha_token",
    "wcl",
    "web_content_link",
    "sign",
    "pr",
    "x-captcha-token",
    "x-device-sign",
}

_TEXT_PATTERNS = [
    re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)((?:access|refresh)_token[\"'=:\s]+)[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)((?:pass_code|passcode|pwd)[\"'=:\s]+)[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)([?&](?:sign|pr|expire|pass_code_token)=)[^&\s\"']+"),
]


def encrypt(value: str | None) -> str | None:
    if value is None:
        return None
    aes = AESGCM(get_settings().secret_bytes)
    nonce = os.urandom(12)
    ct = aes.encrypt(nonce, value.encode("utf-8"), None)
    return base64.urlsafe_b64encode(nonce + ct).decode("ascii")


def decrypt(value: str | None) -> str | None:
    if value is None:
        return None
    raw = base64.urlsafe_b64decode(value.encode("ascii"))
    aes = AESGCM(get_settings().secret_bytes)
    return aes.decrypt(raw[:12], raw[12:], None).decode("utf-8")


def redact_text(text: str) -> str:
    for pattern in _TEXT_PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + "***", text)
    return text


def sanitize(value: Any, *, _depth: int = 0) -> Any:
    if _depth > 8:
        return "<truncated>"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in _SECRET_KEYS or lowered.endswith("_token") or lowered.endswith("_secret"):
                out[key] = "***"
            else:
                out[key] = sanitize(item, _depth=_depth + 1)
        return out
    if isinstance(value, list):
        return [sanitize(item, _depth=_depth + 1) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
