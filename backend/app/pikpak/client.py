from __future__ import annotations

import asyncio
import json
import random
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from aiolimiter import AsyncLimiter

from ..config import get_settings
from .captcha import mint_drive_captcha
from .constants import (
    ANDROID_CLIENT_ID,
    ANDROID_CLIENT_VERSION,
    ANDROID_PACKAGE_NAME,
    ANDROID_USER_AGENT,
    WEB_CLIENT_ID,
    WEB_CLIENT_VERSION,
    WEB_PACKAGE_NAME,
    WEB_USER_AGENT,
)
from .errors import (
    CaptchaFailed,
    NotFound,
    PermissionDenied,
    PikPakError,
    RateLimited,
    ShareUnavailable,
    Unauthorized,
)
from .limiters import anon_limiter


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class FileNode:
    id: str
    name: str = ""
    kind: str = "drive#file"
    size: int = 0
    hash: str = ""
    mime_type: str | None = None
    parent_id: str | None = None
    user_id: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    medias: list[dict[str, Any]] = field(default_factory=list)
    web_content_link: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    share_id: str | None = None
    share_status: str | None = None
    share_status_text: str | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any], *, share_id: str | None = None) -> "FileNode":
        params = data.get("params") or {}
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except json.JSONDecodeError:
                params = {}
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or ""),
            kind=str(data.get("kind") or "drive#file"),
            size=_as_int(data.get("size")),
            hash=str(data.get("hash") or ""),
            mime_type=data.get("mime_type"),
            parent_id=data.get("parent_id"),
            user_id=data.get("user_id"),
            params=params,
            medias=data.get("medias") or [],
            web_content_link=data.get("web_content_link"),
            raw=data,
            share_id=share_id,
        )

    @property
    def is_folder(self) -> bool:
        return self.kind == "drive#folder"

    @property
    def is_video(self) -> bool:
        return bool(self.mime_type and self.mime_type.startswith("video/"))

    @property
    def anonymous_play_range(self) -> float | None:
        return _as_float(self.params.get("anonymous_play_range"))

    @property
    def anonymous_play_seconds(self) -> int | None:
        value = self.params.get("anonymous_play_seconds")
        return _as_int(value) if value is not None else None

    @property
    def original_share_id(self) -> str:
        return str(self.params.get("original_share_id") or "")

    @property
    def original_file_id(self) -> str:
        return str(self.params.get("original_file_id") or "")

    @property
    def platform(self) -> str:
        return str(self.params.get("platform") or "")

    @property
    def platform_icon(self) -> str:
        return str(self.params.get("platform_icon") or "")

    @property
    def global_file_kind(self) -> int:
        return _as_int(self.params.get("global_file_kind"), 0)

    def original_media(self) -> dict[str, Any] | None:
        for media in self.medias:
            if not media.get("is_origin"):
                continue
            if media.get("category") != "category_origin":
                continue
            link = media.get("link") or {}
            url = link.get("url") or ""
            query = _query(url)
            if query.get("t") == "0" and _as_int(query.get("f"), -1) == self.size:
                return media
        return None

    def original_media_url(self) -> str | None:
        media = self.original_media()
        if media:
            return str((media.get("link") or {}).get("url") or "") or None
        return None


def _query(url: str) -> dict[str, str]:
    parsed = urlparse(url)
    out: dict[str, str] = {}
    if not parsed.query:
        return out
    for pair in parsed.query.split("&"):
        if "=" in pair:
            key, value = pair.split("=", 1)
            out[key] = value
    return out


@dataclass
class ShareSnapshot:
    share_id: str
    share_status: str = "OK"
    share_status_text: str = ""
    title: str = ""
    file_num: str = ""
    user_id: str | None = None
    anonymous_play_seconds: int | None = None
    files: list[FileNode] = field(default_factory=list)
    pass_code_token: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.share_status == "OK"


@dataclass
class FolderPage:
    files: list[FileNode]
    next_page_token: str = ""


@dataclass
class AccountCredentials:
    id: str
    sub: str
    device_id: str
    access_token: str | None
    refresh_token: str
    token_expires_at: datetime | None = None


TokenCallback = Callable[[str, str, datetime], Awaitable[None]]


