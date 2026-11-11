from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import (
    CopyIndex,
    Event,
    LineageEdge,
    LineageFolder,
    LineageGraphFolder,
    LineageGraphItem,
    LineageGraphShare,
    LineageNode,
    LineageRun,
    LineageStatus,
    RawResponse,
    ShareSnapshot,
    WindowProbe,
    utcnow,
)
from ..pikpak.client import AnonymousPikPakClient, FileNode, ShareSnapshot as ShareSnapshotData
from ..pikpak.errors import NotFound, PikPakError
from ..pikpak.limiters import share_limiter, window_limiter
from ..pikpak.urls import parse_share_input
from ..security import decrypt, encrypt, sanitize
from . import events
from .windows import analyze_windows, probe_window

log = structlog.get_logger(__name__)


class LineageNotFound(Exception):
    pass


class LineageStateConflict(Exception):
    pass


def _file_key(share_id: str, file_id: str) -> str:
    return f"share:{share_id}/file:{file_id}"


def _folder_key(share_id: str, file_id: str) -> str:
    return f"share:{share_id}/folder:{file_id}"


def _share_key(share_id: str) -> str:
    return f"share:{share_id}"


def _node_type(node: FileNode) -> str:
    if node.is_folder:
        return "folder"
    return "file"


async def create_run(
    session: AsyncSession,
    *,
    input_value: str,
    pass_code: str | None = None,
    file_id: str | None = None,
    hash_value: str | None = None,
    size: int | None = None,
    include_tree: bool = True,
    probe_windows: bool = False,
    graph: bool = False,
    items: list[dict[str, Any]] | None = None,
) -> LineageRun:
    if graph:
        parsed = parse_share_input(input_value, pass_code)
        selections = [
            {
                "file_id": str(item.get("file_id") or ""),
                "kind": str(item.get("kind") or "drive#file"),
                "name": str(item.get("name") or ""),
                "relative_path": item.get("relative_path"),
                "size": int(item.get("size") or 0),
            }
            for item in (items or [])
            if str(item.get("file_id") or "")
        ]
        run = LineageRun(
            input_type="share_graph",
            input_json=json.dumps(
                {
                    "type": "share_graph",
                    "share_id": parsed.share_id,
                    "has_pass_code": bool(parsed.pass_code),
                    "selections": selections,
                }
            ),
            share_id=parsed.share_id,
            file_id=None,
            include_tree=True,
            probe_windows=False,
            status=LineageStatus.QUEUED,
            pass_code_enc=encrypt(parsed.pass_code),
            passcode_unverified=bool(parsed.pass_code),
        )
    elif hash_value and size is not None:
        run = LineageRun(
            input_type="fingerprint",
            input_json=json.dumps(
                {"type": "fingerprint", "hash": hash_value.upper(), "size": size}
            ),
            hash=hash_value.upper(),
            size=size,
            include_tree=include_tree,
            probe_windows=probe_windows,
            status=LineageStatus.QUEUED,
            passcode_unverified=False,
        )
    else:
        parsed = parse_share_input(input_value, pass_code)
        input_type = "share_url" if parsed.source_url else ("share_file" if file_id else "share_id")
        run = LineageRun(
            input_type=input_type,
            input_json=json.dumps(
                {
                    "type": input_type,
                    "share_id": parsed.share_id,
                    "file_id": file_id,
                    "has_pass_code": bool(parsed.pass_code),
                }
            ),
            share_id=parsed.share_id,
            file_id=file_id,
            include_tree=include_tree,
            probe_windows=probe_windows,
            status=LineageStatus.QUEUED,
            pass_code_enc=encrypt(parsed.pass_code),
            passcode_unverified=bool(parsed.pass_code),
        )
    session.add(run)
    await session.flush()
    events.emit(
        session,
        "lineage_run",
        run.id,
        "lineage_run.created",
        {"input_type": run.input_type},
    )
    return run


