from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ..aria2 import aria2
from ..config import get_settings
from ..db import get_session_factory

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
async def health() -> JSONResponse:
    checks: dict[str, bool] = {}
    details: dict[str, str] = {}
    try:
        factory = get_session_factory()
        async with factory() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception as exc:  # noqa: BLE001
        checks["database"] = False
        details["database"] = str(exc)

    try:
        if aria2.available:
            await aria2.get_version()
            checks["aria2"] = True
        else:
            checks["aria2"] = False
            details["aria2"] = "aria2c not available"
    except Exception as exc:  # noqa: BLE001
        checks["aria2"] = False
        details["aria2"] = str(exc)

    settings = get_settings()
    details["pikpak_drive_base_url"] = settings.pikpak_drive_base_url
    details["pikpak_user_base_url"] = settings.pikpak_user_base_url
    try:
        settings.ensure_dirs()
        handle, path = tempfile.mkstemp(dir=settings.state_dir, prefix=".health-")
        os.close(handle)
        Path(path).unlink(missing_ok=True)
        checks["data_dir"] = True
    except Exception as exc:  # noqa: BLE001
        checks["data_dir"] = False
        details["data_dir"] = str(exc)

    ok = all(checks.values())
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "ok" if ok else "degraded", "checks": checks, "details": details},
    )
