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
