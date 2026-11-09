from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db import get_session
from ..models import CopyIndex, LineageRun, RawResponse
from ..services import lineage as service
from ..pikpak.urls import SHARE_ID_RE
from .schemas import LineageCreate, LineageRunOut

router = APIRouter(prefix="/api", tags=["lineage"])


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def run_out(run: LineageRun) -> LineageRunOut:
    return LineageRunOut(
        id=run.id,
        input_type=run.input_type,
        status=run.status,
        share_id=run.share_id,
        file_id=run.file_id,
        hash=run.hash,
        size=run.size,
        include_tree=run.include_tree,
        probe_windows=run.probe_windows,
        requests=run.requests,
        shares_probed=run.shares_probed,
        items_enumerated=run.items_enumerated,
        folders_done=run.folders_done,
        truncated=run.truncated,
        termination=run.termination,
        passcode_unverified=run.passcode_unverified,
        error_code=run.error_code,
        error_message=run.error_message,
        created_at=iso(run.created_at),
        started_at=iso(run.started_at),
        finished_at=iso(run.finished_at),
    )


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"error": {"code": code, "message": message, "retryable": False, "details": {}}},
    )


@router.post("/lineage-runs", response_model=LineageRunOut, status_code=202)
async def create_lineage_run(
    body: LineageCreate, session: AsyncSession = Depends(get_session)
) -> LineageRunOut:
    if body.graph and body.hash and body.size is not None:
        raise _error(400, "INVALID_INPUT", "全量血缘图只支持分享输入")
    if body.hash and body.size is not None:
        run = await service.create_run(
            session,
            input_value="",
            hash_value=body.hash,
            size=body.size,
            include_tree=body.include_tree,
            probe_windows=body.probe_windows,
        )
    else:
        raw_input = body.input or body.share_id
        if not raw_input:
            raise _error(400, "INVALID_INPUT", "需要分享链接、share_id 或 hash+size")
        if body.file_id and not SHARE_ID_RE.fullmatch(body.file_id):
            raise _error(400, "INVALID_INPUT", "file_id 格式非法")
        try:
            run = await service.create_run(
                session,
                input_value=raw_input,
                pass_code=body.pass_code,
                file_id=body.file_id,
                include_tree=body.include_tree,
                probe_windows=body.probe_windows,
                graph=body.graph,
                items=[item.model_dump() for item in body.items]
                if body.items is not None
                else None,
            )
        except ValueError as exc:
            raise _error(400, "INVALID_INPUT", str(exc)) from exc
    await session.commit()
    return run_out(run)


