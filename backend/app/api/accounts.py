from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import Account, AccountState, CleanupState, CleanupTask
from ..pikpak.errors import PikPakError, Unauthorized
from ..services import events
from ..services.accounts import (
    import_account,
    make_client,
    update_quota_from_about,
)
from ..services.cleanup import dispatch_cleanups
from .schemas import AccountCreate, AccountOut, CleanupOut

router = APIRouter(prefix="/api", tags=["accounts"])


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def account_out(account: Account) -> AccountOut:
    return AccountOut(
        id=account.id,
        label=account.label,
        email=account.email,
        sub=account.sub,
        device_id=account.device_id,
        state=account.state,
        quota_limit=account.quota_limit,
        quota_usage=account.quota_usage,
        cloud_download_limit=account.cloud_download_limit,
        cloud_download_used=account.cloud_download_used,
        cooldown_until=iso(account.cooldown_until),
        last_used_at=iso(account.last_used_at),
        daily_date=account.daily_date,
        daily_restore_count=account.daily_restore_count,
        lease_task_id=account.lease_task_id,
        lease_expires_at=iso(account.lease_expires_at),
        last_error=account.last_error,
        created_at=iso(account.created_at),
    )


def cleanup_out(cleanup: CleanupTask) -> CleanupOut:
    return CleanupOut(
        id=cleanup.id,
        account_id=cleanup.account_id,
        drive_file_id=cleanup.drive_file_id,
        task_id=cleanup.task_id,
        state=cleanup.state,
        attempts=cleanup.attempts,
        next_attempt_at=iso(cleanup.next_attempt_at),
        last_error=cleanup.last_error,
        created_at=iso(cleanup.created_at),
    )


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"error": {"code": code, "message": message, "retryable": False, "details": {}}},
    )


@router.get("/accounts", response_model=list[AccountOut])
async def list_accounts(session: AsyncSession = Depends(get_session)) -> list[AccountOut]:
    rows = (
        await session.execute(select(Account).order_by(Account.created_at.asc()))
    ).scalars().all()
    return [account_out(account) for account in rows]


@router.post("/accounts", response_model=AccountOut, status_code=201)
async def create_account(
    body: AccountCreate, session: AsyncSession = Depends(get_session)
) -> AccountOut:
    try:
        account = await import_account(
            session,
            label=body.label,
            email=body.email,
            refresh_token=body.refresh_token,
        )
    except Unauthorized as exc:
        raise _error(400, "ACCOUNT_TOKEN_INVALID", str(exc)) from exc
    events.emit(session, "account", account.id, "account.imported", {"label": account.label})
    await session.commit()
    return account_out(account)


@router.post("/accounts/{account_id}/refresh", response_model=AccountOut)
async def refresh_account(
    account_id: str, session: AsyncSession = Depends(get_session)
) -> AccountOut:
    account = await session.get(Account, account_id)
    if account is None:
        raise _error(404, "ACCOUNT_NOT_FOUND", "账号不存在")
    client = make_client(account)
    try:
        await client.refresh_token()
        about = await client.about()
        await update_quota_from_about(account, about)
        account.state = AccountState.ACTIVE
        account.last_error = None
    except Unauthorized as exc:
        account.state = AccountState.INVALID
        account.last_error = str(exc)
        await session.commit()
        raise _error(400, "ACCOUNT_TOKEN_INVALID", str(exc)) from exc
    except PikPakError as exc:
        await session.commit()
        raise _error(502, "ACCOUNT_REFRESH_FAILED", str(exc)) from exc
    finally:
        await client.close()
    await session.commit()
    return account_out(account)


@router.post("/accounts/{account_id}/enable", response_model=AccountOut)
async def enable_account(
    account_id: str, session: AsyncSession = Depends(get_session)
) -> AccountOut:
    account = await session.get(Account, account_id)
    if account is None:
        raise _error(404, "ACCOUNT_NOT_FOUND", "账号不存在")
    account.state = AccountState.ACTIVE
    account.cooldown_until = None
    account.last_error = None
    await session.commit()
    return account_out(account)


@router.post("/accounts/{account_id}/disable", response_model=AccountOut)
async def disable_account(
    account_id: str, session: AsyncSession = Depends(get_session)
) -> AccountOut:
    account = await session.get(Account, account_id)
    if account is None:
        raise _error(404, "ACCOUNT_NOT_FOUND", "账号不存在")
    account.state = AccountState.DISABLED
    await session.commit()
    return account_out(account)


@router.delete("/accounts/{account_id}", status_code=204)
async def delete_account(account_id: str, session: AsyncSession = Depends(get_session)) -> None:
    account = await session.get(Account, account_id)
    if account is None:
        raise _error(404, "ACCOUNT_NOT_FOUND", "账号不存在")
    if account.lease_task_id:
        raise _error(409, "ACCOUNT_LEASED", "账号正在执行任务，暂不可删除")
    pending = (
        await session.execute(
            select(CleanupTask.id).where(
                CleanupTask.account_id == account_id,
                CleanupTask.state.in_([CleanupState.PENDING, CleanupState.RETRYING, CleanupState.FAILED]),
            )
        )
    ).first()
    if pending is not None:
        raise _error(409, "ACCOUNT_HAS_CLEANUP", "账号仍有云盘清理任务")
    await session.delete(account)
    await session.commit()


@router.get("/cleanups", response_model=list[CleanupOut])
async def list_cleanups(
    state: str | None = None,
    limit: int = Query(200, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> list[CleanupOut]:
    stmt = select(CleanupTask).order_by(CleanupTask.created_at.desc()).limit(limit)
    if state:
        stmt = stmt.where(CleanupTask.state == state)
    rows = (await session.execute(stmt)).scalars().all()
    return [cleanup_out(row) for row in rows]


@router.post("/cleanups/dispatch", response_model=dict)
async def run_cleanups(session: AsyncSession = Depends(get_session)) -> dict:
    processed = await dispatch_cleanups(session)
    return {"processed": processed}
