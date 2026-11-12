from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fake_pikpak_server import ACCOUNT_SHARE, FOLDER_SHARE


@pytest.mark.asyncio
async def test_empty_trash_refusal_returns_409_with_details(env, fake_server, monkeypatch) -> None:
    from app.db import get_session_factory
    from app.main import app
    from app.services import download
    from app.services.accounts import import_account

    factory = get_session_factory()
    async with factory() as session:
        account = await import_account(
            session,
            label="trash-refused",
            email=None,
            refresh_token="fake-refresh-token",
        )
        await session.commit()
        account_id = account.id

    async def refused(session, account, *, force=False):
        return {
            "refused": True,
            "trash_bytes": 12345,
            "quota_before": {},
            "quota_after": {},
        }

    monkeypatch.setattr(download, "empty_account_trash", refused)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(f"/api/accounts/{account_id}/trash/empty")
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "TRASH_NOT_EMPTY"
    assert body["error"]["details"]["trash_bytes"] == 12345


@pytest.mark.asyncio
async def test_empty_trash_service_refuses_nonzero_trash(env, monkeypatch) -> None:
    from app.db import get_session_factory
    from app.models import Account
    from app.services import download

    class RefusingClient:
        def __init__(self) -> None:
            self.emptied = False

        async def about(self):
            return {"quota": {"limit": 100, "usage": 10, "usage_in_trash": 12345}}

        async def empty_trash(self):
            self.emptied = True
            return "TASK"

        async def close(self):
            return None

    client = RefusingClient()
    monkeypatch.setattr(download, "make_client", lambda account: client)
    factory = get_session_factory()
    async with factory() as session:
        account = Account(
            label="trash-guard",
            device_id="trash-guard-device",
            refresh_token_enc="token",
        )
        session.add(account)
        await session.flush()
        result = await download.empty_account_trash(session, account)
    assert result["refused"] is True
    assert result["trash_bytes"] == 12345
    assert client.emptied is False


@pytest.mark.asyncio
async def test_settings_save_does_not_override_connection_per_server(env, monkeypatch) -> None:
    from app.db import get_session_factory
    from app.services import settings as settings_service

    settings = settings_service.get_settings()
    monkeypatch.setattr(settings, "aria2_split", 8)
    monkeypatch.setattr(settings, "aria2_max_connection_per_server", 16)
    factory = get_session_factory()
    async with factory() as session:
        await settings_service.update_overrides(session, {"task_connections": 7})
    assert settings.aria2_split == 7
    assert settings.aria2_max_connection_per_server == 16


@pytest.mark.asyncio
async def test_folder_listing_failure_retries_then_fails_job(env) -> None:
    from app.db import get_session_factory
    from app.models import (
        DownloadFolder,
        DownloadJob,
        FolderStatus,
        JobStatus,
    )
    from app.pikpak.errors import PikPakError
    from app.services import download

    class FailingClient:
        async def list_folder(self, *args, **kwargs):
            raise PikPakError("folder list failed", code="SERVER_ERROR", retryable=True)

    factory = get_session_factory()
    async with factory() as session:
        job = DownloadJob(
            input_url="https://mypikpak.com/s/FOLDERFAIL000001",
            share_id="FOLDERFAIL000001",
            status=JobStatus.ENUMERATING,
        )
        session.add(job)
        await session.flush()
        folder = DownloadFolder(
            job_id=job.id,
            folder_id="FOLDERFAILROOT001",
            path="root",
            status=FolderStatus.PENDING,
        )
        session.add(folder)
        await session.commit()
        job_id = job.id
        folder_id = folder.id

    for attempt in range(3):
        async with factory() as session:
            job = await session.get(DownloadJob, job_id)
            with pytest.raises(PikPakError):
                await download._process_folders(session, job, FailingClient())
            folder = await session.get(DownloadFolder, folder_id)
            assert folder.attempts == attempt + 1
            if attempt < 2:
                assert folder.status == FolderStatus.PENDING
            else:
                assert folder.status == FolderStatus.FAILED

    async with factory() as session:
        job = await session.get(DownloadJob, job_id)
        assert job.status == JobStatus.FAILED
        assert job.error_code == "SERVER_ERROR"