@router.get("/lineage-runs", response_model=list[LineageRunOut])
async def list_lineage_runs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> list[LineageRunOut]:
    rows = (
        await session.execute(
            select(LineageRun)
            .order_by(LineageRun.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()
    return [run_out(run) for run in rows]


@router.get("/lineage-runs/{run_id}")
async def get_lineage_run(
    run_id: str, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    try:
        run = await service.get_run(session, run_id)
    except service.LineageNotFound as exc:
        raise _error(404, "RUN_NOT_FOUND", str(exc)) from exc
    report = await service.build_report(session, run)
    return {"run": run_out(run).model_dump(), "report": report}


@router.get("/lineage-runs/{run_id}/graph/shares/{share_id}")
async def get_graph_share(
    run_id: str,
    share_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        run = await service.get_run(session, run_id)
    except service.LineageNotFound as exc:
        raise _error(404, "LINEAGE_NOT_FOUND", str(exc)) from exc
    from ..services.lineage_graph import build_share_detail

    try:
        return await build_share_detail(
            session, run, share_id, offset=offset, limit=limit
        )
    except service.LineageNotFound as exc:
        raise _error(404, "GRAPH_NODE_NOT_FOUND", str(exc)) from exc


@router.post("/lineage-runs/{run_id}/graph/enrich")
async def enrich_graph_titles(
    run_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        run = await service.get_run(session, run_id)
    except service.LineageNotFound as exc:
        raise _error(404, "LINEAGE_NOT_FOUND", str(exc)) from exc
    if run.input_type != "share_graph":
        raise _error(400, "INVALID_RUN_TYPE", "只有全量血缘图支持补齐名称")
    from ..pikpak.client import AnonymousPikPakClient
    from ..services.lineage_graph import _enrich_pending_titles

    client = AnonymousPikPakClient()
    try:
        updated = await _enrich_pending_titles(session, run, client)
        await session.commit()
    finally:
        await client.close()
    return {"updated": updated}


@router.get("/lineage-runs/{run_id}/copies")
async def list_copies(
    run_id: str,
    hash: str | None = None,
    size: int | None = None,
    cursor: int | None = None,
    limit: int = Query(100, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        await service.get_run(session, run_id)
    except service.LineageNotFound as exc:
        raise _error(404, "RUN_NOT_FOUND", str(exc)) from exc
    stmt = select(CopyIndex).where(CopyIndex.run_id == run_id)
    if hash:
        stmt = stmt.where(CopyIndex.hash == hash.upper())
    if size is not None:
        stmt = stmt.where(CopyIndex.size == size)
    if cursor is not None:
        stmt = stmt.where(CopyIndex.id > cursor)
    rows = (
        await session.execute(stmt.order_by(CopyIndex.id.asc()).limit(limit + 1))
    ).scalars().all()
    next_cursor = rows[limit].id if len(rows) > limit else None
    rows = rows[:limit]
    return {
        "items": [
            {
                "id": row.id,
                "share_id": row.share_id,
                "file_id": row.file_id,
                "path": row.parent_path,
                "name": row.name,
                "kind": row.kind,
                "size": row.size,
                "hash": row.hash,
                "mime_type": row.mime_type,
                "is_video": row.is_video,
            }
            for row in rows
        ],
        "next_cursor": next_cursor,
    }


@router.post("/lineage-runs/{run_id}/cancel", response_model=LineageRunOut)
async def cancel_lineage_run(
    run_id: str, session: AsyncSession = Depends(get_session)
) -> LineageRunOut:
    try:
        run = await service.cancel_run(session, run_id)
    except service.LineageNotFound as exc:
        raise _error(404, "RUN_NOT_FOUND", str(exc)) from exc
    return run_out(run)


@router.delete("/lineage-runs/{run_id}", status_code=204)
async def delete_lineage_run(
    run_id: str, session: AsyncSession = Depends(get_session)
) -> Response:
    try:
        await service.delete_run(session, run_id)
    except service.LineageNotFound as exc:
        raise _error(404, "RUN_NOT_FOUND", str(exc)) from exc
    except service.LineageStateConflict as exc:
        raise _error(409, "STATE_CONFLICT", str(exc)) from exc
    return Response(status_code=204)


@router.get("/lineage-runs/{run_id}/export")
async def export_lineage_run(
    run_id: str,
    format: str = Query("json", pattern="^(json|markdown|dot)$"),
    session: AsyncSession = Depends(get_session),
) -> Response:
    try:
        run = await service.get_run(session, run_id)
    except service.LineageNotFound as exc:
        raise _error(404, "RUN_NOT_FOUND", str(exc)) from exc
    report = await service.build_report(session, run)
    if format == "json":
        import json

        return Response(
            json.dumps(report, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="lineage-{run.id}.json"'},
        )
    if format == "markdown":
        return Response(
            service.report_markdown(report),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="lineage-{run.id}.md"'},
        )
    return Response(
        service.report_dot(report),
        media_type="text/vnd.graphviz; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="lineage-{run.id}.dot"'},
    )


@router.get("/lineage-runs/{run_id}/evidence")
async def list_evidence(
    run_id: str, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    try:
        await service.get_run(session, run_id)
    except service.LineageNotFound as exc:
        raise _error(404, "RUN_NOT_FOUND", str(exc)) from exc
    rows = (
        await session.execute(
            select(RawResponse)
            .where(RawResponse.run_id == run_id)
            .order_by(RawResponse.seq.asc())
        )
    ).scalars().all()
    return {
        "items": [
            {
                "seq": row.seq,
                "method": row.method,
                "url": row.url,
                "status": row.status,
                "created_at": iso(row.created_at),
            }
            for row in rows
        ]
    }


@router.get("/lineage-runs/{run_id}/evidence/{seq}")
async def read_evidence(
    run_id: str, seq: int, session: AsyncSession = Depends(get_session)
) -> Response:
    try:
        await service.get_run(session, run_id)
    except service.LineageNotFound as exc:
        raise _error(404, "RUN_NOT_FOUND", str(exc)) from exc
    row = (
        await session.execute(
            select(RawResponse).where(
                RawResponse.run_id == run_id, RawResponse.seq == seq
            )
        )
    ).scalar_one_or_none()
    if row is None or not row.file_path:
        raise _error(404, "EVIDENCE_NOT_FOUND", "证据不存在")
    evidence_root = get_settings().evidence_dir.resolve()
    path = __import__("pathlib").Path(row.file_path).resolve()
    if not str(path).startswith(str(evidence_root)) or not path.is_file():
        raise _error(404, "EVIDENCE_NOT_FOUND", "证据文件不存在")
    return Response(path.read_text(encoding="utf-8"), media_type="application/json")
