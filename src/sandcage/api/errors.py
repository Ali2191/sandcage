"""API errors: each carries its HTTP status and a message fit to show a visitor (never a
host path, a key or the access code)."""

from __future__ import annotations


class ApiError(Exception):
    status = 500

    def __init__(self, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after


class BadRequestError(ApiError):
    status = 400


class NotFoundError(ApiError):
    status = 404


class ConflictError(ApiError):
    """The request does not fit the run's state (nothing pending, not acting, busy...)."""

    status = 409


class LockedError(ApiError):
    """The patch is withheld until a human releases it."""

    status = 423


class InvalidError(ApiError):
    status = 422


class UnavailableError(ApiError):
    status = 503
