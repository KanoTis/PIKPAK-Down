from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

CHUNK_SIZES = (256 * 1024, 512 * 1024, 1024 * 1024, 2 * 1024 * 1024, 4 * 1024 * 1024)
READ_BUFFER = 4 * 1024 * 1024


@dataclass
class VerifyResult:
    ok: bool
    chunk: int | None
    roots: dict[int, str]


def merkle_roots(path: str | Path, sizes: tuple[int, ...] = CHUNK_SIZES) -> dict[int, str]:
    roots = {size: hashlib.sha1() for size in sizes}
    carry: dict[int, bytes] = {size: b"" for size in sizes}
    with open(path, "rb") as handle:
        while True:
            block = handle.read(READ_BUFFER)
            if not block:
                break
            for size in sizes:
                data = carry[size] + block
                offset = 0
                limit = len(data) - (len(data) % size)
                while offset < limit:
                    roots[size].update(hashlib.sha1(data[offset : offset + size]).digest())
                    offset += size
                carry[size] = data[offset:]
    for size in sizes:
        if carry[size]:
            roots[size].update(hashlib.sha1(carry[size]).digest())
    return {size: digest.hexdigest().upper() for size, digest in roots.items()}


def verify_file(path: str | Path, size: int, expected_hash: str) -> VerifyResult:
    file_path = Path(path)
    if not file_path.is_file():
        return VerifyResult(False, None, {})
    if file_path.stat().st_size != size:
        return VerifyResult(False, None, {})
    expected = expected_hash.strip().upper()
    roots = merkle_roots(file_path)
    for chunk, digest in roots.items():
        if digest == expected:
            return VerifyResult(True, chunk, roots)
    return VerifyResult(False, None, roots)


def extract_tar_member(
    archive: str | Path,
    output: str | Path,
    *,
    expected_size: int,
    expected_name: str | None = None,
) -> bool:
    """Extract the first regular tar member matching size/name."""
    import shutil
    import tarfile

    try:
        with tarfile.open(archive, mode="r:") as tar:
            candidates = [
                member
                for member in tar.getmembers()
                if member.isfile() and member.size == expected_size
            ]
            if not candidates:
                return False
            member = candidates[0]
            if expected_name and len(candidates) > 1:
                named = [
                    item
                    for item in candidates
                    if item.name.rsplit("/", 1)[-1] == expected_name
                ]
                if named:
                    member = named[0]
            source = tar.extractfile(member)
            if source is None:
                return False
            with open(output, "wb") as target:
                shutil.copyfileobj(source, target, length=1 << 20)
        return Path(output).stat().st_size == expected_size
    except (tarfile.TarError, OSError):
        return False
