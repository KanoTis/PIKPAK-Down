from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import AppSetting

log = structlog.get_logger(__name__)

ACTUAL_KEYS: dict[str, tuple[int, int]] = {
    "download_max_concurrency": (1, 16),
    "download_plan_concurrency": (1, 16),
    "aria2_split": (1, 16),
    "aria2_max_connection_per_server": (1, 16),
    "lineage_max_items": (1000, 1_000_000),
    "lineage_max_requests": (100, 100_000),
    "lineage_max_shares": (10, 10_000),
    "lineage_max_folders": (100, 100_000),
}

API_KEYS = (
    "download_max_concurrency",
    "download_plan_concurrency",
    "task_connections",
    "duplicate_name_policy",
    "lineage_max_items",
    "lineage_max_requests",
    "lineage_max_shares",
    "lineage_max_folders",
)

DUPLICATE_POLICIES = ("rename", "overwrite", "skip")


class SettingsValidation(Exception):
    pass


def _check(key: str, value: int) -> int:
    bounds = ACTUAL_KEYS[key]
    if value < bounds[0] or value > bounds[1]:
        raise SettingsValidation(f"{key} 必须在 {bounds[0]} 到 {bounds[1]} 之间")
    return int(value)


def snapshot() -> dict[str, Any]:
    settings = get_settings()
    return {
        "download_max_concurrency": settings.download_max_concurrency,
        "download_plan_concurrency": settings.download_plan_concurrency,
        "task_connections": settings.aria2_split,
        "duplicate_name_policy": settings.duplicate_name_policy,
        "lineage_max_items": settings.lineage_max_items,
        "lineage_max_requests": settings.lineage_max_requests,
        "lineage_max_shares": settings.lineage_max_shares,
        "lineage_max_folders": settings.lineage_max_folders,
    }


async def load_overrides(session: AsyncSession) -> None:
    rows = (await session.execute(select(AppSetting))).scalars().all()
    settings = get_settings()
    for row in rows:
        if row.key == "duplicate_name_policy":
            if row.value not in DUPLICATE_POLICIES:
                log.warning("runtime_setting_invalid", key=row.key, value=row.value)
                continue
            settings.duplicate_name_policy = row.value
            continue
        if row.key not in ACTUAL_KEYS:
            continue
        try:
            value = _check(row.key, int(row.value))
        except (SettingsValidation, ValueError):
            log.warning("runtime_setting_invalid", key=row.key, value=row.value)
            continue
        setattr(settings, row.key, value)
    if rows:
        log.info("runtime_settings_loaded", count=len(rows))


async def update_overrides(session: AsyncSession, payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    actual: dict[str, Any] = {}
    for key in API_KEYS:
        value = payload.get(key)
        if value is None:
            continue
        if key == "task_connections":
            checked = _check("aria2_split", int(value))
            actual["aria2_split"] = checked
            actual["aria2_max_connection_per_server"] = checked
        elif key == "duplicate_name_policy":
            if value not in DUPLICATE_POLICIES:
                raise SettingsValidation("duplicate_name_policy 取值非法")
            actual[key] = value
        else:
            actual[key] = _check(key, int(value))
    if not actual:
        return snapshot()

    for key, value in actual.items():
        setattr(settings, key, value)
        row = await session.get(AppSetting, key)
        if row is None:
            session.add(AppSetting(key=key, value=str(value)))
        else:
            row.value = str(value)
    await session.commit()

    try:
        from ..aria2 import aria2

        await aria2.set_max_concurrent_downloads(settings.download_max_concurrency)
    except Exception as exc:  # noqa: BLE001
        log.warning("aria2_runtime_option_failed", error=str(exc))
    return snapshot()
