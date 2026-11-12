from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Account, AccountState, CleanupState, CleanupTask, utcnow
from ..pikpak.errors import NotFound, PikPakError, Unauthorized
from .accounts import make_client, mark_invalid, release_account
from .remote_files import delete_folder_tree, delete_remote_file

MAX_CLEANUP_ATTEMPTS = 10


async def enqueue_cleanup(
    session: AsyncSession,
    *,
    account_id: str,
    drive_file_id: str,
    task_id: str | None,
    error: str | None,
) -> CleanupTask:
    existing = (
        await session.execute(
            select(CleanupTask).where(
                CleanupTask.account_id == account_id,
                CleanupTask.drive_file_id == drive_file_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.state != CleanupState.DONE:
            existing.state = CleanupState.RETRYING
            existing.last_error = error
            existing.next_attempt_at = utcnow()
            existing.task_id = task_id or existing.task_id
        return existing
    cleanup = CleanupTask(
        account_id=account_id,
        drive_file_id=drive_file_id,
        task_id=task_id,
        state=CleanupState.PENDING,
        last_error=error,
        next_attempt_at=utcnow(),
    )
    session.add(cleanup)
    await session.flush()
    return cleanup


async def _claim_account_for_cleanup(
    session: AsyncSession, account_id: str, cleanup_id: str
) -> bool:
    now = utcnow()
    result = await session.execute(
        update(Account)
        .where(
            Account.id == account_id,
            Account.state.in_([AccountState.ACTIVE, AccountState.DISABLED]),
            (Account.lease_task_id.is_(None)) | (Account.lease_expires_at < now),
            (Account.cooldown_until.is_(None)) | (Account.cooldown_until < now),
        )
        .values(
            lease_task_id=cleanup_id,
            lease_expires_at=now + timedelta(minutes=10),
            last_used_at=now,
            updated_at=now,
        )
    )
    return bool(result.rowcount)


async def dispatch_cleanups(session: AsyncSession, limit: int = 10) -> int:
    now = utcnow()
    rows = (
        await session.execute(
            select(CleanupTask)
            .where(
                CleanupTask.state.in_([CleanupState.PENDING, CleanupState.RETRYING]),
                (CleanupTask.next_attempt_at.is_(None)) | (CleanupTask.next_attempt_at <= now),
            )
            .order_by(CleanupTask.created_at.asc())
            .limit(limit)
        )
    ).scalars().all()
    processed = 0
    for cleanup in rows:
        account = await session.get(Account, cleanup.account_id)
        if account is None:
            cleanup.state = CleanupState.FAILED
            cleanup.last_error = "account deleted"
            await session.commit()
            continue
        if account.state == AccountState.INVALID:
            cleanup.last_error = "account invalid; cleanup blocked"
            await session.commit()
            continue
        if account.state == AccountState.COOLDOWN and account.cooldown_until:
            until = account.cooldown_until
            if until.tzinfo is None:
                until = until.replace(tzinfo=now.tzinfo)
            if until > now:
                continue
        if not await _claim_account_for_cleanup(session, account.id, cleanup.id):
            await session.commit()
            continue
        await session.commit()
        processed += 1

        client = make_client(account)
        try:
            is_folder = False
            try:
                node = await client.get_file(cleanup.drive_file_id)
                node = node.get("file") or node
                is_folder = str(node.get("kind") or "") == "drive#folder"
            except NotFound:
                is_folder = False
            except PikPakError:
                is_folder = False
            if is_folder:
                await delete_folder_tree(client, cleanup.drive_file_id)
            else:
                await delete_remote_file(client, cleanup.drive_file_id)
            cleanup.state = CleanupState.DONE
            cleanup.completed_at = utcnow()
            cleanup.last_error = None
            cleanup.attempts += 1
            await release_account(session, account.id, cleanup.id)
        except NotFound:
            cleanup.state = CleanupState.DONE
            cleanup.completed_at = utcnow()
            cleanup.last_error = None
            cleanup.attempts += 1
            await release_account(session, account.id, cleanup.id)
        except Unauthorized as exc:
            await mark_invalid(session, account, str(exc))
            cleanup.state = CleanupState.RETRYING
            cleanup.last_error = f"account invalid: {exc}"
            cleanup.attempts += 1
            cleanup.next_attempt_at = utcnow() + timedelta(hours=1)
        except PikPakError as exc:
            cleanup.attempts += 1
            cleanup.last_error = str(exc)
            if cleanup.attempts >= MAX_CLEANUP_ATTEMPTS:
                cleanup.state = CleanupState.FAILED
            else:
                cleanup.state = CleanupState.RETRYING
                delay = min(3600, 30 * (2 ** min(cleanup.attempts, 7)))
                cleanup.next_attempt_at = utcnow() + timedelta(seconds=delay)
            await release_account(session, account.id, cleanup.id)
        finally:
            await client.close()
        await session.commit()
    return processed
