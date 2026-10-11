"""Private-network acceptance with real PostgreSQL and cryptographically signed WebAuthn."""

import base64
import hashlib
import json
import os
import secrets
import time
from urllib.parse import urlsplit
from uuid import uuid4

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _client_data(kind: str, challenge: str, origin: str) -> bytes:
    return json.dumps({
        "type": kind, "challenge": challenge, "origin": origin, "crossOrigin": False,
    }, separators=(",", ":")).encode()


class TestAuthenticator:
    """An isolated software authenticator, not a biometric/iPad assurance claim."""

    def __init__(self, rp_id: str, origin: str):
        self.rp_id = rp_id
        self.origin = origin
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = secrets.token_bytes(32)
        self.user_handle = ""
        self.counter = 0

    def registration(self, options: dict) -> dict:
        self.user_handle = options["user"]["id"]
        public = self.key.public_key().public_numbers()
        cose = cbor2.dumps({
            1: 2, 3: -7, -1: 1,
            -2: public.x.to_bytes(32, "big"), -3: public.y.to_bytes(32, "big"),
        })
        auth_data = (
            hashlib.sha256(self.rp_id.encode()).digest() + b"\x45" + bytes(4)
            + bytes(16) + len(self.credential_id).to_bytes(2, "big") + self.credential_id + cose
        )
        return {
            "id": _b64(self.credential_id), "rawId": _b64(self.credential_id),
            "type": "public-key", "clientExtensionResults": {},
            "authenticatorAttachment": "platform",
            "response": {
                "clientDataJSON": _b64(_client_data(
                    "webauthn.create", options["challenge"], self.origin,
                )),
                "attestationObject": _b64(cbor2.dumps({
                    "fmt": "none", "attStmt": {}, "authData": auth_data,
                })),
                "transports": ["internal"],
            },
        }

    def assertion(self, options: dict) -> dict:
        self.counter += 1
        client = _client_data("webauthn.get", options["challenge"], self.origin)
        auth_data = (
            hashlib.sha256(self.rp_id.encode()).digest()
            + b"\x05" + self.counter.to_bytes(4, "big")
        )
        signature = self.key.sign(
            auth_data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()),
        )
        return {
            "id": _b64(self.credential_id), "rawId": _b64(self.credential_id),
            "type": "public-key", "clientExtensionResults": {},
            "authenticatorAttachment": "platform",
            "response": {
                "clientDataJSON": _b64(client), "authenticatorData": _b64(auth_data),
                "signature": _b64(signature), "userHandle": self.user_handle,
            },
        }


def _response(response, status: int = 200):
    if response.status_code != status:
        print(json.dumps({
            "status": "FAIL", "check": "http-expectation",
            "method": response.request.method, "path": response.request.url.path,
            "expected_status": status, "actual_status": response.status_code,
        }), flush=True)
        raise AssertionError(
            f"{response.request.method} {response.request.url.path}: "
            f"expected {status}, received {response.status_code}"
        )
    return response.json()


