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
        from app.services.download import StateConflict
        task = await session.get(DownloadTask, task_id)
        task.status = "RESTORING"
        await session.commit()
        try:
            await pause_task(session, task_id)
        except StateConflict:
            pass
        else:
            raise AssertionError("RESTORING must not be pausable")
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
