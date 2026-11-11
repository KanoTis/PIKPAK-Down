from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import time

import httpx
import pytest
import uvicorn

from fake_pikpak_server import (
    ANON_SHARE,
    ACCOUNT_SHARE,
    BADLINK_SHARE,
    FAIL_SHARE,
    FOLDER_FILE1,
    FOLDER_FILE2,
    FOLDER_ROOT,
    FOLDER_SHARE,
    NESTED_FILE,
    NESTED_ROOT,
    NESTED_SHARE,
    NESTED_SUB,
    SLOW_HASH,
    SLOW_SHARE,
    SLOW_SIZE,
    create_app,
)


@pytest.fixture(scope="session")
def fake_server():
    app = create_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("fake server failed to start")
    port = server.servers[0].sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    yield base
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
async def env(fake_server):
    from app.aria2 import aria2
    from app.config import get_settings, reset_settings_cache
    from app.db import dispose_engine
    from app.migrations import upgrade_sync
    from app.pikpak.limiters import reset_limiters

    data_dir = tempfile.mkdtemp(prefix="pikpak-it-")
    os.environ["DATA_DIR"] = data_dir
    os.environ["DOWNLOADS_DIR"] = os.path.join(data_dir, "downloads")
    os.environ["CACHE_DIR"] = os.path.join(data_dir, "cache")
    os.environ["PIKPAK_DRIVE_BASE_URL"] = fake_server
    os.environ["PIKPAK_USER_BASE_URL"] = fake_server
    reset_settings_cache()
    reset_limiters()
    await dispose_engine()
    await asyncio.to_thread(upgrade_sync)
    settings = get_settings()
    aria2.settings = settings
    aria2.port = settings.aria2_rpc_port
    await aria2.start()
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(f"{fake_server}/__reset")
    yield settings
    await aria2.stop()
    await dispose_engine()


async def _tick() -> None:
    from app.db import get_session_factory
    from app.services.download import dispatch_downloads

    factory = get_session_factory()
    async with factory() as session:
        await dispatch_downloads(session)


async def run_until(job_id: str, predicate, timeout: float = 40.0):
    from app.db import get_session_factory
    from app.models import DownloadJob, DownloadTask
    from sqlalchemy import select

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await _tick()
        factory = get_session_factory()
        async with factory() as session:
            job = await session.get(DownloadJob, job_id)
            tasks = (
                await session.execute(select(DownloadTask).where(DownloadTask.job_id == job_id))
            ).scalars().all()
            if job is not None and predicate(job, tasks):
                return job, tasks
        await asyncio.sleep(0.1)
    raise AssertionError(f"job {job_id} did not reach expected state in time")


async def create_job(url: str, target_subdir: str | None = None):
    from app.db import get_session_factory
    from app.services.download import create_job as create

    factory = get_session_factory()
    async with factory() as session:
        job = await create(
            session,
            input_url=url,
            pass_code=None,
            target_subdir=target_subdir,
        )
        await session.commit()
        return job.id


async def import_fake_account():
    from app.db import get_session_factory
    from app.services.accounts import import_account

    factory = get_session_factory()
    async with factory() as session:
        account = await import_account(
            session,
            label="fake",
            email=None,
            refresh_token="fake-refresh-token",
        )
        await session.commit()
        return account.id


@pytest.mark.asyncio
async def test_anonymous_share_downloads_without_account(env, fake_server) -> None:
    job_id = await create_job(f"https://mypikpak.com/s/{ANON_SHARE}")
    job, tasks = await run_until(job_id, lambda job, tasks: job.status == "COMPLETED")
    assert len(tasks) == 1
    task = tasks[0]
    assert task.status == "DONE"
    assert task.route == "anonymous"
    assert task.remote_deleted_at is None
    async with httpx.AsyncClient(timeout=10) as client:
        state = (await client.get(f"{fake_server}/__state")).json()
    assert state["restore_calls"] == 0
    assert state["delete_calls"] == 0

    assert task.target_path == "anon.bin"
    path = env.downloads_dir / task.target_path
    assert path.read_bytes() == bytes(range(256)) * 4097
    assert task.verified_chunk == 256 * 1024