def verify_accounts(admin_connection, app_password: str) -> None:
    from backend.app import create_app
    from backend.identity import IdentityStore
    from backend.postgres_setup import runtime_conninfo, seed_tenant
    from backend.postgres_store import PostgresStore
    from backend.seed import demo_snapshot

    os.environ["POC_AUTH_MODE"] = "accounts"
    origin = os.environ["POC_AUTH_ORIGIN"]
    hostname = urlsplit(origin).hostname
    if hostname is None:
        raise ValueError("Verification requires POC_AUTH_ORIGIN with a hostname.")
    authenticator = TestAuthenticator(hostname, origin)
    admin = IdentityStore(admin_connection.info.dsn)
    dsn = runtime_conninfo(admin_connection, app_password)
    suffix = uuid4().hex[:16]
    tenant_a, tenant_b = (f"validation-identity-{suffix}-{side}" for side in ("a", "b"))
    for tenant in (tenant_a, tenant_b):
        seed_tenant(admin_connection, demo_snapshot().model_copy(update={"tenant_id": tenant}))
    admin_connection.commit()
    users = [
        (f"verify-a-{suffix}", tenant_a, "coordinator"),
        (f"verify-b-{suffix}", tenant_b, "coordinator"),
        (f"verify-v-{suffix}", tenant_a, "viewer"),
    ]
    password = secrets.token_urlsafe(32)
    for username, tenant, role in users:
        admin.provision_user(
            username=username, display_name=username, password=password,
            tenant_id=tenant, tenant_name=tenant, role=role,
        )

    def app():
        return create_app(
            mode="azure-poc", store=PostgresStore(dsn), identity_store=IdentityStore(dsn),
        )

    headers = {"Origin": origin, "X-Requested-With": "staff-matching"}
    try:
        with TestClient(app(), base_url=origin) as first, \
                TestClient(app(), base_url=origin) as second:
            first.headers.update(headers)
            second.headers.update(headers)
            assert _response(first.get("/api/auth/session"))["authenticated"] is False
            _response(first.get("/api/staff"), 401)
            for client, (username, tenant, _role) in zip((first, second), users):
                session = _response(client.post("/api/auth/login", json={
                    "username": username, "password": password,
                }))
                assert session["tenant"]["id"] == tenant
                assert _response(client.get("/api/health"))["tenant_id"] == tenant
            _response(first.get("/api/staff", headers={"X-Tenant-ID": tenant_b}), 409)
            _response(first.post("/api/auth/tenant", json={"tenant_id": tenant_b}), 403)
            runs = [
                _response(client.post("/api/matching/optimize", json={
                    "idempotency_key": uuid4().hex, "time_limit_seconds": 0.5,
                }), 202)["run_id"]
                for client in (first, second)
            ]
            for client, run_id in zip((first, second), runs):
                for _ in range(100):
                    run = _response(client.get(f"/api/matching/runs/{run_id}"))
                    if run["status"] in ("completed", "failed"):
                        break
                    time.sleep(0.1)
                assert run["status"] == "completed"
                for proposal in run["proposals"]:
                    _response(client.post(f"/api/proposals/{proposal['id']}/respond", json={
                        "decision": "decline", "expected_version": proposal["version"],
                        "idempotency_key": uuid4().hex,
                    }))
            _response(first.get(f"/api/matching/runs/{runs[1]}"), 404)
            print(json.dumps({"status": "PASS", "check": "request-and-worker-tenant-isolation"}))

            options = _response(first.post("/api/auth/passkeys/registration/options", json={}))
            registration = authenticator.registration(options)
            assert second.post("/api/auth/passkeys/registration/verify", json={
                "credential": registration, "name": "verification",
            }).status_code in (400, 401, 403, 409)
            _response(first.post("/api/auth/passkeys/registration/verify", json={
                "credential": registration, "name": "verification",
            }))
            assert first.post("/api/auth/passkeys/registration/verify", json={
                "credential": registration, "name": "verification",
            }).status_code in (400, 401, 403, 409)
            _response(first.post("/api/auth/logout", json={}))
            _response(first.get("/api/staff"), 401)
            options = _response(first.post("/api/auth/passkeys/authentication/options", json={}))
            assertion = authenticator.assertion(options)
            assert second.post("/api/auth/passkeys/authentication/verify", json={
                "credential": assertion,
            }).status_code in (400, 401, 403, 409)
            session = _response(first.post("/api/auth/passkeys/authentication/verify", json={
                "credential": assertion,
            }))
            assert session["user"]["username"] == users[0][0]
            assert session["auth_method"] == "passkey"
            assert first.post("/api/auth/passkeys/authentication/verify", json={
                "credential": assertion,
            }).status_code in (400, 401, 403, 409)
            keys = _response(first.get("/api/auth/passkeys"))["items"]
            assert len(keys) == 1
            _response(first.delete(f"/api/auth/passkeys/{keys[0]['id']}"))
            assert _response(first.get("/api/auth/passkeys"))["items"] == []
            print(json.dumps({"status": "PASS", "check": "signed-webauthn-and-replay-binding"}))

            _response(second.post("/api/auth/logout", json={}))
            _response(second.post("/api/auth/login", json={
                "username": users[2][0], "password": password,
            }))
            _response(second.post("/api/matching/optimize", json={
                "idempotency_key": uuid4().hex,
            }), 403)
            changed = secrets.token_urlsafe(32)
            _response(first.post("/api/auth/password", json={
                "current_password": password, "new_password": changed,
            }))
            _response(first.post("/api/auth/logout", json={}))
            _response(first.post("/api/auth/login", json={
                "username": users[0][0], "password": password,
            }), 401)
            _response(first.post("/api/auth/login", json={
                "username": users[0][0], "password": changed,
            }))
            print(json.dumps({"status": "PASS", "check": "viewer-password-change-and-logout"}))
    finally:
        for username, _tenant, _role in users:
            admin.recover_password(
                username, secrets.token_urlsafe(48), revoke_passkeys=True,
                actor="verification-cleanup",
            )