async def dispatch_lineage(session: AsyncSession) -> None:
    settings = get_settings()
    active = int(
        (
            await session.execute(
                select(func.count(LineageRun.id)).where(
                    LineageRun.status.notin_(
                        LineageStatus.TERMINAL | {LineageStatus.QUEUED}
                    )
                )
            )
        ).scalar_one()
    )
    run = (
        await session.execute(
            select(LineageRun)
            .where(
                LineageRun.status.notin_(LineageStatus.TERMINAL),
                LineageRun.status != LineageStatus.QUEUED,
            )
            .order_by(LineageRun.updated_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if run is None:
        if active >= settings.lineage_max_active_runs:
            return
        run = (
            await session.execute(
                select(LineageRun)
                .where(LineageRun.status == LineageStatus.QUEUED)
                .order_by(LineageRun.created_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if run is None:
        return
    client = AnonymousPikPakClient(limiter=await share_limiter())
    try:
        await _advance(session, run, client)
    except Exception as exc:  # noqa: BLE001
        log.exception("lineage_run_failed", run_id=run.id)
        await session.rollback()
        run = await session.get(LineageRun, run.id)
        if run is not None and run.status not in LineageStatus.TERMINAL:
            run.status = LineageStatus.FAILED
            run.error_code = "INTERNAL_ERROR"
            run.error_message = str(exc)[:500]
            run.finished_at = utcnow()
            await session.commit()
    finally:
        await client.close()


async def _advance(
    session: AsyncSession, run: LineageRun, client: AnonymousPikPakClient
) -> None:
    if run.input_type == "share_graph":
        from .lineage_graph import advance_graph

        await advance_graph(session, run, client)
        return
    if run.status == LineageStatus.QUEUED:
        run.status = LineageStatus.RESOLVING
        run.started_at = utcnow()
        await session.commit()
        return
    if run.status == LineageStatus.RESOLVING:
        await _resolve_target(session, run, client)
        await _finalize_stats(session, run)
        if run.status not in LineageStatus.TERMINAL:
            if run.input_type == "fingerprint":
                run.status = LineageStatus.DONE
                run.finished_at = utcnow()
            elif run.truncated:
                run.status = LineageStatus.DONE
                run.finished_at = utcnow()
            else:
                run.status = LineageStatus.DIRECT_CHAIN
        await session.commit()
        return
    if run.status == LineageStatus.DIRECT_CHAIN:
        await _walk_direct_chain(session, run, client)
        await _walk_folder_chain(session, run, client)
        await _finalize_stats(session, run)
        if run.truncated:
            run.status = LineageStatus.DONE
            run.finished_at = utcnow()
        elif run.include_tree and await _has_tree(session, run):
            run.status = LineageStatus.ENUMERATING
        elif not run.include_tree:
            run.status = LineageStatus.PROBING if run.probe_windows else LineageStatus.DONE
            if run.status == LineageStatus.DONE:
                run.finished_at = utcnow()
        else:
            run.status = LineageStatus.SWEEPING
        await session.commit()
        return
    if run.status == LineageStatus.ENUMERATING:
        done = await _enumerate_chunk(session, run, client)
        await _finalize_stats(session, run)
        if done or run.truncated:
            run.status = LineageStatus.DONE if run.truncated else LineageStatus.SWEEPING
            if run.status == LineageStatus.DONE:
                run.finished_at = utcnow()
        await session.commit()
        return
    if run.status == LineageStatus.SWEEPING:
        done = await _sweep_chunk(session, run, client)
        await _finalize_stats(session, run)
        if done or run.truncated:
            if run.truncated:
                run.status = LineageStatus.DONE
                run.finished_at = utcnow()
            else:
                run.status = LineageStatus.PROBING if run.probe_windows else LineageStatus.DONE
                if run.status == LineageStatus.DONE:
                    run.finished_at = utcnow()
        await session.commit()
        return
    if run.status == LineageStatus.PROBING:
        done = await _probe_chunk(session, run, client)
        await _finalize_stats(session, run)
        if done or run.truncated:
            run.status = LineageStatus.DONE
            run.finished_at = utcnow()
        await session.commit()


async def _finalize_stats(session: AsyncSession, run: LineageRun) -> None:
    run.shares_probed = int(
        (
            await session.execute(
                select(func.count(ShareSnapshot.id)).where(ShareSnapshot.run_id == run.id)
            )
        ).scalar_one()
    )
    run.items_enumerated = int(
        (
            await session.execute(
                select(func.count(LineageNode.id)).where(
                    LineageNode.run_id == run.id, LineageNode.role == "tenant"
                )
            )
        ).scalar_one()
    )
    run.folders_done = int(
        (
            await session.execute(
                select(func.count(LineageFolder.id)).where(
                    LineageFolder.run_id == run.id,
                    LineageFolder.status == "DONE",
                )
            )
        ).scalar_one()
    )
    run.stats_json = json.dumps(
        {
            "requests": run.requests,
            "shares": run.shares_probed,
            "items": run.items_enumerated,
            "folders": run.folders_done,
            "http_4xx": 0,
            "http_5xx": 0,
        }
    )
    run.updated_at = utcnow()


async def _consume_request(session: AsyncSession, run: LineageRun) -> bool:
    settings = get_settings()
    if run.requests >= settings.lineage_max_requests:
        run.truncated = True
        run.termination = "budget_exhausted"
        return False
    run.requests += 1
    return True


async def _record_raw(
    session: AsyncSession,
    run: LineageRun,
    method: str,
    url: str,
    status: int | None,
    payload: Any,
) -> None:
    settings = get_settings()
    run_dir = settings.evidence_dir / run.id
    run_dir.mkdir(parents=True, exist_ok=True)
    seq = run.requests
    path = run_dir / f"{seq:06d}.json"
    try:
        path.write_text(
            json.dumps(
                sanitize({"method": method, "url": url, "status": status, "body": payload}),
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        session.add(
            RawResponse(
                run_id=run.id,
                seq=seq,
                method=method,
                url=url,
                status=status,
                file_path=str(path),
            )
        )
    except OSError:
        log.warning("lineage_evidence_write_failed", run_id=run.id, seq=seq)


async def _store_snapshot(
    session: AsyncSession, run: LineageRun, snapshot: ShareSnapshotData
) -> ShareSnapshot:
    existing = (
        await session.execute(
            select(ShareSnapshot).where(
                ShareSnapshot.run_id == run.id, ShareSnapshot.share_id == snapshot.share_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    raw = dict(snapshot.raw)
    raw.pop("pass_code_token", None)
    row = ShareSnapshot(
        run_id=run.id,
        share_id=snapshot.share_id,
        share_status=snapshot.share_status,
        share_status_text=snapshot.share_status_text,
        title=snapshot.title,
        file_num=snapshot.file_num,
        user_id=snapshot.user_id,
        anonymous_play_seconds=snapshot.anonymous_play_seconds,
        root_json=json.dumps(sanitize(snapshot.files[0].raw) if snapshot.files else None, default=str),
        pass_code_token_enc=encrypt(snapshot.pass_code_token),
        raw_json=json.dumps(sanitize(raw), ensure_ascii=False, default=str),
        probed_at=utcnow(),
    )
    session.add(row)
    await session.flush()
    await _record_raw(
        session,
        run,
        "GET",
        f"/drive/v1/share?share_id={snapshot.share_id}",
        200,
        raw,
    )
    return row


async def _get_snapshot(
    session: AsyncSession,
    run: LineageRun,
    client: AnonymousPikPakClient,
    share_id: str,
    pass_code: str | None = None,
) -> ShareSnapshotData | None:
    row = (
        await session.execute(
            select(ShareSnapshot).where(
                ShareSnapshot.run_id == run.id, ShareSnapshot.share_id == share_id
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        return _snapshot_from_row(row)
    if not await _consume_request(session, run):
        return None
    # Release SQLite's single write lock before the network round trip.
    await session.commit()
    try:
        snapshot = await client.get_share(share_id, pass_code)
    except (PikPakError, NotFound) as exc:
        row = ShareSnapshot(
            run_id=run.id,
            share_id=share_id,
            share_status=getattr(exc, "share_status", None) or "ERROR",
            share_status_text=str(exc),
            probed_at=utcnow(),
            raw_json=json.dumps({"error": str(exc)}),
        )
        session.add(row)
        await session.flush()
        await session.commit()
        return None
    await _store_snapshot(session, run, snapshot)
    await session.commit()
    return snapshot


def _snapshot_from_row(row: ShareSnapshot) -> ShareSnapshotData:
    raw = json.loads(row.raw_json or "{}")
    if not isinstance(raw, dict):
        raw = {}
    files = raw.get("files") or []
    return ShareSnapshotData(
        share_id=row.share_id,
        share_status=row.share_status or "OK",
        share_status_text=row.share_status_text or "",
        title=row.title or "",
        file_num=row.file_num or "",
        user_id=row.user_id,
        anonymous_play_seconds=row.anonymous_play_seconds,
        files=[FileNode.from_json(item, share_id=row.share_id) for item in files],
        pass_code_token=decrypt(row.pass_code_token_enc) or "",
        raw=raw,
    )


async def _store_node(
    session: AsyncSession,
    run: LineageRun,
    node: FileNode,
    *,
    role: str,
    depth: int = 0,
    confidence: str = "observed",
    reason: str | None = None,
    share_status: str | None = None,
    share_status_text: str | None = None,
) -> LineageNode:
    is_folder = node.is_folder
    key = _folder_key(node.share_id or "", node.id) if is_folder else _file_key(node.share_id or "", node.id)
    existing = (
        await session.execute(
            select(LineageNode).where(LineageNode.run_id == run.id, LineageNode.node_key == key)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    row = LineageNode(
        run_id=run.id,
        node_key=key,
        node_type=_node_type(node),
        share_id=node.share_id,
        file_id=node.id,
        parent_id=node.parent_id,
        name=node.name,
        kind=node.kind,
        size=node.size,
        hash=node.hash or None,
        mime_type=node.mime_type,
        user_id=node.user_id,
        platform=node.platform,
        platform_icon=node.platform_icon,
        original_share_id=node.original_share_id or None,
        original_file_id=node.original_file_id or None,
        anonymous_play_seconds=node.anonymous_play_seconds,
        anonymous_play_range=str(node.params.get("anonymous_play_range") or "") or None,
        share_status=share_status or node.share_status,
        share_status_text=share_status_text,
        role=role,
        depth=depth,
        confidence=confidence,
        reason=reason,
        raw_json=json.dumps(sanitize(node.raw), ensure_ascii=False, default=str),
    )
    session.add(row)
    await session.flush()
    if not is_folder and node.hash:
        await _index_copy(session, run, node)
    return row


async def _index_copy(session: AsyncSession, run: LineageRun, node: FileNode) -> None:
    existing = (
        await session.execute(
            select(CopyIndex).where(
                CopyIndex.run_id == run.id,
                CopyIndex.share_id == (node.share_id or ""),
                CopyIndex.file_id == node.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return
    session.add(
        CopyIndex(
            run_id=run.id,
            share_id=node.share_id or "",
            file_id=node.id,
            parent_path=None,
            name=node.name,
            kind=node.kind,
            size=node.size,
            hash=node.hash or None,
            mime_type=node.mime_type,
            is_video=node.is_video,
        )
    )


async def _add_edge(
    session: AsyncSession,
    run: LineageRun,
    from_key: str,
    to_key: str,
    edge_type: str,
    *,
    via_field: str | None = None,
    confidence: str = "observed",
) -> None:
    existing = (
        await session.execute(
            select(LineageEdge).where(
                LineageEdge.run_id == run.id,
                LineageEdge.from_key == from_key,
                LineageEdge.to_key == to_key,
                LineageEdge.edge_type == edge_type,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            LineageEdge(
                run_id=run.id,
                from_key=from_key,
                to_key=to_key,
                edge_type=edge_type,
                via_field=via_field,
                confidence=confidence,
            )
        )


async def _terminate(
    session: AsyncSession,
    run: LineageRun,
    from_key: str,
    reason: str,
    *,
    confidence: str = "observed",
    detail: str | None = None,
) -> None:
    key = f"terminal:{reason}:{from_key}"
    existing = (
        await session.execute(
            select(LineageNode).where(LineageNode.run_id == run.id, LineageNode.node_key == key)
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            LineageNode(
                run_id=run.id,
                node_key=key,
                node_type="terminal",
                role="terminal",
                reason=reason,
                confidence=confidence,
                raw_json=json.dumps({"detail": detail} if detail else {}),
            )
        )
    await _add_edge(session, run, from_key, key, "termination", confidence=confidence)


async def _resolve_target(
    session: AsyncSession, run: LineageRun, client: AnonymousPikPakClient
) -> None:
    if run.input_type == "fingerprint":
        session.add(
            LineageNode(
                run_id=run.id,
                node_key=f"fingerprint:{run.hash}:{run.size}",
                node_type="fingerprint",
                role="target",
                hash=run.hash,
                size=run.size,
                raw_json=json.dumps({"hash": run.hash, "size": run.size}),
            )
        )
        await session.flush()
        return

    pass_code = decrypt(run.pass_code_enc)
    snapshot = await _get_snapshot(session, run, client, run.share_id or "", pass_code)
    if snapshot is None:
        run.status = LineageStatus.DONE
        run.termination = "share_unavailable"
        run.finished_at = utcnow()
        return
    if not snapshot.ok:
        run.termination = f"share_{snapshot.share_status.lower()}"
        run.status = LineageStatus.DONE
        run.finished_at = utcnow()
        return
    if not snapshot.files:
        run.termination = "empty_share"
        run.status = LineageStatus.DONE
        run.finished_at = utcnow()
        return
    root = snapshot.files[0]
    node = root
    if run.file_id:
        if not await _consume_request(session, run):
            return
        try:
            node = await client.file_info(
                run.share_id or "", run.file_id, snapshot.pass_code_token
            )
        except (PikPakError, NotFound) as exc:
            run.status = LineageStatus.DONE
            run.termination = "target_file_not_found"
            run.error_code = getattr(exc, "code", "FILE_NOT_FOUND")
            run.error_message = str(exc)
            run.finished_at = utcnow()
            return
    elif not root.is_folder:
        if not await _consume_request(session, run):
            return
        try:
            node = await client.file_info(
                run.share_id or "", root.id, snapshot.pass_code_token
            )
        except (PikPakError, NotFound):
            node = root
    node.share_id = run.share_id
    await _record_raw(
        session,
        run,
        "GET",
        f"/drive/v1/share/file_info?share_id={run.share_id}&file_id={node.id}",
        200,
        node.raw,
    )
    await _store_node(
        session,
        run,
        node,
        role="target",
        depth=0,
        share_status=snapshot.share_status,
        share_status_text=snapshot.share_status_text,
    )
    run.file_id = node.id
    run.hash = node.hash or None
    run.size = node.size
    root_folder_id: str | None = None
    if node.is_folder:
        root_folder_id = node.id
    elif snapshot.files and snapshot.files[0].is_folder:
        root_folder_id = snapshot.files[0].id
    if root_folder_id:
        existing = (
            await session.execute(
                select(LineageFolder).where(
                    LineageFolder.run_id == run.id, LineageFolder.folder_id == root_folder_id
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                LineageFolder(
                    run_id=run.id,
                    folder_id=root_folder_id,
                    path="",
                    status="PENDING",
                )
            )


async def _has_tree(session: AsyncSession, run: LineageRun) -> bool:
    target = (
        await session.execute(
            select(LineageNode).where(LineageNode.run_id == run.id, LineageNode.role == "target")
        )
    ).scalar_one_or_none()
    if target is not None and target.node_type == "folder":
        return True
    snapshot = (
        await session.execute(
            select(ShareSnapshot).where(
                ShareSnapshot.run_id == run.id, ShareSnapshot.share_id == run.share_id
            )
        )
    ).scalar_one_or_none()
    if snapshot is not None:
        try:
            raw = json.loads(snapshot.root_json or "null")
        except json.JSONDecodeError:
            raw = None
        return bool(raw and raw.get("kind") == "drive#folder")
    return False


async def _walk_direct_chain(
    session: AsyncSession, run: LineageRun, client: AnonymousPikPakClient
) -> None:
    target = (
        await session.execute(
            select(LineageNode).where(LineageNode.run_id == run.id, LineageNode.role == "target")
        )
    ).scalar_one_or_none()
    if target is None or target.node_type != "file":
        return
    current = target
    visited = {target.node_key}
    for depth in range(get_settings().lineage_max_depth):
        if not current.original_share_id and not current.original_file_id:
            await _terminate(session, run, current.node_key, "empty_original_share_id")
            return
        if current.original_file_id.startswith("G__") and not current.original_share_id:
            await _terminate(
                session, run, current.node_key, "global_file_id", detail=current.original_file_id
            )
            return
        if not current.original_file_id:
            await _terminate(session, run, current.node_key, "empty_original_file_id")
            return
        if not current.original_share_id:
            await _terminate(session, run, current.node_key, "empty_original_share_id")
            return
        if current.original_share_id == current.share_id:
            await _terminate(session, run, current.node_key, "self_reference")
            return
        parent_share = current.original_share_id
        snapshot = await _get_snapshot(session, run, client, parent_share)
        if snapshot is None:
            await _terminate(
                session, run, current.node_key, "share_unavailable", confidence="observed"
            )
            return
        if not snapshot.ok:
            await _terminate(
                session, run, current.node_key, f"share_{snapshot.share_status.lower()}"
            )
            return
        if not await _consume_request(session, run):
            return
        try:
            parent = await client.file_info(
                parent_share, current.original_file_id, snapshot.pass_code_token
            )
        except NotFound:
            parent = await _fingerprint_fallback(session, run, client, snapshot, current)
            if parent is None:
                await _terminate(
                    session,
                    run,
                    current.node_key,
                    "parent_file_not_found",
                    confidence="inferred",
                )
                return
            parent.share_id = parent_share
            parent_node = await _store_node(
                session,
                run,
                parent,
                role="ancestor",
                depth=depth + 1,
                confidence="inferred",
                share_status=snapshot.share_status,
            )
            await _add_edge(
                session,
                run,
                current.node_key,
                parent_node.node_key,
                "file_restore",
                via_field="hash+size",
                confidence="inferred",
            )
            current = parent_node
            continue
        except PikPakError as exc:
            await _terminate(
                session, run, current.node_key, getattr(exc, "code", "parent_file_error").lower()
            )
            return
        parent.share_id = parent_share
        parent_node = await _store_node(
            session,
            run,
            parent,
            role="ancestor",
            depth=depth + 1,
            share_status=snapshot.share_status,
            share_status_text=snapshot.share_status_text,
        )
        await _add_edge(
            session,
            run,
            current.node_key,
            parent_node.node_key,
            "file_restore",
            via_field="original_share_id+original_file_id",
        )
        await _record_raw(
            session,
            run,
            "GET",
            f"/drive/v1/share/file_info?share_id={parent_share}&file_id={parent.id}",
            200,
            parent.raw,
        )
        await session.commit()
        current = parent_node
        if current.node_key in visited:
            await _terminate(session, run, current.node_key, "cycle_detected")
            return
        visited.add(current.node_key)
    await _terminate(session, run, current.node_key, "depth_limit")


async def _fingerprint_fallback(
    session: AsyncSession,
    run: LineageRun,
    client: AnonymousPikPakClient,
    snapshot: ShareSnapshotData,
    current: LineageNode,
) -> FileNode | None:
    settings = get_settings()
    root = snapshot.files[0] if snapshot.files else None
    if root is None:
        return None
    if not root.is_folder and root.hash == (current.hash or "") and root.size == (current.size or 0):
        return root
    if not root.is_folder:
        return None
    budget = min(2000, settings.lineage_max_items - run.items_enumerated)
    seen = 0
    queue = [root.id]
    visited: set[str] = set()
    while queue and seen < budget and not run.truncated:
        folder_id = queue.pop(0)
        if folder_id in visited:
            continue
        visited.add(folder_id)
        page_token: str | None = None
        while True:
            if not await _consume_request(session, run):
                return None
            try:
                page = await client.list_folder(
                    snapshot.share_id, folder_id, snapshot.pass_code_token, page_token
                )
            except PikPakError:
                break
            for item in page.files:
                seen += 1
                if item.is_folder:
                    queue.append(item.id)
                elif item.hash == (current.hash or "") and item.size == (current.size or 0):
                    return item
            page_token = page.next_page_token or None
            if not page_token:
                break
    return None


async def _walk_folder_chain(
    session: AsyncSession, run: LineageRun, client: AnonymousPikPakClient
) -> None:
    target = (
        await session.execute(
            select(LineageNode).where(LineageNode.run_id == run.id, LineageNode.role == "target")
        )
    ).scalar_one_or_none()
    if target is None:
        return
    folder_node: FileNode | None = None
    if target.node_type == "folder":
        folder_node = _node_to_file_node(target)
    else:
        snapshot = (
            await session.execute(
                select(ShareSnapshot).where(
                    ShareSnapshot.run_id == run.id, ShareSnapshot.share_id == run.share_id
                )
            )
        ).scalar_one_or_none()
        if snapshot is None:
            return
        data = _snapshot_from_row(snapshot)
        if data.files and data.files[0].is_folder:
            folder_node = data.files[0]
    if folder_node is None:
        return
    folder_node.share_id = run.share_id
    current = await _store_node(session, run, folder_node, role="folder_root", depth=0)
    visited = {current.node_key}
    for depth in range(get_settings().lineage_max_depth):
        if not current.original_share_id:
            await _terminate(session, run, current.node_key, "empty_original_share_id")
            return
        if current.original_share_id == current.share_id:
            await _terminate(session, run, current.node_key, "self_reference")
            return
        parent_share = current.original_share_id
        snapshot = await _get_snapshot(session, run, client, parent_share)
        if snapshot is None:
            await _terminate(session, run, current.node_key, "share_unavailable")
            return
        if not snapshot.ok:
            await _terminate(
                session, run, current.node_key, f"share_{snapshot.share_status.lower()}"
            )
            return
        parent_file: FileNode | None = None
        if current.original_file_id:
            if not await _consume_request(session, run):
                return
            try:
                parent_file = await client.file_info(
                    parent_share, current.original_file_id, snapshot.pass_code_token
                )
            except (NotFound, PikPakError):
                parent_file = None
        if parent_file is None:
            if snapshot.files and snapshot.files[0].is_folder:
                parent_file = snapshot.files[0]
            else:
                await _terminate(
                    session,
                    run,
                    current.node_key,
                    "parent_folder_not_found",
                    confidence="inferred",
                )
                return
        parent_file.share_id = parent_share
        parent_node = await _store_node(
            session, run, parent_file, role="parent_folder", depth=depth + 1
        )
        await _add_edge(
            session,
            run,
            current.node_key,
            parent_node.node_key,
            "folder_restore",
            via_field="original_share_id+original_file_id",
        )
        await session.commit()
        current = parent_node
        if current.node_key in visited:
            await _terminate(session, run, current.node_key, "cycle_detected")
            return
        visited.add(current.node_key)
    await _terminate(session, run, current.node_key, "depth_limit")


def _node_to_file_node(node: LineageNode) -> FileNode:
    try:
        raw = json.loads(node.raw_json or "{}")
    except json.JSONDecodeError:
        raw = {}
    raw.setdefault("id", node.file_id)
    raw.setdefault("name", node.name)
    raw.setdefault("kind", node.kind)
    raw.setdefault("size", str(node.size or 0))
    raw.setdefault("hash", node.hash or "")
    if not isinstance(raw.get("params"), dict):
        raw["params"] = {}
    raw["params"].setdefault("original_share_id", node.original_share_id or "")
    raw["params"].setdefault("original_file_id", node.original_file_id or "")
    return FileNode.from_json(raw, share_id=node.share_id)


async def _enumerate_chunk(
    session: AsyncSession, run: LineageRun, client: AnonymousPikPakClient
) -> bool:
    settings = get_settings()
    snapshot = (
        await session.execute(
            select(ShareSnapshot).where(
                ShareSnapshot.run_id == run.id, ShareSnapshot.share_id == run.share_id
            )
        )
    ).scalar_one_or_none()
    if snapshot is None:
        return True
    token = decrypt(snapshot.pass_code_token_enc) or ""
    folder_count = int(
        (
            await session.execute(
                select(func.count(LineageFolder.id)).where(LineageFolder.run_id == run.id)
            )
        ).scalar_one()
    )
    for _ in range(10):
        if run.truncated:
            return True
        folder = (
            await session.execute(
                select(LineageFolder)
                .where(
                    LineageFolder.run_id == run.id,
                    LineageFolder.status.in_(["PENDING", "ENUMERATING"]),
                )
                .order_by(LineageFolder.id.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if folder is None:
            return True
        folder.status = "ENUMERATING"
        await session.flush()
        page_token = folder.page_token
        for _page in range(20):
            if not await _consume_request(session, run):
                folder.status = "DONE"
                return True
            try:
                page = await client.list_folder(
                    run.share_id or "", folder.folder_id, token, page_token
                )
            except (PikPakError, NotFound) as exc:
                folder.status = "DONE"
                await _terminate(
                    session,
                    run,
                    _folder_key(run.share_id or "", folder.folder_id),
                    getattr(exc, "code", "folder_error").lower(),
                    confidence="inferred",
                )
                break
            await _record_raw(
                session,
                run,
                "GET",
                f"/drive/v1/share/detail?share_id={run.share_id}&parent_id={folder.folder_id}",
                200,
                {"files": [item.raw for item in page.files], "next_page_token": page.next_page_token},
            )
            for item in page.files:
                if run.items_enumerated >= settings.lineage_max_items:
                    run.truncated = True
                    run.termination = "budget_exhausted"
                    folder.status = "DONE"
                    return True
                item.share_id = run.share_id
                row = await _store_node(
                    session,
                    run,
                    item,
                    role="tenant",
                    depth=1,
                )
                if item.is_folder:
                    existing = (
                        await session.execute(
                            select(LineageFolder).where(
                                LineageFolder.run_id == run.id,
                                LineageFolder.folder_id == item.id,
                            )
                        )
                    ).scalar_one_or_none()
                    if existing is None:
                        if folder_count >= settings.lineage_max_folders:
                            run.truncated = True
                            run.termination = "budget_exhausted"
                            continue
                        session.add(
                            LineageFolder(
                                run_id=run.id,
                                folder_id=item.id,
                                path=f"{folder.path}/{item.name}".strip("/"),
                                status="PENDING",
                            )
                        )
                        folder_count += 1
                if row.node_type == "file":
                    copy = (
                        await session.execute(
                            select(CopyIndex).where(
                                CopyIndex.run_id == run.id,
                                CopyIndex.share_id == (run.share_id or ""),
                                CopyIndex.file_id == item.id,
                            )
                        )
                    ).scalar_one_or_none()
                    if copy is not None:
                        copy.parent_path = f"{folder.path}/{item.name}".strip("/")
            folder.page_token = page.next_page_token or None
            if not page.next_page_token:
                folder.status = "DONE"
                await session.commit()
                break
            page_token = page.next_page_token
            await session.commit()
        await session.flush()
    return False


async def _sweep_chunk(
    session: AsyncSession, run: LineageRun, client: AnonymousPikPakClient
) -> bool:
    settings = get_settings()
    if run.truncated:
        return True
    known = set(
        (
            await session.execute(
                select(ShareSnapshot.share_id).where(ShareSnapshot.run_id == run.id)
            )
        ).scalars().all()
    )
    rows = (
        await session.execute(
            select(LineageNode.original_share_id, func.min(LineageNode.depth))
            .where(
                LineageNode.run_id == run.id,
                LineageNode.original_share_id.is_not(None),
                LineageNode.original_share_id != "",
            )
            .group_by(LineageNode.original_share_id)
        )
    ).all()
    pending = [(sid, depth) for sid, depth in rows if sid not in known]
    if not pending:
        return True
    for share_id, depth in pending[:10]:
        if run.shares_probed >= settings.lineage_max_shares:
            run.truncated = True
            run.termination = "budget_exhausted"
            return True
        if depth >= settings.lineage_max_depth:
            session.add(
                ShareSnapshot(
                    run_id=run.id,
                    share_id=share_id,
                    share_status="DEPTH_LIMIT",
                    share_status_text="lineage max depth reached",
                    probed_at=utcnow(),
                    raw_json=json.dumps({"reason": "depth_limit"}),
                )
            )
            await session.flush()
            continue
        snapshot = await _get_snapshot(session, run, client, share_id)
        if snapshot is None:
            continue
        root = snapshot.files[0] if snapshot.files else None
        session.add(
            LineageNode(
                run_id=run.id,
                node_key=_share_key(share_id),
                node_type="share",
                share_id=share_id,
                name=snapshot.title,
                user_id=snapshot.user_id,
                share_status=snapshot.share_status,
                share_status_text=snapshot.share_status_text,
                anonymous_play_seconds=snapshot.anonymous_play_seconds,
                original_share_id=root.original_share_id if root else None,
                original_file_id=root.original_file_id if root else None,
                role="tenant_ref",
                depth=depth + 1,
                confidence="observed",
                raw_json=json.dumps(sanitize(snapshot.raw), ensure_ascii=False, default=str),
            )
        )
        await session.flush()
        await session.commit()
    return False


async def _probe_chunk(
    session: AsyncSession, run: LineageRun, client: AnonymousPikPakClient
) -> bool:
    if run.truncated:
        return True
    if not run.hash or run.size is None:
        return True
    copies = (
        await session.execute(
            select(CopyIndex).where(
                CopyIndex.run_id == run.id,
                CopyIndex.hash == run.hash,
                CopyIndex.size == run.size,
            )
        )
    ).scalars().all()
    if not copies:
        copies = (
            await session.execute(
                select(CopyIndex)
                .where(CopyIndex.hash == run.hash, CopyIndex.size == run.size)
                .order_by(CopyIndex.id.desc())
                .limit(20)
            )
        ).scalars().all()
    probed = set(
        (
            await session.execute(
                select(WindowProbe.share_id, WindowProbe.file_id).where(
                    WindowProbe.run_id == run.id
                )
            )
        ).all()
    )
    pending = [copy for copy in copies if (copy.share_id, copy.file_id) not in probed]
    if not pending:
        return True
    probe_client = None
    try:
        for copy in pending[:3]:
            snapshot = await _get_snapshot(session, run, client, copy.share_id)
            if snapshot is None:
                if run.truncated:
                    return True
                session.add(
                    WindowProbe(
                        run_id=run.id,
                        share_id=copy.share_id,
                        file_id=copy.file_id,
                        size=run.size,
                        supported=False,
                        raw_json=json.dumps({"reason": "share_unavailable", "intervals": []}),
                    )
                )
                await session.flush()
                continue
            if not snapshot.ok:
                session.add(
                    WindowProbe(
                        run_id=run.id,
                        share_id=copy.share_id,
                        file_id=copy.file_id,
                        size=run.size,
                        supported=False,
                        raw_json=json.dumps(
                            {"reason": f"share_{snapshot.share_status.lower()}", "intervals": []}
                        ),
                    )
                )
                await session.flush()
                continue
            if not await _consume_request(session, run):
                return True
            try:
                node = await client.file_info(
                    copy.share_id, copy.file_id, snapshot.pass_code_token
                )
            except (PikPakError, NotFound):
                session.add(
                    WindowProbe(
                        run_id=run.id,
                        share_id=copy.share_id,
                        file_id=copy.file_id,
                        size=run.size,
                        supported=False,
                        raw_json=json.dumps({"reason": "file_info_failed", "intervals": []}),
                    )
                )
                await session.flush()
                continue
            url = node.original_media_url()
            if not url:
                session.add(
                    WindowProbe(
                        run_id=run.id,
                        share_id=copy.share_id,
                        file_id=copy.file_id,
                        size=node.size,
                        supported=False,
                        raw_json=json.dumps({"reason": "no_original_media", "intervals": []}),
                    )
                )
                await session.flush()
                continue
            if probe_client is None:
                import httpx

                probe_client = httpx.AsyncClient(
                    timeout=httpx.Timeout(60.0), follow_redirects=True
                )
            result = await probe_window(
                url, node.size, client=probe_client, limiter=await window_limiter()
            )
            session.add(
                WindowProbe(
                    run_id=run.id,
                    share_id=copy.share_id,
                    file_id=copy.file_id,
                    media_name=(node.original_media() or {}).get("media_name"),
                    url_host=None,
                    size=node.size,
                    no_range_status=result.no_range_status,
                    head_end=result.head_end,
                    tail_start=result.tail_start,
                    tail_end=result.tail_end,
                    full_200=result.full_200,
                    supported=result.supported,
                    raw_json=json.dumps(
                        {
                            "intervals": result.intervals,
                            "reason": result.reason,
                            "raw": result.raw,
                        }
                    ),
                    probed_at=utcnow(),
                )
            )
            await session.flush()
            await session.commit()
    finally:
        if probe_client is not None:
            await probe_client.aclose()
    return False


async def get_run(session: AsyncSession, run_id: str) -> LineageRun:
    run = await session.get(LineageRun, run_id)
    if run is None:
        raise LineageNotFound("溯源任务不存在")
    return run


async def resume_run(session: AsyncSession, run_id: str) -> LineageRun:
    run = await get_run(session, run_id)
    if run.input_type != "share_graph":
        raise LineageStateConflict("只有全量血缘图支持继续运行")
    if not run.truncated:
        raise LineageStateConflict("该运行没有被预算截断")
    if run.status != LineageStatus.DONE:
        raise LineageStateConflict("只有已完成的截断运行可以继续")
    await session.execute(
        update(LineageGraphShare)
        .where(
            LineageGraphShare.run_id == run.id,
            LineageGraphShare.state.in_(
                ["PENDING", "ENUMERATING", "BUDGET"]
            ),
        )
        .values(state="PENDING", error_code=None, error_message=None)
    )
    await session.execute(
        update(LineageGraphFolder)
        .where(
            LineageGraphFolder.run_id == run.id,
            LineageGraphFolder.state.in_(["PENDING", "ENUMERATING"]),
        )
        .values(state="PENDING")
    )
    start = (
        await session.execute(
            select(LineageGraphShare.id).where(
                LineageGraphShare.run_id == run.id,
                LineageGraphShare.role == "start",
            )
        )
    ).first()
    run.truncated = False
    run.termination = None
    run.error_code = None
    run.error_message = None
    run.finished_at = None
    run.status = LineageStatus.ENUMERATING if start else LineageStatus.RESOLVING
    run.updated_at = utcnow()
    events.emit(session, "lineage_run", run.id, "lineage_run.resumed", {})
    await session.commit()
    return run


async def cancel_run(session: AsyncSession, run_id: str) -> LineageRun:
    run = await get_run(session, run_id)
    if run.status in LineageStatus.TERMINAL:
        return run
    run.status = LineageStatus.CANCELLED
    run.finished_at = utcnow()
    events.emit(session, "lineage_run", run.id, "lineage_run.cancelled", {})
    await session.commit()
    return run




async def delete_run(session: AsyncSession, run_id: str) -> str:
    run = await get_run(session, run_id)
    if run.status not in LineageStatus.TERMINAL:
        raise LineageStateConflict("溯源任务仍在进行，请先取消再删除")
    for model in (
        LineageGraphItem,
        LineageGraphFolder,
        LineageGraphShare,
        LineageNode,
        LineageEdge,
        ShareSnapshot,
        CopyIndex,
        WindowProbe,
        RawResponse,
        LineageFolder,
    ):
        await session.execute(delete(model).where(model.run_id == run.id))
    await session.execute(
        delete(Event).where(
            Event.entity_type == "lineage_run",
            Event.entity_id == run.id,
        )
    )
    await session.execute(delete(LineageRun).where(LineageRun.id == run.id))
    await session.commit()
    shutil.rmtree(get_settings().evidence_dir / run.id, ignore_errors=True)
    return run.id


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


async def build_report(session: AsyncSession, run: LineageRun) -> dict[str, Any]:
    nodes = (
        await session.execute(
            select(LineageNode).where(LineageNode.run_id == run.id).order_by(LineageNode.depth)
        )
    ).scalars().all()
    edges = (
        await session.execute(select(LineageEdge).where(LineageEdge.run_id == run.id))
    ).scalars().all()
    snapshots = {
        row.share_id: row
        for row in (
            await session.execute(
                select(ShareSnapshot).where(ShareSnapshot.run_id == run.id)
            )
        ).scalars().all()
    }
    probes = (
        await session.execute(
            select(WindowProbe).where(WindowProbe.run_id == run.id).order_by(WindowProbe.id)
        )
    ).scalars().all()
    raw_count = int(
        (
            await session.execute(
                select(func.count(RawResponse.id)).where(RawResponse.run_id == run.id)
            )
        ).scalar_one()
    )

    by_key = {node.node_key: node for node in nodes}
    target_node = next((node for node in nodes if node.role == "target"), None)
    target: dict[str, Any]
    if run.input_type == "fingerprint":
        target = {"hash": run.hash, "size": run.size, "type": "fingerprint"}
    elif target_node is not None:
        target = {
            "share_id": target_node.share_id,
            "file_id": target_node.file_id,
            "name": target_node.name,
            "size": target_node.size,
            "hash": target_node.hash,
            "kind": target_node.kind,
            "mime_type": target_node.mime_type,
            "node": target_node.node_key,
            "platform": target_node.platform,
        }
    else:
        target = {"share_id": run.share_id, "file_id": run.file_id}

    direct_keys: list[str] = []
    if target_node is not None and target_node.node_type == "file":
        direct_keys.append(target_node.node_key)
    direct_keys.extend(
        node.node_key for node in nodes if node.role == "ancestor" and node.node_type == "file"
    )
    folder_keys: list[str] = []
    if target_node is not None and target_node.node_type == "folder":
        folder_keys.append(target_node.node_key)
    folder_keys.extend(
        node.node_key
        for node in nodes
        if node.role in ("parent_folder", "folder_root")
    )

    def chain_for(keys: list[str]) -> list[dict[str, Any]]:
        ordered = [by_key[key] for key in keys if key in by_key]
        ordered.sort(key=lambda node: node.depth)
        out = []
        for node in ordered:
            out.append(
                {
                    "node": node.node_key,
                    "role": "target" if node.role == "target" else "parent",
                    "share_id": node.share_id,
                    "file_id": node.file_id,
                    "name": node.name,
                    "size": node.size,
                    "hash": node.hash,
                    "kind": node.kind,
                    "platform": node.platform,
                    "depth": node.depth,
                    "confidence": node.confidence,
                }
            )
        return out

    direct_chain = chain_for(direct_keys)
    folder_chain = chain_for(folder_keys)

    terminations = [
        {
            "node": node.node_key,
            "reason": node.reason,
            "confidence": node.confidence,
            "detail": json.loads(node.raw_json or "{}").get("detail")
            if node.raw_json
            else None,
        }
        for node in nodes
        if node.node_type == "terminal"
    ]
    for edge in edges:
        if edge.edge_type == "termination":
            if edge.from_key in direct_keys:
                for item in terminations:
                    if item["node"] == edge.to_key:
                        item["chain"] = "direct"
            if edge.from_key in folder_keys:
                for item in terminations:
                    if item["node"] == edge.to_key:
                        item["chain"] = "folder"
    direct_terminals = [item for item in terminations if item.get("chain") == "direct"]
    folder_terminals = [item for item in terminations if item.get("chain") == "folder"]
    direct_chain.extend(
        {"node": item["node"], "role": "terminal", "reason": item["reason"], "confidence": item["confidence"]}
        for item in direct_terminals
    )
    folder_chain.extend(
        {"node": item["node"], "role": "terminal", "reason": item["reason"], "confidence": item["confidence"]}
        for item in folder_terminals
    )

    ref_counts: dict[str, int] = {}
    for node in nodes:
        if node.role == "tenant" and node.original_share_id:
            ref_counts[node.original_share_id] = ref_counts.get(node.original_share_id, 0) + 1
    tenant_refs = []
    for share_id, count in sorted(ref_counts.items(), key=lambda item: item[1], reverse=True):
        snapshot = snapshots.get(share_id)
        tenant_refs.append(
            {
                "share_id": share_id,
                "ref_count": count,
                "share_status": snapshot.share_status if snapshot else None,
                "seconds": snapshot.anonymous_play_seconds if snapshot else None,
                "owner": snapshot.user_id if snapshot else None,
            }
        )

    if run.input_type == "fingerprint":
        copies = (
            await session.execute(
                select(CopyIndex)
                .where(CopyIndex.hash == run.hash, CopyIndex.size == run.size)
                .order_by(CopyIndex.id.desc())
                .limit(200)
            )
        ).scalars().all()
    else:
        copies = (
            await session.execute(
                select(CopyIndex).where(
                    CopyIndex.run_id == run.id,
                    CopyIndex.hash == run.hash,
                    CopyIndex.size == run.size,
                )
            )
        ).scalars().all()
    copy_rows = [
        {
            "hash": copy.hash,
            "size": copy.size,
            "share_id": copy.share_id,
            "file_id": copy.file_id,
            "path": copy.parent_path,
            "name": copy.name,
            "kind": copy.kind,
            "mime_type": copy.mime_type,
            "is_video": copy.is_video,
            "share_status": snapshots[copy.share_id].share_status
            if copy.share_id in snapshots
            else None,
        }
        for copy in copies
    ]

    window_rows = []
    all_intervals: list[tuple[int, int]] = []
    for probe in probes:
        try:
            raw = json.loads(probe.raw_json or "{}")
        except json.JSONDecodeError:
            raw = {}
        intervals = [tuple(item) for item in raw.get("intervals") or []]
        all_intervals.extend(intervals)
        window_rows.append(
            {
                "share_id": probe.share_id,
                "file_id": probe.file_id,
                "media_name": probe.media_name,
                "size": probe.size,
                "no_range_status": probe.no_range_status,
                "head": [0, probe.head_end] if probe.head_end is not None else None,
                "tail": [probe.tail_start, probe.tail_end]
                if probe.tail_start is not None
                else None,
                "full_200": probe.full_200,
                "supported": probe.supported,
                "reason": raw.get("reason"),
                "intervals": [list(item) for item in intervals],
            }
        )
    windows: dict[str, Any] = {}
    if probes:
        analysis = analyze_windows(run.size or 0, all_intervals)
        windows = {
            "size": run.size,
            "shares": window_rows,
            "union_bytes": analysis["covered_bytes"],
            "union_ratio": analysis["union_ratio"],
            "merged": [list(item) for item in analysis["merged"]],
            "missing": [list(item) for item in analysis["missing"]],
            "conclusion": analysis["conclusion"],
        }

    duration_ms = 0
    if run.finished_at and run.started_at:
        start = run.started_at.replace(tzinfo=timezone.utc) if run.started_at.tzinfo is None else run.started_at
        end = run.finished_at.replace(tzinfo=timezone.utc) if run.finished_at.tzinfo is None else run.finished_at
        duration_ms = max(0, int((end - start).total_seconds() * 1000))
    stats = {
        "requests": run.requests,
        "shares": len(snapshots),
        "items": sum(1 for node in nodes if node.role == "tenant"),
        "folders": sum(1 for node in nodes if node.node_type == "folder"),
        "duration_ms": duration_ms,
        "http_4xx": 0,
        "http_5xx": 0,
    }
    probed_at = max((row.probed_at for row in snapshots.values()), default=None)
    report = {
        "run_id": run.id,
        "status": run.status,
        "input": json.loads(run.input_json),
        "target": target,
        "direct_chain": direct_chain,
        "folder_chain": folder_chain,
        "tenant_refs": tenant_refs,
        "copies": copy_rows,
        "anonymous_windows": windows,
        "terminations": terminations,
        "stats": stats,
        "evidence": {
            "raw_dir": str(get_settings().evidence_dir / run.id),
            "count": raw_count,
        },
        "probed_at": _iso(probed_at),
        "truncated": run.truncated,
        "passcode_unverified": run.passcode_unverified,
        "error": {"code": run.error_code, "message": run.error_message}
        if run.error_code
        else None,
    }
    if run.input_type == "share_graph":
        from .lineage_graph import build_graph

        report["graph"] = await build_graph(session, run)
    return report


def report_markdown(report: dict[str, Any]) -> str:
    target = report.get("target") or {}
    lines = [
        "# PikPak 血缘报告",
        "",
        f"- 运行 ID：`{report.get('run_id')}`",
        f"- 状态：`{report.get('status')}`",
        f"- 目标：`{target.get('name') or target.get('file_id') or target.get('hash')}`",
        f"- 大小：`{target.get('size')}`",
        f"- Hash：`{target.get('hash')}`",
        f"- 快照时间：`{report.get('probed_at')}`",
        f"- 预算截断：`{report.get('truncated')}`",
        f"- 提取码未实测：`{report.get('passcode_unverified')}`",
        "",
        "## 直系链",
        "",
    ]
    for item in report.get("direct_chain") or []:
        lines.append(
            f"- `{item.get('node')}` ({item.get('role')})"
            + (f" — {item.get('reason')}" if item.get("reason") else "")
        )
    lines += ["", "## 文件夹链", ""]
    for item in report.get("folder_chain") or []:
        lines.append(
            f"- `{item.get('node')}` ({item.get('role')})"
            + (f" — {item.get('reason')}" if item.get("reason") else "")
        )
    lines += ["", "## 聚合引用（tenant refs）", "", "| share_id | 引用数 | 状态 | seconds | 属主 |", "|---|---|---|---|---|"]
    for item in report.get("tenant_refs") or []:
        lines.append(
            f"| `{item.get('share_id')}` | {item.get('ref_count')} | {item.get('share_status')} | {item.get('seconds')} | `{item.get('owner')}` |"
        )
    lines += ["", "## 副本", "", "| share_id | file_id | 名称 | 大小 | 状态 |", "|---|---|---|---|---|"]
    for item in report.get("copies") or []:
        lines.append(
            f"| `{item.get('share_id')}` | `{item.get('file_id')}` | {item.get('name')} | {item.get('size')} | {item.get('share_status')} |"
        )
    windows = report.get("anonymous_windows") or {}
    lines += ["", "## 匿名窗口", ""]
    if windows:
        lines.append(f"- 并集：`{windows.get('union_bytes')}` / `{windows.get('size')}`")
        lines.append(f"- 结论：`{windows.get('conclusion')}`")
        for item in windows.get("shares") or []:
            lines.append(
                f"- `{item.get('share_id')}` head={item.get('head')} tail={item.get('tail')} supported={item.get('supported')}"
            )
    else:
        lines.append("- 未执行窗口探针")
    lines += ["", "## 终止点", ""]
    for item in report.get("terminations") or []:
        lines.append(
            f"- `{item.get('node')}` — {item.get('reason')} ({item.get('confidence')})"
        )
    stats = report.get("stats") or {}
    lines += [
        "",
        "## 统计",
        "",
        f"- 请求：`{stats.get('requests')}`",
        f"- 分享：`{stats.get('shares')}`",
        f"- 条目：`{stats.get('items')}`",
        f"- 耗时：`{stats.get('duration_ms')} ms`",
    ]
    return "\n".join(lines) + "\n"


def report_dot(report: dict[str, Any]) -> str:
    def esc(value: Any) -> str:
        text = str(value or "")
        return text.replace("\\", "\\\\").replace('"', '\\"')

    lines = ["digraph lineage {", "  rankdir=LR;", '  node [shape=box, fontsize=10];']
    seen: set[str] = set()
    for section in ("direct_chain", "folder_chain"):
        for item in report.get(section) or []:
            key = item.get("node")
            if not key or key in seen:
                continue
            seen.add(key)
            label = item.get("name") or item.get("reason") or key
            shape = "ellipse" if item.get("role") == "terminal" else "box"
            lines.append(f'  "{esc(key)}" [label="{esc(label)}", shape={shape}];')
    for item in report.get("tenant_refs") or []:
        key = f"share:{item.get('share_id')}"
        if key not in seen:
            seen.add(key)
            lines.append(
                f'  "{esc(key)}" [label="{esc(item.get("share_id"))}\\nrefs={item.get("ref_count")}", style=dashed];'
            )
    target_key = (report.get("target") or {}).get("node")
    if target_key:
        for item in report.get("tenant_refs") or []:
            key = f"share:{item.get('share_id')}"
            if key in seen:
                lines.append(
                    f'  "{esc(target_key)}" -> "{esc(key)}" [label="tenant_ref", style=dotted, arrowhead=none];'
                )
    for section in ("direct_chain", "folder_chain"):
        chain = report.get(section) or []
        for left, right in zip(chain, chain[1:]):
            edge_type = "folder_restore" if section == "folder_chain" else "file_restore"
            style = "dashed" if right.get("role") == "terminal" else "solid"
            lines.append(
                f'  "{esc(left.get("node"))}" -> "{esc(right.get("node"))}" [label="{edge_type}", style={style}];'
            )
    lines.append("}")
    return "\n".join(lines) + "\n"