@pytest.mark.asyncio
async def test_account_handoff_deletes_before_download(env, fake_server) -> None:
    await import_fake_account()
    job_id = await create_job(f"https://mypikpak.com/s/{ACCOUNT_SHARE}")
    job, tasks = await run_until(job_id, lambda job, tasks: job.status == "COMPLETED")
    task = tasks[0]
    assert task.status == "DONE"
    assert task.route == "account"
    assert task.remote_deleted_at is not None
    assert task.verify_attempts == 0
    async with httpx.AsyncClient(timeout=10) as client:
        state = (await client.get(f"{fake_server}/__state")).json()
    assert state["restore_calls"] == 1
    assert state["delete_calls"] >= 1
    delete_times = [item["t"] for item in state["events"] if item["type"] == "delete"]
    aria2_requests = [
        item for item in state["cdn_requests"] if "aria2" in item["ua"].lower()
    ]
    assert aria2_requests
    assert delete_times
    assert min(item["t"] for item in aria2_requests) > min(delete_times)

    from app.db import get_session_factory
    from app.models import Account

    factory = get_session_factory()
    async with factory() as session:
        account = (
            await session.execute(__import__("sqlalchemy").select(Account))
        ).scalar_one()
        assert account.lease_task_id is None
    assert task.target_path == "account.mp4"
    assert (env.downloads_dir / task.target_path).read_bytes() == bytes(range(256)) * 4097


@pytest.mark.asyncio
async def test_no_account_queues_without_failing(env, fake_server) -> None:
    job_id = await create_job(f"https://mypikpak.com/s/{ACCOUNT_SHARE}")
    job, tasks = await run_until(
        job_id,
        lambda job, tasks: bool(tasks) and tasks[0].status == "ACCOUNT_QUEUED",
    )
    task = tasks[0]
    assert task.queue_reason == "NO_ACCOUNT"
    assert job.status == "READY"


@pytest.mark.asyncio
async def test_delete_failure_queues_cleanup_then_404_completes(env, fake_server) -> None:
    await import_fake_account()
    job_id = await create_job(f"https://mypikpak.com/s/{FAIL_SHARE}")
    job, tasks = await run_until(job_id, lambda job, tasks: job.status == "COMPLETED")
    task = tasks[0]
    assert task.status == "DONE"
    assert task.remote_deleted_at is None

    from app.db import get_session_factory
    from app.models import CleanupTask, utcnow
    from app.services.cleanup import dispatch_cleanups

    factory = get_session_factory()
    async with factory() as session:
        cleanup = (
            await session.execute(__import__("sqlalchemy").select(CleanupTask))
        ).scalar_one()
        assert cleanup.state in ("PENDING", "RETRYING")
        cleanup.next_attempt_at = utcnow()
        await session.commit()
        await dispatch_cleanups(session)
        await session.refresh(cleanup)
        assert cleanup.state == "DONE"


@pytest.mark.asyncio
async def test_probe_failure_never_starts_aria2(env, fake_server) -> None:
    await import_fake_account()
    job_id = await create_job(f"https://mypikpak.com/s/{BADLINK_SHARE}")
    job, tasks = await run_until(
        job_id,
        lambda job, tasks: bool(tasks)
        and tasks[0].status == "ACCOUNT_QUEUED"
        and tasks[0].error_code == "CDN_LINK_UNUSABLE",
        timeout=30,
    )
    async with httpx.AsyncClient(timeout=10) as client:
        state = (await client.get(f"{fake_server}/__state")).json()
    assert not [item for item in state["cdn_requests"] if "aria2" in item["ua"].lower()]


