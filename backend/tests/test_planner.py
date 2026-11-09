from __future__ import annotations

from app.pikpak.client import FileNode
from app.services.planner import anonymous_plan, url_expire


def _node(**overrides) -> FileNode:
    data = {
        "id": "file-1",
        "name": "a.jpg",
        "kind": "drive#file",
        "size": "1024",
        "hash": "ABC",
        "mime_type": "image/jpeg",
        "web_content_link": "https://cdn.example/x?expire=1700000000&sign=secret",
        "params": {},
        "medias": [],
    }
    data.update(overrides)
    return FileNode.from_json(data, share_id="VPshare0000000000001")


def test_non_video_uses_anonymous_wcl() -> None:
    plan = anonymous_plan(_node())
    assert plan is not None
    assert plan.route == "anonymous"
    assert plan.url.startswith("https://cdn.example/x")
    assert plan.url_expire == 1_700_000_000


def test_video_range_below_one_needs_account() -> None:
    node = _node(
        mime_type="video/mp4",
        size="1000",
        web_content_link=None,
        params={"anonymous_play_range": "0.40"},
        medias=[
            {
                "is_origin": True,
                "category": "category_origin",
                "link": {"url": "https://cdn.example/v?t=0&f=1000&expire=123"},
            }
        ],
    )
    assert anonymous_plan(node) is None


def test_video_range_one_uses_original_media() -> None:
    node = _node(
        mime_type="video/mp4",
        size="1000",
        web_content_link=None,
        params={"anonymous_play_range": "1.00"},
        medias=[
            {
                "is_origin": True,
                "category": "category_origin",
                "link": {"url": "https://cdn.example/v?t=0&f=1000&expire=456"},
            },
            {
                "is_origin": False,
                "category": "category_transcode",
                "link": {"url": "https://cdn.example/v?t=1&f=200&expire=789"},
            },
        ],
    )
    plan = anonymous_plan(node)
    assert plan is not None
    assert "t=0" in plan.url
    assert plan.url_expire == 456


def test_video_missing_original_media_needs_account() -> None:
    node = _node(
        mime_type="video/mp4",
        size="1000",
        web_content_link=None,
        params={"anonymous_play_range": "1.00"},
        medias=[],
    )
    assert anonymous_plan(node) is None


def test_url_expire_parsing() -> None:
    assert url_expire("https://x/y?expire=42&sign=z") == 42
    assert url_expire("https://x/y") is None
    assert url_expire(None) is None
