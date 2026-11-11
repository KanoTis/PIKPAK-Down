from __future__ import annotations

import asyncio
import os
import random
import shutil
from datetime import datetime, timezone
from pathlib import Path

import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..aria2 import Aria2Error, aria2
from ..config import get_settings
from ..db import get_session_factory
from ..models import (
    Account,
    DownloadFolder,
    DownloadJob,
    DownloadJobItem,
    DownloadTask,
    Event,
    FileObject,
    FolderStatus,
    JobStatus,
    TaskStatus,
    utcnow,
)
from ..pikpak.client import AnonymousPikPakClient, FileNode
from ..pikpak.errors import (
    NotFound,
    PikPakError,
    RateLimited,
    ShareUnavailable,
    Unauthorized,
)
from ..pikpak.limiters import window_limiter
from ..pikpak.urls import parse_share_input, validate_share_id
from ..security import decrypt, encrypt
from . import events
from .accounts import (
    account_state_summary,
    claim_account,
    increment_restore_count,
    make_client,
    mark_cooldown,
    mark_invalid,
    quota_available,
    release_account,
    renew_lease,
    update_quota_from_about,
)
from .cleanup import enqueue_cleanup
from .planner import DownloadPlan, anonymous_plan
from .verifier import extract_tar_member, verify_file
from .windows import probe_tail_exact, probe_with_retries

log = structlog.get_logger(__name__)

_dispatch_lock = asyncio.Lock()
_plan_claim_lock = asyncio.Lock()


class StateConflict(Exception):
    pass


class DownloadNotFound(Exception):
    pass


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _set_status(task: DownloadTask, status: str) -> None:
    task.status = status
    task.updated_at = utcnow()


def _safe_relative_path(value: str | None, fallback: str) -> str:
    text = (value or fallback or "").replace("\\", "/").strip()
    parts = [part for part in text.split("/") if part not in ("", ".", "..")]
    return "/".join(parts)[:1024]


def _safe_subdir(value: str | None) -> str:
    text = (value or "").replace("\\", "/").strip()
    parts = [part for part in text.split("/") if part not in ("", ".", "..")]
    return "/".join(parts)[:512]


def _join_relative(parent: str, child: str) -> str:
    parent = _safe_subdir(parent)
    child = _safe_relative_path(child, "")
    return f"{parent}/{child}".strip("/")


def _download_path(relative: str) -> Path:
    settings = get_settings()
    root = settings.downloads_dir.resolve()
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("下载路径越界")
    return candidate


def _download_relative(path: Path) -> str:
    root = get_settings().downloads_dir.resolve()
    return path.resolve().relative_to(root).as_posix()


