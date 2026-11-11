from __future__ import annotations

import json
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import (
    LineageGraphFolder,
    LineageGraphItem,
    LineageGraphShare,
    LineageRun,
    LineageStatus,
    ShareSnapshot,
    utcnow,
)
from ..pikpak.client import AnonymousPikPakClient, FileNode
from ..security import decrypt, sanitize
from .lineage import LineageNotFound, _consume_request, _get_snapshot, _record_raw

log = structlog.get_logger(__name__)

SHARE_STATE_PENDING = "PENDING"
SHARE_STATE_ENUMERATING = "ENUMERATING"
SHARE_STATE_DONE = "DONE"
SHARE_STATE_UNAVAILABLE = "UNAVAILABLE"
SHARE_STATE_DEPTH_LIMIT = "DEPTH_LIMIT"
SHARE_STATE_BUDGET = "BUDGET"

FOLDER_STATE_PENDING = "PENDING"
FOLDER_STATE_ENUMERATING = "ENUMERATING"
FOLDER_STATE_DONE = "DONE"


def share_node_id(share_id: str) -> str:
    return f"share:{share_id}"


def _join_path(parent: str, name: str) -> str:
    parent = (parent or "").strip("/")
    name = (name or "").strip("/")
    if not parent:
        return name
    if not name:
        return parent
    return f"{parent}/{name}"


def _item_payload(item: LineageGraphItem) -> dict[str, Any]:
    return {
        "id": item.file_id,
        "share_id": item.share_id,
        "file_id": item.file_id,
        "kind": item.kind,
        "name": item.name,
        "path": item.path,
        "parent_id": item.parent_id,
        "size": item.size,
        "hash": item.hash,
        "mime_type": item.mime_type,
        "is_video": item.is_video,
        "original_share_id": item.original_share_id,
        "original_file_id": item.original_file_id,
    }


def _effective_state(run: LineageRun, row: LineageGraphShare) -> str:
    if run.truncated and row.state in (SHARE_STATE_PENDING, SHARE_STATE_ENUMERATING):
        return SHARE_STATE_BUDGET
    return row.state


def _share_payload(
    row: LineageGraphShare,
    *,
    state: str | None = None,
    file_count: int = 0,
    folder_count: int = 0,
    incoming_count: int = 0,
    outgoing_count: int = 0,
) -> dict[str, Any]:
    effective = state or row.state
    error_code = row.error_code
    error_message = row.error_message
    if effective == SHARE_STATE_BUDGET:
        error_code = error_code or "BUDGET_EXHAUSTED"
        error_message = error_message or "预算已截断，未展开该分享"
    known_name = row.title or row.root_name
    if not known_name:
        known_name = (
            "未展开分享"
            if effective in (SHARE_STATE_PENDING, SHARE_STATE_BUDGET)
            else row.share_id
        )
    return {
        "id": share_node_id(row.share_id),
        "share_id": row.share_id,
        "depth": row.depth,
        "role": row.role,
        "state": effective,
        "status": row.share_status,
        "status_text": row.share_status_text,
        "title": known_name,
        "name_known": bool(row.title or row.root_name),
        "source_hint": None,
        "owner": row.user_id,
        "file_num": row.file_num,
        "anonymous_play_seconds": row.anonymous_play_seconds,
        "root_kind": row.root_kind,
        "root_file_id": row.root_file_id,
        "root_name": row.root_name,
        "file_count": file_count,
        "folder_count": folder_count,
        "incoming_count": incoming_count,
        "outgoing_count": outgoing_count,
        "error_code": error_code,
        "error_message": error_message,
        "url": f"https://mypikpak.com/s/{row.share_id}",
    }


async def _get_share_row(
    session: AsyncSession, run: LineageRun, share_id: str
) -> LineageGraphShare | None:
    return (
        await session.execute(
            select(LineageGraphShare).where(
                LineageGraphShare.run_id == run.id,
                LineageGraphShare.share_id == share_id,
            )
        )
    ).scalar_one_or_none()


