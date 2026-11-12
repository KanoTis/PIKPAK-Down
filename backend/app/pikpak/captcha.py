from __future__ import annotations

import asyncio
import hashlib
import time

import httpx

from .constants import DRIVE_SALTS, WEB_CLIENT_ID, WEB_CLIENT_VERSION, WEB_PACKAGE_NAME


def drive_sign(client_id: str, device_id: str, timestamp_ms: str) -> str:
    digest = client_id + WEB_CLIENT_VERSION + WEB_PACKAGE_NAME + device_id + timestamp_ms
    for salt in DRIVE_SALTS:
        digest = hashlib.md5((digest + salt).encode("utf-8")).hexdigest()
    return "1." + digest


async def mint_drive_captcha(
    http: httpx.AsyncClient,
    user_base_url: str,
    *,
    device_id: str,
    sub: str,
    action: str,
) -> tuple[str, float]:
    timestamp = str(int(time.time() * 1000))
    body = {
        "client_id": WEB_CLIENT_ID,
        "action": action,
        "device_id": device_id,
        "captcha_token": "",
        "meta": {
            "captcha_sign": drive_sign(WEB_CLIENT_ID, device_id, timestamp),
            "client_version": WEB_CLIENT_VERSION,
            "package_name": WEB_PACKAGE_NAME,
            "user_id": sub,
            "timestamp": timestamp,
        },
    }
    headers = {
        "X-Client-ID": WEB_CLIENT_ID,
        "X-Device-ID": device_id,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Content-Type": "application/json",
    }
    last_error = ""
    for attempt in range(3):
        try:
            response = await http.post(
                f"{user_base_url.rstrip('/')}/v1/shield/captcha/init",
                json=body,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}".strip()
            if attempt == 2:
                raise RuntimeError(f"captcha init failed: {last_error}") from exc
            await asyncio.sleep(1.0 + attempt)
            continue
        if response.status_code == 429 or response.status_code >= 500:
            last_error = f"{response.status_code} {response.text[:200]}"
            if attempt == 2:
                raise RuntimeError(f"captcha init failed: {last_error}")
            await asyncio.sleep(1.0 + attempt)
            continue
        if response.status_code >= 400:
            raise RuntimeError(
                f"captcha init failed: {response.status_code} {response.text[:200]}"
            )
        data = response.json()
        break
    else:
        raise RuntimeError(f"captcha init failed: {last_error}")
    token = data.get("captcha_token") or data.get("token")
    if not token:
        raise RuntimeError("captcha init returned no token")
    expires_in = float(data.get("expires_in") or 300)
    return str(token), time.time() + max(30.0, expires_in - 30.0)