@pytest.mark.asyncio
async def test_unexpected_planning_error_resets_task(env, monkeypatch) -> None:
    from app.db import get_session_factory
    from app.models import DownloadJob, DownloadTask, JobStatus, TaskStatus
    from app.services import download

    async def crash(session, task, client):
        download._set_status(task, TaskStatus.PLANNING)
        await session.commit()
        raise RuntimeError("boom")

    monkeypatch.setattr(download, "_plan_task", crash)
    factory = get_session_factory()
    async with factory() as session:
        job = DownloadJob(
            input_url="https://mypikpak.com/s/PLANCRASH0000001",
            share_id="PLANCRASH0000001",
            status=JobStatus.READY,
        )
        session.add(job)
        await session.flush()
        task = DownloadTask(
            job_id=job.id,
            share_file_id="PLANCRASHFILE001",
            name="crash.bin",
            relative_path="crash.bin",
            size=1,
            expected_hash="HASH",
            status=TaskStatus.DISCOVERED,
        )
        session.add(task)
        await session.commit()
        task_id = task.id
        await download._dispatch_plans(session)

    async with factory() as session:
        task = await session.get(DownloadTask, task_id)
        assert task.status == TaskStatus.DISCOVERED
        assert task.queue_reason == "PLAN_FAILED"


@pytest.mark.asyncio
async def test_concurrent_job_creation_merges_to_one_job(env) -> None:
    from app.db import get_session_factory
    from app.models import DownloadJob
    from app.services.download import create_job
    from sqlalchemy import func, select

    factory = get_session_factory()

    async def create() -> str:
        async with factory() as session:
            job = await create_job(
                session,
                input_url=f"https://mypikpak.com/s/{ACCOUNT_SHARE}",
                pass_code=None,
                target_subdir="concurrent",
            )
            await session.commit()
            return job.id

    ids = await asyncio.gather(*(create() for _ in range(4)))
    async with factory() as session:
        count = (
            await session.execute(
                select(func.count(DownloadJob.id)).where(
                    DownloadJob.share_id == ACCOUNT_SHARE,
                    DownloadJob.target_subdir == "concurrent",
                )
            )
        ).scalar_one()
    assert count == 1
    assert len(set(ids)) == 1


def test_job_out_uses_state_counts() -> None:
    from app.api.downloads import job_out
    from app.models import DownloadJob, JobStatus, TaskStatus

    job = DownloadJob(
        id="count-job",
        input_url="https://mypikpak.com/s/COUNTJOB00000001",
        share_id="COUNTJOB00000001",
        status=JobStatus.READY,
        target_subdir="",
        total_files=50_005,
        completed_files=0,
        failed_files=0,
        total_bytes=0,
        downloaded_bytes=0,
        folder_total=0,
        folder_done=0,
    )
    result = job_out(
        job,
        None,
        {
            TaskStatus.DONE: 50_000,
            TaskStatus.ANON_DOWNLOADING: 2,
            TaskStatus.PAUSED: 3,
        },
    )
    assert result.completed_files == 0
    assert result.active_files == 2
    assert result.queued_files == 3
    assert result.cancelled_files == 0


@pytest.mark.asyncio
async def test_audit_detects_task_missing_from_share(env, fake_server) -> None:
    from app.db import get_session_factory
    from app.main import app
    from app.models import DownloadJob, DownloadTask
    from app.services.download import create_job, dispatch_downloads
    from sqlalchemy import delete, select

    factory = get_session_factory()
    async with factory() as session:
        job = await create_job(
            session,
            input_url=f"https://mypikpak.com/s/{FOLDER_SHARE}",
            pass_code=None,
        )
        await session.commit()
        job_id = job.id

    for _ in range(300):
        async with factory() as session:
            await dispatch_downloads(session)
            job = await session.get(DownloadJob, job_id)
            tasks = (
                await session.execute(
                    select(DownloadTask).where(DownloadTask.job_id == job_id)
                )
            ).scalars().all()
            if len(tasks) == 2:
                break
        await asyncio.sleep(0.1)
    else:
        raise AssertionError("job did not become ready")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as client:
        response = await client.get(f"/api/download-jobs/{job_id}/audit")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["expected_files"] == 2
        assert body["missing_count"] == 0

    async with factory() as session:
        removed = (
            await session.execute(
                select(DownloadTask.id).where(DownloadTask.job_id == job_id).limit(1)
            )
        ).scalar_one()
        await session.execute(delete(DownloadTask).where(DownloadTask.id == removed))
        await session.commit()

    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as client:
        response = await client.get(f"/api/download-jobs/{job_id}/audit")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["task_files"] == 1
        assert body["missing_count"] == 1
        assert body["missing"][0]["file_id"]


