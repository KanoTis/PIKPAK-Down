from __future__ import annotations

import asyncio

from aiolimiter import AsyncLimiter

from ..config import get_settings

_anon: AsyncLimiter | None = None
_share: AsyncLimiter | None = None
_window: AsyncLimiter | None = None
_lock = asyncio.Lock()


def _build() -> None:
    global _anon, _share, _window
    settings = get_settings()
    _anon = AsyncLimiter(max_rate=max(settings.anon_api_qps, 0.1), time_period=1)
    _share = AsyncLimiter(max_rate=max(settings.share_probe_qps, 0.1), time_period=1)
    _window = AsyncLimiter(max_rate=max(settings.window_probe_qps, 0.1), time_period=1)


async def anon_limiter() -> AsyncLimiter:
    async with _lock:
        if _anon is None:
            _build()
        assert _anon is not None
        return _anon


async def share_limiter() -> AsyncLimiter:
    async with _lock:
        if _share is None:
            _build()
        assert _share is not None
        return _share


async def window_limiter() -> AsyncLimiter:
    async with _lock:
        if _window is None:
            _build()
        assert _window is not None
        return _window


def reset_limiters() -> None:
    global _anon, _share, _window
    _anon = _share = _window = None
