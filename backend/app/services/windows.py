from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
from aiolimiter import AsyncLimiter

_CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)")


@dataclass
class WindowProbeResult:
    size: int
    no_range_status: int | None = None
    head_end: int | None = None
    tail_start: int | None = None
    tail_end: int | None = None
    full_200: bool = False
    supported: bool = True
    reason: str | None = None
    intervals: list[tuple[int, int]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


def parse_content_range(value: str | None) -> tuple[int, int, int | None] | None:
    if not value:
        return None
    match = _CONTENT_RANGE_RE.search(value)
    if not match:
        return None
    total = None if match.group(3) == "*" else int(match.group(3))
    return int(match.group(1)), int(match.group(2)), total


def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    cleaned = sorted((start, end) for start, end in intervals if end >= start)
    if not cleaned:
        return []
    merged = [cleaned[0]]
    for start, end in cleaned[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end + 1:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def complement(merged: list[tuple[int, int]], size: int) -> list[tuple[int, int]]:
    if size <= 0:
        return []
    missing: list[tuple[int, int]] = []
    cursor = 0
    for start, end in merged:
        if start > cursor:
            missing.append((cursor, start - 1))
        cursor = max(cursor, end + 1)
    if cursor <= size - 1:
        missing.append((cursor, size - 1))
    return missing


def covered_bytes(merged: list[tuple[int, int]]) -> int:
    return sum(end - start + 1 for start, end in merged)


def analyze_windows(
    size: int, intervals: list[tuple[int, int]]
) -> dict[str, Any]:
    merged = merge_intervals(intervals)
    missing = complement(merged, size)
    covered = covered_bytes(merged)
    return {
        "merged": merged,
        "missing": missing,
        "covered_bytes": covered,
        "union_ratio": (covered / size) if size else 0.0,
        "conclusion": "anonymous_full" if not missing else "anonymous_incomplete",
    }


async def _probe(
    client: httpx.AsyncClient,
    url: str,
    byte: int | None,
    *,
    no_range: bool = False,
    limiter: AsyncLimiter | None = None,
) -> tuple[int, str | None, str | None]:
    if limiter is not None:
        await limiter.acquire()
    headers = {}
    if not no_range and byte is not None:
        headers["Range"] = f"bytes={byte}-{byte}"
    async with client.stream("GET", url, headers=headers) as response:
        content_range = response.headers.get("Content-Range")
        try:
            async for _chunk in response.aiter_bytes():
                break
        except (httpx.HTTPError, httpx.StreamError):
            pass
        return response.status_code, content_range, response.headers.get("X-Xos-Err-Desc")


async def probe_window(
    url: str,
    size: int,
    *,
    client: httpx.AsyncClient | None = None,
    limiter: AsyncLimiter | None = None,
    max_binary_steps: int = 64,
) -> WindowProbeResult:
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=httpx.Timeout(60.0), follow_redirects=True)
    result = WindowProbeResult(size=size)
    try:
        if size == 0:
            result.full_200 = True
            result.intervals = []
            return result

        status, content_range, err = await _probe(client, url, None, no_range=True, limiter=limiter)
        result.no_range_status = status
        result.raw["no_range"] = {"status": status, "content_range": content_range, "err": err}
        if status == 200:
            result.full_200 = True
            result.head_end = size - 1
            result.intervals = [(0, size - 1)]
            return result
        if status != 206:
            result.supported = False
            result.reason = f"no_range_status_{status}"
            return result
        parsed = parse_content_range(content_range)
        if not parsed or (parsed[2] is not None and parsed[2] != size):
            result.supported = False
            result.reason = "bad_content_range"
            return result
        head_end = parsed[1]
        if head_end < 0 or head_end >= size:
            result.supported = False
            result.reason = "head_end_out_of_bounds"
            return result
        result.head_end = head_end
        result.intervals = [(0, head_end)]

        # Validate the prefix boundary: head_end readable, head_end+1 not.
        if head_end < size - 1:
            status_end, _cr, _err = await _probe(client, url, head_end, limiter=limiter)
            status_next, _cr2, _err2 = await _probe(client, url, head_end + 1, limiter=limiter)
            if status_end != 206:
                result.supported = False
                result.reason = "head_end_not_readable"
                return result
            if status_next == 206:
                result.supported = False
                result.reason = "head_not_closed"
                return result
            if status_next != 416:
                result.supported = False
                result.reason = f"unexpected_head_boundary_{status_next}"
                return result

            lo, hi = head_end + 1, size - 1
            tail_start: int | None = None
            steps = 0
            while lo <= hi and steps < max_binary_steps:
                steps += 1
                mid = (lo + hi) // 2
                mid_status, _cr3, _err3 = await _probe(client, url, mid, limiter=limiter)
                if mid_status == 206:
                    tail_start = mid
                    hi = mid - 1
                elif mid_status == 416:
                    lo = mid + 1
                else:
                    result.supported = False
                    result.reason = f"unexpected_tail_probe_{mid_status}"
                    return result
            if tail_start is None:
                result.supported = False
                result.reason = "no_tail_window"
                return result
            # Verify the boundary and the final byte.
            status_tail, _cr4, _err4 = await _probe(client, url, tail_start, limiter=limiter)
            status_before, _cr5, _err5 = await _probe(client, url, tail_start - 1, limiter=limiter)
            status_last, _cr6, _err6 = await _probe(client, url, size - 1, limiter=limiter)
            if status_tail != 206 or status_before != 416 or status_last != 206:
                result.supported = False
                result.reason = "tail_boundary_mismatch"
                return result
            result.tail_start = tail_start
            result.tail_end = size - 1
            result.intervals.append((tail_start, size - 1))
        return result
    finally:
        if owns_client:
            await client.aclose()


async def probe_head_only(
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
    limiter: AsyncLimiter | None = None,
) -> dict[str, Any]:
    """Cheap liveness probe: stream GET and read headers only."""
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=True)
    try:
        if limiter is not None:
            await limiter.acquire()
        async with client.stream("GET", url) as response:
            return {
                "status": response.status_code,
                "content_range": response.headers.get("Content-Range"),
                "content_length": response.headers.get("Content-Length"),
            }
    finally:
        if owns_client:
            await client.aclose()


