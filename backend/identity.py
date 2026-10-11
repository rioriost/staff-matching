"""PoC account/password/passkey HTTP boundary; production must select another provider.

Integration: initialize IdentityStore with the restricted runtime connection in
lifespan, install AccountAuthMiddleware, and include create_identity_router().
Only an administrator applies db/identity/001_identity.sql or provisions accounts.
"""

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.identity_store import Identity, IdentityStore
from backend.store import StoreError

__all__ = [
    "AccountAuthMiddleware",
    "AuthSettings",
    "Identity",
    "IdentityStore",
    "create_identity_router",
]

SESSION_COOKIE = "__Host-staff-session"
FLOW_COOKIE = "__Host-staff-passkey-flow"
MAX_AUTH_BODY = 32768
MAX_SAAS_BODY = 262144
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
PUBLIC_POSTS = {
    "/api/auth/login",
    "/api/auth/passkeys/authentication/options",
    "/api/auth/passkeys/authentication/verify",
}


@dataclass(frozen=True)
class AuthSettings:
    origin: str
    rp_id: str
    rp_name: str = "Staff matching PoC"

    def __post_init__(self) -> None:
        try:
            parsed = urlsplit(self.origin)
            valid = (
                parsed.scheme == "https"
                and parsed.hostname
                and self.origin.isascii()
                and self.origin == self.origin.strip()
                and not parsed.username
                and not parsed.password
                and not parsed.path
                and not parsed.query
                and not parsed.fragment
                and self.origin == f"https://{parsed.netloc}"
                and parsed.netloc == parsed.netloc.lower()
                and re.fullmatch(r"[a-z0-9.-]+(?::[0-9]+)?", parsed.netloc)
                and not parsed.hostname.endswith(".")
                and all(
                    re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                    for label in parsed.hostname.split(".")
                )
                and parsed.port != 0
                and self.rp_id == parsed.hostname
                and 1 <= len(self.rp_name) <= 160
            )
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise RuntimeError(
                "POC_AUTH_ORIGIN must be an exact HTTPS origin without a path; "
                "POC_AUTH_RP_ID must equal its hostname."
            )

    @classmethod
    def from_environment(cls) -> "AuthSettings":
        origin = os.getenv("POC_AUTH_ORIGIN", "")
        try:
            hostname = urlsplit(origin).hostname or ""
        except ValueError:
            hostname = ""
        return cls(
            origin=origin,
            rp_id=os.getenv("POC_AUTH_RP_ID", hostname),
            rp_name=os.getenv("POC_AUTH_RP_NAME", "Staff matching PoC"),
        )


def _error_response(status: int, code: str, message: str) -> JSONResponse:
    headers = {"Retry-After": "900"} if status == 429 else None
    if code == "authentication_busy":
        headers = {"Retry-After": "1"}
    return JSONResponse(
        {"detail": {"code": code, "message": message}},
        status_code=status,
        headers=headers,
    )