class _BaseClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        http: httpx.AsyncClient | None = None,
        limiter: AsyncLimiter | None = None,
    ) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.pikpak_drive_base_url).rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(60.0))
        self._owns_http = http is None
        self._limiter = limiter

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _acquire(self) -> None:
        limiter = self._limiter or await anon_limiter()
        await limiter.acquire()

    async def _raw_request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        attempts: int = 3,
    ) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(attempts):
            await self._acquire()
            try:
                response = await self._http.request(
                    method, url, headers=headers, params=params, json=json_body
                )
            except httpx.RequestError as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep(min(8.0, 2**attempt) + random.random() * 0.5)
                    continue
                raise PikPakError(
                    f"network error: {exc.__class__.__name__}",
                    code="NETWORK_ERROR",
                    retryable=True,
                ) from exc
            if response.status_code >= 500 and attempt + 1 < attempts:
                last_error = PikPakError(
                    f"upstream {response.status_code}", status=response.status_code, retryable=True
                )
                await asyncio.sleep(min(8.0, 2**attempt) + random.random() * 0.5)
                continue
            return response
        raise PikPakError(
            "upstream request failed", code="UPSTREAM_ERROR", retryable=True
        ) from last_error


def _error_payload(response: httpx.Response) -> tuple[str, str, int | None]:
    data: dict[str, Any] = {}
    try:
        parsed = response.json()
        if isinstance(parsed, dict):
            data = parsed
    except ValueError:
        pass
    code = str(data.get("error") or data.get("error_code") or response.status_code)
    reason = str(
        data.get("error_description")
        or data.get("error")
        or response.text[:300]
        or response.reason_phrase
    )
    error_code = data.get("error_code")
    try:
        error_code_int = int(error_code) if error_code is not None else None
    except (TypeError, ValueError):
        error_code_int = None
    return code, reason, error_code_int


def raise_for_response(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    code, reason, error_code = _error_payload(response)
    status = response.status_code
    kwargs = {"status": status, "reason": reason}
    if status == 429 or error_code == 4002 or "too frequent" in reason.lower():
        raise RateLimited(reason, **kwargs)
    if status == 401:
        raise Unauthorized(reason, **kwargs)
    if status == 403:
        raise PermissionDenied(reason, **kwargs)
    if status == 404:
        raise NotFound(reason, **kwargs)
    if status == 416:
        raise PikPakError(
            "range not satisfiable", code="RANGE_NOT_SATISFIABLE", status=status, retryable=False
        )
    if error_code == 9 or "captcha" in reason.lower():
        raise CaptchaFailed(reason, status=status, reason=reason)
    raise PikPakError(
        reason,
        code=code,
        status=status,
        retryable=status >= 500,
    )


class AnonymousPikPakClient(_BaseClient):
    def __init__(
        self,
        *,
        base_url: str | None = None,
        http: httpx.AsyncClient | None = None,
        limiter: AsyncLimiter | None = None,
        device_id: str | None = None,
    ) -> None:
        super().__init__(base_url=base_url, http=http, limiter=limiter)
        self.device_id = device_id or secrets.token_hex(16)

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": ANDROID_USER_AGENT,
            "Referer": "https://mypikpak.com/",
            "X-Client-ID": ANDROID_CLIENT_ID,
            "X-Device-ID": self.device_id,
            "X-Client-Version": ANDROID_CLIENT_VERSION,
            "X-Package-Name": ANDROID_PACKAGE_NAME,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
        }

    async def get_share(self, share_id: str, pass_code: str | None = None) -> ShareSnapshot:
        params: dict[str, Any] = {
            "share_id": share_id,
            "limit": 100,
            "thumbnail_size": "SIZE_LARGE",
        }
        if pass_code:
            params["pass_code"] = pass_code
        response = await self._raw_request(
            "GET", f"{self.base_url}/drive/v1/share", headers=self._headers(), params=params
        )
        raise_for_response(response)
        data = response.json()
        params_obj = data.get("params") or {}
        seconds = params_obj.get("anonymous_play_seconds")
        return ShareSnapshot(
            share_id=share_id,
            share_status=str(data.get("share_status") or "OK"),
            share_status_text=str(data.get("share_status_text") or ""),
            title=str(data.get("title") or ""),
            file_num=str(data.get("file_num") or ""),
            user_id=(data.get("user_info") or {}).get("user_id"),
            anonymous_play_seconds=_as_int(seconds) if seconds is not None else None,
            files=[
                FileNode.from_json(item, share_id=share_id) for item in (data.get("files") or [])
            ],
            pass_code_token=str(data.get("pass_code_token") or ""),
            raw=data,
        )

    async def file_info(
        self, share_id: str, file_id: str, pass_code_token: str | None = None
    ) -> FileNode:
        params: dict[str, Any] = {"share_id": share_id, "file_id": file_id}
        if pass_code_token is not None:
            params["pass_code_token"] = pass_code_token
        response = await self._raw_request(
            "GET",
            f"{self.base_url}/drive/v1/share/file_info",
            headers=self._headers(),
            params=params,
        )
        raise_for_response(response)
        data = response.json()
        status = str(data.get("share_status") or "OK")
        if status != "OK":
            raise ShareUnavailable(
                status, str(data.get("share_status_text") or ""), details={"share_id": share_id}
            )
        info = data.get("file_info")
        if not info:
            raise NotFound("share returned no file_info", details={"share_id": share_id, "file_id": file_id})
        node = FileNode.from_json(info, share_id=share_id)
        node.share_status = status
        node.share_status_text = str(data.get("share_status_text") or "")
        return node

    async def list_folder(
        self,
        share_id: str,
        parent_id: str,
        pass_code_token: str | None = None,
        page_token: str | None = None,
    ) -> FolderPage:
        params: dict[str, Any] = {
            "share_id": share_id,
            "parent_id": parent_id,
            "limit": 100,
        }
        if pass_code_token is not None:
            params["pass_code_token"] = pass_code_token
        if page_token:
            params["page_token"] = page_token
        response = await self._raw_request(
            "GET",
            f"{self.base_url}/drive/v1/share/detail",
            headers=self._headers(),
            params=params,
        )
        raise_for_response(response)
        data = response.json()
        return FolderPage(
            files=[
                FileNode.from_json(item, share_id=share_id) for item in (data.get("files") or [])
            ],
            next_page_token=str(data.get("next_page_token") or ""),
        )

    async def batch_file_info(
        self, share_id: str, file_ids: list[str], pass_code_token: str | None = None
    ) -> list[FileNode]:
        body: dict[str, Any] = {"share_id": share_id, "file_ids": file_ids}
        if pass_code_token is not None:
            body["pass_code_token"] = pass_code_token
        response = await self._raw_request(
            "POST",
            f"{self.base_url}/drive/v1/share:batch_file_info",
            headers=self._headers(),
            params={"unzip_password": ""},
            json_body=body,
        )
        raise_for_response(response)
        data = response.json()
        if isinstance(data, list):
            items = data
        else:
            items = data.get("files") or []
        return [FileNode.from_json(item, share_id=share_id) for item in items]


