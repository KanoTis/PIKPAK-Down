from __future__ import annotations

import base64
import binascii
import os
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _decode_secret(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("APP_SECRET_KEY must be base64url encoded") from exc
    if len(raw) != 32:
        raise ValueError("APP_SECRET_KEY must decode to exactly 32 bytes")
    return raw


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    data_dir: Path = Path("/data")
    downloads_dir: Path = Path("/downloads")
    cache_dir: Path = Path("/cache")
    app_secret_key: str
    database_url: str | None = None

    pikpak_drive_base_url: str = "https://api-drive.mypikpak.com"
    pikpak_user_base_url: str = "https://user.mypikpak.com"

    download_max_concurrency: int = 2
    aria2_split: int = 8
    aria2_max_connection_per_server: int = 8
    aria2_min_split_size: str = "8M"
    aria2_rpc_port: int = 6800
    download_max_rate: int = 0
    duplicate_name_policy: str = "rename"

    anon_api_qps: float = 5.0
    share_probe_qps: float = 2.0
    window_probe_qps: float = 2.0

    lineage_max_active_runs: int = 1
    lineage_max_depth: int = 5
    lineage_max_requests: int = 2000
    lineage_max_shares: int = 200
    lineage_max_folders: int = 2000
    lineage_max_items: int = 50000

    account_daily_reset_tz: str = "Asia/Shanghai"
    account_lease_seconds: int = 1800
    account_cooldown_seconds: int = 300

    @field_validator("app_secret_key")
    @classmethod
    def _validate_secret(cls, value: str) -> str:
        _decode_secret(value)
        return value

    @field_validator("data_dir", "downloads_dir", "cache_dir", mode="before")
    @classmethod
    def _expand_path(cls, value: object) -> object:
        if isinstance(value, str):
            return Path(os.path.expanduser(value))
        return value

    @model_validator(mode="after")
    def _default_database_url(self) -> "Settings":
        if not self.database_url:
            self.database_url = f"sqlite+aiosqlite:///{(self.data_dir / 'pikpak.db').as_posix()}"
        return self

    @property
    def secret_bytes(self) -> bytes:
        return _decode_secret(self.app_secret_key)

    @property
    def tmp_dir(self) -> Path:
        # Legacy alias: download temporaries now live in cache_dir.
        return self.cache_dir

    @property
    def files_dir(self) -> Path:
        # Legacy content-addressed store, kept for old FileObject rows.
        return self.data_dir / "files"

    @property
    def evidence_dir(self) -> Path:
        return self.data_dir / "evidence"

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.downloads_dir,
            self.cache_dir,
            self.files_dir,
            self.evidence_dir,
            self.state_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
            try:
                path.chmod(0o700)
            except OSError:
                pass


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
