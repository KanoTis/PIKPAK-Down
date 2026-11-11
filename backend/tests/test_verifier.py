from __future__ import annotations

import hashlib

from app.services.verifier import verify_file


def test_merkle_vectors(tmp_path) -> None:
    data = bytes(range(256)) * 4097
    path = tmp_path / "sample.bin"
    path.write_bytes(data)
    assert len(data) == 1_048_832
    assert hashlib.sha1(data).hexdigest().upper() != "26BFA5D48027D096B078D5BCD95C4557306612CC"

    result = verify_file(path, len(data), "26BFA5D48027D096B078D5BCD95C4557306612CC")
    assert result.ok and result.chunk == 256 * 1024

    result = verify_file(path, len(data), "1FC4E8AF979048C723126F174F61831AF31D2332")
    assert result.ok and result.chunk == 512 * 1024

    result = verify_file(path, len(data), "934160125014E735B50E0D2208797FD7563DC9F8")
    assert result.ok and result.chunk == 1024 * 1024


def test_merkle_tamper_fails(tmp_path) -> None:
    data = bytearray(bytes(range(256)) * 4097)
    path = tmp_path / "bad.bin"
    data[500_000] ^= 0xFF
    path.write_bytes(bytes(data))
    assert not verify_file(path, len(data), "26BFA5D48027D096B078D5BCD95C4557306612CC").ok


def test_size_mismatch_fails(tmp_path) -> None:
    data = bytes(range(256)) * 4097
    path = tmp_path / "short.bin"
    path.write_bytes(data[:-1])
    assert not verify_file(path, len(data), "26BFA5D48027D096B078D5BCD95C4557306612CC").ok


def test_missing_file_fails(tmp_path) -> None:
    assert not verify_file(tmp_path / "nope.bin", 1, "AA").ok


def test_extract_tar_member_by_size_and_name(tmp_path):
    import io
    import tarfile

    from app.services.verifier import extract_tar_member

    payload = b"x" * 2048
    archive = tmp_path / "data.tar"
    with tarfile.open(archive, "w") as tar:
        info = tarfile.TarInfo("video.mp4")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
        other = tarfile.TarInfo("other.bin")
        other.size = 16
        tar.addfile(other, io.BytesIO(b"y" * 16))

    output = tmp_path / "out.mp4"
    assert extract_tar_member(
        archive, output, expected_size=len(payload), expected_name="video.mp4"
    )
    assert output.read_bytes() == payload
    assert not extract_tar_member(archive, tmp_path / "missing", expected_size=99)
