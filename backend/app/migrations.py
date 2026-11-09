from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def alembic_config() -> Config:
    root = _repo_root()
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "backend" / "alembic"))
    return cfg


def upgrade_sync() -> None:
    command.upgrade(alembic_config(), "head")
