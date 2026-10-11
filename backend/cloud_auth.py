"""Shared-coordinator PoC authentication, not individual staff identity verification."""

import base64
import binascii
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


@dataclass(frozen=True, repr=False)
class CoordinatorCredentials:
    username: bytes
    password: bytes

    @classmethod
    def from_environment(cls) -> "CoordinatorCredentials":
        username = os.getenv("POC_AUTH_USERNAME", "")
        password = os.getenv("POC_AUTH_PASSWORD", "")
        if not username.strip() or ":" in username or any(ord(char) < 32 for char in username):
            raise RuntimeError("Azure PoC requires a valid POC_AUTH_USERNAME.")
        if len(password) < 32 or not password.strip():
            raise RuntimeError(
                "Azure PoC requires a high-entropy POC_AUTH_PASSWORD (32+ characters)."
            )
        return cls(username.encode("utf-8"), password.encode("utf-8"))

    def accepts(self, authorization: str) -> bool:
        scheme, _, encoded = authorization.partition(" ")
        if scheme.lower() != "basic" or len(encoded) > 8192:
            return False
        try:
            username, separator, password = base64.b64decode(encoded, validate=True).partition(b":")
        except (ValueError, binascii.Error):
            return False
        username_matches = secrets.compare_digest(username, self.username)
        password_matches = secrets.compare_digest(password, self.password)
        return bool(separator) & username_matches & password_matches


def _origin(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            return None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return parsed.scheme, parsed.hostname, port
    except ValueError:
        return None


class CloudAccessMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        credentials: Callable[[], CoordinatorCredentials | None],
    ):
        self.app = app
        self.credentials = credentials

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        if request.url.path == "/healthz" and request.method in ("GET", "HEAD"):
            await self.app(scope, receive, send)
            return

        async def reject(status: int, code: str, message: str, *, challenge: bool = False) -> None:
            headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
            if challenge:
                headers["WWW-Authenticate"] = 'Basic realm="Staff matching PoC", charset="UTF-8"'
            response = JSONResponse(
                {"detail": {"code": code, "message": message}},
                status_code=status,
                headers=headers,
            )
            await response(scope, receive, send)

        expected = self.credentials()
        if expected is None:
            await reject(503, "unavailable", "Application is not ready.")
            return
        origin = request.headers.get("origin")
        current_origin = _origin(f"{request.url.scheme}://{request.url.netloc}")
        if (
            (origin is not None and (current_origin is None or _origin(origin) != current_origin))
            or request.headers.get("sec-fetch-site") in ("cross-site", "same-site")
        ):
            await reject(403, "cross_origin_denied", "Cross-origin access is not permitted.")
            return
        if not expected.accepts(request.headers.get("authorization", "")):
            await reject(
                401,
                "authentication_required",
                "Coordinator authentication required.",
                challenge=True,
            )
            return
        if request.method not in ("GET", "HEAD", "OPTIONS") and (
            origin is None or request.headers.get("x-requested-with") != "staff-matching"
        ):
            await reject(403, "csrf_rejected", "A same-origin application request is required.")
            return

        async def private_response(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "same-origin"
            await send(message)

        await self.app(scope, receive, private_response)
