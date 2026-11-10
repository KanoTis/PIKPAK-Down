from __future__ import annotations

import os
from pathlib import Path

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_session
from ..models import (
    DownloadJob,
    DownloadTask,
    FileObject,
    TaskStatus,
)
from ..pikpak.errors import PikPakError
from ..services import download as service
from ..services.preview import preview_share
from .schemas import (
    DownloadJobCreate,
    DownloadJobOut,
    DownloadTaskOut,
    PreviewItemOut,
    SharePreviewOut,
    SharePreviewRequest,
    TaskListOut,
)

router = APIRouter(prefix="/api", tags=["downloads"])


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def job_out(job: DownloadJob) -> DownloadJobOut:
    return DownloadJobOut(
        id=job.id,
        input_url=job.input_url,
        share_id=job.share_id,
        root_file_id=job.root_file_id,
        root_kind=job.root_kind,
        root_name=job.root_name,
        target_subdir=job.target_subdir,
        status=job.status,
        total_files=job.total_files,
        completed_files=job.completed_files,
        failed_files=job.failed_files,
        total_bytes=job.total_bytes,
        downloaded_bytes=job.downloaded_bytes,
        folder_total=job.folder_total,
        folder_done=job.folder_done,
        error_code=job.error_code,
        error_message=job.error_message,
        created_at=iso(job.created_at),
        started_at=iso(job.started_at),
        finished_at=iso(job.finished_at),
    )


def task_out(task: DownloadTask) -> DownloadTaskOut:
    return DownloadTaskOut(
        id=task.id,
        job_id=task.job_id,
        share_file_id=task.share_file_id,
        name=task.name,
        relative_path=task.relative_path,
        target_path=task.target_path,
        size=task.size,
        expected_hash=task.expected_hash,
        mime_type=task.mime_type,
        is_video=task.is_video,
        route=task.route,
        status=task.status,
        resume_state=task.resume_state,
        source_share_id=task.source_share_id,
        source_file_id=task.source_file_id,
        source_url_expire=task.source_url_expire,
        account_id=task.account_id,
        account_label=task.account_label,
        drive_file_id=task.drive_file_id,
        remote_deleted_at=iso(task.remote_deleted_at),
        aria2_gid=task.aria2_gid,
        bytes_done=task.bytes_done,
        bytes_total=task.bytes_total,
        speed=task.speed,
        attempts=task.attempts,
        verify_attempts=task.verify_attempts,
        download_cycles=task.download_cycles,
        object_id=task.object_id,
        error_code=task.error_code,
        error_message=task.error_message,
        queue_reason=task.queue_reason,
        verified_chunk=task.verified_chunk,
        sort_index=task.sort_index,
        created_at=iso(task.created_at),
        started_at=iso(task.started_at),
        finished_at=iso(task.finished_at),
    )


def _error(status: int, code: str, message: str, *, retryable: bool = False) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"error": {"code": code, "message": message, "retryable": retryable, "details": {}}},
    )


@router.post("/download-jobs", response_model=DownloadJobOut, status_code=202)
async def create_download_job(
    body: DownloadJobCreate, session: AsyncSession = Depends(get_session)
) -> DownloadJobOut:
    try:
        if body.items is not None and not body.items:
            raise _error(400, "EMPTY_SELECTION", "请选择要下载的文件或文件夹")
        job = await service.create_job(
            session,
            input_url=body.url,
            pass_code=body.pass_code,
            items=[item.model_dump() for item in body.items] if body.items is not None else None,
            target_subdir=body.target_subdir,
        )
    except ValueError as exc:
        raise _error(400, "INVALID_SHARE_URL", str(exc)) from exc
    await session.commit()
    return job_out(job)


