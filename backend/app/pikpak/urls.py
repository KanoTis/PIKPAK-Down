from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

SHARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,48}$")
HOST_RE = re.compile(r"(^|\.)mypikpak\.com$", re.IGNORECASE)


@dataclass(frozen=True)
class ShareInput:
    share_id: str
    pass_code: str | None = None
    file_id: str | None = None
    source_url: str | None = None


def parse_share_input(value: str, pass_code: str | None = None) -> ShareInput:
    text = value.strip()
    if not text:
        raise ValueError("分享链接不能为空")

    if SHARE_ID_RE.fullmatch(text):
        return ShareInput(share_id=text, pass_code=pass_code)

    parsed = urlparse(text if "://" in text else "https://" + text)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("仅支持 http/https 分享链接")
    if not parsed.hostname or not HOST_RE.search(parsed.hostname):
        raise ValueError("仅支持 mypikpak.com 分享链接")

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0] != "s" or not SHARE_ID_RE.fullmatch(parts[1]):
        raise ValueError("分享链接中缺少合法的 share_id")
    query = parse_qs(parsed.query)
    code = pass_code
    for key in ("pwd", "pass_code", "password"):
        if not code and query.get(key):
            code = query[key][0]
    return ShareInput(share_id=parts[1], pass_code=code, source_url=text)


def validate_share_id(value: str) -> str:
    if not SHARE_ID_RE.fullmatch(value):
        raise ValueError("share_id 格式非法")
    return value