async def probe_byte(
    url: str,
    offset: int,
    *,
    client: httpx.AsyncClient | None = None,
    limiter: AsyncLimiter | None = None,
) -> int:
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=True)
    try:
        status, _cr, _err = await _probe(client, url, offset, limiter=limiter)
        return status
    finally:
        if owns_client:
            await client.aclose()


async def probe_tail_exact(
    url: str,
    size: int,
    *,
    client: httpx.AsyncClient | None = None,
    limiter: AsyncLimiter | None = None,
) -> bool:
    """Require an exact 206 covering only the final byte."""
    if size <= 0:
        return True
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=True)
    try:
        status, content_range, _err = await _probe(client, url, size - 1, limiter=limiter)
        if status != 206:
            return False
        parsed = parse_content_range(content_range)
        if parsed is None:
            return False
        start, end, total = parsed
        return start == size - 1 and end == size - 1 and (total is None or total == size)
    finally:
        if owns_client:
            await client.aclose()


async def probe_three_points(
    url: str,
    size: int,
    *,
    client: httpx.AsyncClient | None = None,
    limiter: AsyncLimiter | None = None,
) -> bool:
    if size <= 0:
        return True
    offsets = sorted({0, size // 2, size - 1})
    for offset in offsets:
        status = await probe_byte(url, offset, client=client, limiter=limiter)
        if status != 206:
            return False
    return True


async def probe_with_retries(
    url: str,
    size: int,
    *,
    client: httpx.AsyncClient | None = None,
    limiter: AsyncLimiter | None = None,
    attempts: int = 3,
) -> bool:
    for attempt in range(attempts):
        try:
            if await probe_three_points(url, size, client=client, limiter=limiter):
                return True
        except httpx.HTTPError:
            pass
        if attempt + 1 < attempts:
            await asyncio.sleep(min(4.0, 2**attempt))
    return False