@router.get("/download-targets")
async def list_download_targets() -> dict:
    settings = get_settings()
    root = settings.downloads_dir
    root.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, str]] = [{"path": "", "label": "根目录"}]
    seen = 0
    for current, dirs, _files in os.walk(root, followlinks=False):
        relative = Path(current).relative_to(root).as_posix()
        depth = 0 if relative == "." else relative.count("/") + 1
        if depth >= 4:
            dirs[:] = []
            continue
        dirs[:] = sorted(
            name for name in dirs if not name.startswith(".") and not (Path(current) / name).is_symlink()
        )
        for name in dirs:
            child = (Path(current) / name).relative_to(root).as_posix()
            items.append({"path": child, "label": child})
            seen += 1
            if seen >= 500:
                dirs[:] = []
                break
        if seen >= 500:
            break
    return {"root": str(root), "items": items}


@router.post("/share-preview", response_model=SharePreviewOut)
async def share_preview(body: SharePreviewRequest) -> SharePreviewOut:
    try:
        result = await preview_share(
            input_url=body.url,
            pass_code=body.pass_code,
            folder_id=body.folder_id,
            folder_name=body.folder_name,
            page_token=body.page_token,
            fetch_all=body.fetch_all if body.fetch_all is not None else True,
        )
    except ValueError as exc:
        raise _error(400, "INVALID_SHARE_URL", str(exc)) from exc
    except PikPakError as exc:
        raise _error(502, exc.code or "SHARE_PREVIEW_FAILED", str(exc)) from exc
    return SharePreviewOut(
        share_id=result.share_id,
        share_status=result.share_status,
        share_status_text=result.share_status_text,
        title=result.title,
        folder_id=result.folder_id,
        folder_name=result.folder_name,
        items=[PreviewItemOut(**item.__dict__) for item in result.items],
        next_page_token=result.next_page_token,
        is_root=result.is_root,
    )