@pytest.mark.asyncio
async def test_restart_reuses_partial_and_finishes(env, fake_server) -> None:
    from app.aria2 import aria2
    from app.db import get_session_factory
    from app.models import DownloadTask
    from app.services.download import recover_interrupted
    from sqlalchemy import select

    job_id = await create_job(f"https://mypikpak.com/s/{SLOW_SHARE}")
    task = None
    for _ in range(200):
        await _tick()
        factory = get_session_factory()
        async with factory() as session:
            task = (
                await session.execute(
                    select(DownloadTask).where(DownloadTask.job_id == job_id)
                )
            ).scalar_one_or_none()
            if task is not None and task.status == "ANON_DOWNLOADING" and task.bytes_done > 0:
                candidate = env.cache_dir / task.id / "download.bin"
                if candidate.exists() and candidate.stat().st_size > 0:
                    break
        await asyncio.sleep(0.05)
    assert task is not None
    assert task.status == "ANON_DOWNLOADING"
    temp = env.cache_dir / task.id / "download.bin"
    assert temp.exists() and temp.stat().st_size > 0
    partial_size = temp.stat().st_size

    await aria2.stop()
    await aria2.start()
    factory = get_session_factory()
    async with factory() as session:
        await recover_interrupted(session)

    job, tasks = await run_until(job_id, lambda job, tasks: job.status == "COMPLETED", timeout=90)
    assert tasks[0].status == "DONE"
    assert tasks[0].bytes_done == SLOW_SIZE
    assert tasks[0].expected_hash == SLOW_HASH
    assert partial_size > 0
    assert tasks[0].target_path == "slow.bin"
    assert (env.downloads_dir / tasks[0].target_path).stat().st_size == SLOW_SIZE


@pytest.mark.asyncio
async def test_folder_share_enumerates_and_downloads_all_files(env, fake_server) -> None:
    job_id = await create_job(f"https://mypikpak.com/s/{FOLDER_SHARE}")
    job, tasks = await run_until(job_id, lambda job, tasks: job.status == "COMPLETED")
    assert job.folder_total == 1
    assert job.folder_done == 1
    assert len(tasks) == 2
    assert {task.status for task in tasks} == {"DONE"}
    paths = {task.relative_path for task in tasks}
    assert paths == {"folder-root/anon-copy.bin", "folder-root/small.bin"}
    for task in tasks:
        assert task.target_path in {
            "folder-root/anon-copy.bin",
            "folder-root/small.bin",
        }
        assert (env.downloads_dir / task.target_path).stat().st_size == task.size


@pytest.mark.asyncio
async def test_preview_and_selected_file_download(env, fake_server) -> None:
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as client:
        preview = await client.post(
            "/api/share-preview",
            json={"url": f"https://mypikpak.com/s/{FOLDER_SHARE}"},
        )
        assert preview.status_code == 200
        body = preview.json()
        assert body["share_status"] == "OK"
        assert body["folder_id"] == FOLDER_ROOT
        assert {item["id"] for item in body["items"]} == {FOLDER_FILE1, "FOLDERFILE00000002"}

        created = await client.post(
            "/api/download-jobs",
            json={
                "url": f"https://mypikpak.com/s/{FOLDER_SHARE}",
                "items": [
                    {
                        "file_id": FOLDER_FILE1,
                        "kind": "drive#file",
                        "name": "anon-copy.bin",
                        "relative_path": "folder-root/anon-copy.bin",
                    }
                ],
            },
        )
        assert created.status_code == 202
        job_id = created.json()["id"]

    job, tasks = await run_until(job_id, lambda job, tasks: job.status == "COMPLETED")
    assert len(tasks) == 1
    assert tasks[0].status == "DONE"
    assert tasks[0].share_file_id == FOLDER_FILE1
    assert tasks[0].relative_path == "folder-root/anon-copy.bin"
    assert tasks[0].target_path == "folder-root/anon-copy.bin"
    assert (env.downloads_dir / tasks[0].target_path).read_bytes() == bytes(range(256)) * 4097
    assert not (env.cache_dir / tasks[0].id).exists()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as client:
        served = await client.get(f"/api/downloads/{tasks[0].id}/file")
        assert served.status_code == 200
        assert served.content == bytes(range(256)) * 4097


