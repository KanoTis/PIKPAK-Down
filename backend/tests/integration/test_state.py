from __future__ import annotations

import httpx
import pytest
from fake_pikpak_server import ACCOUNT_SHARE


@pytest.mark.asyncio
async def test_pause_resume_cancel_retry_state_machine(env, fake_server) -> None:
    from app.db import get_session_factory
    from app.models import DownloadTask
    from app.services.download import (
        cancel_job,
        cancel_task,
        create_job,
        dispatch_downloads,
        pause_task,
        resume_task,
        retry_task,
    )
    from sqlalchemy import select

    factory = get_session_factory()
    async with factory() as session:
        job = await create_job(
            session, input_url=f"https://mypikpak.com/s/{ACCOUNT_SHARE}", pass_code=None
        )
        await session.commit()
        job_id = job.id

    task = None
    for _ in range(40):
        async with factory() as session:
            await dispatch_downloads(session)
            task = (
                await session.execute(select(DownloadTask).where(DownloadTask.job_id == job_id))
            ).scalar_one_or_none()
            if task is not None and task.status == "ACCOUNT_QUEUED":
                break
        import asyncio

        await asyncio.sleep(0.05)
    assert task is not None and task.status == "ACCOUNT_QUEUED"
    task_id = task.id

    async with factory() as session:
        task = await session.get(DownloadTask, task_id)
        task.status = "RESTORING"
        await session.commit()
        paused = await pause_task(session, task_id)
        assert paused.status == "PAUSED"
        resumed = await resume_task(session, task_id)
        assert resumed.status == "ACCOUNT_QUEUED"
        task.status = "ACCOUNT_QUEUED"
        await session.commit()
        paused = await pause_task(session, task_id)
        assert paused.status == "PAUSED"
        resumed = await resume_task(session, task_id)
        assert resumed.status == "DISCOVERED"
        cancelled = await cancel_task(session, task_id)
        assert cancelled.status == "CANCELLED"
        from app.models import DownloadJob
        job = await session.get(DownloadJob, job_id)
        assert job.status == "CANCELLED"
        retried = await retry_task(session, task_id)
        assert retried.status == "DISCOVERED"
        job = await session.get(DownloadJob, job_id)
        assert job.status == "READY"
        job = await cancel_job(session, job_id)
        assert job.status == "CANCELLED"


@pytest.mark.asyncio
async def test_account_drive_lists_root_items(env, fake_server) -> None:
    from app.db import get_session_factory
    from app.main import app
    from app.services.accounts import import_account

    factory = get_session_factory()
    async with factory() as session:
        account = await import_account(
            session,
            label="drive-list",
            email=None,
            refresh_token="fake-refresh-token",
        )
        await session.commit()
        account_id = account.id

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/api/accounts/{account_id}/drive")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["parent_id"] == ""
    assert body["quota"]["limit"]
    assert isinstance(body["files"], list)


@pytest.mark.asyncio
async def test_reconcile_endpoint_removes_terminal_restore_folder(env, fake_server) -> None:
    from app.db import get_session_factory
    from app.main import app
    from app.models import DownloadJob, DownloadTask, JobStatus, TaskStatus
    from app.services.accounts import import_account

    factory = get_session_factory()
    async with factory() as session:
        account = await import_account(
            session,
            label="reconcile-api",
            email=None,
            refresh_token="fake-refresh-token",
        )
        job = DownloadJob(
            input_url="https://mypikpak.com/s/RECONCILEAPI00001",
            share_id="RECONCILEAPI00001",
            status=JobStatus.READY,
        )
        session.add(job)
        await session.flush()
        task = DownloadTask(
            job_id=job.id,
            account_id=account.id,
            share_file_id="RECONCILEAPIFILE01",
            drive_file_id="RECONCILEAPIFILE01",
            restore_folder_id="RESTORE_FOLDER",
            name="reconcile-api.bin",
            relative_path="reconcile-api.bin",
            size=1,
            expected_hash="HASH",
            status=TaskStatus.DONE,
        )
        session.add(task)
        await session.commit()
        account_id = account.id
        task_id = task.id

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(f"/api/accounts/{account_id}/reconcile")
    assert response.status_code == 200
    body = response.json()
    assert body["tar_folders_removed"] == 0
    assert body["files"]["folders_removed"] == 1
    assert "quota_before" in body and "quota_after" in body

    async with factory() as session:
        task = await session.get(DownloadTask, task_id)
        assert task.remote_deleted_at is not None


@pytest.mark.asyncio
async def test_pause_retries_when_database_is_locked(env, fake_server) -> None:
    import sqlite3

    from app.db import get_session_factory
    from app.main import app
    from app.models import DownloadJob, DownloadTask, JobStatus, TaskStatus
    from app.services import download
    from sqlalchemy.exc import OperationalError

    factory = get_session_factory()
    async with factory() as session:
        job = DownloadJob(
            input_url="https://mypikpak.com/s/PAUSELOCK0000001",
            share_id="PAUSELOCK0000001",
            status=JobStatus.READY,
        )
        session.add(job)
        await session.flush()
        task = DownloadTask(
            job_id=job.id,
            share_file_id="PAUSELOCKFILE0001",
            name="paused.bin",
            relative_path="paused.bin",
            size=1,
            expected_hash="HASH",
            status=TaskStatus.ACCOUNT_QUEUED,
        )
        session.add(task)
        await session.commit()
        task_id = task.id

    original = download.pause_task
    calls = 0

    async def flaky_pause(session, target_id):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise OperationalError(
                "UPDATE download_tasks",
                {},
                sqlite3.OperationalError("database is locked"),
            )
        return await original(session, target_id)

    download.pause_task = flaky_pause
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(f"/api/downloads/{task_id}/pause")
        assert response.status_code == 200
        assert response.json()["status"] == TaskStatus.PAUSED
        assert calls == 3
    finally:
        download.pause_task = original


@pytest.mark.asyncio
async def test_api_error_contracts(env, fake_server) -> None:
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/accounts",
            json={"label": "bad", "refresh_token": "not-a-token"},
        )
        assert response.status_code == 400
        body = response.json()
        assert body["error"]["code"] == "ACCOUNT_TOKEN_INVALID"

        response = await client.post(
            "/api/download-jobs",
            json={"url": "not a share url"},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_SHARE_URL"

        response = await client.post(
            "/api/download-jobs",
            json={"url": "https://mypikpak.com/s/VANON000000000001", "items": []},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "EMPTY_SELECTION"

        response = await client.get("/api/download-jobs/missing")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "JOB_NOT_FOUND"

        response = await client.post(
            "/api/downloads/missing/pause",
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "TASK_NOT_FOUND"

        response = await client.post(
            "/api/lineage-runs",
            json={"input": "https://mypikpak.com/s/VANON000000000001", "include_tree": "nope"},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"
