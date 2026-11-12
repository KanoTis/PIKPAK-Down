from __future__ import annotations

import pytest
from app.aria2 import aria2


@pytest.mark.asyncio
async def test_add_uri_accepts_pikpak_cdn_certificate(monkeypatch, tmp_path) -> None:
    calls: list[tuple[str, list]] = []

    async def fake_call(method: str, params: list) -> str:
        calls.append((method, params))
        return "gid-test"

    monkeypatch.setattr(aria2, "call", fake_call)
    gid = await aria2.add_uri(
        "https://cdn.example.com/file",
        tmp_path,
        "download.bin",
        single_connection=True,
    )

    assert gid == "gid-test"
    _, params = calls[0]
    options = params[1]
    assert options["check-certificate"] == "false"
    assert options["split"] == "1"


@pytest.mark.asyncio
async def test_start_disables_global_certificate_check(monkeypatch) -> None:
    from app.aria2 import Aria2Manager

    manager = Aria2Manager()
    captured: dict[str, list[str]] = {}
    version = {"version": "1.37.0"}

    class FakeProc:
        pass

    class FakeHttp:
        async def aclose(self) -> None:
            return None

    def fake_popen(args, **kwargs):
        captured["args"] = list(args)
        return FakeProc()

    async def fake_call(method, params=None):
        return version

    monkeypatch.setattr(manager, "_binary", lambda: "aria2c")
    monkeypatch.setattr(manager, "_pick_port", lambda: 16800)
    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr("httpx.AsyncClient", lambda **kwargs: FakeHttp())
    monkeypatch.setattr(manager, "call", fake_call)

    await manager.start()
    assert "--check-certificate=false" in captured["args"]
    assert "--continue=true" in captured["args"]
    manager.process = None