@pytest.mark.asyncio
async def test_selected_folder_downloads_subtree(env, fake_server) -> None:
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as client:
        created = await client.post(
            "/api/download-jobs",
            json={
                "url": f"https://mypikpak.com/s/{FOLDER_SHARE}",
                "target_subdir": "library",
                "items": [
                    {
                        "file_id": FOLDER_ROOT,
                        "kind": "drive#folder",
                        "name": "folder-root",
                        "relative_path": "folder-root",
                    }
                ],
            },
        )
        assert created.status_code == 202
        job_id = created.json()["id"]

    job, tasks = await run_until(job_id, lambda job, tasks: job.status == "COMPLETED")
    assert len(tasks) == 2
    assert {task.status for task in tasks} == {"DONE"}
    assert {task.relative_path for task in tasks} == {
        "folder-root/anon-copy.bin",
        "folder-root/small.bin",
    }
    assert {task.target_path for task in tasks} == {
        "library/folder-root/anon-copy.bin",
        "library/folder-root/small.bin",
    }
    for task in tasks:
        assert (env.downloads_dir / task.target_path).stat().st_size == task.size


@pytest.mark.asyncio
async def test_preview_nested_folder_and_selected_subtree(env, fake_server) -> None:
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as client:
        root = await client.post(
            "/api/share-preview",
            json={"url": f"https://mypikpak.com/s/{NESTED_SHARE}"},
        )
        assert root.status_code == 200
        root_body = root.json()
        assert root_body["folder_id"] == NESTED_ROOT
        assert {item["id"] for item in root_body["items"]} == {NESTED_SUB, NESTED_FILE}

        sub = await client.post(
            "/api/share-preview",
            json={
                "url": f"https://mypikpak.com/s/{NESTED_SHARE}",
                "folder_id": NESTED_SUB,
                "folder_name": "sub",
            },
        )
        assert sub.status_code == 200
        assert [item["id"] for item in sub.json()["items"]] == [NESTED_FILE]

        created = await client.post(
            "/api/download-jobs",
            json={
                "url": f"https://mypikpak.com/s/{NESTED_SHARE}",
                "items": [
                    {
                        "file_id": NESTED_SUB,
                        "kind": "drive#folder",
                        "name": "sub",
                        "relative_path": "root/sub",
                    }
                ],
            },
        )
        assert created.status_code == 202
        job_id = created.json()["id"]

    job, tasks = await run_until(job_id, lambda job, tasks: job.status == "COMPLETED")
    assert len(tasks) == 1
    assert tasks[0].status == "DONE"
    assert tasks[0].relative_path == "root/sub/small.bin"


@pytest.mark.asyncio
async def test_account_added_after_queue_unblocks_task(env, fake_server) -> None:
    job_id = await create_job(f"https://mypikpak.com/s/{ACCOUNT_SHARE}")
    job, tasks = await run_until(
        job_id,
        lambda job, tasks: bool(tasks) and tasks[0].status == "ACCOUNT_QUEUED",
    )
    assert tasks[0].queue_reason == "NO_ACCOUNT"

    await import_fake_account()
    job, tasks = await run_until(
        job_id,
        lambda job, tasks: job.status == "COMPLETED",
        timeout=60,
    )
    assert tasks[0].status == "DONE"
    assert tasks[0].account_id is not None


@pytest.mark.asyncio
async def test_stale_account_lease_is_recovered(env, fake_server) -> None:
    from datetime import timedelta

    from app.db import get_session_factory
    from app.models import Account, utcnow

    job_id = await create_job(f"https://mypikpak.com/s/{ACCOUNT_SHARE}")
    job, tasks = await run_until(
        job_id,
        lambda job, tasks: bool(tasks) and tasks[0].status == "ACCOUNT_QUEUED",
    )
    assert tasks[0].queue_reason == "NO_ACCOUNT"
    account_id = await import_fake_account()

    factory = get_session_factory()
    async with factory() as session:
        account = await session.get(Account, account_id)
        account.lease_task_id = "deleted-task-id"
        account.lease_expires_at = utcnow() + timedelta(minutes=30)
        await session.commit()

    job, tasks = await run_until(
        job_id,
        lambda job, tasks: job.status == "COMPLETED",
        timeout=60,
    )
    assert tasks[0].status == "DONE"


