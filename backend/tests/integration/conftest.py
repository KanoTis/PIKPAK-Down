from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import time

import httpx
import pytest
import uvicorn

from fake_pikpak_server import create_app


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
