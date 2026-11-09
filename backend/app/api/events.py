from __future__ import annotations

import asyncio
import json
import time
from datetime import timezone

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from sqlalchemy import func, select

from ..db import get_session_factory
from ..models import Event

router = APIRouter(prefix="/api", tags=["events"])


def _event_payload(event: Event) -> dict:
    try:
        payload = json.loads(event.payload_json or "{}")
    except json.JSONDecodeError:
        payload = {}
    created = event.created_at
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return {
        "id": event.id,
        "type": event.event_type,
        "entity_type": event.entity_type,
        "entity_id": event.entity_id,
        "payload": payload,
        "created_at": created.isoformat() if created else None,
    }


@router.get("/events", response_class=EventSourceResponse)
async def event_stream(
    request: Request,
    response: Response,
    after: int | None = Query(None, ge=0),
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
):
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    start = after
    if start is None and last_event_id:
        try:
            start = int(last_event_id)
        except ValueError:
            start = None
    factory = get_session_factory()
    if start is None:
        async with factory() as session:
            start = int((await session.execute(select(func.max(Event.id)))).scalar_one() or 0)

    last = start or 0
    last_sent = time.monotonic()
    while True:
        async with factory() as session:
            rows = (
                await session.execute(
                    select(Event)
                    .where(Event.id > last)
                    .order_by(Event.id.asc())
                    .limit(200)
                )
            ).scalars().all()
        if rows:
            for event in rows:
                last = event.id
                yield ServerSentEvent(
                    data=_event_payload(event),
                    event=event.entity_type,
                    id=str(event.id),
                )
            last_sent = time.monotonic()
        elif time.monotonic() - last_sent >= 15:
            yield ServerSentEvent(comment="keep-alive")
            last_sent = time.monotonic()
        await asyncio.sleep(0.5)