@pytest.mark.asyncio
async def test_account_reclaims_own_lease(env, fake_server) -> None:
    from datetime import timedelta

    from app.db import get_session_factory
    from app.models import Account, utcnow

    job_id = await create_job(f"https://mypikpak.com/s/{ACCOUNT_SHARE}")
    job, tasks = await run_until(
        job_id,
        lambda job, tasks: bool(tasks) and tasks[0].status == "ACCOUNT_QUEUED",
    )
    task = tasks[0]
    account_id = await import_fake_account()

    factory = get_session_factory()
    async with factory() as session:
        account = await session.get(Account, account_id)
        account.lease_task_id = task.id
        account.lease_expires_at = utcnow() + timedelta(minutes=30)
        await session.commit()

    job, tasks = await run_until(
        job_id,
        lambda job, tasks: job.status == "COMPLETED",
        timeout=60,
    )
    assert tasks[0].status == "DONE"


@pytest.mark.asyncio
async def test_same_share_merges_into_existing_job(env, fake_server) -> None:
    from app.db import get_session_factory
    from app.services.download import create_job as create

    async def create_selection(file_id: str, name: str) -> str:
        factory = get_session_factory()
        async with factory() as session:
            job = await create(
                session,
                input_url=f"https://mypikpak.com/s/{FOLDER_SHARE}",
                pass_code=None,
                items=[
                    {
                        "file_id": file_id,
                        "kind": "drive#file",
                        "name": name,
                        "relative_path": f"folder-root/{name}",
                    }
                ],
            )
            await session.commit()
            return job.id

    first_id = await create_selection(FOLDER_FILE1, "anon-copy.bin")
    await run_until(first_id, lambda job, tasks: job.status == "COMPLETED")
    second_id = await create_selection(FOLDER_FILE2, "small.bin")
    assert second_id == first_id

    job, tasks = await run_until(
        first_id, lambda job, tasks: job.status == "COMPLETED" and len(tasks) == 2
    )
    assert {task.target_path for task in tasks} == {
        "folder-root/anon-copy.bin",
        "folder-root/small.bin",
    }
    assert all(task.status == "DONE" for task in tasks)


@pytest.mark.asyncio
async def test_duplicate_policy_rename_and_skip(env, fake_server) -> None:
    target = env.downloads_dir / "anon.bin"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"old-different-content")

    settings = env
    settings.duplicate_name_policy = "rename"
    job_id = await create_job(f"https://mypikpak.com/s/{ANON_SHARE}")
    job, tasks = await run_until(job_id, lambda job, tasks: job.status == "COMPLETED")
    assert tasks[0].target_path == "anon (1).bin"
    assert target.read_bytes() == b"old-different-content"

    target.unlink()
    target.write_bytes(b"old-different-content")
    settings.duplicate_name_policy = "skip"
    job_id = await create_job(f"https://mypikpak.com/s/{ANON_SHARE}")
    job, tasks = await run_until(job_id, lambda job, tasks: job.status == "COMPLETED")
    assert tasks[0].status == "DONE"
    assert tasks[0].error_code == "DUPLICATE_SKIPPED"
    assert target.read_bytes() == b"old-different-content"
    settings.duplicate_name_policy = "rename"


@pytest.mark.asyncio
async def test_job_and_file_category_filters(env, fake_server) -> None:
    from app.main import app

    job_id = await create_job(f"https://mypikpak.com/s/{ACCOUNT_SHARE}")
    job, tasks = await run_until(
        job_id,
        lambda job, tasks: bool(tasks) and tasks[0].status == "ACCOUNT_QUEUED",
    )
    assert tasks[0].queue_reason == "NO_ACCOUNT"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as client:
        listed = await client.get("/api/download-jobs")
        assert listed.status_code == 200
        body = [item for item in listed.json() if item["id"] == job_id][0]
        assert body["category"] == "queued"
        assert body["queued_files"] == 1

        queued = await client.get(
            f"/api/download-jobs/{job_id}/files", params={"category": "queued"}
        )
        assert queued.status_code == 200
        assert [item["status"] for item in queued.json()["items"]] == ["ACCOUNT_QUEUED"]

        downloading = await client.get(
            f"/api/download-jobs/{job_id}/files", params={"category": "downloading"}
        )
        assert downloading.json()["items"] == []