async def _get_or_create_share(
    session: AsyncSession,
    run: LineageRun,
    share_id: str,
    *,
    depth: int,
    role: str,
) -> LineageGraphShare | None:
    existing = await _get_share_row(session, run, share_id)
    if existing is not None:
        return existing
    settings = get_settings()
    if depth > settings.lineage_max_depth:
        row = LineageGraphShare(
            run_id=run.id,
            share_id=share_id,
            depth=depth,
            role=role,
            state=SHARE_STATE_DEPTH_LIMIT,
            error_code="DEPTH_LIMIT",
            error_message="超过血缘最大深度",
        )
        session.add(row)
        await session.flush()
        return row
    count = int(
        (
            await session.execute(
                select(func.count(LineageGraphShare.id)).where(
                    LineageGraphShare.run_id == run.id
                )
            )
        ).scalar_one()
    )
    if count >= settings.lineage_max_shares:
        run.truncated = True
        run.termination = "budget_exhausted"
        return None
    row = LineageGraphShare(
        run_id=run.id,
        share_id=share_id,
        depth=depth,
        role=role,
        state=SHARE_STATE_PENDING,
    )
    session.add(row)
    await session.flush()
    return row


async def _store_item(
    session: AsyncSession,
    run: LineageRun,
    share: LineageGraphShare,
    node: FileNode,
    *,
    path: str,
    traced: bool = False,
) -> LineageGraphItem | None:
    existing = (
        await session.execute(
            select(LineageGraphItem).where(
                LineageGraphItem.run_id == run.id,
                LineageGraphItem.share_id == share.share_id,
                LineageGraphItem.file_id == node.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if node.original_share_id and not existing.original_share_id:
            existing.original_share_id = node.original_share_id
        if node.original_file_id and not existing.original_file_id:
            existing.original_file_id = node.original_file_id
        if node.hash and not existing.hash:
            existing.hash = node.hash
        if path and not existing.path:
            existing.path = path
        if traced:
            existing.traced = True
        return existing
    settings = get_settings()
    if run.items_enumerated >= settings.lineage_max_items:
        run.truncated = True
        run.termination = "budget_exhausted"
        return None
    row = LineageGraphItem(
        run_id=run.id,
        share_id=share.share_id,
        file_id=node.id,
        kind=node.kind or ("drive#folder" if node.is_folder else "drive#file"),
        name=node.name,
        path=path,
        parent_id=node.parent_id or None,
        size=node.size,
        hash=node.hash or None,
        mime_type=node.mime_type or None,
        is_video=node.is_video,
        original_share_id=node.original_share_id or None,
        original_file_id=node.original_file_id or None,
        traced=traced,
        depth=share.depth,
    )
    session.add(row)
    run.items_enumerated += 1
    await session.flush()
    return row


async def _ensure_folder(
    session: AsyncSession,
    run: LineageRun,
    share: LineageGraphShare,
    node: FileNode,
    *,
    path: str,
) -> LineageGraphFolder | None:
    existing = (
        await session.execute(
            select(LineageGraphFolder).where(
                LineageGraphFolder.run_id == run.id,
                LineageGraphFolder.share_id == share.share_id,
                LineageGraphFolder.folder_id == node.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if path and not existing.path:
            existing.path = path
        return existing
    settings = get_settings()
    count = int(
        (
            await session.execute(
                select(func.count(LineageGraphFolder.id)).where(
                    LineageGraphFolder.run_id == run.id
                )
            )
        ).scalar_one()
    )
    if count >= settings.lineage_max_folders:
        run.truncated = True
        run.termination = "budget_exhausted"
        return None
    row = LineageGraphFolder(
        run_id=run.id,
        share_id=share.share_id,
        folder_id=node.id,
        path=path,
        state=FOLDER_STATE_PENDING,
        depth=share.depth,
    )
    session.add(row)
    await session.flush()
    return row


async def _handle_origin_refs(
    session: AsyncSession,
    run: LineageRun,
    share: LineageGraphShare,
    item: LineageGraphItem,
) -> None:
    if not item.traced:
        return
    origin = (item.original_share_id or "").strip()
    if not origin or origin == share.share_id:
        return
    await _get_or_create_share(
        session,
        run,
        origin,
        depth=share.depth + 1,
        role="parent",
    )




def _graph_selections(run: LineageRun) -> list[dict[str, Any]]:
    try:
        data = json.loads(run.input_json or "{}")
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    selections = data.get("selections")
    if not isinstance(selections, list):
        return []
    return [item for item in selections if isinstance(item, dict)]


async def _seed_selected_items(
    session: AsyncSession,
    run: LineageRun,
    client: AnonymousPikPakClient,
    share: LineageGraphShare,
    snapshot: Any,
    selections: list[dict[str, Any]],
) -> None:
    share.share_status = snapshot.share_status
    share.share_status_text = snapshot.share_status_text
    share.title = snapshot.title
    share.user_id = snapshot.user_id
    share.file_num = snapshot.file_num
    share.anonymous_play_seconds = snapshot.anonymous_play_seconds
    share.raw_json = json.dumps(sanitize(snapshot.raw), ensure_ascii=False, default=str)
    root = snapshot.files[0] if snapshot.files else None
    if root is not None:
        share.root_kind = root.kind
        share.root_file_id = root.id
        share.root_name = root.name

    snap_row = (
        await session.execute(
            select(ShareSnapshot).where(
                ShareSnapshot.run_id == run.id,
                ShareSnapshot.share_id == share.share_id,
            )
        )
    ).scalar_one_or_none()
    token = decrypt(snap_row.pass_code_token_enc) if snap_row is not None else ""

    file_ids = [
        str(item.get("file_id") or "")
        for item in selections
        if str(item.get("kind") or "") != "drive#folder"
        and str(item.get("file_id") or "")
    ]
    infos: dict[str, FileNode] = {}
    if file_ids and await _consume_request(session, run):
        await session.commit()
        try:
            rows = await client.batch_file_info(
                share.share_id, file_ids[:100], token or ""
            )
            infos = {row.id: row for row in rows}
        except Exception:  # noqa: BLE001
            infos = {}

    has_folder = False
    for selection in selections:
        file_id = str(selection.get("file_id") or "")
        if not file_id:
            continue
        kind = str(selection.get("kind") or "drive#file")
        name = str(selection.get("name") or "")
        path = str(selection.get("relative_path") or name)
        node = FileNode(
            id=file_id,
            name=name,
            kind=kind,
            size=int(selection.get("size") or 0),
            share_id=share.share_id,
        )
        info = infos.get(file_id)
        if info is not None:
            node.name = info.name or node.name
            node.hash = info.hash
            node.mime_type = info.mime_type
            node.parent_id = info.parent_id
            node.size = info.size or node.size
            node.params = info.params
        stored = await _store_item(session, run, share, node, path=path, traced=True)
        if stored is None:
            break
        if kind == "drive#folder":
            has_folder = True
            if await _ensure_folder(session, run, share, node, path=path) is None:
                break
        else:
            await _handle_origin_refs(session, run, share, stored)
    share.state = SHARE_STATE_ENUMERATING if has_folder else SHARE_STATE_DONE
    await session.flush()


def _apply_share_snapshot(share: LineageGraphShare, snapshot: Any) -> None:
    share.share_status = snapshot.share_status
    share.share_status_text = snapshot.share_status_text
    share.title = snapshot.title
    share.user_id = snapshot.user_id
    share.file_num = snapshot.file_num
    share.anonymous_play_seconds = snapshot.anonymous_play_seconds
    share.raw_json = json.dumps(sanitize(snapshot.raw), ensure_ascii=False, default=str)
    root = snapshot.files[0] if snapshot.files else None
    if root is not None:
        share.root_kind = root.kind
        share.root_file_id = root.id
        share.root_name = root.name


async def _seed_share(
    session: AsyncSession,
    run: LineageRun,
    share: LineageGraphShare,
    snapshot: Any,
) -> None:
    _apply_share_snapshot(share, snapshot)
    root = snapshot.files[0] if snapshot.files else None
    if root is None:
        share.state = SHARE_STATE_DONE
        await session.flush()
        return
    root.share_id = share.share_id
    path = root.name or ""
    stored = await _store_item(session, run, share, root, path=path, traced=True)
    if stored is not None:
        await _handle_origin_refs(session, run, share, stored)
    if root.is_folder:
        await _ensure_folder(session, run, share, root, path=path)
        share.state = SHARE_STATE_ENUMERATING
    else:
        share.state = SHARE_STATE_DONE
    await session.flush()


async def _process_share_row(
    session: AsyncSession,
    run: LineageRun,
    client: AnonymousPikPakClient,
    share: LineageGraphShare,
) -> None:
    if share.state != SHARE_STATE_PENDING:
        return
    share.state = SHARE_STATE_ENUMERATING
    await session.flush()
    pass_code = decrypt(run.pass_code_enc) if share.role == "start" else None
    snapshot = await _get_snapshot(
        session, run, client, share.share_id, pass_code=pass_code
    )
    if snapshot is None:
        snap_row = (
            await session.execute(
                select(ShareSnapshot).where(
                    ShareSnapshot.run_id == run.id,
                    ShareSnapshot.share_id == share.share_id,
                )
            )
        ).scalar_one_or_none()
        if snap_row is not None:
            share.share_status = snap_row.share_status
            share.share_status_text = snap_row.share_status_text
            share.error_code = snap_row.share_status
            share.error_message = snap_row.share_status_text
        share.state = (
            SHARE_STATE_BUDGET if run.truncated else SHARE_STATE_UNAVAILABLE
        )
        await session.flush()
        return
    if not snapshot.ok:
        share.share_status = snapshot.share_status
        share.share_status_text = snapshot.share_status_text
        share.title = snapshot.title or share.title
        share.state = SHARE_STATE_DONE
        await session.flush()
        return
    _apply_share_snapshot(share, snapshot)
    if share.role != "start":
        await _resolve_trace_refs(session, run, client, share, snapshot)
        return
    selections = _graph_selections(run)
    if selections:
        await _seed_selected_items(session, run, client, share, snapshot, selections)
    else:
        await _seed_share(session, run, share, snapshot)
    await session.flush()


async def _resolve_trace_refs(
    session: AsyncSession,
    run: LineageRun,
    client: AnonymousPikPakClient,
    share: LineageGraphShare,
    snapshot: Any,
) -> None:
    refs = (
        await session.execute(
            select(
                LineageGraphItem.original_file_id,
                LineageGraphItem.name,
            ).where(
                LineageGraphItem.run_id == run.id,
                LineageGraphItem.original_share_id == share.share_id,
                LineageGraphItem.traced.is_(True),
                LineageGraphItem.original_file_id.is_not(None),
                LineageGraphItem.original_file_id != "",
            )
        )
    ).all()
    seen: set[str] = set()
    token = snapshot.pass_code_token or ""
    for file_id, name in refs:
        file_id = str(file_id or "")
        if not file_id or file_id in seen:
            continue
        seen.add(file_id)
        if not await _consume_request(session, run):
            share.state = SHARE_STATE_BUDGET
            await session.flush()
            return
        await session.commit()
        try:
            node = await client.file_info(share.share_id, file_id, token)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "lineage_trace_file_failed",
                run_id=run.id,
                share_id=share.share_id,
                file_id=file_id,
                error=str(exc),
            )
            share.error_code = getattr(exc, "code", "TRACE_FILE_NOT_FOUND")
            share.error_message = str(exc)
            continue
        node.share_id = share.share_id
        stored = await _store_item(
            session,
            run,
            share,
            node,
            path=node.name or str(name or file_id),
            traced=True,
        )
        if stored is None:
            share.state = SHARE_STATE_BUDGET
            await session.flush()
            return
        await _handle_origin_refs(session, run, share, stored)
    share.state = SHARE_STATE_DONE
    await session.flush()


async def _enrich_missing_origins(
    session: AsyncSession,
    run: LineageRun,
    client: AnonymousPikPakClient,
    share: LineageGraphShare,
    files: list[FileNode],
    token: str,
) -> None:
    missing = [item for item in files if not item.is_folder and not item.original_share_id]
    if not missing:
        return
    if not await _consume_request(session, run):
        return
    try:
        infos = await client.batch_file_info(
            share.share_id, [item.id for item in missing[:100]], token
        )
    except Exception:  # noqa: BLE001
        return
    by_id = {item.id: item for item in infos}
    for item in missing:
        info = by_id.get(item.id)
        if info is None:
            continue
        if not item.original_share_id and info.original_share_id:
            item.params["original_share_id"] = info.original_share_id
        if not item.original_file_id and info.original_file_id:
            item.params["original_file_id"] = info.original_file_id
        item.hash = info.hash or item.hash
        item.size = info.size or item.size
        item.mime_type = info.mime_type or item.mime_type


async def _process_folder_row(
    session: AsyncSession,
    run: LineageRun,
    client: AnonymousPikPakClient,
    folder: LineageGraphFolder,
) -> None:
    folder.state = FOLDER_STATE_ENUMERATING
    await session.flush()
    share = await _get_share_row(session, run, folder.share_id)
    if share is None or share.role != "start":
        folder.state = FOLDER_STATE_DONE
        return
    snap_row = (
        await session.execute(
            select(ShareSnapshot).where(
                ShareSnapshot.run_id == run.id,
                ShareSnapshot.share_id == folder.share_id,
            )
        )
    ).scalar_one_or_none()
    token = decrypt(snap_row.pass_code_token_enc) if snap_row is not None else ""
    if not await _consume_request(session, run):
        folder.state = FOLDER_STATE_DONE
        return
    try:
        page = await client.list_folder(
            folder.share_id, folder.folder_id, token or "", folder.page_token
        )
    except Exception as exc:  # noqa: BLE001
        folder.state = FOLDER_STATE_DONE
        share.error_code = getattr(exc, "code", "FOLDER_ERROR")
        share.error_message = str(exc)[:500]
        await session.flush()
        return
    await _record_raw(
        session,
        run,
        "GET",
        f"/drive/v1/share/detail?share_id={folder.share_id}&parent_id={folder.folder_id}",
        200,
        {"files": [item.raw for item in page.files], "next_page_token": page.next_page_token},
    )
    await _enrich_missing_origins(session, run, client, share, page.files, token or "")
    for node in page.files:
        node.share_id = folder.share_id
        path = _join_path(folder.path, node.name)
        stored = await _store_item(session, run, share, node, path=path, traced=True)
        if stored is None:
            break
        if node.is_folder:
            if await _ensure_folder(session, run, share, node, path=path) is None:
                break
        else:
            await _handle_origin_refs(session, run, share, stored)
    folder.page_token = page.next_page_token or None
    folder.state = (
        FOLDER_STATE_ENUMERATING if page.next_page_token else FOLDER_STATE_DONE
    )
    await session.flush()



async def _enrich_pending_titles(
    session: AsyncSession, run: LineageRun, client: AnonymousPikPakClient
) -> int:
    updated = 0
    rows = (
        await session.execute(
            select(LineageGraphShare)
            .where(
                LineageGraphShare.run_id == run.id,
                LineageGraphShare.state.in_(
                    [SHARE_STATE_PENDING, SHARE_STATE_ENUMERATING]
                ),
                LineageGraphShare.title.is_(None),
            )
            .order_by(LineageGraphShare.depth.asc(), LineageGraphShare.id.asc())
            .limit(300)
        )
    ).scalars().all()
    for row in rows:
        if not await _consume_request(session, run):
            break
        try:
            snapshot = await client.get_share(row.share_id, None)
        except Exception:  # noqa: BLE001
            continue
        row.share_status = snapshot.share_status
        row.share_status_text = snapshot.share_status_text
        if snapshot.title:
            row.title = snapshot.title
            updated += 1
        row.user_id = snapshot.user_id or row.user_id
        row.file_num = snapshot.file_num or row.file_num
        row.anonymous_play_seconds = (
            snapshot.anonymous_play_seconds
            if snapshot.anonymous_play_seconds is not None
            else row.anonymous_play_seconds
        )
        root = snapshot.files[0] if snapshot.files else None
        if root is not None:
            row.root_kind = root.kind
            row.root_file_id = root.id
            row.root_name = root.name
        await session.flush()
        await session.commit()
    return updated


async def _mark_finished_shares(session: AsyncSession, run: LineageRun) -> None:
    rows = (
        await session.execute(
            select(LineageGraphShare).where(
                LineageGraphShare.run_id == run.id,
                LineageGraphShare.state == SHARE_STATE_ENUMERATING,
            )
        )
    ).scalars().all()
    for share in rows:
        pending = int(
            (
                await session.execute(
                    select(func.count(LineageGraphFolder.id)).where(
                        LineageGraphFolder.run_id == run.id,
                        LineageGraphFolder.share_id == share.share_id,
                        LineageGraphFolder.state.in_(
                            [FOLDER_STATE_PENDING, FOLDER_STATE_ENUMERATING]
                        ),
                    )
                )
            ).scalar_one()
        )
        if pending == 0:
            share.state = SHARE_STATE_DONE


async def _graph_done(session: AsyncSession, run: LineageRun) -> bool:
    if run.truncated:
        return True
    pending_shares = int(
        (
            await session.execute(
                select(func.count(LineageGraphShare.id)).where(
                    LineageGraphShare.run_id == run.id,
                    LineageGraphShare.state.in_(
                        [SHARE_STATE_PENDING, SHARE_STATE_ENUMERATING]
                    ),
                )
            )
        ).scalar_one()
    )
    pending_folders = int(
        (
            await session.execute(
                select(func.count(LineageGraphFolder.id)).where(
                    LineageGraphFolder.run_id == run.id,
                    LineageGraphFolder.state.in_(
                        [FOLDER_STATE_PENDING, FOLDER_STATE_ENUMERATING]
                    ),
                )
            )
        ).scalar_one()
    )
    return pending_shares == 0 and pending_folders == 0


async def _update_stats(session: AsyncSession, run: LineageRun) -> None:
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
                select(func.count(LineageGraphItem.id)).where(
                    LineageGraphItem.run_id == run.id
                )
            )
        ).scalar_one()
    )
    run.folders_done = int(
        (
            await session.execute(
                select(func.count(LineageGraphFolder.id)).where(
                    LineageGraphFolder.run_id == run.id,
                    LineageGraphFolder.state == FOLDER_STATE_DONE,
                )
            )
        ).scalar_one()
    )
    run.progress = f"shares={run.shares_probed} items={run.items_enumerated} folders={run.folders_done}"
    run.updated_at = utcnow()


async def advance_graph(
    session: AsyncSession, run: LineageRun, client: AnonymousPikPakClient
) -> None:
    if run.status == LineageStatus.QUEUED:
        run.status = LineageStatus.RESOLVING
        run.started_at = utcnow()
        await session.commit()
        return
    if run.status == LineageStatus.RESOLVING:
        start = await _get_or_create_share(
            session, run, run.share_id or "", depth=0, role="start"
        )
        if start is None:
            run.status = LineageStatus.DONE
            run.finished_at = utcnow()
            await session.commit()
            return
        await _process_share_row(session, run, client, start)
        await _mark_finished_shares(session, run)
        await _update_stats(session, run)
        if run.truncated:
            await _enrich_pending_titles(session, run, client)
        if run.truncated or await _graph_done(session, run):
            run.status = LineageStatus.DONE
            run.finished_at = utcnow()
            if run.termination is None:
                run.termination = "complete"
        else:
            run.status = LineageStatus.ENUMERATING
        await session.commit()
        return
    if run.status == LineageStatus.ENUMERATING:
        for _ in range(4):
            next_share_depth = (
                await session.execute(
                    select(func.min(LineageGraphShare.depth)).where(
                        LineageGraphShare.run_id == run.id,
                        LineageGraphShare.state == SHARE_STATE_PENDING,
                    )
                )
            ).scalar_one_or_none()
            folder_filters = [
                LineageGraphFolder.run_id == run.id,
                LineageGraphFolder.state.in_(
                    [FOLDER_STATE_PENDING, FOLDER_STATE_ENUMERATING]
                ),
            ]
            if next_share_depth is not None:
                folder_filters.append(
                    LineageGraphFolder.depth <= int(next_share_depth)
                )
            folder = (
                await session.execute(
                    select(LineageGraphFolder)
                    .where(*folder_filters)
                    .order_by(
                        LineageGraphFolder.depth.asc(),
                        LineageGraphFolder.id.asc(),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if folder is not None:
                await _process_folder_row(session, run, client, folder)
                await session.commit()
                if run.truncated:
                    break
                continue
            share = (
                await session.execute(
                    select(LineageGraphShare)
                    .where(
                        LineageGraphShare.run_id == run.id,
                        LineageGraphShare.state == SHARE_STATE_PENDING,
                    )
                    .order_by(LineageGraphShare.depth.asc(), LineageGraphShare.id.asc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if share is not None:
                await _process_share_row(session, run, client, share)
                await session.commit()
                if run.truncated:
                    break
                continue
            break
        await _mark_finished_shares(session, run)
        await _update_stats(session, run)
        if run.truncated:
            await _enrich_pending_titles(session, run, client)
        if run.truncated or await _graph_done(session, run):
            run.status = LineageStatus.DONE
            run.finished_at = utcnow()
            if run.termination is None:
                run.termination = "complete"
        await session.commit()


async def _share_counts(
    session: AsyncSession, run: LineageRun
) -> dict[str, dict[str, int]]:
    rows = (
        await session.execute(
            select(
                LineageGraphItem.share_id,
                LineageGraphItem.kind,
                func.count(LineageGraphItem.id),
            )
            .where(LineageGraphItem.run_id == run.id)
            .group_by(LineageGraphItem.share_id, LineageGraphItem.kind)
        )
    ).all()
    counts: dict[str, dict[str, int]] = {}
    for share_id, kind, count in rows:
        bucket = counts.setdefault(share_id, {"file_count": 0, "folder_count": 0})
        if kind == "drive#folder":
            bucket["folder_count"] += int(count)
        else:
            bucket["file_count"] += int(count)
    return counts


async def build_graph(session: AsyncSession, run: LineageRun) -> dict[str, Any]:
    shares = (
        await session.execute(
            select(LineageGraphShare)
            .where(LineageGraphShare.run_id == run.id)
            .order_by(LineageGraphShare.depth.asc(), LineageGraphShare.id.asc())
        )
    ).scalars().all()
    items = (
        await session.execute(
            select(LineageGraphItem)
            .where(LineageGraphItem.run_id == run.id)
            .order_by(LineageGraphItem.share_id.asc(), LineageGraphItem.id.asc())
        )
    ).scalars().all()
    counts = await _share_counts(session, run)
    by_share: dict[str, list[LineageGraphItem]] = {}
    for item in items:
        by_share.setdefault(item.share_id, []).append(item)

    edge_groups: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        origin = (item.original_share_id or "").strip()
        if not origin or origin == item.share_id:
            continue
        key = (item.share_id, origin)
        group = edge_groups.setdefault(
            key,
            {
                "files": [],
                "file_count": 0,
                "total_bytes": 0,
            },
        )
        group["file_count"] += 1
        group["total_bytes"] += int(item.size or 0)
        if len(group["files"]) < 12:
            group["files"].append(_item_payload(item))

    incoming_hint: dict[str, str] = {}
    for item in items:
        origin = (item.original_share_id or "").strip()
        if not origin or origin in incoming_hint:
            continue
        hint = item.path or item.name
        if hint:
            incoming_hint[origin] = hint

    known = {row.share_id for row in shares}
    incoming: dict[str, int] = {}
    outgoing: dict[str, int] = {}
    edges: list[dict[str, Any]] = []
    for (source, target), group in edge_groups.items():
        if target not in known:
            continue
        incoming[target] = incoming.get(target, 0) + int(group["file_count"])
        outgoing[source] = outgoing.get(source, 0) + int(group["file_count"])
        target_row = next(row for row in shares if row.share_id == target)
        edges.append(
            {
                "id": f"{share_node_id(source)}->{share_node_id(target)}",
                "from": share_node_id(source),
                "to": share_node_id(target),
                "from_share_id": source,
                "to_share_id": target,
                "file_count": group["file_count"],
                "total_bytes": group["total_bytes"],
                "files": group["files"],
                "target_status": target_row.share_status,
                "target_state": _effective_state(run, target_row),
            }
        )

    nodes: list[dict[str, Any]] = []
    for row in shares:
        bucket = counts.get(row.share_id, {"file_count": 0, "folder_count": 0})
        node = _share_payload(
            row,
            state=_effective_state(run, row),
            file_count=bucket["file_count"],
            folder_count=bucket["folder_count"],
            incoming_count=incoming.get(row.share_id, 0),
            outgoing_count=outgoing.get(row.share_id, 0),
        )
        node["source_hint"] = incoming_hint.get(row.share_id)
        node["preview"] = [_item_payload(item) for item in by_share.get(row.share_id, [])[:5]]
        nodes.append(node)

    total_files = sum(1 for item in items if item.kind != "drive#folder")
    total_folders = sum(1 for item in items if item.kind == "drive#folder")
    max_depth = max((row.depth for row in shares), default=0)
    return {
        "start": share_node_id(run.share_id or ""),
        "start_share_id": run.share_id,
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "shares": len(shares),
            "edges": len(edges),
            "files": total_files,
            "folders": total_folders,
            "max_depth": max_depth,
            "requests": run.requests,
            "truncated": run.truncated,
            "termination": run.termination,
        },
    }


async def build_share_detail(
    session: AsyncSession,
    run: LineageRun,
    share_id: str,
    *,
    offset: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    row = await _get_share_row(session, run, share_id)
    if row is None:
        raise LineageNotFound("血缘节点不存在")
    offset = max(0, offset)
    limit = max(1, min(limit, 500))
    total = int(
        (
            await session.execute(
                select(func.count(LineageGraphItem.id)).where(
                    LineageGraphItem.run_id == run.id,
                    LineageGraphItem.share_id == share_id,
                )
            )
        ).scalar_one()
    )
    items = (
        await session.execute(
            select(LineageGraphItem)
            .where(
                LineageGraphItem.run_id == run.id,
                LineageGraphItem.share_id == share_id,
            )
            .order_by(LineageGraphItem.kind.desc(), LineageGraphItem.path.asc())
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()
    counts = (await _share_counts(session, run)).get(
        share_id, {"file_count": 0, "folder_count": 0}
    )

    incoming_rows = (
        await session.execute(
            select(LineageGraphItem).where(
                LineageGraphItem.run_id == run.id,
                LineageGraphItem.original_share_id == share_id,
            )
        )
    ).scalars().all()
    outgoing_rows = (
        await session.execute(
            select(LineageGraphItem).where(
                LineageGraphItem.run_id == run.id,
                LineageGraphItem.share_id == share_id,
                LineageGraphItem.original_share_id.is_not(None),
                LineageGraphItem.original_share_id != "",
            )
        )
    ).scalars().all()
    share_rows = {
        item.share_id: item
        for item in (
            await session.execute(
                select(LineageGraphShare).where(LineageGraphShare.run_id == run.id)
            )
        ).scalars().all()
    }

    def grouped(
        rows: list[LineageGraphItem], key: str, *, file_limit: int
    ) -> list[dict[str, Any]]:
        buckets: dict[str, list[LineageGraphItem]] = {}
        for item in rows:
            value = getattr(item, key) or ""
            if not value or value == share_id:
                continue
            buckets.setdefault(value, []).append(item)
        out = []
        for other, group in sorted(buckets.items(), key=lambda pair: -len(pair[1])):
            other_row = share_rows.get(other)
            out.append(
                {
                    "share_id": other,
                    "title": (other_row.title or other_row.root_name or other) if other_row else other,
                    "status": other_row.share_status if other_row else None,
                    "state": other_row.state if other_row else None,
                    "depth": other_row.depth if other_row else None,
                    "file_count": len(group),
                    "total_bytes": sum(int(item.size or 0) for item in group),
                    "files": [_item_payload(item) for item in group[:file_limit]],
                }
            )
        return out

    node = _share_payload(
        row,
        state=_effective_state(run, row),
        file_count=counts["file_count"],
        folder_count=counts["folder_count"],
        incoming_count=len(incoming_rows),
        outgoing_count=len(outgoing_rows),
    )
    node["source_hint"] = next(
        (item.path or item.name for item in incoming_rows if item.path or item.name), None
    )
    node["preview"] = [_item_payload(item) for item in items[:5]]
    return {
        "node": node,
        "content": {
            "total": total,
            "offset": offset,
            "limit": limit,
            "next_offset": offset + len(items) if offset + len(items) < total else None,
            "items": [_item_payload(item) for item in items],
        },
        "incoming": grouped(incoming_rows, "share_id", file_limit=200),
        "outgoing": grouped(outgoing_rows, "original_share_id", file_limit=5),
    }