@pytest.mark.asyncio
async def test_selection_merge_prefers_active_job_over_completed(env) -> None:
    from app.db import get_session_factory
    from app.models import DownloadJob, DownloadJobItem, JobStatus
    from app.services.download import create_job

    factory = get_session_factory()
    async with factory() as session:
        active = DownloadJob(
            input_url="https://mypikpak.com/s/MERGEACTIVE000001",
            share_id=ACCOUNT_SHARE,
            status=JobStatus.READY,
            target_subdir="merge-race",
        )
        completed = DownloadJob(
            input_url="https://mypikpak.com/s/MERGECOMPLETE0001",
            share_id=ACCOUNT_SHARE,
            status=JobStatus.COMPLETED,
            target_subdir="merge-race",
        )
        session.add_all([active, completed])
        await session.flush()
        active_id = active.id
        completed_id = completed.id
        job = await create_job(
            session,
            input_url=f"https://mypikpak.com/s/{ACCOUNT_SHARE}",
            pass_code=None,
            target_subdir="merge-race",
            items=[
                {
                    "file_id": "MERGEFILE00000001",
                    "kind": "drive#file",
                    "name": "merge.bin",
                    "relative_path": "merge.bin",
                }
            ],
        )
        await session.commit()
        assert job.id == active_id
        assert job.status == JobStatus.ENUMERATING

    async with factory() as session:
        completed_items = (
            await session.execute(
                DownloadJobItem.__table__.select().where(
                    DownloadJobItem.job_id == completed_id
                )
            )
        ).all()
        assert completed_items == []


@pytest.mark.asyncio
async def test_cleanup_probe_pikpak_error_keeps_retrying(env, monkeypatch) -> None:
    from app.db import get_session_factory
    from app.models import Account, CleanupState, CleanupTask
    from app.pikpak.errors import PikPakError
    from app.services import cleanup

    class FailingProbeClient:
        def __init__(self) -> None:
            self.deleted = False

        async def get_file(self, file_id):
            raise PikPakError("probe 503", code="SERVER_ERROR", retryable=True)

        async def delete_files(self, ids):
            self.deleted = True
            return "TASK"

        async def close(self):
            return None

    client = FailingProbeClient()
    factory = get_session_factory()
    async with factory() as session:
        account = Account(
            label="cleanup-probe",
            device_id="cleanup-probe-device",
            refresh_token_enc="token",
        )
        session.add(account)
        await session.flush()
        task = CleanupTask(
            account_id=account.id,
            drive_file_id="CLEANUPPROBEFILE01",
            state=CleanupState.PENDING,
        )
        session.add(task)
        await session.commit()
        task_id = task.id

    monkeypatch.setattr(cleanup, "make_client", lambda account: client)
    async with factory() as session:
        await cleanup.dispatch_cleanups(session)

    async with factory() as session:
        task = await session.get(CleanupTask, task_id)
        assert task.state == CleanupState.RETRYING
        assert task.attempts == 1
        assert "probe 503" in (task.last_error or "")
    assert client.deleted is False


@pytest.mark.asyncio
async def test_folder_retry_success_clears_attempts_and_error(env, monkeypatch) -> None:
    from app.db import get_session_factory
    from app.models import DownloadFolder, DownloadJob, FolderStatus, JobStatus
    from app.pikpak.errors import PikPakError
    from app.services import download

    class FlakyClient:
        def __init__(self) -> None:
            self.calls = 0

        async def list_folder(self, *args, **kwargs):
            self.calls += 1
            if self.calls <= 2:
                raise PikPakError("503", code="SERVER_ERROR", retryable=True)
            return SimpleNamespace(files=[], next_page_token=None)

    async def no_enrich(session, job, client):
        return False

    monkeypatch.setattr(download, "_enrich_tasks", no_enrich)
    client = FlakyClient()
    factory = get_session_factory()
    async with factory() as session:
        job = DownloadJob(
            input_url="https://mypikpak.com/s/FOLDERFLAKY000001",
            share_id="FOLDERFLAKY000001",
            status=JobStatus.ENUMERATING,
        )
        session.add(job)
        await session.flush()
        folder = DownloadFolder(
            job_id=job.id,
            folder_id="FLAKYROOT00000001",
            path="root",
            status=FolderStatus.PENDING,
        )
        session.add(folder)
        await session.commit()
        job_id = job.id
        folder_id = folder.id

    for attempt in range(2):
        async with factory() as session:
            job = await session.get(DownloadJob, job_id)
            with pytest.raises(PikPakError):
                await download._process_folders(session, job, client)
            folder = await session.get(DownloadFolder, folder_id)
            job = await session.get(DownloadJob, job_id)
            assert folder.status == FolderStatus.PENDING
            assert folder.attempts == attempt + 1
            assert job.error_code is None
            assert job.error_message is None

    async with factory() as session:
        job = await session.get(DownloadJob, job_id)
        await download._process_folders(session, job, client)
        await session.commit()
        folder = await session.get(DownloadFolder, folder_id)
        job = await session.get(DownloadJob, job_id)
        assert folder.status == FolderStatus.DONE
        assert folder.attempts == 0
        assert job.error_code is None


