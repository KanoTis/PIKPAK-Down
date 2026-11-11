from __future__ import annotations

import httpx
import pytest
from app.pikpak.client import AccountCredentials, AccountPikPakClient


def _credentials() -> AccountCredentials:
    return AccountCredentials(
        id="account-test",
        sub="sub-test",
        device_id="device-test",
        access_token="access-test",
        refresh_token="refresh-test",
    )


@pytest.mark.asyncio
async def test_create_folder_uses_empty_root_parent() -> None:
    calls: list[dict[str, object]] = []

    async with httpx.AsyncClient() as http:
        client = AccountPikPakClient(_credentials(), http=http)

        async def fake_drive(method: str, path: str, **kwargs: object) -> httpx.Response:
            calls.append({"method": method, "path": path, **kwargs})
            return httpx.Response(
                200,
                json={"file": {"id": "FOLDER0000000001"}},
                request=httpx.Request(method, "http://test"),
            )

        client._drive = fake_drive  # type: ignore[method-assign]
        assert await client.create_folder("pikpak_tar_abc") == {"id": "FOLDER0000000001"}
        assert calls[0]["json_body"] == {
            "kind": "drive#folder",
            "parent_id": "",
            "name": "pikpak_tar_abc",
        }

        await client.create_folder("nested", "PARENT000000001")
        assert calls[1]["json_body"] == {
            "kind": "drive#folder",
            "parent_id": "PARENT000000001",
            "name": "nested",
        }