class AccountPikPakClient(_BaseClient):
    def __init__(
        self,
        credentials: AccountCredentials,
        *,
        drive_base_url: str | None = None,
        user_base_url: str | None = None,
        http: httpx.AsyncClient | None = None,
        limiter: AsyncLimiter | None = None,
        on_tokens: TokenCallback | None = None,
    ) -> None:
        settings = get_settings()
        super().__init__(base_url=drive_base_url or settings.pikpak_drive_base_url, http=http, limiter=limiter)
        self.creds = credentials
        self.user_base_url = (user_base_url or settings.pikpak_user_base_url).rstrip("/")
        self.on_tokens = on_tokens
        self._captcha_cache: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    def _base_headers(self) -> dict[str, str]:
        return {
            "User-Agent": WEB_USER_AGENT,
            "Referer": "https://mypikpak.com/",
            "Origin": "https://mypikpak.com",
            "X-Client-ID": WEB_CLIENT_ID,
            "X-Device-ID": self.creds.device_id,
            "X-Client-Version": WEB_CLIENT_VERSION,
            "X-Package-Name": WEB_PACKAGE_NAME,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
        }

    async def _persist_tokens(self, access: str, refresh: str, expires_at: datetime) -> None:
        self.creds.access_token = access
        self.creds.refresh_token = refresh
        self.creds.token_expires_at = expires_at
        if self.on_tokens is not None:
            await self.on_tokens(access, refresh, expires_at)

    async def refresh_token(self) -> None:
        body = {
            "client_id": WEB_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": self.creds.refresh_token,
        }
        response = await self._raw_request(
            "POST",
            f"{self.user_base_url}/v1/auth/token",
            headers=self._base_headers(),
            params={"client_id": WEB_CLIENT_ID},
            json_body=body,
            attempts=2,
        )
        if response.status_code >= 400:
            code, reason, error_code = _error_payload(response)
            raise Unauthorized(
                reason or "refresh token invalid",
                code=code or "ACCOUNT_TOKEN_INVALID",
                status=response.status_code,
                reason=reason,
                details={"error_code": error_code},
            )
        data = response.json()
        access = data.get("access_token")
        refresh = data.get("refresh_token") or self.creds.refresh_token
        if not access:
            raise Unauthorized("refresh returned no access token", code="ACCOUNT_TOKEN_INVALID")
        expires_in = _as_int(data.get("expires_in"), 7200)
        expires_at = datetime.fromtimestamp(time.time() + expires_in, tz=timezone.utc)
        await self._persist_tokens(str(access), str(refresh), expires_at)

    async def ensure_access_token(self) -> None:
        expires = self.creds.token_expires_at
        if not self.creds.access_token:
            await self.refresh_token()
            return
        if expires is None:
            await self.refresh_token()
            return
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires.timestamp() - time.time() < 600:
            await self.refresh_token()

    async def _captcha(self, action: str, *, force: bool = False) -> str:
        cached = self._captcha_cache.get(action)
        if cached and not force and cached[1] > time.time():
            return cached[0]
        try:
            token, expires_at = await mint_drive_captcha(
                self._http,
                self.user_base_url,
                device_id=self.creds.device_id,
                sub=self.creds.sub,
                action=action,
            )
        except (RuntimeError, httpx.HTTPError) as exc:
            raise CaptchaFailed(f"captcha init failed: {exc}") from exc
        self._captcha_cache[action] = (token, expires_at)
        return token

    async def _drive(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        allow_404: bool = False,
    ) -> httpx.Response:
        async with self._lock:
            await self.ensure_access_token()
            action = f"{method}:{path}"
            for attempt in range(2):
                captcha = await self._captcha(action, force=attempt > 0)
                headers = self._base_headers()
                headers["Authorization"] = f"Bearer {self.creds.access_token}"
                headers["X-Captcha-Token"] = captcha
                response = await self._raw_request(
                    method,
                    f"{self.base_url}{path}",
                    headers=headers,
                    params=params,
                    json_body=json_body,
                )
                if response.status_code == 401 and attempt == 0:
                    await self.refresh_token()
                    continue
                if response.status_code == 400:
                    _code, reason, error_code = _error_payload(response)
                    if error_code == 9 or "captcha" in reason.lower():
                        self._captcha_cache.pop(action, None)
                        if attempt == 0:
                            continue
                        raise CaptchaFailed(reason)
                if allow_404 and response.status_code == 404:
                    return response
                raise_for_response(response)
                return response
            raise PikPakError("drive request failed", code="DRIVE_REQUEST_FAILED", retryable=True)

    async def about(self) -> dict[str, Any]:
        response = await self._drive("GET", "/drive/v1/about")
        return response.json()

    async def restore(
        self, share_id: str, share_file_id: str, pass_code_token: str | None = None
    ) -> dict[str, Any]:
        body = {
            "share_id": share_id,
            "file_ids": [share_file_id],
            "pass_code_token": pass_code_token or "",
        }
        response = await self._drive("POST", "/drive/v1/share/restore", json_body=body)
        return response.json()

    async def get_restore_task(self, task_id: str) -> dict[str, Any]:
        response = await self._drive("GET", f"/drive/v1/tasks/{task_id}")
        return response.json()

    async def list_files(
        self, parent_id: str, page_token: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "parent_id": parent_id,
            "limit": 100,
            "with_audit": "true",
            "client_id": WEB_CLIENT_ID,
            "filters": "{}",
        }
        if page_token:
            params["page_token"] = page_token
        response = await self._drive("GET", "/drive/v1/files", params=params)
        return response.json()

    async def get_file(self, file_id: str, usage: str = "FETCH") -> dict[str, Any]:
        response = await self._drive(
            "GET",
            f"/drive/v1/files/{file_id}",
            params={"usage": usage, "client_id": WEB_CLIENT_ID},
        )
        return response.json()

    async def create_folder(
        self, name: str, parent_id: str | None = None
    ) -> dict[str, Any]:
        # PIKPAK root is the empty parent id; "*" is only valid for listing.
        response = await self._drive(
            "POST",
            "/drive/v1/files",
            json_body={
                "kind": "drive#folder",
                "parent_id": parent_id or "",
                "name": name,
            },
        )
        payload = response.json()
        # PIKPAK wraps the new folder in {"file": {...}}.
        return payload.get("file") or payload

    async def move_files(self, ids: list[str], parent_id: str) -> dict[str, Any]:
        response = await self._drive(
            "POST",
            "/drive/v1/files:batchMove",
            json_body={"ids": ids, "to": {"parent_id": parent_id}},
        )
        return response.json()

    async def pack_folder(self, folder_id: str) -> dict[str, Any]:
        response = await self._drive(
            "POST",
            "/drive/v1/files/pack",
            json_body={"folderId": folder_id},
        )
        return response.json()

    async def batch_untrash(self, ids: list[str]) -> dict[str, Any]:
        response = await self._drive(
            "POST",
            "/drive/v1/files:batchUntrash",
            json_body={"ids": ids},
        )
        return response.json()

    async def trash_files(self, file_ids: list[str]) -> int:
        if not file_ids:
            return 200
        response = await self._drive(
            "POST",
            "/drive/v1/files:batchTrash",
            json_body={"ids": file_ids},
        )
        return response.status_code

    async def delete_file(self, file_id: str) -> int:
        return await self.delete_files([file_id])

    async def delete_files(self, file_ids: list[str]) -> int:
        if not file_ids:
            return 200
        response = await self._drive(
            "POST",
            "/drive/v1/files:batchDelete",
            json_body={"ids": file_ids},
        )
        return response.status_code