@pytest.mark.asyncio
async def test_duplicate_content_reuses_local_file(env, fake_server) -> None:
    first_id = await create_job(f"https://mypikpak.com/s/{ANON_SHARE}", "dup-a")
    await run_until(first_id, lambda job, tasks: job.status == "COMPLETED")

    async with httpx.AsyncClient(timeout=10) as client:
        before = (await client.get(f"{fake_server}/__state")).json()["cdn_requests"]
    second_id = await create_job(f"https://mypikpak.com/s/{ANON_SHARE}", "dup-b")
    job, tasks = await run_until(second_id, lambda job, tasks: job.status == "COMPLETED")
    task = tasks[0]
    assert task.route == "local"
    assert task.target_path == "dup-b/anon.bin"
    assert (env.downloads_dir / task.target_path).stat().st_size == task.size
    async with httpx.AsyncClient(timeout=10) as client:
        after = (await client.get(f"{fake_server}/__state")).json()["cdn_requests"]
    assert len(after) == len(before)


@pytest.mark.asyncio
async def test_duplicate_content_does_not_download_twice(env, fake_server) -> None:
    from app.db import get_session_factory
    from app.models import DownloadJob, DownloadTask
    from sqlalchemy import select

    first_id = await create_job(f"https://mypikpak.com/s/{ANON_SHARE}", "dup-c")
    second_id = await create_job(f"https://mypikpak.com/s/{ANON_SHARE}", "dup-d")

    factory = get_session_factory()
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        await _tick()
        async with factory() as session:
            jobs = (
                await session.execute(
                    select(DownloadJob).where(DownloadJob.id.in_([first_id, second_id]))
                )
            ).scalars().all()
            if all(job.status == "COMPLETED" for job in jobs):
                break
        await asyncio.sleep(0.1)
    else:
        raise AssertionError("duplicate jobs did not complete")

    async with factory() as session:
        tasks = (
            await session.execute(
                select(DownloadTask).where(
                    DownloadTask.job_id.in_([first_id, second_id])
                )
            )
        ).scalars().all()
    assert len(tasks) == 2
    assert {task.route for task in tasks} == {"anonymous", "local"}
    async with httpx.AsyncClient(timeout=10) as client:
        state = (await client.get(f"{fake_server}/__state")).json()
    aria2_requests = [
        item for item in state["cdn_requests"] if "aria2" in item["ua"].lower()
    ]
    assert len(aria2_requests) == 1


@pytest.mark.asyncio
async def test_dispatch_plans_runs_concurrently(env, fake_server, monkeypatch) -> None:
    from app.db import get_session_factory
    from app.models import DownloadJob, DownloadTask, JobStatus, TaskStatus, utcnow
    from app.services import download

    env.download_max_concurrency = 8
    env.download_plan_concurrency = 4

    factory = get_session_factory()
    async with factory() as session:
        for index in range(4):
            job = DownloadJob(
                input_url=f"https://mypikpak.com/s/PLAN{index:04d}",
                share_id=f"PLAN{index:04d}",
                status=JobStatus.READY,
            )
            session.add(job)
            await session.flush()
            session.add(
                DownloadTask(
                    job_id=job.id,
                    share_file_id=f"PLANFILE{index:04d}",
                    name=f"plan-{index}.bin",
                    size=1,
                    status=TaskStatus.DISCOVERED,
                )
            )
        await session.commit()

    active = 0
    peak = 0

    async def fake_plan(session, task, client):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.15)
        task.status = TaskStatus.DONE
        task.finished_at = utcnow()
        await session.commit()
        active -= 1

    monkeypatch.setattr(download, "_plan_task", fake_plan)
    async with factory() as session:
        await download._dispatch_plans(session)

    assert peak == 4
