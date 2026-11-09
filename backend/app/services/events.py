from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Event
from ..security import sanitize


def emit(
    session: AsyncSession,
    entity_type: str,
    entity_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> Event:
    """Add an event to the caller's transaction; commit is owned by the caller."""
    event = Event(
        entity_type=entity_type,
        entity_id=entity_id,
        event_type=event_type,
        payload_json=json.dumps(sanitize(payload or {}), ensure_ascii=False, default=str),
    )
    session.add(event)
    return event
