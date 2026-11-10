from __future__ import annotations

from dataclasses import dataclass, field

from ..pikpak.client import AnonymousPikPakClient, FileNode
from ..pikpak.errors import PikPakError
from ..pikpak.urls import parse_share_input


@dataclass
class PreviewItem:
    id: str
    name: str
    kind: str
    size: int
    hash: str
    mime_type: str | None
    is_video: bool


@dataclass
class PreviewResult:
    share_id: str
    share_status: str
    share_status_text: str
    title: str
    folder_id: str | None = None
    folder_name: str | None = None
    items: list[PreviewItem] = field(default_factory=list)
    next_page_token: str = ""
    is_root: bool = True


def _item(node: FileNode) -> PreviewItem:
    return PreviewItem(
        id=node.id,
        name=node.name,
        kind=node.kind,
        size=node.size,
        hash=node.hash,
        mime_type=node.mime_type,
        is_video=node.is_video,
    )


async def _collect_folder(
    client: AnonymousPikPakClient,
    share_id: str,
    folder_id: str,
    token: str,
    page_token: str | None,
) -> tuple[list[PreviewItem], str]:
    items: list[PreviewItem] = []
    next_token = page_token
    seen: set[str] = set()
    while True:
        page = await client.list_folder(share_id, folder_id, token, next_token)
        items.extend(_item(node) for node in page.files)
        next_token = page.next_page_token or ""
        if not next_token or next_token in seen:
            break
        seen.add(next_token)
    return items, next_token


async def preview_share(
    *,
    input_url: str,
    pass_code: str | None = None,
    folder_id: str | None = None,
    folder_name: str | None = None,
    page_token: str | None = None,
    fetch_all: bool = True,
) -> PreviewResult:
    parsed = parse_share_input(input_url, pass_code)
    client = AnonymousPikPakClient()
    try:
        snapshot = await client.get_share(parsed.share_id, parsed.pass_code)
        result = PreviewResult(
            share_id=parsed.share_id,
            share_status=snapshot.share_status,
            share_status_text=snapshot.share_status_text,
            title=snapshot.title,
        )
        if not snapshot.ok:
            return result
        token = snapshot.pass_code_token
        if folder_id:
            if fetch_all:
                items, next_token = await _collect_folder(
                    client, parsed.share_id, folder_id, token, page_token
                )
                result.items = items
                result.next_page_token = next_token
            else:
                page = await client.list_folder(
                    parsed.share_id, folder_id, token, page_token
                )
                result.items = [_item(node) for node in page.files]
                result.next_page_token = page.next_page_token
            result.folder_id = folder_id
            result.folder_name = folder_name
            result.is_root = False
            return result
        if not snapshot.files:
            return result
        root = snapshot.files[0]
        if root.is_folder:
            if fetch_all:
                items, next_token = await _collect_folder(
                    client, parsed.share_id, root.id, token, page_token
                )
                result.items = items
                result.next_page_token = next_token
            else:
                page = await client.list_folder(
                    parsed.share_id, root.id, token, page_token
                )
                result.items = [_item(node) for node in page.files]
                result.next_page_token = page.next_page_token
            result.folder_id = root.id
            result.folder_name = root.name
            result.is_root = True
            return result
        result.items = [_item(node) for node in snapshot.files]
        result.is_root = True
        return result
    except PikPakError:
        raise
    finally:
        await client.close()
