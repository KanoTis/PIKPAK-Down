from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..services import settings as service
from .schemas import RuntimeSettingsOut, RuntimeSettingsUpdate

router = APIRouter(prefix="/api", tags=["settings"])


@router.get("/settings", response_model=RuntimeSettingsOut)
async def get_runtime_settings() -> RuntimeSettingsOut:
    return RuntimeSettingsOut(**service.snapshot())


@router.patch("/settings", response_model=RuntimeSettingsOut)
async def update_runtime_settings(
    body: RuntimeSettingsUpdate,
    session: AsyncSession = Depends(get_session),
) -> RuntimeSettingsOut:
    try:
        values = await service.update_overrides(
            session, body.model_dump(exclude_none=True)
        )
    except service.SettingsValidation as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "code": "INVALID_SETTING",
                    "message": str(exc),
                    "retryable": False,
                    "details": {},
                }
            },
        ) from exc
    return RuntimeSettingsOut(**values)
