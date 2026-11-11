from __future__ import annotations

import base64
import json
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_session_factory
from ..models import Account, AccountState, DownloadTask, TaskStatus, utcnow
from ..pikpak.client import AccountCredentials, AccountPikPakClient
from ..pikpak.errors import PikPakError, Unauthorized
from ..security import decrypt, encrypt


class AccountUnavailable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _jwt_sub(token: str) -> str:
    parts = token.split(".")
    if len(parts) < 2:
        raise Unauthorized("access token is not a JWT", code="ACCOUNT_TOKEN_INVALID")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, json.JSONDecodeError) as exc:
        raise Unauthorized("access token payload invalid", code="ACCOUNT_TOKEN_INVALID") from exc
    sub = data.get("sub")
    if not sub:
        raise Unauthorized("access token has no sub", code="ACCOUNT_TOKEN_INVALID")
    return str(sub)


def local_today() -> str:
    settings = get_settings()
    try:
        tz = ZoneInfo(settings.account_daily_reset_tz)
    except Exception:
        tz = timezone.utc
    return datetime.now(tz).date().isoformat()


def _ensure_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def make_token_callback(account_id: str):
    async def _persist(access: str, refresh: str, expires_at: datetime) -> None:
        factory = get_session_factory()
        async with factory() as session:
            await session.execute(
                update(Account)
                .where(Account.id == account_id)
                .values(
                    access_token_enc=encrypt(access),
                    refresh_token_enc=encrypt(refresh),
                    token_expires_at=expires_at,
                    updated_at=utcnow(),
                )
            )
            await session.commit()

    return _persist


def make_client(
    account: Account,
    *,
    http: httpx.AsyncClient | None = None,
) -> AccountPikPakClient:
    creds = AccountCredentials(
        id=account.id,
        sub=account.sub or "",
        device_id=account.device_id,
        access_token=decrypt(account.access_token_enc),
        refresh_token=decrypt(account.refresh_token_enc) or "",
        token_expires_at=account.token_expires_at,
    )
    return AccountPikPakClient(creds, http=http, on_tokens=make_token_callback(account.id))