@router.get("/download-jobs", response_model=list[DownloadJobOut])
async def list_download_jobs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> list[DownloadJobOut]:
    rows = (
        await session.execute(
            select(DownloadJob)
            .order_by(DownloadJob.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()
    return [job_out(job) for job in rows]


@router.get("/download-jobs/{job_id}", response_model=DownloadJobOut)
async def get_download_job(
    job_id: str, session: AsyncSession = Depends(get_session)
) -> DownloadJobOut:
    job = await session.get(DownloadJob, job_id)
    if job is None:
        raise _error(404, "JOB_NOT_FOUND", "下载任务不存在")
    return job_out(job)


def _parse_cursor(cursor: str | None) -> tuple[int, str] | None:
    if not cursor:
        return None
    try:
        sort_part, row_id = cursor.split(":", 1)
        return int(sort_part), row_id
    except (ValueError, AttributeError):
        return None


@router.get("/download-jobs/{job_id}/files", response_model=TaskListOut)
async def list_job_files(
    job_id: str,
    cursor: str | None = None,
    limit: int = Query(100, ge=1, le=200),
    state: str | None = None,
    q: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> TaskListOut:
    job = await session.get(DownloadJob, job_id)
    if job is None:
        raise _error(404, "JOB_NOT_FOUND", "下载任务不存在")
    stmt = select(DownloadTask).where(DownloadTask.job_id == job_id)
    if state:
        stmt = stmt.where(DownloadTask.status == state)
    if q:
        stmt = stmt.where(DownloadTask.name.ilike(f"%{q}%"))
    parsed = _parse_cursor(cursor)
    if parsed is not None:
        sort_index, row_id = parsed
        stmt = stmt.where(
            or_(
                DownloadTask.sort_index > sort_index,
                and_(DownloadTask.sort_index == sort_index, DownloadTask.id > row_id),
            )
        )
    rows = (
        await session.execute(
            stmt.order_by(DownloadTask.sort_index.asc(), DownloadTask.id.asc()).limit(limit + 1)
        )
    ).scalars().all()
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = f"{last.sort_index}:{last.id}"
        rows = rows[:limit]
    return TaskListOut(items=[task_out(task) for task in rows], next_cursor=next_cursor)


@router.post("/download-jobs/{job_id}/cancel", response_model=DownloadJobOut)
async def cancel_job(job_id: str, session: AsyncSession = Depends(get_session)) -> DownloadJobOut:
    try:
        job = await service.cancel_job(session, job_id)
    except service.DownloadNotFound as exc:
        raise _error(404, "JOB_NOT_FOUND", str(exc)) from exc
    return job_out(job)


@router.delete("/download-jobs/{job_id}", status_code=204)
async def delete_job(
    job_id: str, session: AsyncSession = Depends(get_session)
) -> Response:
    try:
        await service.delete_job(session, job_id)
    except service.DownloadNotFound as exc:
        raise _error(404, "JOB_NOT_FOUND", str(exc)) from exc
    except service.StateConflict as exc:
        raise _error(409, "STATE_CONFLICT", str(exc)) from exc
    return Response(status_code=204)


@router.get("/downloads/{task_id}", response_model=DownloadTaskOut)
async def get_download(task_id: str, session: AsyncSession = Depends(get_session)) -> DownloadTaskOut:
    task = await session.get(DownloadTask, task_id)
    if task is None:
        raise _error(404, "TASK_NOT_FOUND", "文件任务不存在")
    return task_out(task)


async def _task_action(session: AsyncSession, task_id: str, action: str) -> DownloadTaskOut:
    try:
        if action == "pause":
            task = await service.pause_task(session, task_id)
        elif action == "resume":
            task = await service.resume_task(session, task_id)
        elif action == "retry":
            task = await service.retry_task(session, task_id)
        elif action == "cancel":
            task = await service.cancel_task(session, task_id)
        else:
            raise _error(400, "INVALID_ACTION", "未知操作")
    except service.DownloadNotFound as exc:
        raise _error(404, "TASK_NOT_FOUND", str(exc)) from exc
    except service.StateConflict as exc:
        raise _error(409, "STATE_CONFLICT", str(exc)) from exc
    return task_out(task)


@router.post("/downloads/{task_id}/pause", response_model=DownloadTaskOut)
async def pause_download(task_id: str, session: AsyncSession = Depends(get_session)) -> DownloadTaskOut:
    return await _task_action(session, task_id, "pause")


@router.post("/downloads/{task_id}/resume", response_model=DownloadTaskOut)
async def resume_download(task_id: str, session: AsyncSession = Depends(get_session)) -> DownloadTaskOut:
    return await _task_action(session, task_id, "resume")


@router.post("/downloads/{task_id}/retry", response_model=DownloadTaskOut)
async def retry_download(task_id: str, session: AsyncSession = Depends(get_session)) -> DownloadTaskOut:
    return await _task_action(session, task_id, "retry")


@router.post("/downloads/{task_id}/cancel", response_model=DownloadTaskOut)
async def cancel_download(task_id: str, session: AsyncSession = Depends(get_session)) -> DownloadTaskOut:
    return await _task_action(session, task_id, "cancel")


@router.get("/downloads/{task_id}/file")
async def download_file(task_id: str, session: AsyncSession = Depends(get_session)):
    task = await session.get(DownloadTask, task_id)
    if task is None:
        raise _error(404, "TASK_NOT_FOUND", "文件任务不存在")
    if task.status != TaskStatus.DONE:
        raise _error(409, "TASK_NOT_READY", "文件尚未完成校验")
    settings = get_settings()
    if task.target_path:
        path = (settings.downloads_dir / task.target_path).resolve()
        root = settings.downloads_dir.resolve()
        if root not in path.parents or not path.is_file():
            raise _error(404, "OBJECT_NOT_FOUND", "本地文件不存在")
    elif task.object_id:
        obj = await session.get(FileObject, task.object_id)
        if obj is None:
            raise _error(404, "OBJECT_NOT_FOUND", "本地对象不存在")
        path = (settings.data_dir / obj.rel_path).resolve()
        files_root = settings.files_dir.resolve()
        if files_root not in path.parents or not path.is_file():
            raise _error(404, "OBJECT_NOT_FOUND", "本地文件不存在")
    else:
        raise _error(404, "OBJECT_NOT_FOUND", "本地文件不存在")
    if path.stat().st_size != task.size:
        raise _error(409, "OBJECT_SIZE_MISMATCH", "本地文件大小异常")
    return FileResponse(
        path,
        filename=task.name or path.name,
        media_type=task.mime_type or "application/octet-stream",
    )
