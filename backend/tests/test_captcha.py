from __future__ import annotations

from app.pikpak.captcha import drive_sign


def test_drive_sign_vector() -> None:
    assert (
        drive_sign(
            "YUMx5nI8ZU8Ap8pm",
            "9dcd3fcc0dfc87f4c294a3f37b74442f",
            "1793961611384",
        )
        == "1.ff4cafbcc78c20565e77efefa6921393"
    )
