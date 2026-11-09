from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import structlog

from .config import get_settings

log = structlog.get_logger(__name__)


class Aria2Error(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"aria2 error {code}: {message}")
        self.code = code
        self.message = message


class Aria2Manager:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.secret = secrets.token_urlsafe(24)
        self.port = self.settings.aria2_rpc_port
        self.process: subprocess.Popen[bytes] | None = None
        self._http: httpx.AsyncClient | None = None
        self._available = False
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return self._available

    def _binary(self) -> str | None:
        configured = os.environ.get("ARIA2C_PATH")
        if configured and Path(configured).exists():
            return configured
        return shutil.which("aria2c")

    def _pick_port(self) -> int:
        for offset in range(10):
            candidate = self.settings.aria2_rpc_port + offset
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.2)
                if probe.connect_ex(("127.0.0.1", candidate)) != 0:
                    return candidate
        return self.settings.aria2_rpc_port

    async def start(self) -> None:
        async with self._lock:
            if self.process is not None:
                return
            binary = self._binary()
            if not binary:
                log.warning("aria2c_missing")
                self._available = False
                return
            self.port = self._pick_port()
            self.settings.cache_dir.mkdir(parents=True, exist_ok=True)
            args = [
                binary,
                "--enable-rpc=true",
                "--rpc-listen-all=false",
                "--rpc-listen-port",
                str(self.port),
                "--rpc-secret",
                self.secret,
                "--continue=true",
                "--max-concurrent-downloads",
                str(self.settings.download_max_concurrency),
                "--split",
                str(self.settings.aria2_split),
                "--max-connection-per-server",
                str(self.settings.aria2_max_connection_per_server),
                "--min-split-size",
                self.settings.aria2_min_split_size,
                "--max-tries=5",
                "--retry-wait=2",
                "--timeout=60",
                "--auto-file-renaming=false",
                "--allow-overwrite=false",
                "--check-certificate=true",
                "--file-allocation=none",
                "--summary-interval=0",
                "--console-log-level=warn",
                "--log-level=warn",
                "--dir",
                str(self.settings.cache_dir),
            ]
            if self.settings.download_max_rate > 0:
                args.append(f"--max-overall-download-limit={self.settings.download_max_rate}")
            self.process = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            self._http = httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{self.port}",
                timeout=httpx.Timeout(15.0),
                trust_env=False,
            )
            for _ in range(50):
                try:
                    version = await self.call("aria2.getVersion")
                    if not str(version.get("version", "")).startswith("1."):
                        raise Aria2Error(-1, "unexpected RPC service on aria2 port")
                    self._available = True
                    log.info("aria2_started", port=self.port)
                    return
                except (Aria2Error, httpx.HTTPError, OSError):
                    if self.process.poll() is not None:
                        break
                    await asyncio.sleep(0.2)
            self._available = False
            log.warning("aria2_rpc_timeout")

    async def stop(self) -> None:
        async with self._lock:
            if self._http is not None:
                try:
                    await self.call("aria2.shutdown")
                except (Aria2Error, httpx.HTTPError):
                    pass
                await self._http.aclose()
                self._http = None
            if self.process is not None:
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                self.process = None
            self._available = False

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        if self._http is None:
            raise Aria2Error(-1, "aria2 not started")
        payload = {
            "jsonrpc": "2.0",
            "id": secrets.token_hex(4),
            "method": method,
            "params": [f"token:{self.secret}"] + (params or []),
        }
        response = await self._http.post("/jsonrpc", json=payload)
        if response.status_code >= 400:
            raise Aria2Error(response.status_code, response.text[:300])
        data = response.json()
        if "error" in data:
            error = data["error"]
            raise Aria2Error(int(error.get("code", -1)), str(error.get("message", "unknown")))
        return data.get("result")

    async def get_version(self) -> dict[str, Any]:
        return await self.call("aria2.getVersion")

    async def set_max_concurrent_downloads(self, value: int) -> None:
        if not self._available:
            return
        await self.call(
            "aria2.changeGlobalOption",
            [{"max-concurrent-downloads": str(max(1, value))}],
        )

    async def add_uri(self, url: str, directory: Path, out: str) -> str:
        directory.mkdir(parents=True, exist_ok=True)
        options = {
            "dir": str(directory),
            "out": out,
            "continue": "true",
            "split": str(self.settings.aria2_split),
            "max-connection-per-server": str(self.settings.aria2_max_connection_per_server),
            "min-split-size": self.settings.aria2_min_split_size,
            "max-tries": "5",
            "retry-wait": "2",
            "timeout": "60",
            "auto-file-renaming": "false",
            "allow-overwrite": "false",
            "check-certificate": "true",
        }
        return str(await self.call("aria2.addUri", [[url], options]))

    async def tell_status(self, gid: str) -> dict[str, Any] | None:
        try:
            return await self.call(
                "aria2.tellStatus",
                [
                    gid,
                    [
                        "status",
                        "totalLength",
                        "completedLength",
                        "downloadSpeed",
                        "errorCode",
                        "errorMessage",
                        "files",
                        "gid",
                    ],
                ],
            )
        except Aria2Error as exc:
            if "not found" in exc.message.lower():
                return None
            raise

    async def pause(self, gid: str) -> None:
        try:
            await self.call("aria2.pause", [gid])
        except Aria2Error as exc:
            if "not found" not in exc.message.lower():
                raise

    async def unpause(self, gid: str) -> None:
        try:
            await self.call("aria2.unpause", [gid])
        except Aria2Error as exc:
            if "not found" not in exc.message.lower():
                raise

    async def remove(self, gid: str) -> None:
        try:
            await self.call("aria2.remove", [gid])
        except Aria2Error as exc:
            if "not found" not in exc.message.lower():
                raise


aria2 = Aria2Manager()
