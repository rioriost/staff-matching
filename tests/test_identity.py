"""HTTP boundaries and real WebAuthn cryptography, with explicitly mocked persistence."""

import base64
import hashlib
import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from threading import Event
from uuid import UUID, uuid4

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend import identity_store
from backend.identity import (
    FLOW_COOKIE,
    SESSION_COOKIE,
    AccountAuthMiddleware,
    AuthSettings,
    Identity,
    IdentityStore,
    create_identity_router,
)
from backend.identity_store import (
    PASSWORD_HASHER,
    _digest,
    _hash_password,
    _password_matches,
    validate_new_password,
)
from backend.store import StoreError

SETTINGS = AuthSettings("https://app.example.test", "app.example.test")
CSRF = {"Origin": SETTINGS.origin, "X-Requested-With": "staff-matching"}
PASSWORD = "spaces are allowed here  "
USER_ID = uuid4()
IDENTITY = Identity(
    str(USER_ID),
    "coordinator",
    "Test account",
    "tenant-a",
    "coordinator",
    str(uuid4()),
    datetime.now(UTC),
    "password",
)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


class Authenticator:
    """Synthetic ES256 authenticator, not a claim of real iPad hardware verification."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = secrets.token_bytes(32)
        numbers = self.key.public_key().public_numbers()
        self.cose_key = cbor2.dumps(
            {
                1: 2,
                3: -7,
                -1: 1,
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )

    def client_data(self, challenge: bytes, ceremony: str, origin: str, cross_origin=False):
        return json.dumps(
            {
                "type": ceremony,
                "challenge": b64(challenge),
                "origin": origin,
                "crossOrigin": cross_origin,
            }
        ).encode()

    def registration(
        self,
        challenge: bytes,
        *,
        origin=SETTINGS.origin,
        rp_id=SETTINGS.rp_id,
        flags=0x5D,
        count=0,
        cross_origin=False,
    ):
        auth_data = (
            hashlib.sha256(rp_id.encode()).digest()
            + bytes([flags])
            + count.to_bytes(4, "big")
            + bytes(16)
            + len(self.credential_id).to_bytes(2, "big")
            + self.credential_id
            + self.cose_key
        )
        return {
            "id": b64(self.credential_id),
            "rawId": b64(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64(
                    self.client_data(
                        challenge,
                        "webauthn.create",
                        origin,
                        cross_origin,
                    )
                ),
                "attestationObject": b64(
                    cbor2.dumps(
                        {
                            "fmt": "none",
                            "attStmt": {},
                            "authData": auth_data,
                        }
                    )
                ),
                "transports": ["internal", "hybrid"],
            },
            "clientExtensionResults": {},
            "authenticatorAttachment": "platform",
        }

    def assertion(
        self,
        challenge: bytes,
        user_id=USER_ID,
        *,
        origin=SETTINGS.origin,
        rp_id=SETTINGS.rp_id,
        flags=0x1D,
        count=0,
        cross_origin=False,
    ):
        client_data = self.client_data(challenge, "webauthn.get", origin, cross_origin)
        auth_data = (
            hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + count.to_bytes(4, "big")
        )
        signature = self.key.sign(
            auth_data + hashlib.sha256(client_data).digest(),
            ec.ECDSA(hashes.SHA256()),
        )
        return {
            "id": b64(self.credential_id),
            "rawId": b64(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64(client_data),
                "authenticatorData": b64(auth_data),
                "signature": b64(signature),
                "userHandle": b64(UUID(str(user_id)).bytes),
            },
            "clientExtensionResults": {},
            "authenticatorAttachment": "platform",
        }


class HTTPStore:
    """HTTP tests deliberately isolate persistence; live tests cover PostgreSQL itself."""

    def __init__(self):
        self.identity = IDENTITY
        self.token = secrets.token_urlsafe(32)
        self.sessions = {self.token: self.identity}
        self.calls = []
        self.consumed = 0

    def resolve_session(self, token):
        return self.sessions.get(token)

    def session_dto(self, identity):
        if identity is None:
            return {"enabled": True, "authenticated": False}
        tenant = {"id": identity.tenant_id, "name": "Demo tenant", "role": identity.role}
        return {
            "enabled": True,
            "authenticated": True,
            "user": {
                "id": identity.user_id,
                "username": identity.username,
                "display_name": identity.display_name,
            },
            "tenants": [tenant],
            "tenant": tenant,
            "auth_method": identity.auth_method,
            "passkey_count": 0,
        }

    def login(self, username, password, source, *, old_token=None):
        self.calls.append(("login", username, source))
        if username != self.identity.username or password != PASSWORD:
            raise StoreError(401, "invalid_credentials", "The credentials could not be verified.")
        if old_token:
            self.sessions.pop(old_token, None)
        token = secrets.token_urlsafe(32)
        self.sessions[token] = self.identity
        return self.identity, token

    def logout(self, identity):
        self.sessions = {k: v for k, v in self.sessions.items() if v != identity}

    def list_passkeys(self, identity):
        return []

    def reauthenticate(self, identity, password, source):
        self.calls.append(("reauth", identity.user_id, source))

    def registration_options(self, identity, settings):
        raise StoreError(403, "reauthentication_required", "Verify your password.")

    def authentication_options(self, source, settings, *, old_flow_token=None):
        return {"challenge": b64(secrets.token_bytes(32)), "rpId": settings.rp_id}, (
            secrets.token_urlsafe(32)
        )

    def _consume_challenge(self, **kwargs):
        self.consumed += 1

    def _rate_limit(self, kind, value, maximum):
        pass


def make_app(store):
    app = FastAPI()
    app.add_middleware(AccountAuthMiddleware, store=lambda: store, settings=SETTINGS)
    app.include_router(create_identity_router(lambda: store, SETTINGS))

    @app.api_route("/", methods=["GET", "HEAD"])
    @app.api_route("/healthz", methods=["GET", "HEAD"])
    @app.get("/assets/app.js")
    def shell():
        return {"shell": True}

    @app.api_route("/api/business", methods=["GET", "POST"])
    @app.api_route("/legacy-business", methods=["GET", "POST"])
    def business(request: Request):
        return {"tenant_id": request.state.identity.tenant_id}

    return app


@pytest.fixture
def http():
    store = HTTPStore()
    with TestClient(make_app(store), base_url=SETTINGS.origin) as client:
        yield client, store


def sign_in(client):
    return client.post(
        "/api/auth/login",
        json={"username": IDENTITY.username, "password": PASSWORD},
        headers=CSRF,
    )


@pytest.mark.parametrize(
    "origin,rp_id",
    [
        ("http://app.example.test", "app.example.test"),
        ("https://app.example.test/", "app.example.test"),
        ("https://app.example.test?", "app.example.test"),
        ("https://app.example.test#", "app.example.test"),
        ("https://user:pass@app.example.test", "app.example.test"),
        ("https://app.example.test/path", "app.example.test"),
        ("https://app.example.test", "example.test"),
        ("https://app.example.test:bad", "app.example.test"),
        ("https://app.example.test:0", "app.example.test"),
        ("https://app..test", "app..test"),
        ("https://app.example.test.", "app.example.test."),
        ("https://app.example.test\n", "app.example.test"),
    ],
)
def test_reject_ambiguous_or_non_https_configuration(origin, rp_id):
    with pytest.raises(RuntimeError):
        AuthSettings(origin, rp_id)


def test_environment_settings(monkeypatch):
    monkeypatch.setenv("POC_AUTH_ORIGIN", SETTINGS.origin)
    monkeypatch.delenv("POC_AUTH_RP_ID", raising=False)
    monkeypatch.setenv("POC_AUTH_RP_NAME", "Demo")
    assert AuthSettings.from_environment() == replace(SETTINGS, rp_name="Demo")
    assert AuthSettings("https://app.example.test:8443", SETTINGS.rp_id)


def test_password_argon2id_preserves_spaces_and_enforces_only_length():
    encoded = PASSWORD_HASHER.hash(PASSWORD)
    assert encoded.startswith("$argon2id$")
    assert _password_matches(encoded, PASSWORD)
    assert not _password_matches(encoded, PASSWORD.strip())
    validate_new_password(" " * 15)
    validate_new_password("あ" * 128)
    for password in ("a" * 14, "a" * 129):
        with pytest.raises(StoreError):
            validate_new_password(password)


@pytest.mark.parametrize("first_operation", ["hash", "verify"])
def test_argon2_hash_and_verify_share_one_nonblocking_work_slot(monkeypatch, first_operation):
    entered, release = Event(), Event()

    class BlockingHasher:
        def hash(self, password):
            entered.set()
            assert release.wait(5)
            return "test-hash"

        def verify(self, encoded, password):
            entered.set()
            assert release.wait(5)
            return True

    monkeypatch.setattr(identity_store, "PASSWORD_HASHER", BlockingHasher())
    first = (
        (lambda: _hash_password(PASSWORD))
        if first_operation == "hash"
        else (lambda: _password_matches("test-hash", PASSWORD))
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(first)
        try:
            assert entered.wait(5)
            for operation in (
                lambda: _hash_password(PASSWORD),
                lambda: _password_matches("test-hash", PASSWORD),
            ):
                with pytest.raises(StoreError) as caught:
                    operation()
                assert caught.value.status == 503
                assert caught.value.code == "authentication_busy"
        finally:
            release.set()
        assert pending.result(timeout=5)
    assert _hash_password(PASSWORD) == "test-hash"
    assert _password_matches("test-hash", PASSWORD)


def test_hash_work_slot_released_after_failure(monkeypatch):
    class FailingHasher:
        def hash(self, password):
            raise RuntimeError("Synthetic hash failure")

    monkeypatch.setattr(identity_store, "PASSWORD_HASHER", FailingHasher())
    for _ in range(2):
        with pytest.raises(RuntimeError, match="Synthetic hash failure"):
            _hash_password(PASSWORD)


def test_password_overload_has_short_retry_after(http, monkeypatch):
    client, store = http

    def overloaded(*args, **kwargs):
        raise StoreError(503, "authentication_busy", "Try again shortly.")

    monkeypatch.setattr(store, "login", overloaded)
    response = sign_in(client)
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert response.json()["detail"]["code"] == "authentication_busy"


@pytest.mark.parametrize("path", ["/", "/healthz", "/assets/app.js", "/api/auth/session"])
def test_public_get_and_safe_headers(http, path):
    client, _ = http
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"
    assert "publickey-credentials-get=(self)" in response.headers["permissions-policy"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/business",
        "/legacy-business",
        "/docs",
        "/openapi.json",
        "/api/auth/passkeys",
    ],
)
def test_every_other_path_requires_session(http, path):
    client, _ = http
    response = client.get(path)
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "authentication_required"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": SETTINGS.origin},
        {"X-Requested-With": "staff-matching"},
        {"Origin": "https://evil.test", "X-Requested-With": "staff-matching"},
        {**CSRF, "Sec-Fetch-Site": "same-site"},
        {**CSRF, "Sec-Fetch-Site": "cross-site"},
    ],
)
def test_login_requires_exact_origin_and_application_header(http, headers):
    client, store = http
    response = client.post(
        "/api/auth/login",
        json={"username": IDENTITY.username, "password": PASSWORD},
        headers=headers,
    )
    assert response.status_code == 403
    assert not store.calls


def test_cross_origin_get_and_duplicate_origin_denied(http):
    client, _ = http
    assert (
        client.get("/api/auth/session", headers={"Origin": "https://evil.test"}).status_code == 403
    )
    assert (
        client.get(
            "/api/auth/session",
            headers=[("Origin", SETTINGS.origin), ("Origin", SETTINGS.origin)],
        ).status_code
        == 403
    )


def test_login_rotation_logout_and_cookie_attributes(http):
    client, store = http
    assert client.get("/api/auth/session").json() == {"enabled": True, "authenticated": False}
    response = sign_in(client)
    assert response.status_code == 200
    cookie = next(
        c for c in response.headers.get_list("set-cookie") if c.startswith(SESSION_COOKIE)
    )
    for attribute in ("Secure", "HttpOnly", "Path=/", "SameSite=lax", "Max-Age=28800"):
        assert attribute in cookie
    assert "Domain=" not in cookie
    assert response.json()["tenant"]["id"] == "tenant-a"
    first_token = client.cookies[SESSION_COOKIE]
    assert sign_in(client).status_code == 200
    assert client.cookies[SESSION_COOKIE] != first_token
    assert store.resolve_session(first_token) is None
    assert client.get("/api/business").status_code == 200
    assert client.get("/api/business", headers={"X-Tenant-ID": "other"}).status_code == 409
    assert (
        client.post(
            "/legacy-business",
            json={},
            headers={**CSRF, "X-Tenant-ID": "other"},
        ).json()["detail"]["code"]
        == "tenant_changed"
    )
    assert client.post("/api/auth/logout", json={}, headers=CSRF).json() == {"ok": True}
    assert client.get("/api/business").status_code == 401
    assert client.get("/api/auth/session").json()["authenticated"] is False


def test_viewer_can_manage_own_identity_but_not_mutate_business(http):
    client, store = http
    store.identity = replace(IDENTITY, role="viewer")
    assert sign_in(client).status_code == 200
    assert client.get("/api/business").status_code == 200
    assert client.post("/api/business", json={}, headers=CSRF).status_code == 403
    assert (
        client.post(
            "/api/auth/reauth",
            json={"password": PASSWORD},
            headers=CSRF,
        ).status_code
        == 200
    )
    response = client.post("/api/auth/passkeys/registration/options", json={}, headers=CSRF)
    assert response.json()["detail"]["code"] == "reauthentication_required"
    assert client.get("/api/auth/passkeys").json() == {"items": []}


def test_flow_cookie_options_are_direct_and_bound(http):
    client, _ = http
    response = client.post("/api/auth/passkeys/authentication/options", json={}, headers=CSRF)
    assert response.status_code == 200
    assert response.json()["rpId"] == SETTINGS.rp_id
    assert len(unb64(response.json()["challenge"])) >= 32
    cookie = response.headers["set-cookie"]
    assert FLOW_COOKIE in cookie and "Secure" in cookie and "HttpOnly" in cookie
    assert "Domain=" not in cookie


def test_malformed_models_are_sanitized_bounded_and_consume_verification(http):
    client, store = http
    for body in (
        {"username": IDENTITY.username, "password": PASSWORD, "admin": True},
        {"username": IDENTITY.username, "password": PASSWORD * 10},
        {"username": "\x00", "password": PASSWORD},
    ):
        response = client.post("/api/auth/login", json=body, headers=CSRF)
        assert response.status_code == 422
        assert PASSWORD not in response.text
        assert response.json()["detail"]["code"] == "invalid_request"
    response = client.post("/api/auth/login", content=b"x" * 32769, headers=CSRF)
    assert response.status_code == 413
    response = client.post(
        "/api/auth/passkeys/authentication/verify",
        json={"credential": {}},
        headers=CSRF,
    )
    assert response.status_code == 422 and store.consumed == 1
    assert (
        client.post(
            "/api/auth/login",
            content="{",
            headers={**CSRF, "Content-Type": "application/json"},
        ).status_code
        == 422
    )


def test_websockets_denied_and_unready_store_fail_closed():
    with TestClient(make_app(None), base_url=SETTINGS.origin) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/auth/session").status_code == 503
        with pytest.raises(WebSocketDisconnect) as caught:
            with client.websocket_connect("/api/business"):
                pytest.fail("WebSocket unexpectedly accepted")
        assert caught.value.code == 1008


class Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class CeremonyStore(IdentityStore):
    """Small scripted DB boundary. Verification below uses unmocked webauthn/cryptography."""

    def __init__(self):
        super().__init__()
        self.rows = {}
        self.challenge = None
        self.events = []
        self.transaction_outcomes = []
        self.flow_token = secrets.token_urlsafe(32)

    @contextmanager
    def _transaction(self):
        try:
            yield self
        except Exception:
            self.transaction_outcomes.append("rollback")
            raise
        else:
            self.transaction_outcomes.append("commit")

    def challenge_for(self, purpose, *, valid=True):
        self.challenge = {
            "purpose": purpose,
            "challenge": secrets.token_bytes(32),
            "valid": valid,
            "user_id": IDENTITY.user_id,
            "session_id": IDENTITY.session_id,
            "flow_hash": _digest(self.flow_token),
        }
        return self.challenge["challenge"]

    def execute(self, sql, args=()):
        if sql.startswith("DELETE FROM staff_identity.challenges"):
            challenge = self.challenge
            if not challenge:
                return Result(None)
            if "'registration'" in sql:
                matches = challenge["purpose"] == "registration" and args == (
                    challenge["session_id"],
                    challenge["user_id"],
                )
            else:
                matches = challenge["purpose"] == "authentication" and args == (
                    challenge["flow_hash"],
                )
            if matches:
                self.challenge = None
                return Result(challenge)
            return Result(None)
        if sql.startswith("SELECT count(*)"):
            return Result({"count": len(self.rows)})
        if sql.startswith("INSERT INTO staff_identity.credentials"):
            key_id, user_id, cred_id, public_key, count, name, device, backed_up = args
            self.rows[cred_id] = {
                "id": key_id,
                "user_id": user_id,
                "credential_id": cred_id,
                "public_key": public_key,
                "sign_count": count,
                "name": name,
                "device_type": device,
                "backed_up": backed_up,
            }
        elif sql.startswith("SELECT user_id") or sql.startswith("SELECT *"):
            return Result(self.rows.get(args[0]))
        elif sql.startswith("UPDATE staff_identity.credentials"):
            count, backed_up, key_id = args
            row = next(r for r in self.rows.values() if r["id"] == key_id)
            row.update(sign_count=count, backed_up=backed_up)
        elif sql.startswith("INSERT INTO staff_identity.audit_events"):
            self.events.append(args[2])
        elif not sql.startswith("UPDATE staff_identity.users"):
            raise AssertionError(f"Unexpected SQL: {sql}")
        return Result(None)

    def _require_session(self, connection, identity, *, recent=False):
        assert identity == IDENTITY
        return {"id": USER_ID}, {}, []

    def _lock_user(self, connection, user_id):
        return {"id": USER_ID}

    def _lock_login_user(self, connection, user_id, old_token):
        return {"id": USER_ID, "enabled": True}

    def _memberships(self, connection, user_id):
        return [{"id": "tenant-a", "role": "coordinator"}]

    def _new_session(self, connection, user, memberships, method, **kwargs):
        return replace(IDENTITY, auth_method=method), "not-a-real-session"

    def _rate_limit(self, kind, value, maximum):
        pass


@pytest.fixture
def enrolled():
    store, authenticator = CeremonyStore(), Authenticator()
    challenge = store.challenge_for("registration")
    credential = authenticator.registration(challenge)
    store.registration_verify(IDENTITY, credential, "Synced passkey", SETTINGS)
    assert store.events == ["passkey_registered"]
    return store, authenticator


def test_actual_es256_registration_and_synced_zero_counter_assertions(enrolled):
    store, authenticator = enrolled
    for _ in range(2):
        challenge = store.challenge_for("authentication")
        credential = authenticator.assertion(challenge)
        identity, _ = store.authentication_verify(
            credential,
            store.flow_token,
            "source",
            SETTINGS,
        )
        assert identity.auth_method == "passkey"
    row = store.rows[authenticator.credential_id]
    assert row["public_key"] == authenticator.cose_key
    assert row["sign_count"] == 0 and row["backed_up"] is True
    assert row["device_type"] == "multi_device"
    assert store.events == ["passkey_registered", "passkey_login", "passkey_login"]


@pytest.mark.parametrize(
    "failure",
    [
        "origin",
        "rp",
        "uv",
        "up",
        "signature",
        "handle",
        "challenge",
        "cross_origin",
        "credential_id",
        "backup_eligibility",
    ],
)
def test_actual_signed_assertion_rejections_consume_challenge(enrolled, failure):
    store, authenticator = enrolled
    challenge = store.challenge_for("authentication")
    kwargs = {}
    if failure == "origin":
        kwargs["origin"] = "https://evil.test"
    elif failure == "rp":
        kwargs["rp_id"] = "evil.test"
    elif failure == "uv":
        kwargs["flags"] = 0x19
    elif failure == "up":
        kwargs["flags"] = 0x1C
    elif failure == "handle":
        kwargs["user_id"] = uuid4()
    elif failure == "cross_origin":
        kwargs["cross_origin"] = True
    elif failure == "backup_eligibility":
        kwargs["flags"] = 5
    credential = authenticator.assertion(
        b"not the challenge" if failure == "challenge" else challenge,
        **kwargs,
    )
    if failure == "signature":
        credential["response"]["signature"] = b64(b"not an ECDSA signature")
    elif failure == "credential_id":
        credential["id"] = b64(b"not the same credential")
    with pytest.raises(StoreError) as caught:
        store.authentication_verify(credential, store.flow_token, "source", SETTINGS)
    assert caught.value.code == "invalid_passkey"
    assert store.challenge is None
    assert store.transaction_outcomes[2] == "commit"
    with pytest.raises(StoreError):
        store.authentication_verify(credential, store.flow_token, "source", SETTINGS)


@pytest.mark.parametrize("failure", ["origin", "rp", "uv", "up", "challenge", "id", "cross_origin"])
def test_registration_security_checks(failure):
    store, authenticator = CeremonyStore(), Authenticator()
    challenge = store.challenge_for("registration")
    kwargs = {}
    if failure == "origin":
        kwargs["origin"] = "https://evil.test"
    elif failure == "rp":
        kwargs["rp_id"] = "evil.test"
    elif failure == "uv":
        kwargs["flags"] = 0x59
    elif failure == "up":
        kwargs["flags"] = 0x5C
    elif failure == "cross_origin":
        kwargs["cross_origin"] = True
    credential = authenticator.registration(
        b"wrong challenge" if failure == "challenge" else challenge,
        **kwargs,
    )
    if failure == "id":
        credential["id"] = credential["rawId"] = b64(b"different attested ID")
    with pytest.raises(StoreError) as caught:
        store.registration_verify(IDENTITY, credential, "Passkey", SETTINGS)
    assert caught.value.code == "invalid_passkey"
    assert store.challenge is None and not store.rows
    assert store.transaction_outcomes == ["commit"]


def test_expired_cross_browser_cross_session_and_cross_user_challenges(enrolled):
    store, authenticator = enrolled
    challenge = store.challenge_for("authentication")
    credential = authenticator.assertion(challenge)
    with pytest.raises(StoreError):
        store.authentication_verify(credential, secrets.token_urlsafe(32), "source", SETTINGS)
    assert store.challenge is not None
    store.challenge["valid"] = False
    with pytest.raises(StoreError):
        store.authentication_verify(credential, store.flow_token, "source", SETTINGS)
    assert store.challenge is None
    challenge = store.challenge_for("registration")
    credential = authenticator.registration(challenge)
    for identity in (
        replace(IDENTITY, session_id=str(uuid4())),
        replace(IDENTITY, user_id=str(uuid4())),
    ):
        with pytest.raises(StoreError):
            store.registration_verify(identity, credential, "Passkey", SETTINGS)
        assert store.challenge is not None


def test_nonzero_counter_must_advance_under_verification(enrolled):
    store, authenticator = enrolled
    for count, accepted in ((1, True), (1, False), (0, False), (2, True)):
        challenge = store.challenge_for("authentication")
        credential = authenticator.assertion(challenge, count=count)
        if accepted:
            store.authentication_verify(credential, store.flow_token, "source", SETTINGS)
        else:
            with pytest.raises(StoreError):
                store.authentication_verify(credential, store.flow_token, "source", SETTINGS)
    assert store.rows[authenticator.credential_id]["sign_count"] == 2


def test_rate_limit_sql_is_bounded_and_rejections_are_committed(monkeypatch):
    store = IdentityStore()
    statements = []
    outcomes = []

    class Connection:
        def execute(self, query, args):
            statements.append((query, args))
            return Result({"attempts": 11})

    @contextmanager
    def transaction():
        yield Connection()
        outcomes.append("commit")

    monkeypatch.setattr(store, "_transaction", transaction)
    with pytest.raises(StoreError) as caught:
        store._rate_limit("password_account", "invented-user", 10)
    assert caught.value.status == 429 and outcomes == ["commit"]
    query, args = statements[0]
    assert "ON CONFLICT (kind, bucket) DO UPDATE" in query
    assert "invented-user" not in query and "invented-user" not in str(args)
    assert 0 <= args[1] < 4096