class AccountAuthMiddleware:
    def __init__(
        self, app: ASGIApp, store: Callable[[], IdentityStore | None], settings: AuthSettings
    ):
        self.app, self.store, self.settings = app, store, settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def secure_response(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "same-origin"
                headers["Strict-Transport-Security"] = "max-age=31536000"
                headers["Permissions-Policy"] = (
                    "publickey-credentials-get=(self), publickey-credentials-create=(self)"
                )
                headers["Content-Security-Policy"] = (
                    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                    "base-uri 'self'; form-action 'self'"
                )
            await send(message)

        async def reject(status: int, code: str, message: str) -> None:
            await _error_response(status, code, message)(scope, receive, secure_response)

        request = Request(scope)
        path, method = request.url.path, request.method
        origins = request.headers.getlist("origin")
        if (origins and origins != [self.settings.origin]) or request.headers.get(
            "sec-fetch-site"
        ) in {"same-site", "cross-site"}:
            await reject(403, "cross_origin_denied", "Cross-origin access is not permitted.")
            return
        if method not in SAFE_METHODS and (
            origins != [self.settings.origin]
            or request.headers.getlist("x-requested-with") != ["staff-matching"]
        ):
            await reject(403, "csrf_rejected", "A same-origin application request is required.")
            return

        public_shell = method in {"GET", "HEAD"} and (
            path in {"/", "/healthz"} or path.startswith("/assets/")
        )
        public = (
            public_shell
            or (method == "GET" and path == "/api/auth/session")
            or (method == "POST" and path in PUBLIC_POSTS)
        )
        active_store = self.store()
        if active_store is None and not public_shell:
            await reject(503, "unavailable", "Authentication is not ready.")
            return
        identity = None
        token = request.cookies.get(SESSION_COOKIE)
        if active_store is not None and token and not public_shell:
            try:
                identity = await run_in_threadpool(active_store.resolve_session, token)
            except StoreError as error:
                await reject(error.status, error.code, error.message)
                return
        scope.setdefault("state", {})["identity"] = identity
        if not public and identity is None:
            await reject(401, "authentication_required", "Sign in to continue.")
            return
        own_identity = path.startswith("/api/auth/")
        if identity and not own_identity:
            tenants = request.headers.getlist("x-tenant-id")
            if tenants and tenants != [identity.tenant_id]:
                await reject(409, "tenant_changed", "The active tenant changed. Reload this view.")
                return
            if identity.role == "viewer" and method not in SAFE_METHODS:
                await reject(403, "read_only", "This membership is read-only.")
                return

        bounded_body = own_identity or path.startswith("/api/saas/")
        if bounded_body and method not in {"GET", "HEAD"}:
            # Count actual ASGI bytes, including chunked bodies, before JSON parsing.
            body = bytearray()
            while True:
                chunk = await receive()
                if chunk["type"] == "http.disconnect":
                    return
                body.extend(chunk.get("body", b""))
                if len(body) > (MAX_AUTH_BODY if own_identity else MAX_SAAS_BODY):
                    await reject(
                        413, "request_too_large", "The request body is too large."
                    )
                    return
                if not chunk.get("more_body", False):
                    break
            delivered = False

            async def bounded_receive() -> Message:
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            await self.app(scope, bounded_receive, secure_response)
        else:
            await self.app(scope, receive, secure_response)


class AuthRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


Password = Annotated[str, Field(min_length=1, max_length=128)]
Base64Data = Annotated[
    str, Field(min_length=1, max_length=16384, pattern=r"^[A-Za-z0-9_-]+={0,2}$")
]
CredentialID = Annotated[str, Field(min_length=1, max_length=1400, pattern=r"^[A-Za-z0-9_-]+$")]


class LoginRequest(AuthRequest):
    username: str = Field(min_length=1, max_length=128)
    password: Password

    @field_validator("username")
    @classmethod
    def username_characters(cls, value: str) -> str:
        if not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Invalid username")
        return value


class TenantRequest(AuthRequest):
    tenant_id: str = Field(min_length=1, max_length=128, pattern=r"^[^\x00-\x1f\x7f]+$")


class PasswordRequest(AuthRequest):
    current_password: Password
    new_password: str = Field(min_length=15, max_length=128)


class ReauthRequest(AuthRequest):
    password: Password


class RegistrationResponse(AuthRequest):
    clientDataJSON: Base64Data
    attestationObject: Base64Data
    transports: list[Literal["usb", "nfc", "ble", "smart-card", "hybrid", "internal"]] = Field(
        default_factory=list,
        max_length=6,
    )
    publicKey: Base64Data | None = None
    publicKeyAlgorithm: int | None = None
    authenticatorData: Base64Data | None = None


class AuthenticationResponse(AuthRequest):
    clientDataJSON: Base64Data
    authenticatorData: Base64Data
    signature: Base64Data
    userHandle: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]


class Credential(AuthRequest):
    id: CredentialID
    rawId: CredentialID
    type: Literal["public-key"]
    authenticatorAttachment: Literal["platform", "cross-platform"] | None = None
    clientExtensionResults: dict[str, Any] = Field(default_factory=dict)


class RegistrationCredential(Credential):
    response: RegistrationResponse


class AuthenticationCredential(Credential):
    response: AuthenticationResponse


class RegistrationRequest(AuthRequest):
    credential: RegistrationCredential
    name: str = Field(min_length=1, max_length=80, pattern=r"^[^\x00-\x1f\x7f]+$")


class AuthenticationRequest(AuthRequest):
    credential: AuthenticationCredential


def _identity(request: Request) -> Identity:
    identity = getattr(request.state, "identity", None)
    if identity is None:
        raise StoreError(401, "authentication_required", "Sign in to continue.")
    return identity


def _source(request: Request) -> str:
    # Do not parse attacker-supplied X-Forwarded-For; trusted proxy handling belongs to ASGI.
    return request.client.host if request.client else "unknown"