async def import_account(
    session: AsyncSession,
    *,
    label: str,
    email: str | None,
    refresh_token: str,
) -> Account:
    device_id = secrets.token_hex(16)
    creds = AccountCredentials(
        id="",
        sub="",
        device_id=device_id,
        access_token=None,
        refresh_token=refresh_token,
    )
    client = AccountPikPakClient(creds)
    try:
        await client.refresh_token()
    except PikPakError as exc:
        raise Unauthorized(
            "refresh token 无效或已失效", code="ACCOUNT_TOKEN_INVALID", reason=str(exc)
        ) from exc
    finally:
        await client.close()

    access = creds.access_token or ""
    sub = _jwt_sub(access)
    expires_at = creds.token_expires_at
    existing = (
        await session.execute(select(Account).where(Account.sub == sub))
    ).scalar_one_or_none()
    now = utcnow()
    if existing is not None:
        existing.label = label
        existing.email = email
        existing.refresh_token_enc = encrypt(creds.refresh_token)
        existing.access_token_enc = encrypt(access)
        existing.token_expires_at = expires_at
        existing.state = AccountState.ACTIVE
        existing.cooldown_until = None
        existing.last_error = None
        existing.updated_at = now
        account = existing
    else:
        account = Account(
            label=label,
            email=email,
            sub=sub,
            device_id=device_id,
            access_token_enc=encrypt(access),
            refresh_token_enc=encrypt(creds.refresh_token),
            token_expires_at=expires_at,
            state=AccountState.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        session.add(account)
    await session.flush()
    return account


async def reset_daily_counters(session: AsyncSession) -> None:
    today = local_today()
    pending = (
        await session.execute(
            select(Account.id)
            .where(
                or_(
                    Account.daily_date.is_(None),
                    Account.daily_date != today,
                )
            )
            .limit(1)
        )
    ).first()
    if pending is None:
        return
    await session.execute(
        update(Account)
        .where(
            or_(
                Account.daily_date.is_(None),
                Account.daily_date != today,
            )
        )
        .values(daily_date=today, daily_restore_count=0)
    )


async def recover_cooldowns(session: AsyncSession) -> None:
    now = utcnow()
    pending = (
        await session.execute(
            select(Account.id)
            .where(
                Account.state == AccountState.COOLDOWN,
                Account.cooldown_until <= now,
            )
            .limit(1)
        )
    ).first()
    if pending is None:
        return
    await session.execute(
        update(Account)
        .where(Account.state == AccountState.COOLDOWN, Account.cooldown_until <= now)
        .values(state=AccountState.ACTIVE, cooldown_until=None, updated_at=now)
    )



async def recover_stale_leases(session: AsyncSession) -> None:
    rows = (
        await session.execute(
            select(Account.id, Account.lease_task_id).where(
                Account.lease_task_id.is_not(None)
            )
        )
    ).all()
    stale: list[tuple[str, str]] = []
    for account_id, task_id in rows:
        if not task_id:
            stale.append((account_id, task_id or ""))
            continue
        task = await session.get(DownloadTask, task_id)
        if (
            task is None
            or task.status in TaskStatus.TERMINAL
            or task.status
            in (
                TaskStatus.PAUSED,
                TaskStatus.ACCOUNT_QUEUED,
                TaskStatus.WCL_READY,
                TaskStatus.VERIFYING,
            )
        ):
            stale.append((account_id, task_id))
    for account_id, task_id in stale:
        await session.execute(
            update(Account)
            .where(Account.id == account_id, Account.lease_task_id == task_id)
            .values(lease_task_id=None, lease_expires_at=None, updated_at=utcnow())
        )
    if stale:
        await session.flush()


async def claim_account(
    session: AsyncSession,
    *,
    task_id: str,
    preferred_account_id: str | None = None,
) -> Account | None:
    settings = get_settings()
    await reset_daily_counters(session)
    await recover_cooldowns(session)
    await recover_stale_leases(session)
    now = utcnow()

    if preferred_account_id:
        result = await session.execute(
            update(Account)
            .where(
                Account.id == preferred_account_id,
                Account.state.in_([AccountState.ACTIVE, AccountState.DISABLED]),
                (Account.lease_task_id.is_(None))
                | (Account.lease_task_id == task_id)
                | (Account.lease_expires_at < now),
            )
            .values(
                lease_task_id=task_id,
                lease_expires_at=now + timedelta(seconds=settings.account_lease_seconds),
                last_used_at=now,
                updated_at=now,
            )
        )
        if result.rowcount:
            await session.flush()
            return await session.get(Account, preferred_account_id)

    candidates = (
        await session.execute(
            select(Account.id)
            .where(
                Account.state == AccountState.ACTIVE,
                (Account.cooldown_until.is_(None)) | (Account.cooldown_until < now),
                (Account.lease_task_id.is_(None))
                | (Account.lease_task_id == task_id)
                | (Account.lease_expires_at < now),
            )
            .order_by(Account.last_used_at.is_(None).desc(), Account.last_used_at.asc(), Account.id.asc())
            .limit(20)
        )
    ).scalars().all()
    for account_id in candidates:
        result = await session.execute(
            update(Account)
            .where(
                Account.id == account_id,
                Account.state == AccountState.ACTIVE,
                (Account.lease_task_id.is_(None))
                | (Account.lease_task_id == task_id)
                | (Account.lease_expires_at < now),
            )
            .values(
                lease_task_id=task_id,
                lease_expires_at=now + timedelta(seconds=settings.account_lease_seconds),
                last_used_at=now,
                updated_at=now,
            )
        )
        if result.rowcount:
            await session.flush()
            return await session.get(Account, account_id)
    return None


async def release_account(session: AsyncSession, account_id: str | None, task_id: str) -> None:
    if not account_id:
        return
    await session.execute(
        update(Account)
        .where(Account.id == account_id, Account.lease_task_id == task_id)
        .values(lease_task_id=None, lease_expires_at=None, updated_at=utcnow())
    )


async def renew_lease(session: AsyncSession, account_id: str | None, task_id: str) -> None:
    if not account_id:
        return
    settings = get_settings()
    now = utcnow()
    await session.execute(
        update(Account)
        .where(Account.id == account_id, Account.lease_task_id == task_id)
        .values(
            lease_expires_at=now + timedelta(seconds=settings.account_lease_seconds),
            updated_at=now,
        )
    )


async def mark_cooldown(
    session: AsyncSession, account: Account, *, seconds: int | None = None, error: str | None = None
) -> None:
    settings = get_settings()
    now = utcnow()
    account.state = AccountState.COOLDOWN
    account.cooldown_until = now + timedelta(seconds=seconds or settings.account_cooldown_seconds)
    account.last_error = error
    account.lease_task_id = None
    account.lease_expires_at = None
    account.updated_at = now


async def mark_invalid(session: AsyncSession, account: Account, error: str) -> None:
    account.state = AccountState.INVALID
    account.last_error = error
    account.lease_task_id = None
    account.lease_expires_at = None
    account.updated_at = utcnow()


async def increment_restore_count(session: AsyncSession, account: Account) -> None:
    today = local_today()
    if account.daily_date != today:
        account.daily_date = today
        account.daily_restore_count = 0
    account.daily_restore_count += 1
    account.updated_at = utcnow()


async def update_quota_from_about(account: Account, about: dict) -> None:
    quota = about.get("quota") or {}
    if "limit" in quota:
        try:
            account.quota_limit = int(quota["limit"])
        except (TypeError, ValueError):
            pass
    if "usage" in quota:
        try:
            account.quota_usage = int(quota["usage"])
        except (TypeError, ValueError):
            pass
    cloud = about.get("cloud_download") or {}
    if "limit" in cloud:
        try:
            account.cloud_download_limit = int(cloud["limit"])
        except (TypeError, ValueError):
            pass
    if "usage" in cloud:
        try:
            account.cloud_download_used = int(cloud["usage"])
        except (TypeError, ValueError):
            pass


def quota_available(account: Account, size: int) -> bool:
    if account.quota_limit is None or account.quota_usage is None:
        return True
    needed = int(size * 1.05)
    return account.quota_limit - account.quota_usage >= needed


async def account_state_summary(session: AsyncSession) -> str:
    rows = (
        await session.execute(select(Account.state).where(Account.state != AccountState.DISABLED))
    ).scalars().all()
    if not rows:
        return "NO_ACCOUNT"
    if all(state in (AccountState.COOLDOWN, AccountState.INVALID) for state in rows):
        return "ALL_COOLDOWN"
    active = [state for state in rows if state == AccountState.ACTIVE]
    if not active:
        return "NO_ACCOUNT"
    leased = int(
        (
            await session.execute(
                select(func.count(Account.id)).where(
                    Account.state == AccountState.ACTIVE,
                    Account.lease_task_id.is_not(None),
                )
            )
        ).scalar_one()
    )
    if leased >= len(active):
        return "ALL_LEASED"
    return "ACCOUNT_BUSY"