def _unique_target(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(1, 1000):
        candidate = path.with_name(f"{stem} ({index}){suffix}")
        if not candidate.exists():
            return candidate
    raise ValueError("目标文件重名过多")


async def create_job(
    session: AsyncSession,
    *,
    input_url: str,
    pass_code: str | None,
    items: list[dict] | None = None,
    target_subdir: str | None = None,
) -> DownloadJob:
    parsed = parse_share_input(input_url, pass_code)
    input_display = parsed.source_url or input_url
    if parsed.source_url:
        input_display = f"https://mypikpak.com/s/{parsed.share_id}"
    selection: list[dict] = []
    seen: set[str] = set()
    for raw in items or []:
        file_id = str(raw.get("file_id") or "").strip()
        if not file_id:
            continue
        validate_share_id(file_id)
        if file_id in seen:
            continue
        seen.add(file_id)
        kind = str(raw.get("kind") or "drive#file")
        if kind not in ("drive#file", "drive#folder"):
            kind = "drive#file"
        name = str(raw.get("name") or "").strip()[:512]
        selection.append(
            {
                "file_id": file_id,
                "kind": kind,
                "name": name,
                "relative_path": _safe_relative_path(raw.get("relative_path"), name),
            }
        )
    if len(selection) > 1000:
        raise ValueError("一次最多选择 1000 项")
    normalized_target = _safe_subdir(target_subdir)
    existing: DownloadJob | None = None
    if selection:
        existing = (
            await session.execute(
                select(DownloadJob)
                .where(
                    DownloadJob.share_id == parsed.share_id,
                    DownloadJob.target_subdir == normalized_target,
                    DownloadJob.status != JobStatus.CANCELLED,
                )
                .order_by(DownloadJob.updated_at.desc(), DownloadJob.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    if existing is not None:
        job = existing
        job.input_url = input_display
        if parsed.pass_code:
            job.pass_code_enc = encrypt(parsed.pass_code)
        existing_ids = set(
            (
                await session.execute(
                    select(DownloadJobItem.share_file_id).where(
                        DownloadJobItem.job_id == job.id
                    )
                )
            ).scalars().all()
        )
        next_index = int(
            (
                await session.execute(
                    select(func.max(DownloadJobItem.sort_index)).where(
                        DownloadJobItem.job_id == job.id
                    )
                )
            ).scalar_one()
            or -1
        ) + 1
        added = 0
        for item in selection:
            if item["file_id"] in existing_ids:
                continue
            session.add(
                DownloadJobItem(
                    job_id=job.id,
                    share_file_id=item["file_id"],
                    kind=item["kind"],
                    name=item["name"],
                    relative_path=item["relative_path"],
                    sort_index=next_index,
                )
            )
            existing_ids.add(item["file_id"])
            next_index += 1
            added += 1
        if not added:
            return job
        job.root_kind = "selection"
        job.root_file_id = job.root_file_id or selection[0]["file_id"]
        total_items = len(existing_ids)
        job.root_name = f"已选择 {total_items} 项"
        if job.status in JobStatus.TERMINAL or job.status == JobStatus.READY:
            job.status = JobStatus.ENUMERATING
            job.finished_at = None
            job.error_code = None
            job.error_message = None
        await session.flush()
        events.emit(
            session,
            "download_job",
            job.id,
            "download_job.merged",
            {"share_id": job.share_id, "added": added, "selected": total_items},
        )
        return job

    job = DownloadJob(
        input_url=input_display,
        share_id=parsed.share_id,
        pass_code_enc=encrypt(parsed.pass_code),
        status=JobStatus.ENUMERATING,
        target_subdir=normalized_target,
    )
    if selection:
        job.root_kind = "selection"
        job.root_file_id = selection[0]["file_id"]
        job.root_name = f"已选择 {len(selection)} 项"
    session.add(job)
    await session.flush()
    for index, item in enumerate(selection):
        session.add(
            DownloadJobItem(
                job_id=job.id,
                share_file_id=item["file_id"],
                kind=item["kind"],
                name=item["name"],
                relative_path=item["relative_path"],
                sort_index=index,
            )
        )
    await session.flush()
    events.emit(
        session,
        "download_job",
        job.id,
        "download_job.created",
        {
            "share_id": job.share_id,
            "status": job.status,
            "selected": len(selection),
        },
    )
    return job


async def _get_job(session: AsyncSession, job_id: str) -> DownloadJob:
    job = await session.get(DownloadJob, job_id)
    if job is None:
        raise DownloadNotFound("下载任务不存在")
    return job


async def _get_task(session: AsyncSession, task_id: str) -> DownloadTask:
    task = await session.get(DownloadTask, task_id)
    if task is None:
        raise DownloadNotFound("文件任务不存在")
    return task


async def _refresh_job(session: AsyncSession, job: DownloadJob) -> None:
    tasks = (
        await session.execute(select(DownloadTask).where(DownloadTask.job_id == job.id))
    ).scalars().all()
    job.total_files = len(tasks)
    job.completed_files = sum(1 for task in tasks if task.status == TaskStatus.DONE)
    job.failed_files = sum(1 for task in tasks if task.status == TaskStatus.FAILED)
    job.total_bytes = sum(int(task.size or 0) for task in tasks)
    job.downloaded_bytes = sum(int(task.bytes_done or 0) for task in tasks)
    job.folder_total = int(
        (
            await session.execute(
                select(func.count(DownloadFolder.id)).where(DownloadFolder.job_id == job.id)
            )
        ).scalar_one()
    )
    job.folder_done = int(
        (
            await session.execute(
                select(func.count(DownloadFolder.id)).where(
                    DownloadFolder.job_id == job.id,
                    DownloadFolder.status == FolderStatus.DONE,
                )
            )
        ).scalar_one()
    )
    job.updated_at = utcnow()
    if job.status not in JobStatus.TERMINAL and job.status != JobStatus.ENUMERATING:
        active = [task for task in tasks if task.status not in TaskStatus.TERMINAL]
        if not active:
            if tasks and all(task.status == TaskStatus.CANCELLED for task in tasks):
                job.status = JobStatus.CANCELLED
            elif job.failed_files:
                job.status = JobStatus.COMPLETED_WITH_ERRORS
            else:
                job.status = JobStatus.COMPLETED
            job.finished_at = utcnow()
            events.emit(
                session,
                "download_job",
                job.id,
                "download_job.finished",
                {"status": job.status, "failed": job.failed_files},
            )


async def _upsert_task(
    session: AsyncSession,
    job: DownloadJob,
    node: FileNode,
    *,
    relative_path: str,
    sort_index: int,
) -> DownloadTask:
    existing = (
        await session.execute(
            select(DownloadTask).where(
                DownloadTask.job_id == job.id,
                DownloadTask.share_file_id == node.id,
            )
        )
    ).scalar_one_or_none()
    target_path = _join_relative(job.target_subdir, relative_path)
    if existing is not None:
        existing.name = node.name or existing.name
        existing.size = node.size
        existing.mime_type = node.mime_type or existing.mime_type
        existing.kind = node.kind or existing.kind
        existing.is_video = node.is_video
        if node.hash:
            existing.expected_hash = node.hash
        existing.relative_path = relative_path or existing.relative_path
        existing.target_path = target_path
        return existing
    task = DownloadTask(
        job_id=job.id,
        share_file_id=node.id,
        expected_hash=node.hash or "",
        name=node.name,
        relative_path=relative_path,
        target_path=target_path,
        size=node.size,
        mime_type=node.mime_type,
        kind=node.kind,
        is_video=node.is_video,
        status=TaskStatus.DISCOVERED,
        sort_index=sort_index,
    )
    session.add(task)
    await session.flush()
    return task


def _join_path(parent: str, name: str) -> str:
    return f"{parent}/{name}".strip("/") if parent else name


async def _enumerate_share(
    session: AsyncSession, job: DownloadJob, client: AnonymousPikPakClient
) -> None:
    pass_code = decrypt(job.pass_code_enc)
    snapshot = await client.get_share(job.share_id, pass_code)
    if not snapshot.ok:
        job.status = JobStatus.FAILED
        job.error_code = f"SHARE_{snapshot.share_status}"
        job.error_message = snapshot.share_status_text or f"分享状态异常: {snapshot.share_status}"
        job.finished_at = utcnow()
        events.emit(
            session,
            "download_job",
            job.id,
            "download_job.failed",
            {"reason": job.error_code},
        )
        return
    job.pass_code_token_enc = encrypt(snapshot.pass_code_token)
    if not snapshot.files:
        job.status = JobStatus.FAILED
        job.error_code = "EMPTY_SHARE"
        job.error_message = "分享中没有文件"
        job.finished_at = utcnow()
        return
    root = snapshot.files[0]
    job.root_file_id = root.id
    job.root_kind = root.kind
    job.root_name = root.name
    if root.is_folder:
        existing = (
            await session.execute(
                select(DownloadFolder).where(
                    DownloadFolder.job_id == job.id,
                    DownloadFolder.folder_id == root.id,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            folder = DownloadFolder(
                job_id=job.id,
                folder_id=root.id,
                path=root.name,
                status=FolderStatus.PENDING,
            )
            session.add(folder)
        else:
            existing.path = root.name
        job.folder_total = 1
    else:
        await _upsert_task(session, job, root, relative_path=root.name, sort_index=0)
        job.status = JobStatus.READY
        events.emit(
            session,
            "download_job",
            job.id,
            "download_job.ready",
            {"root": root.name, "kind": root.kind},
        )
    await session.flush()


async def _enumerate_selection(
    session: AsyncSession, job: DownloadJob, client: AnonymousPikPakClient
) -> None:
    pass_code = decrypt(job.pass_code_enc)
    snapshot = await client.get_share(job.share_id, pass_code)
    if not snapshot.ok:
        job.status = JobStatus.FAILED
        job.error_code = f"SHARE_{snapshot.share_status}"
        job.error_message = snapshot.share_status_text or f"分享状态异常: {snapshot.share_status}"
        job.finished_at = utcnow()
        return
    job.pass_code_token_enc = encrypt(snapshot.pass_code_token)
    token = snapshot.pass_code_token
    items = (
        await session.execute(
            select(DownloadJobItem)
            .where(DownloadJobItem.job_id == job.id)
            .order_by(DownloadJobItem.sort_index.asc())
        )
    ).scalars().all()
    if not items:
        job.status = JobStatus.FAILED
        job.error_code = "EMPTY_SELECTION"
        job.error_message = "没有选择要下载的内容"
        job.finished_at = utcnow()
        return
    if snapshot.title:
        job.root_name = f"{snapshot.title} · 已选择 {len(items)} 项"[:512]
    existing_tasks = set(
        (
            await session.execute(
                select(DownloadTask.share_file_id).where(DownloadTask.job_id == job.id)
            )
        ).scalars().all()
    )
    file_items = [
        item
        for item in items
        if item.kind != "drive#folder" and item.share_file_id not in existing_tasks
    ]
    for offset in range(0, len(file_items), 100):
        batch = file_items[offset : offset + 100]
        nodes = await client.batch_file_info(
            job.share_id, [item.share_file_id for item in batch], token
        )
        by_id = {node.id: node for node in nodes}
        for item in batch:
            node = by_id.get(item.share_file_id)
            if node is None:
                try:
                    node = await client.file_info(job.share_id, item.share_file_id, token)
                except PikPakError:
                    continue
            if node.is_folder:
                continue
            await _upsert_task(
                session,
                job,
                node,
                relative_path=item.relative_path or node.name,
                sort_index=item.sort_index,
            )
    existing_folders = set(
        (
            await session.execute(
                select(DownloadFolder.folder_id).where(DownloadFolder.job_id == job.id)
            )
        ).scalars().all()
    )
    for item in items:
        if item.kind != "drive#folder" or item.share_file_id in existing_folders:
            continue
        session.add(
            DownloadFolder(
                job_id=job.id,
                folder_id=item.share_file_id,
                path=item.relative_path or item.name,
                status=FolderStatus.PENDING,
            )
        )
    await session.flush()
    task_count = int(
        (
            await session.execute(
                select(func.count(DownloadTask.id)).where(DownloadTask.job_id == job.id)
            )
        ).scalar_one()
    )
    folder_count = int(
        (
            await session.execute(
                select(func.count(DownloadFolder.id)).where(DownloadFolder.job_id == job.id)
            )
        ).scalar_one()
    )
    if task_count == 0 and folder_count == 0:
        job.status = JobStatus.FAILED
        job.error_code = "SELECTION_NOT_FOUND"
        job.error_message = "所选内容在分享中不存在"
        job.finished_at = utcnow()


async def _enrich_tasks(
    session: AsyncSession, job: DownloadJob, client: AnonymousPikPakClient
) -> bool:
    token = decrypt(job.pass_code_token_enc)
    rows = (
        await session.execute(
            select(DownloadTask).where(
                DownloadTask.job_id == job.id,
                DownloadTask.expected_hash == "",
                DownloadTask.status.notin_(TaskStatus.TERMINAL),
            )
        )
    ).scalars().all()
    if not rows:
        return True
    for offset in range(0, len(rows), 100):
        batch = rows[offset : offset + 100]
        nodes = await client.batch_file_info(
            job.share_id, [task.share_file_id for task in batch], token
        )
        by_id = {node.id: node for node in nodes}
        for task in batch:
            node = by_id.get(task.share_file_id)
            if node is None:
                continue
            task.expected_hash = node.hash or task.expected_hash
            task.size = node.size
            task.mime_type = node.mime_type or task.mime_type
            task.is_video = node.is_video
    await session.flush()
    missing = (
        await session.execute(
            select(DownloadTask).where(
                DownloadTask.job_id == job.id, DownloadTask.expected_hash == ""
            )
        )
    ).scalars().all()
    for task in missing:
        _set_status(task, TaskStatus.FAILED)
        task.error_code = "INVALID_FILE_METADATA"
        task.error_message = "文件缺少 hash"
        task.finished_at = utcnow()
    if missing:
        events.emit(
            session,
            "download_job",
            job.id,
            "download_job.invalid_metadata",
            {"count": len(missing)},
        )
    return True


async def _process_folders(
    session: AsyncSession, job: DownloadJob, client: AnonymousPikPakClient
) -> None:
    token = decrypt(job.pass_code_token_enc)
    for _ in range(10):
        folder = (
            await session.execute(
                select(DownloadFolder)
                .where(
                    DownloadFolder.job_id == job.id,
                    DownloadFolder.status.in_([FolderStatus.PENDING, FolderStatus.ENUMERATING]),
                )
                .order_by(DownloadFolder.id.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if folder is None:
            break
        folder.status = FolderStatus.ENUMERATING
        await session.commit()
        try:
            page = await client.list_folder(
                job.share_id, folder.folder_id, token, folder.page_token
            )
        except RateLimited:
            raise
        except PikPakError as exc:
            folder.status = FolderStatus.DONE
            folder.page_token = None
            job.error_message = f"目录枚举失败: {exc}"
            events.emit(
                session,
                "download_job",
                job.id,
                "download_job.folder_failed",
                {"folder_id": folder.folder_id, "error": str(exc)},
            )
            await session.commit()
            continue
        sort_base = int(
            (
                await session.execute(
                    select(func.count(DownloadTask.id)).where(DownloadTask.job_id == job.id)
                )
            ).scalar_one()
        )
        for index, node in enumerate(page.files):
            relative = _join_path(folder.path, node.name)
            if node.is_folder:
                exists = (
                    await session.execute(
                        select(DownloadFolder).where(
                            DownloadFolder.job_id == job.id,
                            DownloadFolder.folder_id == node.id,
                        )
                    )
                ).scalar_one_or_none()
                if exists is None:
                    session.add(
                        DownloadFolder(
                            job_id=job.id,
                            folder_id=node.id,
                            path=relative,
                            status=FolderStatus.PENDING,
                        )
                    )
            else:
                await _upsert_task(
                    session, job, node, relative_path=relative, sort_index=sort_base + index
                )
        folder.page_token = page.next_page_token or None
        if not page.next_page_token:
            folder.status = FolderStatus.DONE
        await session.commit()

    pending = (
        await session.execute(
            select(func.count(DownloadFolder.id)).where(
                DownloadFolder.job_id == job.id,
                DownloadFolder.status != FolderStatus.DONE,
            )
        )
    ).scalar_one()
    if pending:
        return
    if await _enrich_tasks(session, job, client):
        if job.status == JobStatus.ENUMERATING:
            job.status = JobStatus.READY
            events.emit(
                session,
                "download_job",
                job.id,
                "download_job.ready",
                {"files": job.total_files, "folders": job.folder_total},
            )
    await session.flush()


async def dispatch_downloads(session: AsyncSession) -> None:
    if _dispatch_lock.locked():
        return
    async with _dispatch_lock:
        try:
            await _dispatch_enumeration(session)
        except Exception:  # noqa: BLE001
            log.exception("download_enumeration_failed")
            await session.rollback()
        try:
            await _dispatch_plans(session)
        except Exception:  # noqa: BLE001
            log.exception("download_planning_failed")
            await session.rollback()
        try:
            await _dispatch_handoffs(session)
        except Exception:  # noqa: BLE001
            log.exception("download_handoff_failed")
            await session.rollback()
        try:
            await _poll_downloads(session)
        except Exception:  # noqa: BLE001
            log.exception("download_poll_failed")
            await session.rollback()


async def _dispatch_enumeration(session: AsyncSession) -> None:
    job_ids = (
        await session.execute(
            select(DownloadJob.id)
            .where(DownloadJob.status == JobStatus.ENUMERATING)
            .order_by(DownloadJob.created_at.asc())
            .limit(5)
        )
    ).scalars().all()
    if not job_ids:
        return
    client = AnonymousPikPakClient()
    try:
        for job_id in job_ids:
            job = await session.get(DownloadJob, job_id)
            if job is None:
                continue
            selected = int(
                (
                    await session.execute(
                        select(func.count(DownloadJobItem.id)).where(
                            DownloadJobItem.job_id == job.id
                        )
                    )
                ).scalar_one()
            )
            if selected:
                try:
                    await _enumerate_selection(session, job, client)
                except RateLimited:
                    await session.commit()
                    continue
                except PikPakError as exc:
                    job.status = JobStatus.FAILED
                    job.error_code = exc.code or "ENUMERATION_FAILED"
                    job.error_message = str(exc)
                    job.finished_at = utcnow()
                await session.commit()
                if job.status == JobStatus.FAILED:
                    continue
                try:
                    await _process_folders(session, job, client)
                except RateLimited:
                    await session.rollback()
                    continue
                except PikPakError as exc:
                    await session.rollback()
                    job = await session.get(DownloadJob, job_id)
                    if job is not None:
                        job.status = JobStatus.FAILED
                        job.error_code = exc.code or "ENUMERATION_FAILED"
                        job.error_message = str(exc)
                        job.finished_at = utcnow()
                await session.commit()
                if job is None:
                    continue
            else:
                if job.root_file_id is None or job.root_kind == "drive#file":
                    try:
                        await _enumerate_share(session, job, client)
                    except ShareUnavailable as exc:
                        job.status = JobStatus.FAILED
                        job.error_code = exc.code
                        job.error_message = str(exc)
                        job.finished_at = utcnow()
                    except RateLimited:
                        await session.commit()
                        continue
                    except PikPakError as exc:
                        job.status = JobStatus.FAILED
                        job.error_code = exc.code or "ENUMERATION_FAILED"
                        job.error_message = str(exc)
                        job.finished_at = utcnow()
                    await session.commit()
                    if job.status == JobStatus.FAILED:
                        continue
                if job.root_kind == "drive#folder":
                    try:
                        await _process_folders(session, job, client)
                    except RateLimited:
                        await session.rollback()
                        continue
                    except PikPakError as exc:
                        await session.rollback()
                        job = await session.get(DownloadJob, job_id)
                        if job is not None and exc.code in ("NOT_FOUND", "PERMISSION_DENIED"):
                            job.status = JobStatus.FAILED
                            job.error_code = exc.code
                            job.error_message = str(exc)
                            job.finished_at = utcnow()
                    await session.commit()
                    if job is None:
                        continue
            await _refresh_job(session, job)
            await session.commit()
    finally:
        await client.close()


async def _active_download_count(session: AsyncSession) -> int:
    return int(
        (
            await session.execute(
                select(func.count(DownloadTask.id)).where(
                    DownloadTask.status.in_(
                        [TaskStatus.ANON_DOWNLOADING, TaskStatus.ACCOUNT_DOWNLOADING]
                    )
                )
            )
        ).scalar_one()
    )


async def _dispatch_plans(session: AsyncSession) -> None:
    settings = get_settings()
    active = await _active_download_count(session)
    slots = max(0, settings.download_max_concurrency - active)
    if slots <= 0:
        return
    workers = min(settings.download_plan_concurrency, slots)
    if workers <= 0:
        return
    task_ids = (
        await session.execute(
            select(DownloadTask.id)
            .join(DownloadJob, DownloadTask.job_id == DownloadJob.id)
            .where(
                DownloadTask.status.in_([TaskStatus.DISCOVERED, TaskStatus.PLANNING]),
                DownloadJob.status.notin_([JobStatus.CANCELLED, JobStatus.FAILED]),
            )
            .order_by(DownloadTask.sort_index.asc())
            .limit(workers)
        )
    ).scalars().all()
    if not task_ids:
        return

    factory = get_session_factory()
    client = AnonymousPikPakClient()

    async def plan_one(task_id: str) -> None:
        async with factory() as worker_session:
            task = await worker_session.get(DownloadTask, task_id)
            if task is None or task.status not in (
                TaskStatus.DISCOVERED,
                TaskStatus.PLANNING,
            ):
                return
            try:
                await _plan_task(worker_session, task, client)
            except RateLimited:
                await worker_session.rollback()
                task = await worker_session.get(DownloadTask, task_id)
                if task is not None and task.status == TaskStatus.PLANNING:
                    _set_status(task, TaskStatus.DISCOVERED)
                await worker_session.commit()
            except Exception:  # noqa: BLE001
                log.exception("plan_task_failed", task_id=task_id)
                await worker_session.rollback()

    try:
        await asyncio.gather(*(plan_one(task_id) for task_id in task_ids))
    finally:
        await client.close()



async def _try_local_reuse(
    session: AsyncSession, job: DownloadJob, task: DownloadTask
) -> bool:
    """Reuse a local target or another completed task with the same content."""
    if not task.expected_hash or task.size < 0:
        return False
    relative = task.target_path or _join_relative(job.target_subdir, task.relative_path)
    try:
        target = _download_path(relative)
    except ValueError:
        return False

    if target.is_file() and target.stat().st_size == task.size:
        result = await asyncio.to_thread(
            verify_file, target, task.size, task.expected_hash
        )
        if result.ok:
            task.target_path = _download_relative(target)
            task.bytes_done = task.size
            task.verified_chunk = result.chunk
            task.route = "local"
            task.source_url_enc = None
            task.error_code = None
            task.error_message = None
            _set_status(task, TaskStatus.DONE)
            task.finished_at = utcnow()
            events.emit(
                session,
                "download_task",
                task.id,
                "download_task.reused_target",
                {"hash": task.expected_hash, "size": task.size},
            )
            await _refresh_job(session, job)
            await session.commit()
            return True

    if target.exists():
        return False

    active_target = (
        await session.execute(
            select(DownloadTask.id)
            .where(
                DownloadTask.id != task.id,
                DownloadTask.target_path == relative,
                DownloadTask.status.in_(
                    [
                        TaskStatus.PLANNING,
                        TaskStatus.ANON_DOWNLOADING,
                        TaskStatus.ACCOUNT_DOWNLOADING,
                        TaskStatus.ACCOUNT_QUEUED,
                        TaskStatus.RESTORING,
                        TaskStatus.WCL_READY,
                        TaskStatus.VERIFYING,
                    ]
                ),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if active_target is not None:
        task.queue_reason = "TARGET_BUSY"
        if task.status != TaskStatus.DISCOVERED:
            _set_status(task, TaskStatus.DISCOVERED)
        await session.commit()
        return True

    active_copy = (
        await session.execute(
            select(DownloadTask.id)
            .where(
                DownloadTask.id != task.id,
                DownloadTask.expected_hash == task.expected_hash,
                DownloadTask.size == task.size,
                DownloadTask.status.in_(
                    [
                        TaskStatus.PLANNING,
                        TaskStatus.ANON_DOWNLOADING,
                        TaskStatus.ACCOUNT_DOWNLOADING,
                        TaskStatus.ACCOUNT_QUEUED,
                        TaskStatus.RESTORING,
                        TaskStatus.WCL_READY,
                        TaskStatus.VERIFYING,
                    ]
                ),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if active_copy is not None:
        task.queue_reason = "DUPLICATE_WAITING"
        if task.status != TaskStatus.DISCOVERED:
            _set_status(task, TaskStatus.DISCOVERED)
        await session.commit()
        return True

    source = (
        await session.execute(
            select(DownloadTask)
            .where(
                DownloadTask.id != task.id,
                DownloadTask.status == TaskStatus.DONE,
                DownloadTask.expected_hash == task.expected_hash,
                DownloadTask.size == task.size,
                DownloadTask.target_path.is_not(None),
            )
            .order_by(DownloadTask.finished_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if source is None or not source.target_path:
        return False
    try:
        source_path = _download_path(source.target_path)
    except ValueError:
        return False
    if not source_path.is_file() or source_path.stat().st_size != task.size:
        return False

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        await asyncio.to_thread(os.link, source_path, target)
    except OSError:
        await asyncio.to_thread(shutil.copyfile, source_path, target)
    task.target_path = _download_relative(target)
    task.bytes_done = task.size
    task.verified_chunk = source.verified_chunk
    task.route = "local"
    task.source_url_enc = None
    task.error_code = None
    task.error_message = None
    _set_status(task, TaskStatus.DONE)
    task.finished_at = utcnow()
    events.emit(
        session,
        "download_task",
        task.id,
        "download_task.local_copy",
        {
            "hash": task.expected_hash,
            "size": task.size,
            "source_task_id": source.id,
        },
    )
    await _refresh_job(session, job)
    await session.commit()
    return True


async def _plan_task(
    session: AsyncSession, task: DownloadTask, client: AnonymousPikPakClient
) -> None:
    job = await session.get(DownloadJob, task.job_id)
    if job is None:
        return

    async with _plan_claim_lock:
        if await _try_local_reuse(session, job, task):
            return
        _set_status(task, TaskStatus.PLANNING)
        task.attempts += 1
        await session.commit()

    token = decrypt(job.pass_code_token_enc)
    try:
        node = await client.file_info(job.share_id, task.share_file_id, token)
    except ShareUnavailable as exc:
        _set_status(task, TaskStatus.FAILED)
        task.error_code = exc.code
        task.error_message = str(exc)
        task.finished_at = utcnow()
        await session.commit()
        return
    except NotFound as exc:
        _set_status(task, TaskStatus.FAILED)
        task.error_code = "FILE_NOT_FOUND"
        task.error_message = str(exc)
        task.finished_at = utcnow()
        await session.commit()
        return
    except RateLimited:
        raise
    except PikPakError as exc:
        if exc.retryable and task.attempts < 3:
            _set_status(task, TaskStatus.DISCOVERED)
        else:
            _set_status(task, TaskStatus.FAILED)
            task.error_code = exc.code or "FILE_INFO_FAILED"
            task.error_message = str(exc)
            task.finished_at = utcnow()
        await session.commit()
        return

    task.name = node.name or task.name
    task.size = node.size
    task.expected_hash = node.hash or task.expected_hash
    task.mime_type = node.mime_type or task.mime_type
    task.is_video = node.is_video
    if not task.expected_hash:
        _set_status(task, TaskStatus.FAILED)
        task.error_code = "INVALID_FILE_METADATA"
        task.error_message = "文件缺少 hash"
        task.finished_at = utcnow()
        await session.commit()
        return

    target_relative = task.target_path or _join_relative(
        job.target_subdir, task.relative_path
    )
    target = _download_path(target_relative)
    if target.is_file() and target.stat().st_size == task.size:
        existing_result = await asyncio.to_thread(
            verify_file, target, task.size, task.expected_hash
        )
        if existing_result.ok:
            task.target_path = _download_relative(target)
            task.bytes_done = task.size
            task.verified_chunk = existing_result.chunk
            task.route = "object"
            task.source_url_enc = None
            _set_status(task, TaskStatus.DONE)
            task.finished_at = utcnow()
            events.emit(
                session,
                "download_task",
                task.id,
                "download_task.reused_target",
                {"hash": task.expected_hash, "size": task.size},
            )
            await _refresh_job(session, job)
            await session.commit()
            return

    plan: DownloadPlan | None = None
    candidate = anonymous_plan(node, share_id=job.share_id)
    if candidate is not None:
        if candidate.url is None or task.size == 0:
            plan = candidate
        elif await probe_tail_exact(
            candidate.url, task.size, limiter=await window_limiter()
        ):
            plan = candidate

    if plan is None:
        try:
            plan = await _find_candidate_plan(session, job, task, node, client)
        except RateLimited:
            raise
        except PikPakError:
            plan = None

    if plan is None:
        task.route = None
        task.source_url_enc = None
        task.source_url_expire = None
        task.source_share_id = None
        task.source_file_id = None
        task.queue_reason = "NO_ANONYMOUS_SOURCE"
        _set_status(task, TaskStatus.ACCOUNT_QUEUED)
        await _refresh_job(session, job)
        await session.commit()
        return

    task.route = plan.route
    task.source_share_id = plan.source_share_id
    task.source_file_id = plan.source_file_id
    task.source_url_enc = encrypt(plan.url) if plan.url else None
    task.source_url_expire = plan.url_expire
    task.error_code = None
    task.error_message = None
    task.queue_reason = None
    if task.size == 0:
        await _finish_empty_task(session, job, task)
        return
    try:
        await _start_aria2(session, task, plan.url or "", TaskStatus.ANON_DOWNLOADING)
    except PikPakError as exc:
        _set_status(task, TaskStatus.FAILED)
        task.error_code = exc.code or "ARIA2_UNAVAILABLE"
        task.error_message = str(exc)
        task.finished_at = utcnow()
        await session.commit()
        return
    await _refresh_job(session, job)
    await session.commit()


async def _find_candidate_plan(
    session: AsyncSession,
    job: DownloadJob,
    task: DownloadTask,
    node: FileNode,
    client: AnonymousPikPakClient,
) -> DownloadPlan | None:
    seen: set[tuple[str, str]] = set()
    token = decrypt(job.pass_code_token_enc)
    copies = (
        await session.execute(
            select(DownloadTask).where(
                DownloadTask.job_id == job.id,
                DownloadTask.expected_hash == task.expected_hash,
                DownloadTask.size == task.size,
                DownloadTask.share_file_id != task.share_file_id,
            )
        )
    ).scalars().all()
    for copy in copies:
        if (job.share_id, copy.share_file_id) in seen:
            continue
        seen.add((job.share_id, copy.share_file_id))
        try:
            copy_node = await client.file_info(job.share_id, copy.share_file_id, token)
        except PikPakError:
            continue
        plan = anonymous_plan(copy_node, share_id=job.share_id)
        if plan and plan.url and await probe_tail_exact(
            plan.url, task.size, limiter=await window_limiter()
        ):
            return plan

    current = node
    for _depth in range(get_settings().lineage_max_depth):
        sid = current.original_share_id
        fid = current.original_file_id
        if not sid or not fid or fid.startswith("G__") or sid == current.share_id:
            break
        if (sid, fid) in seen:
            break
        seen.add((sid, fid))
        try:
            snapshot = await client.get_share(sid)
            if not snapshot.ok:
                break
            parent = await client.file_info(sid, fid, snapshot.pass_code_token)
        except PikPakError:
            break
        if parent.hash == task.expected_hash and parent.size == task.size:
            plan = anonymous_plan(parent, share_id=sid)
            if plan and plan.url and await probe_tail_exact(
                plan.url, task.size, limiter=await window_limiter()
            ):
                return plan
        current = parent
    return None


async def _finish_empty_task(
    session: AsyncSession, job: DownloadJob, task: DownloadTask
) -> None:
    temp_dir = get_settings().cache_dir / task.id
    temp_dir.mkdir(parents=True, exist_ok=True)
    path = temp_dir / "download.bin"
    path.write_bytes(b"")
    await _verify_task(session, job, task)


async def _start_aria2(
    session: AsyncSession,
    task: DownloadTask,
    url: str,
    status: str,
    *,
    size: int | None = None,
    single_connection: bool = False,
) -> None:
    if not url:
        raise PikPakError("empty download url", code="EMPTY_URL", retryable=True)
    if not aria2.available:
        raise PikPakError("aria2 不可用", code="ARIA2_UNAVAILABLE", retryable=True)
    directory = get_settings().cache_dir / task.id
    gid = await aria2.add_uri(
        url, directory, "download.bin", single_connection=single_connection
    )
    task.aria2_gid = gid
    task.bytes_total = int(size or task.size)
    task.speed = 0
    task.error_code = None
    task.error_message = None
    task.started_at = task.started_at or utcnow()
    _set_status(task, status)
    events.emit(
        session,
        "download_task",
        task.id,
        "download_task.started",
        {"route": task.route, "size": task.size, "gid": gid},
    )


async def _dispatch_handoffs(session: AsyncSession) -> None:
    # Only carry ids across the loop: rollback expires ORM instances and a later
    # lazy attribute access would raise MissingGreenlet in async context.
    task_ids = (
        await session.execute(
            select(DownloadTask.id)
            .join(DownloadJob, DownloadTask.job_id == DownloadJob.id)
            .where(
                DownloadTask.status.in_(
                    [TaskStatus.ACCOUNT_QUEUED, TaskStatus.RESTORING, TaskStatus.WCL_READY]
                ),
                DownloadJob.status.notin_([JobStatus.CANCELLED, JobStatus.FAILED]),
            )
            .order_by(DownloadTask.updated_at.asc())
            .limit(3)
        )
    ).scalars().all()
    for task_id in task_ids:
        try:
            await run_account_handoff(session, task_id)
        except RateLimited:
            await session.rollback()
        except Exception:  # noqa: BLE001
            log.exception("account_handoff_failed", task_id=task_id)
            await session.rollback()


async def run_account_handoff(session: AsyncSession, task_id: str) -> None:
    task = await _get_task(session, task_id)
    if task.status not in (
        TaskStatus.ACCOUNT_QUEUED,
        TaskStatus.RESTORING,
        TaskStatus.WCL_READY,
    ):
        return
    job = await session.get(DownloadJob, task.job_id)
    if job is None:
        return
    preferred = task.account_id if task.restore_started_at else None
    account = await claim_account(session, task_id=task.id, preferred_account_id=preferred)
    if account is None:
        task.queue_reason = await account_state_summary(session)
        _set_status(task, TaskStatus.ACCOUNT_QUEUED)
        await session.commit()
        return
    if task.account_id != account.id:
        task.account_id = account.id
        task.account_label = account.label
    task.queue_reason = None
    await session.commit()
    client = make_client(account)
    try:
        await _handoff_with_account(session, job, task, account, client)
    except RateLimited as exc:
        await mark_cooldown(
            session,
            account,
            seconds=get_settings().account_cooldown_seconds + random.randint(0, 60),
            error=str(exc),
        )
        await release_account(session, account.id, task.id)
        task.queue_reason = "RATE_LIMITED"
        task.error_code = "RATE_LIMITED"
        task.error_message = str(exc)
        _set_status(task, TaskStatus.ACCOUNT_QUEUED)
        events.emit(session, "download_task", task.id, "download_task.rate_limited", {})
        await session.commit()
    except Unauthorized as exc:
        await mark_invalid(session, account, str(exc))
        await release_account(session, account.id, task.id)
        task.queue_reason = "ACCOUNT_INVALID"
        task.error_code = "ACCOUNT_INVALID"
        task.error_message = str(exc)
        _set_status(task, TaskStatus.ACCOUNT_QUEUED)
        await session.commit()
    except PikPakError as exc:
        await _handle_handoff_error(session, job, task, account, exc)
    finally:
        await client.close()


async def _handoff_via_tar(
    session: AsyncSession,
    job: DownloadJob,
    task: DownloadTask,
    account: Account,
    client,
) -> None:
    if not task.drive_file_id:
        raise PikPakError("缺少已转存文件，无法打包", code="TAR_NO_SOURCE", retryable=True)
    folder_name = f"pikpak_tar_{task.id[:8]}"
    folder = await client.create_folder(folder_name)
    folder_id = str(folder.get("id") or "")
    if not folder_id:
        raise PikPakError("创建打包目录失败", code="TAR_FOLDER_FAILED", retryable=True)
    task.tar_folder_id = folder_id
    await client.move_files([task.drive_file_id], folder_id)
    await session.commit()

    packed = await client.pack_folder(folder_id)
    pack_task_id = str(packed.get("task_id") or "")
    if not pack_task_id:
        raise PikPakError("pack 未返回任务", code="TAR_PACK_FAILED", retryable=True)

    deadline = int(utcnow().timestamp()) + 300
    tar_id = ""
    tar_size = 0
    while int(utcnow().timestamp()) < deadline:
        state = await client.get_restore_task(pack_task_id)
        phase = str(state.get("phase") or "").upper()
        message = str(state.get("message") or "").lower()
        if phase in ("PHASE_TYPE_COMPLETE", "COMPLETE") or message == "completed":
            tar_id = str(state.get("file_id") or "")
            params = state.get("params") or {}
            try:
                tar_size = int(params.get("total_size") or 0)
            except (TypeError, ValueError):
                tar_size = 0
            break
        if phase in ("PHASE_TYPE_ERROR", "ERROR") or message in ("error", "failed"):
            raise PikPakError(
                f"pack 失败: {state.get('message') or state}",
                code="TAR_PACK_FAILED",
                retryable=True,
            )
        await renew_lease(session, account.id, task.id)
        await asyncio.sleep(3)
    if not tar_id:
        raise PikPakError("pack 超时", code="TAR_PACK_TIMEOUT", retryable=True)

    task.drive_file_id = tar_id
    task.tar_size = tar_size
    task.remote_deleted_at = None
    task.error_code = None
    task.error_message = None
    await session.commit()

    try:
        await client.batch_untrash([tar_id])
    except PikPakError:
        log.warning("tar_untrash_failed", task_id=task.id, tar_id=tar_id)

    drive = await client.get_file(tar_id, usage="FETCH")
    wcl = str(drive.get("web_content_link") or "")
    if not wcl:
        raise PikPakError(
            "tar 未签发直链",
            code="TAR_WCL_MISSING",
            retryable=False,
        )
    if not tar_size:
        try:
            tar_size = int(drive.get("size") or 0)
        except (TypeError, ValueError):
            tar_size = 0
    if not tar_size:
        raise PikPakError("tar 大小未知", code="TAR_SIZE_MISSING", retryable=False)

    task.tar_size = tar_size
    task.source_url_enc = encrypt(wcl)
    task.source_url_expire = _url_expire(wcl)
    task.source_share_id = job.share_id
    task.source_file_id = task.share_file_id
    task.route = "tar"
    task.error_code = None
    task.error_message = None
    await session.commit()

    await _delete_drive_file(session, task, account, client)
    await release_account(session, account.id, task.id)
    await session.commit()

    # tar is downloaded as one stream: no multipart/range splitting.
    try:
        await _start_aria2(
            session,
            task,
            wcl,
            TaskStatus.ACCOUNT_DOWNLOADING,
            size=tar_size,
            single_connection=True,
        )
    except PikPakError as exc:
        task.error_code = exc.code or "ARIA2_UNAVAILABLE"
        task.error_message = str(exc)
        task.download_cycles += 1
        if task.download_cycles >= 3:
            _set_status(task, TaskStatus.FAILED)
            task.finished_at = utcnow()
        else:
            task.queue_reason = "ARIA2_UNAVAILABLE"
            _set_status(task, TaskStatus.WCL_READY)
        await session.commit()
        return
    await session.commit()


async def _handoff_with_account(
    session: AsyncSession,
    job: DownloadJob,
    task: DownloadTask,
    account: Account,
    client,
) -> None:
    if task.status == TaskStatus.WCL_READY and task.source_url_enc:
        wcl = decrypt(task.source_url_enc) or ""
        expire = task.source_url_expire
        if wcl and (expire is None or expire > int(utcnow().timestamp()) + 300):
            if task.drive_file_id and not task.remote_deleted_at:
                await _delete_drive_file(session, task, account, client)
            await release_account(session, account.id, task.id)
            await session.commit()
            expected_size = task.tar_size if task.route == "tar" else task.size
            if task.route != "tar" and not await probe_with_retries(
                wcl, expected_size, limiter=await window_limiter()
            ):
                await _clear_account_link(session, task)
                task.download_cycles += 1
                task.error_code = "CDN_LINK_UNUSABLE"
                task.error_message = "复用已保存直链时 Range 探针失败"
                if task.download_cycles >= 3:
                    _set_status(task, TaskStatus.FAILED)
                    task.finished_at = utcnow()
                else:
                    task.queue_reason = "CDN_LINK_UNUSABLE"
                    _set_status(task, TaskStatus.ACCOUNT_QUEUED)
                await session.commit()
                return
            try:
                await _start_aria2(
                    session,
                    task,
                    wcl,
                    TaskStatus.ACCOUNT_DOWNLOADING,
                    size=expected_size,
                    single_connection=task.route == "tar",
                )
            except PikPakError as exc:
                task.error_code = exc.code or "ARIA2_UNAVAILABLE"
                task.error_message = str(exc)
                task.download_cycles += 1
                if task.download_cycles >= 3:
                    _set_status(task, TaskStatus.FAILED)
                    task.finished_at = utcnow()
                else:
                    task.queue_reason = "ARIA2_UNAVAILABLE"
                    _set_status(task, TaskStatus.WCL_READY)
            await session.commit()
            return
    await client.ensure_access_token()
    about = await client.about()
    await update_quota_from_about(account, about)
    # Release the write transaction before any network work or folder cleanup.
    await session.commit()
    if not quota_available(account, task.size):
        removed = await _cleanup_stale_tar_folders(
            session, client, exclude_task_id=task.id
        )
        if removed:
            about = await client.about()
            await update_quota_from_about(account, about)
            await session.commit()
    if not quota_available(account, task.size):
        await release_account(session, account.id, task.id)
        task.queue_reason = "NO_QUOTA"
        _set_status(task, TaskStatus.ACCOUNT_QUEUED)
        await session.commit()
        return

    if task.restore_started_at and not task.restore_task_id and not task.drive_file_id:
        found = await _find_existing_restore(client, task)
        if found is not None:
            folder_id, file_id = found
            task.restore_folder_id = folder_id
            task.drive_file_id = file_id
            task.restore_task_id = "recovered"
            await session.commit()

    if not task.restore_task_id:
        task.restore_started_at = utcnow()
        task.source_share_id = job.share_id
        task.source_file_id = task.share_file_id
        _set_status(task, TaskStatus.RESTORING)
        await session.commit()
        token = decrypt(job.pass_code_token_enc)
        response = await client.restore(job.share_id, task.share_file_id, token)
        task.restore_folder_id = str(response.get("file_id") or "")
        task.restore_task_id = str(
            response.get("restore_task_id") or response.get("task_id") or ""
        )
        await increment_restore_count(session, account)
        events.emit(
            session,
            "download_task",
            task.id,
            "download_task.restore_started",
            {"restore_task_id": task.restore_task_id, "account": account.label},
        )
        await session.commit()
        if not task.restore_task_id:
            raise PikPakError("restore returned no task id", code="RESTORE_NO_TASK", retryable=True)

    if task.restore_task_id and task.restore_task_id != "recovered":
        state = await client.get_restore_task(task.restore_task_id)
        message = str(state.get("message") or "")
        phase = str(state.get("phase") or "")
        if message.lower() != "completed" and phase != "PHASE_TYPE_COMPLETE":
            if message.lower() in ("error", "failed") or phase == "PHASE_TYPE_ERROR":
                raise PikPakError(
                    f"restore failed: {state.get('message') or state}",
                    code="RESTORE_FAILED",
                    retryable=False,
                )
            await renew_lease(session, account.id, task.id)
            _set_status(task, TaskStatus.RESTORING)
            await session.commit()
            return

    if not task.drive_file_id:
        found = await _locate_restored_file(client, task)
        if found is None:
            task.attempts += 1
            if task.attempts >= 10:
                raise PikPakError(
                    "restored file not found in destination folder",
                    code="RESTORE_TARGET_NOT_FOUND",
                    retryable=False,
                )
            await renew_lease(session, account.id, task.id)
            await session.commit()
            return
        task.drive_file_id = found
        await session.commit()

    try:
        drive = await client.get_file(task.drive_file_id, usage="FETCH")
    except NotFound:
        await _clear_account_link(session, task)
        task.download_cycles += 1
        task.error_code = "REMOTE_DELETED"
        task.error_message = "云盘文件已删除且直链不可复用，需要重新转存"
        if task.download_cycles >= 3:
            _set_status(task, TaskStatus.FAILED)
            task.finished_at = utcnow()
        else:
            task.queue_reason = "REMOTE_DELETED"
            _set_status(task, TaskStatus.ACCOUNT_QUEUED)
        await release_account(session, account.id, task.id)
        await session.commit()
        return
    except PikPakError as exc:
        if exc.code == "daily_download_size_exceeded":
            await _handoff_via_tar(session, job, task, account, client)
            return
        raise
    wcl = str(drive.get("web_content_link") or "")
    if not wcl:
        global_kind = int((drive.get("params") or {}).get("global_file_kind") or 0)
        await _delete_drive_file(session, task, account, client)
        await release_account(session, account.id, task.id)
        if global_kind in (1, 2):
            raise PikPakError(
                "全局归档文件跨账号转存后无直链",
                code="UNSUPPORTED_GLOBAL_FILE",
                retryable=False,
            )
        task.attempts += 1
        if task.attempts >= 3:
            raise PikPakError("wcl missing", code="WCL_MISSING", retryable=False)
        _set_status(task, TaskStatus.ACCOUNT_QUEUED)
        task.queue_reason = "WCL_MISSING"
        await session.commit()
        return

    expire = _url_expire(wcl)
    task.source_url_enc = encrypt(wcl)
    task.source_url_expire = expire
    task.source_share_id = job.share_id
    task.source_file_id = task.share_file_id
    task.route = "account"
    task.error_code = None
    task.error_message = None
    _set_status(task, TaskStatus.WCL_READY)
    events.emit(
        session,
        "download_task",
        task.id,
        "download_task.wcl_ready",
        {"expire": expire, "drive_file_id": task.drive_file_id},
    )
    await session.commit()

    await _delete_drive_file(session, task, account, client)
    await release_account(session, account.id, task.id)
    await session.commit()

    if not await probe_with_retries(wcl, task.size, limiter=await window_limiter()):
        await _clear_account_link(session, task)
        task.download_cycles += 1
        task.error_code = "CDN_LINK_UNUSABLE"
        task.error_message = "取到直链后 Range 探针失败"
        if task.download_cycles >= 3:
            _set_status(task, TaskStatus.FAILED)
            task.finished_at = utcnow()
        else:
            task.queue_reason = "CDN_LINK_UNUSABLE"
            _set_status(task, TaskStatus.ACCOUNT_QUEUED)
        await session.commit()
        return

    try:
        await _start_aria2(session, task, wcl, TaskStatus.ACCOUNT_DOWNLOADING)
    except PikPakError as exc:
        task.error_code = exc.code or "ARIA2_UNAVAILABLE"
        task.error_message = str(exc)
        task.download_cycles += 1
        if task.download_cycles >= 3:
            _set_status(task, TaskStatus.FAILED)
            task.finished_at = utcnow()
        else:
            task.queue_reason = "ARIA2_UNAVAILABLE"
            _set_status(task, TaskStatus.WCL_READY)
        await session.commit()
        return
    await session.commit()


def _url_expire(url: str) -> int | None:
    from .planner import url_expire

    return url_expire(url)


async def _find_existing_restore(
    client, task: DownloadTask
) -> tuple[str, str] | None:
    data = await client.list_files("")
    files = data.get("files") or []
    for item in files:
        node = FileNode.from_json(item)
        if node.is_folder and node.name == "Pack From Shared":
            found = await _find_in_folder(client, node.id, task)
            if found is not None:
                return node.id, found
    return None


async def _locate_restored_file(client, task: DownloadTask) -> str | None:
    if task.restore_folder_id:
        return await _find_in_folder(client, task.restore_folder_id, task)
    return None


async def _find_in_folder(client, folder_id: str, task: DownloadTask) -> str | None:
    page_token: str | None = None
    exact: list[str] = []
    fingerprint: list[str] = []
    for _ in range(100):
        data = await client.list_files(folder_id, page_token)
        for item in data.get("files") or []:
            node = FileNode.from_json(item)
            if node.is_folder:
                continue
            phase = str(item.get("phase") or "")
            if phase and phase != "PHASE_TYPE_COMPLETE":
                continue
            expected_share = task.source_share_id or ""
            same_source = node.original_share_id in ("", expected_share)
            same_fingerprint = (
                node.hash == task.expected_hash and node.size == task.size
            )
            if (
                node.original_share_id == expected_share
                and node.original_file_id == task.share_file_id
                and same_fingerprint
            ):
                exact.append(node.id)
            elif same_fingerprint and same_source:
                fingerprint.append(node.id)
        page_token = data.get("next_page_token") or None
        if not page_token:
            break
    if exact:
        return exact[0]
    unique = sorted(set(fingerprint))
    if len(unique) == 1:
        return unique[0]
    if len(unique) > 1:
        raise PikPakError(
            "multiple restored files match fingerprint",
            code="RESTORE_TARGET_AMBIGUOUS",
            retryable=False,
        )
    return None


TAR_FOLDER_PREFIX = "pikpak_tar_"
_ACTIVE_TAR_STATES = {
    TaskStatus.PLANNING,
    TaskStatus.ANON_DOWNLOADING,
    TaskStatus.RESTORING,
    TaskStatus.WCL_READY,
    TaskStatus.ACCOUNT_DOWNLOADING,
    TaskStatus.VERIFYING,
    TaskStatus.PAUSED,
}


def _reset_tar_task(task: DownloadTask) -> None:
    task.drive_file_id = None
    task.restore_task_id = None
    task.restore_folder_id = None
    task.restore_started_at = None
    task.remote_deleted_at = None
    task.source_url_enc = None
    task.source_url_expire = None
    task.tar_size = 0
    task.tar_folder_id = None
    task.error_code = None
    task.error_message = None
    task.queue_reason = "NO_QUOTA"


async def _cleanup_stale_tar_folders(
    session: AsyncSession,
    client,
    *,
    exclude_task_id: str | None = None,
) -> int:
    """Delete app-created tar folders that no running task still needs."""
    removed = 0
    page_token: str | None = None
    for _ in range(50):
        try:
            data = await client.list_files("", page_token)
        except PikPakError as exc:
            log.warning("tar_folder_list_failed", error=str(exc))
            break
        for item in data.get("files") or []:
            node = FileNode.from_json(item)
            if not node.is_folder or not node.name.startswith(TAR_FOLDER_PREFIX):
                continue
            suffix = node.name[len(TAR_FOLDER_PREFIX) :]
            if not suffix:
                continue
            tasks = (
                await session.execute(
                    select(DownloadTask).where(DownloadTask.id.like(f"{suffix}%"))
                )
            ).scalars().all()
            if exclude_task_id and any(task.id == exclude_task_id for task in tasks):
                continue
            if any(task.status in _ACTIVE_TAR_STATES for task in tasks):
                continue
            try:
                await client.delete_file(node.id)
            except NotFound:
                pass
            except PikPakError as exc:
                log.warning(
                    "tar_folder_delete_failed",
                    folder_id=node.id,
                    error=str(exc),
                )
                continue
            for task in tasks:
                if task.tar_folder_id == node.id:
                    if task.status == TaskStatus.ACCOUNT_QUEUED:
                        _reset_tar_task(task)
                    else:
                        task.tar_folder_id = None
            removed += 1
        page_token = data.get("next_page_token") or None
        if not page_token:
            break
    if removed:
        await session.commit()
        log.info("stale_tar_folders_removed", count=removed)
    return removed


async def _delete_tar_folder(session: AsyncSession, task: DownloadTask) -> None:
    folder_id = task.tar_folder_id
    if not folder_id or not task.account_id:
        return
    account = await session.get(Account, task.account_id)
    if account is None:
        return
    client = make_client(account)
    try:
        await client.delete_file(folder_id)
        task.tar_folder_id = None
        await session.commit()
    except NotFound:
        task.tar_folder_id = None
        await session.commit()
    except (PikPakError, Unauthorized) as exc:
        await enqueue_cleanup(
            session,
            account_id=account.id,
            drive_file_id=folder_id,
            task_id=task.id,
            error=str(exc),
        )
        await session.commit()
    finally:
        await client.close()


async def _delete_drive_file(
    session: AsyncSession, task: DownloadTask, account: Account, client
) -> None:
    if not task.drive_file_id:
        return
    try:
        await client.delete_file(task.drive_file_id)
        task.remote_deleted_at = utcnow()
        task.updated_at = utcnow()
    except NotFound:
        task.remote_deleted_at = utcnow()
        task.updated_at = utcnow()
    except (PikPakError, Unauthorized) as exc:
        await enqueue_cleanup(
            session,
            account_id=account.id,
            drive_file_id=task.drive_file_id,
            task_id=task.id,
            error=str(exc),
        )
        events.emit(
            session,
            "download_task",
            task.id,
            "download_task.cleanup_queued",
            {"drive_file_id": task.drive_file_id, "error": str(exc)},
        )
    await session.commit()


async def _clear_account_link(session: AsyncSession, task: DownloadTask) -> None:
    task.source_url_enc = None
    task.source_url_expire = None
    task.source_share_id = None
    task.source_file_id = None
    task.drive_file_id = None
    task.restore_task_id = None
    task.restore_folder_id = None
    task.restore_started_at = None
    task.remote_deleted_at = None
    task.tar_size = 0
    task.tar_folder_id = None
    task.aria2_gid = None
    await session.flush()


async def _handle_handoff_error(
    session: AsyncSession,
    job: DownloadJob,
    task: DownloadTask,
    account: Account,
    exc: PikPakError,
) -> None:
    await session.commit()
    if task.drive_file_id and not task.remote_deleted_at:
        cleanup_client = make_client(account)
        try:
            if task.tar_folder_id:
                try:
                    await cleanup_client.delete_file(task.tar_folder_id)
                except (NotFound, PikPakError):
                    pass
                task.tar_folder_id = None
            await _delete_drive_file(session, task, account, cleanup_client)
        except Exception:  # noqa: BLE001
            pass
        finally:
            await cleanup_client.close()
    await release_account(session, account.id, task.id)
    task.error_code = exc.code or "ACCOUNT_ERROR"
    task.error_message = str(exc)
    if exc.retryable and task.attempts < 3:
        task.attempts += 1
        task.queue_reason = "PROTOCOL_ERROR"
        _set_status(task, TaskStatus.ACCOUNT_QUEUED)
    else:
        _set_status(task, TaskStatus.FAILED)
        task.finished_at = utcnow()
    await _refresh_job(session, job)
    await session.commit()


async def _poll_downloads(session: AsyncSession) -> None:
    tasks = (
        await session.execute(
            select(DownloadTask).where(
                DownloadTask.status.in_(
                    [
                        TaskStatus.ANON_DOWNLOADING,
                        TaskStatus.ACCOUNT_DOWNLOADING,
                        TaskStatus.VERIFYING,
                    ]
                )
            )
        )
    ).scalars().all()
    for task in tasks:
        job = await session.get(DownloadJob, task.job_id)
        if job is None or job.status == JobStatus.CANCELLED:
            continue
        if task.status == TaskStatus.VERIFYING:
            await _verify_task(session, job, task)
            continue
        if not task.aria2_gid:
            await _requeue_lost_download(session, task)
            continue
        try:
            state = await aria2.tell_status(task.aria2_gid)
        except Exception:  # noqa: BLE001
            log.exception("aria2_status_failed", task_id=task.id)
            continue
        if state is None:
            await _requeue_lost_download(session, task)
            continue
        status = str(state.get("status") or "")
        task.bytes_total = int(state.get("totalLength") or task.size or 0)
        task.bytes_done = int(state.get("completedLength") or task.bytes_done or 0)
        task.speed = int(state.get("downloadSpeed") or 0)
        if status in ("active", "waiting", "paused"):
            task.updated_at = utcnow()
            await session.commit()
            continue
        if status == "complete":
            _set_status(task, TaskStatus.VERIFYING)
            task.speed = 0
            await session.commit()
            await _verify_task(session, job, task)
            continue
        if status in ("error", "removed"):
            error_message = str(state.get("errorMessage") or status)
            await _handle_download_failure(session, task, error_message)
    await session.commit()


async def _requeue_lost_download(session: AsyncSession, task: DownloadTask) -> None:
    task.aria2_gid = None
    task.speed = 0
    if task.route in ("account", "tar"):
        await _clear_account_link(session, task)
        task.download_cycles += 1
        if task.download_cycles >= 3:
            _set_status(task, TaskStatus.FAILED)
            task.error_code = "CDN_LINK_EXPIRED"
            task.finished_at = utcnow()
        else:
            task.queue_reason = "ARIA2_GID_LOST"
            _set_status(task, TaskStatus.ACCOUNT_QUEUED)
    else:
        task.source_url_enc = None
        task.source_url_expire = None
        _set_status(task, TaskStatus.DISCOVERED)
    await session.commit()


async def _handle_download_failure(
    session: AsyncSession, task: DownloadTask, message: str
) -> None:
    task.error_message = message
    task.speed = 0
    task.aria2_gid = None
    if task.route in ("account", "tar"):
        await _clear_account_link(session, task)
        task.download_cycles += 1
        if task.download_cycles >= 3:
            _set_status(task, TaskStatus.FAILED)
            task.error_code = "CDN_LINK_EXPIRED"
            task.finished_at = utcnow()
        else:
            task.queue_reason = "DOWNLOAD_LINK_FAILED"
            _set_status(task, TaskStatus.ACCOUNT_QUEUED)
    else:
        task.source_url_enc = None
        task.source_url_expire = None
        task.route = None
        task.queue_reason = "ANONYMOUS_FAILED"
        _set_status(task, TaskStatus.ACCOUNT_QUEUED)
    events.emit(
        session,
        "download_task",
        task.id,
        "download_task.download_failed",
        {"message": message, "route": task.route},
    )
    await session.commit()


async def _prepare_download_target(
    target: Path, task: DownloadTask
) -> tuple[Path, str | None, object | None]:
    """Resolve an existing target before writing; returns (path, action, verify_result)."""
    settings = get_settings()
    if target.is_file():
        if target.stat().st_size == task.size:
            existing = await asyncio.to_thread(
                verify_file, target, task.size, task.expected_hash
            )
            if existing.ok:
                return target, "reuse", existing
        policy = settings.duplicate_name_policy
        if policy == "skip":
            return target, "skip", None
        if policy == "overwrite":
            target.unlink(missing_ok=True)
            return target, None, None
        return _unique_target(target), None, None
    return target, None, None


async def _mark_task_done(
    session: AsyncSession,
    job: DownloadJob,
    task: DownloadTask,
    target: Path,
    result,
) -> None:
    task.target_path = _download_relative(target)
    task.object_id = None
    task.bytes_done = task.size
    task.verified_chunk = result.chunk
    task.speed = 0
    task.source_url_enc = None
    task.error_code = None
    task.error_message = None
    _set_status(task, TaskStatus.DONE)
    task.finished_at = utcnow()
    events.emit(
        session,
        "download_task",
        task.id,
        "download_task.done",
        {
            "hash": task.expected_hash,
            "size": task.size,
            "chunk": result.chunk,
            "target_path": task.target_path,
        },
    )
    await _refresh_job(session, job)
    await session.commit()
    if task.route == "tar" and task.tar_folder_id:
        await _delete_tar_folder(session, task)


async def _mark_duplicate_skipped(
    session: AsyncSession,
    job: DownloadJob,
    task: DownloadTask,
    target: Path,
    chunk: int | None,
) -> None:
    task.target_path = _download_relative(target)
    task.object_id = None
    task.bytes_done = 0
    task.verified_chunk = chunk
    task.speed = 0
    task.source_url_enc = None
    task.error_code = "DUPLICATE_SKIPPED"
    task.error_message = "同名文件已存在且内容不同，按设置跳过"
    _set_status(task, TaskStatus.DONE)
    task.finished_at = utcnow()
    events.emit(
        session,
        "download_task",
        task.id,
        "download_task.duplicate_skipped",
        {"target_path": task.target_path},
    )
    await _refresh_job(session, job)
    await session.commit()
    if task.route == "tar" and task.tar_folder_id:
        await _delete_tar_folder(session, task)


async def _verify_task(session: AsyncSession, job: DownloadJob, task: DownloadTask) -> None:
    if not task.expected_hash:
        _set_status(task, TaskStatus.FAILED)
        task.error_code = "INVALID_FILE_METADATA"
        task.finished_at = utcnow()
        await _refresh_job(session, job)
        await session.commit()
        return
    settings = get_settings()
    temp = settings.cache_dir / task.id / "download.bin"
    if task.route == "tar":
        result = None
        if task.tar_size and temp.is_file() and temp.stat().st_size == task.tar_size:
            relative = task.target_path or _join_relative(
                job.target_subdir, task.relative_path
            )
            target = _download_path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)

            target, action, existing = await _prepare_download_target(target, task)
            if action == "reuse" and existing is not None:
                temp.unlink(missing_ok=True)
                shutil.rmtree(settings.cache_dir / task.id, ignore_errors=True)
                await _mark_task_done(session, job, task, target, existing)
                return
            if action == "skip":
                temp.unlink(missing_ok=True)
                shutil.rmtree(settings.cache_dir / task.id, ignore_errors=True)
                await _mark_duplicate_skipped(session, job, task, target, None)
                return

            partial = target.parent / f".{task.id}.part"
            partial.unlink(missing_ok=True)
            extracted_ok = await asyncio.to_thread(
                extract_tar_member,
                temp,
                partial,
                expected_size=task.size,
                expected_name=task.name,
            )
            if extracted_ok:
                result = await asyncio.to_thread(
                    verify_file, partial, task.size, task.expected_hash
                )
            if result is not None and result.ok:
                target, action, existing = await _prepare_download_target(
                    target, task
                )
                if action == "reuse" and existing is not None:
                    partial.unlink(missing_ok=True)
                    shutil.rmtree(settings.cache_dir / task.id, ignore_errors=True)
                    await _mark_task_done(session, job, task, target, existing)
                    return
                if action == "skip":
                    partial.unlink(missing_ok=True)
                    shutil.rmtree(settings.cache_dir / task.id, ignore_errors=True)
                    await _mark_duplicate_skipped(
                        session, job, task, target, result.chunk
                    )
                    return
                os.replace(partial, target)
                shutil.rmtree(settings.cache_dir / task.id, ignore_errors=True)
                await _mark_task_done(session, job, task, target, result)
                return
            partial.unlink(missing_ok=True)
    else:
        result = await asyncio.to_thread(
            verify_file, temp, task.size, task.expected_hash
        )
    if task.route != "tar" and result is not None and result.ok:
        settings = get_settings()
        relative = task.target_path or _join_relative(
            job.target_subdir, task.relative_path
        )
        target = _download_path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            reusable = target.stat().st_size == task.size
            if reusable:
                existing_result = await asyncio.to_thread(
                    verify_file, target, task.size, task.expected_hash
                )
                reusable = existing_result.ok
            if reusable:
                temp.unlink(missing_ok=True)
            else:
                policy = settings.duplicate_name_policy
                if policy == "overwrite":
                    target.unlink(missing_ok=True)
                elif policy == "skip":
                    temp.unlink(missing_ok=True)
                    control = Path(str(temp) + ".aria2")
                    control.unlink(missing_ok=True)
                    shutil.rmtree(settings.cache_dir / task.id, ignore_errors=True)
                    task.target_path = _download_relative(target)
                    task.object_id = None
                    task.bytes_done = 0
                    task.verified_chunk = result.chunk
                    task.speed = 0
                    task.source_url_enc = None
                    task.error_code = "DUPLICATE_SKIPPED"
                    task.error_message = "同名文件已存在且内容不同，按设置跳过"
                    _set_status(task, TaskStatus.DONE)
                    task.finished_at = utcnow()
                    events.emit(
                        session,
                        "download_task",
                        task.id,
                        "download_task.duplicate_skipped",
                        {"target_path": task.target_path},
                    )
                    await _refresh_job(session, job)
                    await session.commit()
                    return
                else:
                    target = _unique_target(target)
        if target.exists():
            temp.unlink(missing_ok=True)
        else:
            try:
                os.replace(temp, target)
            except OSError:
                shutil.move(str(temp), str(target))
        control = Path(str(temp) + ".aria2")
        control.unlink(missing_ok=True)
        shutil.rmtree(settings.cache_dir / task.id, ignore_errors=True)
        task.target_path = _download_relative(target)
        task.object_id = None
        task.bytes_done = task.size
        task.verified_chunk = result.chunk
        task.speed = 0
        task.source_url_enc = None
        task.error_code = None
        task.error_message = None
        _set_status(task, TaskStatus.DONE)
        task.finished_at = utcnow()
        events.emit(
            session,
            "download_task",
            task.id,
            "download_task.done",
            {
                "hash": task.expected_hash,
                "size": task.size,
                "chunk": result.chunk,
                "target_path": task.target_path,
            },
        )
        await _refresh_job(session, job)
        await session.commit()
        return

    task.verify_attempts += 1
    temp.unlink(missing_ok=True)
    Path(str(temp) + ".aria2").unlink(missing_ok=True)
    if task.route == "tar":
        shutil.rmtree(get_settings().cache_dir / task.id, ignore_errors=True)
    task.aria2_gid = None
    task.bytes_done = 0
    task.speed = 0
    task.verified_chunk = None
    if task.verify_attempts < 3:
        await _clear_account_link(session, task)
        task.route = None
        task.error_code = "VERIFY_RETRY"
        task.error_message = "Merkle 校验失败，重新下载"
        _set_status(task, TaskStatus.DISCOVERED)
    else:
        task.error_code = "CORRUPT"
        task.error_message = "Merkle 校验连续失败"
        _set_status(task, TaskStatus.FAILED)
        task.finished_at = utcnow()
    events.emit(
        session,
        "download_task",
        task.id,
        "download_task.verify_failed",
        {"attempt": task.verify_attempts, "hash": task.expected_hash},
    )
    await _refresh_job(session, job)
    await session.commit()


async def pause_task(session: AsyncSession, task_id: str) -> DownloadTask:
    task = await _get_task(session, task_id)
    if task.status == TaskStatus.PAUSED:
        return task
    if task.status not in (
        TaskStatus.DISCOVERED,
        TaskStatus.PLANNING,
        TaskStatus.ACCOUNT_QUEUED,
        TaskStatus.RESTORING,
        TaskStatus.WCL_READY,
        TaskStatus.VERIFYING,
        TaskStatus.ANON_DOWNLOADING,
        TaskStatus.ACCOUNT_DOWNLOADING,
    ):
        raise StateConflict("当前状态不可暂停")
    task.resume_state = task.status
    if task.status in (TaskStatus.ANON_DOWNLOADING, TaskStatus.ACCOUNT_DOWNLOADING):
        if task.aria2_gid:
            await aria2.pause(task.aria2_gid)
    if task.account_id:
        await release_account(session, task.account_id, task.id)
    _set_status(task, TaskStatus.PAUSED)
    task.speed = 0
    events.emit(session, "download_task", task.id, "download_task.paused", {})
    await session.commit()
    return task


async def resume_task(session: AsyncSession, task_id: str) -> DownloadTask:
    task = await _get_task(session, task_id)
    if task.status != TaskStatus.PAUSED:
        raise StateConflict("仅暂停任务可继续")
    target = task.resume_state or TaskStatus.DISCOVERED
    task.resume_state = None
    if target in (TaskStatus.ANON_DOWNLOADING, TaskStatus.ACCOUNT_DOWNLOADING) and task.aria2_gid:
        try:
            await aria2.unpause(task.aria2_gid)
            _set_status(task, target)
        except Aria2Error:
            _set_status(task, TaskStatus.DISCOVERED)
    elif target in (TaskStatus.RESTORING, TaskStatus.WCL_READY):
        _set_status(task, TaskStatus.ACCOUNT_QUEUED)
    else:
        _set_status(task, TaskStatus.DISCOVERED)
    events.emit(session, "download_task", task.id, "download_task.resumed", {})
    await session.commit()
    return task


async def cancel_task(session: AsyncSession, task_id: str) -> DownloadTask:
    task = await _get_task(session, task_id)
    if task.status == TaskStatus.DONE:
        raise StateConflict("已完成任务不可取消")
    if task.status == TaskStatus.CANCELLED:
        return task
    if task.aria2_gid:
        await aria2.remove(task.aria2_gid)
        task.aria2_gid = None
        await session.commit()
    if task.drive_file_id and not task.remote_deleted_at and task.account_id:
        account = await session.get(Account, task.account_id)
        if account is not None:
            client = make_client(account)
            try:
                await _delete_drive_file(session, task, account, client)
            finally:
                await client.close()
    if task.account_id:
        await release_account(session, task.account_id, task.id)
    if task.tar_folder_id:
        await _delete_tar_folder(session, task)
    shutil.rmtree(get_settings().cache_dir / task.id, ignore_errors=True)
    _set_status(task, TaskStatus.CANCELLED)
    task.finished_at = utcnow()
    task.speed = 0
    events.emit(session, "download_task", task.id, "download_task.cancelled", {})
    job = await session.get(DownloadJob, task.job_id)
    if job is not None:
        await _refresh_job(session, job)
    await session.commit()
    return task


async def retry_task(session: AsyncSession, task_id: str) -> DownloadTask:
    task = await _get_task(session, task_id)
    if task.status not in (TaskStatus.FAILED, TaskStatus.CANCELLED):
        raise StateConflict("仅失败或已取消任务可重试")
    task.attempts = 0
    task.verify_attempts = 0
    task.download_cycles = 0
    task.error_code = None
    task.error_message = None
    task.queue_reason = None
    task.resume_state = None
    task.aria2_gid = None
    task.route = None
    task.object_id = None
    task.bytes_done = 0
    task.speed = 0
    task.source_url_enc = None
    task.source_url_expire = None
    task.source_share_id = None
    task.source_file_id = None
    task.drive_file_id = None
    task.restore_task_id = None
    task.restore_folder_id = None
    task.restore_started_at = None
    task.remote_deleted_at = None
    task.tar_size = 0
    task.tar_folder_id = None
    task.finished_at = None
    _set_status(task, TaskStatus.DISCOVERED)
    job = await session.get(DownloadJob, task.job_id)
    if job is not None and job.status in JobStatus.TERMINAL:
        job.status = JobStatus.READY
        job.finished_at = None
        job.error_code = None
        job.error_message = None
    if job is not None:
        await _refresh_job(session, job)
    events.emit(session, "download_task", task.id, "download_task.retried", {})
    await session.commit()
    return task


async def retry_job(session: AsyncSession, job_id: str) -> DownloadJob:
    job = await _get_job(session, job_id)
    tasks = (
        await session.execute(
            select(DownloadTask).where(
                DownloadTask.job_id == job.id,
                DownloadTask.status.in_([TaskStatus.FAILED, TaskStatus.CANCELLED]),
            )
        )
    ).scalars().all()
    for task in tasks:
        await retry_task(session, task.id)
    job.status = JobStatus.READY
    job.finished_at = None
    job.error_code = None
    job.error_message = None
    await _refresh_job(session, job)
    await session.commit()
    return job


async def cancel_job(session: AsyncSession, job_id: str) -> DownloadJob:
    job = await _get_job(session, job_id)
    if job.status == JobStatus.CANCELLED:
        return job
    tasks = (
        await session.execute(
            select(DownloadTask).where(
                DownloadTask.job_id == job.id,
                DownloadTask.status.notin_(TaskStatus.TERMINAL),
            )
        )
    ).scalars().all()
    for task in tasks:
        await cancel_task(session, task.id)
    job.status = JobStatus.CANCELLED
    job.finished_at = utcnow()
    events.emit(session, "download_job", job.id, "download_job.cancelled", {})
    await session.commit()
    return job




async def delete_job(session: AsyncSession, job_id: str) -> str:
    job = await _get_job(session, job_id)
    if job.status not in JobStatus.TERMINAL:
        raise StateConflict("任务仍在进行，请先取消再删除")
    tasks = (
        await session.execute(select(DownloadTask).where(DownloadTask.job_id == job.id))
    ).scalars().all()
    task_ids = [task.id for task in tasks]
    object_ids = sorted({task.object_id for task in tasks if task.object_id})
    target_paths = sorted({task.target_path for task in tasks if task.target_path})
    settings = get_settings()

    for task in tasks:
        if task.tar_folder_id:
            await _delete_tar_folder(session, task)

    for task in tasks:
        relative = task.target_path or _join_relative(
            job.target_subdir, task.relative_path
        )
        if not relative:
            continue
        try:
            partial = _download_path(relative).parent / f".{task.id}.part"
        except ValueError:
            continue
        partial.unlink(missing_ok=True)

    for relative in target_paths:
        shared = (
            await session.execute(
                select(func.count(DownloadTask.id)).where(
                    DownloadTask.target_path == relative,
                    DownloadTask.job_id != job.id,
                )
            )
        ).scalar_one()
        if shared:
            continue
        try:
            path = _download_path(relative)
        except ValueError:
            continue
        if path.is_file():
            path.unlink(missing_ok=True)
        parent = path.parent
        root = settings.downloads_dir.resolve()
        while parent != root and parent.is_dir() and not any(parent.iterdir()):
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    for task_id in task_ids:
        shutil.rmtree(settings.cache_dir / task_id, ignore_errors=True)
    if task_ids:
        await session.execute(
            delete(Event).where(
                Event.entity_type == "download_task",
                Event.entity_id.in_(task_ids),
            )
        )
    await session.execute(delete(DownloadTask).where(DownloadTask.job_id == job.id))
    await session.execute(delete(DownloadFolder).where(DownloadFolder.job_id == job.id))
    await session.execute(delete(DownloadJobItem).where(DownloadJobItem.job_id == job.id))
    await session.execute(
        delete(Event).where(
            Event.entity_type == "download_job",
            Event.entity_id == job.id,
        )
    )
    await session.execute(delete(DownloadJob).where(DownloadJob.id == job.id))
    await session.flush()

    for object_id in object_ids:
        still_used = (
            await session.execute(
                select(func.count(DownloadTask.id)).where(
                    DownloadTask.object_id == object_id
                )
            )
        ).scalar_one()
        if still_used:
            continue
        obj = await session.get(FileObject, object_id)
        if obj is None:
            continue
        path = (settings.data_dir / obj.rel_path).resolve()
        try:
            path.relative_to(settings.data_dir.resolve())
        except ValueError:
            path = None
        if path is not None and path.is_file():
            try:
                path.unlink(missing_ok=True)
            except OSError:
                log.warning("download_file_delete_failed", path=str(path))
        await session.delete(obj)

    await session.commit()
    return job.id


async def recover_interrupted(session: AsyncSession) -> None:
    from sqlalchemy import update as sa_update

    await session.execute(
        sa_update(Account)
        .where(Account.lease_task_id.is_not(None))
        .values(lease_task_id=None, lease_expires_at=None)
    )
    await session.execute(
        sa_update(DownloadTask)
        .where(DownloadTask.status == TaskStatus.PLANNING)
        .values(status=TaskStatus.DISCOVERED, updated_at=utcnow())
    )
    await session.execute(
        sa_update(DownloadFolder)
        .where(DownloadFolder.status == FolderStatus.ENUMERATING)
        .values(status=FolderStatus.PENDING)
    )
    await session.commit()