def _set_cookie(response: Response, key: str, value: str, max_age: int) -> None:
    response.set_cookie(
        key,
        value,
        max_age=max_age,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def _clear_cookie(response: Response, key: str) -> None:
    response.delete_cookie(key, path="/", secure=True, httponly=True, samesite="lax")


def create_identity_router(store: Callable[[], IdentityStore], settings: AuthSettings) -> APIRouter:
    class IdentityRoute(APIRoute):
        def get_route_handler(self):
            handler = super().get_route_handler()

            async def safe_handler(request: Request):
                try:
                    return await handler(request)
                except RequestValidationError:
                    # Never include Pydantic's input values (passwords/credentials) in errors.
                    if request.url.path.startswith("/api/auth/passkeys/authentication/"):
                        try:
                            await run_in_threadpool(
                                store()._rate_limit,
                                "anonymous",
                                _source(request),
                                120,
                            )
                        except StoreError as error:
                            return _error_response(error.status, error.code, error.message)
                        except HTTPException as error:
                            return _error_response(
                                error.status_code, "invalid_request", "The request is invalid."
                            )
                    if request.url.path.endswith("/verify"):
                        identity = getattr(request.state, "identity", None)
                        try:
                            if request.url.path == "/api/auth/passkeys/registration/verify":
                                if identity:
                                    await run_in_threadpool(
                                        store()._consume_challenge,
                                        identity=identity,
                                    )
                            else:
                                await run_in_threadpool(
                                    store()._consume_challenge,
                                    flow_token=request.cookies.get(FLOW_COOKIE),
                                )
                        except StoreError as error:
                            if error.code != "invalid_passkey":
                                raise
                    return _error_response(422, "invalid_request", "The request is invalid.")
                except StoreError as error:
                    return _error_response(error.status, error.code, error.message)

            return safe_handler

    router = APIRouter(prefix="/api/auth", route_class=IdentityRoute)

    def session_response(result: tuple[Identity, str], response: Response) -> dict:
        identity, token = result
        dto = store().session_dto(identity)
        _set_cookie(response, SESSION_COOKIE, token, 28800)
        _clear_cookie(response, FLOW_COOKIE)
        return dto

    @router.get("/session")
    def session(request: Request):
        return store().session_dto(getattr(request.state, "identity", None))

    @router.post("/login")
    def login(body: LoginRequest, request: Request, response: Response):
        return session_response(
            store().login(
                body.username,
                body.password,
                _source(request),
                old_token=request.cookies.get(SESSION_COOKIE),
            ),
            response,
        )

    @router.post("/logout")
    def logout(body: AuthRequest, request: Request, response: Response):
        store().logout(_identity(request))
        _clear_cookie(response, SESSION_COOKIE)
        _clear_cookie(response, FLOW_COOKIE)
        return {"ok": True}

    @router.post("/tenant")
    def tenant(body: TenantRequest, request: Request, response: Response):
        return session_response(
            store().change_tenant(_identity(request), body.tenant_id),
            response,
        )

    @router.post("/password")
    def password(body: PasswordRequest, request: Request, response: Response):
        return session_response(
            store().change_password(
                _identity(request),
                body.current_password,
                body.new_password,
                _source(request),
            ),
            response,
        )

    @router.post("/reauth")
    def reauth(body: ReauthRequest, request: Request):
        store().reauthenticate(_identity(request), body.password, _source(request))
        return {"ok": True}

    @router.get("/passkeys")
    def passkeys(request: Request):
        return {"items": store().list_passkeys(_identity(request))}

    @router.post("/passkeys/registration/options")
    def registration_options(body: AuthRequest, request: Request):
        return store().registration_options(_identity(request), settings)

    @router.post("/passkeys/registration/verify")
    def registration_verify(body: RegistrationRequest, request: Request):
        store().registration_verify(
            _identity(request),
            body.credential.model_dump(exclude_none=True),
            body.name,
            settings,
        )
        return {"ok": True}

    @router.post("/passkeys/authentication/options")
    def authentication_options(body: AuthRequest, request: Request, response: Response):
        options, flow = store().authentication_options(
            _source(request),
            settings,
            old_flow_token=request.cookies.get(FLOW_COOKIE),
        )
        _set_cookie(response, FLOW_COOKIE, flow, 300)
        return options

    @router.post("/passkeys/authentication/verify")
    def authentication_verify(body: AuthenticationRequest, request: Request, response: Response):
        return session_response(
            store().authentication_verify(
                body.credential.model_dump(exclude_none=True),
                request.cookies.get(FLOW_COOKIE),
                _source(request),
                settings,
                old_token=request.cookies.get(SESSION_COOKIE),
            ),
            response,
        )

    @router.delete("/passkeys/{credential_id}")
    def remove_passkey(credential_id: str, request: Request):
        store().delete_passkey(_identity(request), credential_id)
        return {"ok": True}

    return router
