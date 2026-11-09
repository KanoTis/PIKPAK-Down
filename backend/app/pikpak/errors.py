from __future__ import annotations

from typing import Any


class PikPakError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        reason: str | None = None,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.reason = reason or message
        self.retryable = retryable
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code or "PIKPAK_ERROR",
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }


class RateLimited(PikPakError):
    def __init__(self, message: str = "operation too frequent", **kwargs: Any) -> None:
        kwargs.setdefault("code", "RATE_LIMITED")
        kwargs.setdefault("retryable", True)
        super().__init__(message, **kwargs)


class NotFound(PikPakError):
    def __init__(self, message: str = "file not found", **kwargs: Any) -> None:
        kwargs.setdefault("code", "NOT_FOUND")
        kwargs.setdefault("retryable", False)
        super().__init__(message, **kwargs)


class Unauthorized(PikPakError):
    def __init__(self, message: str = "unauthenticated", **kwargs: Any) -> None:
        kwargs.setdefault("code", "UNAUTHORIZED")
        kwargs.setdefault("retryable", False)
        super().__init__(message, **kwargs)


class PermissionDenied(PikPakError):
    def __init__(self, message: str = "permission denied", **kwargs: Any) -> None:
        kwargs.setdefault("code", "PERMISSION_DENIED")
        kwargs.setdefault("retryable", False)
        super().__init__(message, **kwargs)


class ShareUnavailable(PikPakError):
    def __init__(self, status: str, message: str = "", **kwargs: Any) -> None:
        kwargs.setdefault("code", f"SHARE_{status}")
        kwargs.setdefault("retryable", False)
        super().__init__(message or f"share status {status}", **kwargs)
        self.share_status = status


class CaptchaFailed(PikPakError):
    def __init__(self, message: str = "captcha rejected", **kwargs: Any) -> None:
        kwargs.setdefault("code", "CAPTCHA_FAILED")
        kwargs.setdefault("retryable", False)
        super().__init__(message, **kwargs)
