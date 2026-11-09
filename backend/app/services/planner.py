from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from ..pikpak.client import FileNode


@dataclass
class DownloadPlan:
    route: str
    url: str | None
    url_expire: int | None
    source_share_id: str
    source_file_id: str


def url_expire(url: str | None) -> int | None:
    if not url:
        return None
    query = parse_qs(urlparse(url).query)
    for key in ("expire", "expires"):
        if query.get(key):
            try:
                return int(query[key][0])
            except ValueError:
                return None
    return None


def original_media_url(node: FileNode) -> str | None:
    return node.original_media_url()


def anonymous_plan(node: FileNode, *, share_id: str | None = None) -> DownloadPlan | None:
    """Return an anonymous plan only when the object is fully readable anonymously."""
    if node.is_folder:
        return None
    if node.size == 0:
        return DownloadPlan("anonymous", None, None, share_id or node.share_id or "", node.id)
    if node.is_video:
        media = node.original_media()
        if not media:
            return None
        play_range = node.anonymous_play_range
        if play_range is None or play_range < 1.0:
            return None
        url = str((media.get("link") or {}).get("url") or "")
        if not url:
            return None
        return DownloadPlan(
            "anonymous", url, url_expire(url), share_id or node.share_id or "", node.id
        )
    if node.web_content_link:
        return DownloadPlan(
            "anonymous",
            node.web_content_link,
            url_expire(node.web_content_link),
            share_id or node.share_id or "",
            node.id,
        )
    return None


def account_plan(node: FileNode, *, share_id: str | None = None) -> DownloadPlan:
    return DownloadPlan(
        "account", None, None, share_id or node.share_id or "", node.id
    )


def needs_account(node: FileNode) -> bool:
    return anonymous_plan(node) is None