@pytest.mark.asyncio
async def test_folder_page_failure_resets_attempts(env, monkeypatch) -> None:
    from app.db import get_session_factory
    from app.models import DownloadFolder, DownloadJob, FolderStatus, JobStatus
    from app.pikpak.errors import PikPakError
    from app.services import download

    class PageFailClient:
        def __init__(self) -> None:
            self.calls = 0

        async def list_folder(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(files=[], next_page_token="NEXT")
            raise PikPakError("503", code="SERVER_ERROR", retryable=True)

    async def no_enrich(session, job, client):
        return False

    monkeypatch.setattr(download, "_enrich_tasks", no_enrich)
    client = PageFailClient()
    factory = get_session_factory()
    async with factory() as session:
        job = DownloadJob(
            input_url="https://mypikpak.com/s/FOLDERPAGE0000001",
            share_id="FOLDERPAGE0000001",
            status=JobStatus.ENUMERATING,
        )
        session.add(job)
        await session.flush()
        folder = DownloadFolder(
            job_id=job.id,
            folder_id="PAGEROOT000000001",
            path="root",
            status=FolderStatus.PENDING,
            attempts=2,
        )
        session.add(folder)
        await session.commit()
        job_id = job.id
        folder_id = folder.id

    async with factory() as session:
        job = await session.get(DownloadJob, job_id)
        with pytest.raises(PikPakError):
            await download._process_folders(session, job, client)
        folder = await session.get(DownloadFolder, folder_id)
        job = await session.get(DownloadJob, job_id)
        assert folder.attempts == 1
        assert folder.status == FolderStatus.PENDING
        assert job.status == JobStatus.ENUMERATING
        assert job.error_code is None


@pytest.mark.asyncio
async def test_selected_job_retryable_folder_error_is_not_fatal(env, monkeypatch) -> None:
    from app.db import get_session_factory
    from app.models import DownloadJob, DownloadJobItem, JobStatus
    from app.pikpak.errors import PikPakError
    from app.services import download

    async def no_enumerate(session, job, client):
        return None

    async def retryable_error(session, job, client):
        raise PikPakError("503", code="SERVER_ERROR", retryable=True)

    monkeypatch.setattr(download, "_enumerate_selection", no_enumerate)
    monkeypatch.setattr(download, "_process_folders", retryable_error)
    factory = get_session_factory()
    async with factory() as session:
        job = DownloadJob(
            input_url="https://mypikpak.com/s/SELECTFAIL0000001",
            share_id="SELECTFAIL0000001",
            status=JobStatus.ENUMERATING,
        )
        session.add(job)
        await session.flush()
        session.add(
            DownloadJobItem(
                job_id=job.id,
                share_file_id="SELECTFAILFILE001",
                kind="drive#folder",
                name="selected",
                relative_path="selected",
            )
        )
        await session.commit()
        job_id = job.id

    async with factory() as session:
        await download._dispatch_enumeration(session)
        job = await session.get(DownloadJob, job_id)
        assert job.status == JobStatus.ENUMERATING
        assert job.error_code is None


@pytest.mark.asyncio
async def test_cleanup_keeps_retrying_when_delete_task_errors(env, monkeypatch) -> None:
    from app.db import get_session_factory
    from app.models import Account, CleanupState, CleanupTask
    from app.services import cleanup

    class FailingDeleteClient:
        async def get_file(self, file_id):
            return {"kind": "drive#file"}

        async def delete_files(self, ids):
            return "DELETEFAILTASK01"

        async def get_restore_task(self, task_id):
            return {"phase": "PHASE_TYPE_ERROR", "message": "failed"}

        async def close(self):
            return None

    factory = get_session_factory()
    async with factory() as session:
        account = Account(
            label="cleanup-error",
            device_id="cleanup-device",
            refresh_token_enc="token",
        )
        session.add(account)
        await session.flush()
        task = CleanupTask(
            account_id=account.id,
            drive_file_id="CLEANUPFAILFILE01",
            state=CleanupState.PENDING,
        )
        session.add(task)
        await session.commit()
        task_id = task.id

    monkeypatch.setattr(cleanup, "make_client", lambda account: FailingDeleteClient())
    async with factory() as session:
        await cleanup.dispatch_cleanups(session)

    async with factory() as session:
        task = await session.get(CleanupTask, task_id)
        assert task.state == CleanupState.RETRYING
        assert task.attempts == 1
        assert "delete task" in (task.last_error or "")
