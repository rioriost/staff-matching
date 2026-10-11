"""Cross-tenant PoC identity storage. Migrate separately; startup never performs DDL.

The database role is a trusted authentication service, not an end-user identity.
Only administrators provision users/memberships or recover passwords. Tokens are
opaque random bearer secrets and only their SHA-256 digests are persisted.
"""

import hashlib
import json
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from threading import BoundedSemaphore
from typing import Any
from uuid import UUID, uuid4

import psycopg
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError
from psycopg.rows import dict_row
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import (
    parse_authentication_credential_json,
    parse_registration_credential_json,
)
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from backend.database import connect
from backend.store import StoreError

PASSWORD_HASHER = PasswordHasher(
    time_cost=3, memory_cost=65536, parallelism=1, hash_len=32, salt_len=16, type=Type.ID
)
# One hash at a time per process bounds Argon2 working memory to 64 MiB.
# Keep one ASGI worker for the 0.5 CPU / 1 GiB PoC allocation.
_PASSWORD_WORK = BoundedSemaphore(1)
# Module initialization is serialized; no request can race this one startup hash.
DUMMY_HASH = PASSWORD_HASHER.hash(secrets.token_urlsafe(48))
PASSWORD_MIN = 15
PASSWORD_MAX = 128
MAX_CREDENTIALS = 20
MAX_CHALLENGES = 10000


@dataclass(frozen=True)
class Identity:
    user_id: str
    username: str
    display_name: str
    tenant_id: str
    role: str
    session_id: str
    authenticated_at: datetime
    auth_method: str


def _error(status: int, code: str, message: str) -> StoreError:
    return StoreError(status, code, message)


def _invalid_login() -> StoreError:
    return _error(401, "invalid_credentials", "The supplied credentials could not be verified.")


def _invalid_ceremony() -> StoreError:
    return _error(400, "invalid_passkey", "The passkey ceremony expired or could not be verified.")


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def _valid_token(value: str | None) -> bool:
    return bool(
        value
        and len(value) == 43
        and all(c.isalnum() or c in "-_" for c in value)
        and value.isascii()
    )


def validate_new_password(password: str) -> None:
    if not isinstance(password, str) or not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        raise _error(422, "invalid_password", "Use a password of 15 to 128 characters.")


@contextmanager
def _password_work() -> Iterator[None]:
    # Reject overload rather than queueing blocked hash jobs in every HTTP worker.
    if not _PASSWORD_WORK.acquire(blocking=False):
        raise _error(503, "authentication_busy", "Authentication is busy. Try again shortly.")
    try:
        yield
    finally:
        _PASSWORD_WORK.release()


def _hash_password(password: str) -> str:
    with _password_work():
        return PASSWORD_HASHER.hash(password)


def _password_matches(encoded: str, password: str) -> bool:
    with _password_work():
        try:
            return PASSWORD_HASHER.verify(encoded, password)
        except (VerificationError, InvalidHashError):
            return False


def _username(username: str) -> str:
    # IDs are case-insensitive; password text is never normalized or stripped.
    return username.strip().casefold()


def _text(value: str, maximum: int, label: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise _error(422, "invalid_input", f"{label} has an invalid length.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise _error(422, "invalid_input", f"{label} contains control characters.")
    return value


def _client_data(data: bytes) -> None:
    """The library validates origin/type/challenge; also disallow embedded ceremonies."""
    value = json.loads(data)
    if not isinstance(value, dict) or value.get("crossOrigin", False) is not False:
        raise _invalid_ceremony()
    if "topOrigin" in value:
        raise _invalid_ceremony()


class IdentityStore:
    def __init__(self, conninfo: str = ""):
        self._conninfo = conninfo

    @contextmanager
    def _transaction(self) -> Iterator[psycopg.Connection]:
        with connect(self._conninfo, row_factory=dict_row) as connection:
            connection.execute("SET LOCAL statement_timeout = '15s'")
            connection.execute("SET LOCAL lock_timeout = '10s'")
            yield connection

    def initialize(self) -> None:
        """Readiness/ACL checks only. Apply db/identity/001_identity.sql as an admin."""
        with self._transaction() as connection:
            version = connection.execute(
                "SELECT max(version) AS version FROM staff_identity.schema_migrations"
            ).fetchone()
            if version["version"] != 1:
                raise RuntimeError("Identity schema version 1 is required.")
            for table in (
                "users",
                "tenants",
                "memberships",
                "sessions",
                "credentials",
                "challenges",
                "rate_limits",
            ):
                connection.execute(f"SELECT 1 FROM staff_identity.{table} LIMIT 0")
            privileges = connection.execute(
                "SELECT has_table_privilege(current_user, 'staff_identity.users', 'INSERT') "
                "OR has_table_privilege(current_user, 'staff_identity.memberships', 'INSERT') "
                "OR has_table_privilege(current_user, 'staff_identity.memberships', 'UPDATE') "
                "OR has_table_privilege(current_user, 'staff_identity.tenants', 'INSERT') "
                "OR has_column_privilege(current_user, 'staff_identity.users', "
                "'enabled', 'UPDATE') "
                "OR has_schema_privilege(current_user, 'staff_identity', 'CREATE') AS unsafe"
            ).fetchone()
            if privileges["unsafe"]:
                raise RuntimeError("Identity runtime requires a restricted role, not an admin.")

    @staticmethod
    def _audit(
        connection,
        event: str,
        user_id: str | UUID | None,
        *,
        actor: str = "self",
        credential_id: str | UUID | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO staff_identity.audit_events(id, user_id, event, actor, credential_id) "
            "VALUES (%s, %s, %s, %s, %s)",
            (uuid4(), user_id, event, actor, credential_id),
        )

    @staticmethod
    def _admin(connection) -> None:
        row = connection.execute(
            "SELECT has_table_privilege(current_user, 'staff_identity.users', 'INSERT') "
            "AND has_table_privilege(current_user, 'staff_identity.memberships', "
            "'INSERT') AS allowed"
        ).fetchone()
        if not row["allowed"]:
            raise _error(403, "admin_required", "A database administrator is required.")

    def provision_user(
        self,
        username: str,
        display_name: str,
        password: str,
        tenant_id: str,
        tenant_name: str,
        role: str = "coordinator",
    ) -> str:
        """Admin-only; return a neutral UUID string. Existing secrets/roles are preserved."""
        username = _text(_username(username), 128, "Username")
        _text(display_name, 160, "Display name")
        _text(tenant_id, 128, "Tenant ID")
        _text(tenant_name, 160, "Tenant name")
        validate_new_password(password)
        if role not in {"admin", "coordinator", "viewer"}:
            raise _error(422, "invalid_role", "The membership role is not supported.")
        with self._transaction() as connection:
            self._admin(connection)
            connection.execute(
                "INSERT INTO staff_identity.tenants(id, name) VALUES (%s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (tenant_id, tenant_name),
            )
            created = connection.execute(
                "INSERT INTO staff_identity.users(id, username, display_name, password_hash) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (username) DO NOTHING RETURNING id",
                (uuid4(), username, display_name, _hash_password(password)),
            ).fetchone()
            user = connection.execute(
                "SELECT id FROM staff_identity.users WHERE username = %s FOR UPDATE",
                (username,),
            ).fetchone()
            membership = connection.execute(
                "INSERT INTO staff_identity.memberships(user_id, tenant_id, role) "
                "VALUES (%s, %s, %s) ON CONFLICT (user_id, tenant_id) DO NOTHING RETURNING user_id",
                (user["id"], tenant_id, role),
            ).fetchone()
            if created or membership:
                self._audit(
                    connection,
                    "account_provisioned" if created else "membership_provisioned",
                    user["id"],
                    actor="admin",
                )
            return str(user["id"])

    def recover_password(
        self,
        username: str,
        new_password: str,
        *,
        revoke_passkeys: bool = False,
        actor: str = "admin-cli",
    ) -> str:
        """Admin recovery revokes every session/challenge; key removal is explicitly opt-in."""
        validate_new_password(new_password)
        _text(actor, 160, "Audit actor")
        with self._transaction() as connection:
            self._admin(connection)
            user = connection.execute(
                "SELECT * FROM staff_identity.users WHERE username = %s FOR UPDATE",
                (_username(username),),
            ).fetchone()
            if user is None:
                raise _error(404, "account_not_found", "The account does not exist.")
            self._replace_password(connection, user["id"], new_password)
            if revoke_passkeys:
                connection.execute(
                    "DELETE FROM staff_identity.credentials WHERE user_id = %s",
                    (user["id"],),
                )
            self._audit(
                connection,
                "password_recovery_with_keys_revoked" if revoke_passkeys else "password_recovery",
                user["id"],
                actor=actor,
            )
            return str(user["id"])

    def _rate_limit(self, kind: str, value: str, maximum: int) -> None:
        bucket = int.from_bytes(_digest(kind + ":" + value)[:4], "big") % 4096
        # Commit rejected attempts too. Buckets collide conservatively, never bypass limits.
        with self._transaction() as connection:
            row = connection.execute(
                "INSERT INTO staff_identity.rate_limits(kind, bucket, attempts) VALUES (%s, %s, 1) "
                "ON CONFLICT (kind, bucket) DO UPDATE SET "
                "attempts = CASE WHEN staff_identity.rate_limits.window_start "
                "<= clock_timestamp() - interval '15 minutes' THEN 1 ELSE "
                "least(staff_identity.rate_limits.attempts + 1, 1000000) END, "
                "window_start = CASE WHEN staff_identity.rate_limits.window_start "
                "<= clock_timestamp() - interval '15 minutes' THEN clock_timestamp() ELSE "
                "staff_identity.rate_limits.window_start END RETURNING attempts",
                (kind, bucket),
            ).fetchone()
        if row["attempts"] > maximum:
            raise _error(429, "rate_limited", "Too many attempts. Try again later.")

    def _password_rate(self, username: str, source: str) -> None:
        self._rate_limit("password_source", source, 60)
        self._rate_limit("password_account", _username(username), 10)

    @staticmethod
    def _memberships(connection, user_id) -> list[dict]:
        return connection.execute(
            "SELECT t.id, t.name, m.role FROM staff_identity.memberships m "
            "JOIN staff_identity.tenants t ON t.id = m.tenant_id "
            "WHERE m.user_id = %s AND m.enabled ORDER BY t.id",
            (user_id,),
        ).fetchall()

    @staticmethod
    def _lock_user(connection, user_id):
        return connection.execute(
            "SELECT * FROM staff_identity.users WHERE id = %s AND enabled FOR UPDATE",
            (user_id,),
        ).fetchone()

    @staticmethod
    def _lock_login_user(connection, user_id, old_token: str | None):
        # Cross-account browser rotation touches two users' sessions. A stable lock
        # order avoids A→B / B→A login deadlocks across replicas.
        user_ids = {UUID(str(user_id))}
        if _valid_token(old_token):
            previous = connection.execute(
                "SELECT user_id FROM staff_identity.sessions WHERE token_hash = %s",
                (_digest(old_token),),
            ).fetchone()
            if previous:
                user_ids.add(previous["user_id"])
        rows = connection.execute(
            "SELECT * FROM staff_identity.users WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
            (list(user_ids),),
        ).fetchall()
        return next((row for row in rows if row["id"] == user_id), None)

    @staticmethod
    def _identity(user, session, role: str) -> Identity:
        return Identity(
            user_id=str(user["id"]),
            username=user["username"],
            display_name=user["display_name"],
            tenant_id=session["tenant_id"],
            role=role,
            session_id=str(session["id"]),
            authenticated_at=session["authenticated_at"],
            auth_method=session["auth_method"],
        )

    def _require_session(self, connection, identity: Identity, *, recent: bool = False):
        user = self._lock_user(connection, identity.user_id)
        session = connection.execute(
            "SELECT *, recent_auth_at > clock_timestamp() - interval '5 minutes' AS recent "
            "FROM staff_identity.sessions WHERE id = %s AND user_id = %s "
            "AND revoked_at IS NULL AND expires_at > clock_timestamp() "
            "AND last_seen_at > clock_timestamp() - interval '30 minutes' FOR UPDATE",
            (identity.session_id, identity.user_id),
        ).fetchone()
        memberships = self._memberships(connection, identity.user_id) if user else []
        if (
            not user
            or not session
            or not any(m["id"] == session["tenant_id"] == identity.tenant_id for m in memberships)
        ):
            raise _error(401, "authentication_required", "Sign in again.")
        if recent and not session["recent"]:
            raise _error(403, "reauthentication_required", "Verify your password to continue.")
        return user, session, memberships

    def resolve_session(self, token: str | None) -> Identity | None:
        if not _valid_token(token):
            return None
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT user_id FROM staff_identity.sessions WHERE token_hash = %s",
                (_digest(token),),
            ).fetchone()
            if not row:
                return None
            user = self._lock_user(connection, row["user_id"])
            if not user:
                return None
            session = connection.execute(
                "UPDATE staff_identity.sessions SET last_seen_at = clock_timestamp() "
                "WHERE token_hash = %s AND revoked_at IS NULL AND expires_at > clock_timestamp() "
                "AND last_seen_at > clock_timestamp() - interval '30 minutes' RETURNING *",
                (_digest(token),),
            ).fetchone()
            if not session:
                return None
            membership = next(
                (
                    m
                    for m in self._memberships(connection, user["id"])
                    if m["id"] == session["tenant_id"]
                ),
                None,
            )
            if not membership:
                return None
            return self._identity(user, session, membership["role"])

    def session_dto(self, identity: Identity | None) -> dict[str, Any]:
        if identity is None:
            return {"enabled": True, "authenticated": False}
        with self._transaction() as connection:
            user, session, memberships = self._require_session(connection, identity)
            selected = next(m for m in memberships if m["id"] == session["tenant_id"])
            count = connection.execute(
                "SELECT count(*) AS count FROM staff_identity.credentials WHERE user_id = %s",
                (identity.user_id,),
            ).fetchone()["count"]
            return {
                "enabled": True,
                "authenticated": True,
                "user": {
                    "id": str(user["id"]),
                    "username": user["username"],
                    "display_name": user["display_name"],
                },
                "tenants": memberships,
                "tenant": selected,
                "auth_method": session["auth_method"],
                "passkey_count": count,
            }

    def _new_session(
        self,
        connection,
        user,
        memberships: list[dict],
        method: str,
        *,
        tenant_id: str | None = None,
        old_token: str | None = None,
        previous: dict | None = None,
    ) -> tuple[Identity, str]:
        membership = next((m for m in memberships if m["id"] == tenant_id), None)
        if not membership:
            membership = memberships[0] if tenant_id is None and memberships else None
        if not membership:
            raise _invalid_login()
        if _valid_token(old_token):
            connection.execute(
                "UPDATE staff_identity.sessions SET revoked_at = clock_timestamp() "
                "WHERE token_hash = %s",
                (_digest(old_token),),
            )
        if previous:
            connection.execute(
                "UPDATE staff_identity.sessions SET revoked_at = clock_timestamp() WHERE id = %s",
                (previous["id"],),
            )
        connection.execute(
            "DELETE FROM staff_identity.sessions WHERE user_id = %s AND "
            "(revoked_at IS NOT NULL OR expires_at <= clock_timestamp() "
            "OR last_seen_at <= clock_timestamp() - interval '30 minutes')",
            (user["id"],),
        )
        connection.execute(
            "DELETE FROM staff_identity.sessions WHERE id IN "
            "(SELECT id FROM staff_identity.sessions WHERE user_id = %s "
            "ORDER BY created_at DESC, id OFFSET 19)",
            (user["id"],),
        )
        token = secrets.token_urlsafe(32)
        session = connection.execute(
            "INSERT INTO staff_identity.sessions(id, token_hash, user_id, tenant_id, auth_method) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING *",
            (uuid4(), _digest(token), user["id"], membership["id"], method),
        ).fetchone()
        if previous:
            session = connection.execute(
                "UPDATE staff_identity.sessions SET authenticated_at = %s, recent_auth_at = %s, "
                "expires_at = %s WHERE id = %s RETURNING *",
                (
                    previous["authenticated_at"],
                    previous["recent_auth_at"],
                    previous["expires_at"],
                    session["id"],
                ),
            ).fetchone()
        return self._identity(user, session, membership["role"]), token

    def login(
        self, username: str, password: str, source: str, *, old_token: str | None = None
    ) -> tuple[Identity, str]:
        self._password_rate(username, source)
        failure = False
        result = None
        with self._transaction() as connection:
            user = connection.execute(
                "SELECT * FROM staff_identity.users WHERE username = %s",
                (_username(username),),
            ).fetchone()
            if user:
                user = self._lock_login_user(connection, user["id"], old_token)
            matches = _password_matches(user["password_hash"] if user else DUMMY_HASH, password)
            memberships = self._memberships(connection, user["id"]) if user else []
            if not user or not matches or not user["enabled"] or not memberships:
                self._audit(
                    connection,
                    "password_login_failed",
                    user["id"] if user else None,
                    actor="anonymous",
                )
                failure = True
            else:
                if PASSWORD_HASHER.check_needs_rehash(user["password_hash"]):
                    connection.execute(
                        "UPDATE staff_identity.users SET password_hash = %s WHERE id = %s",
                        (_hash_password(password), user["id"]),
                    )
                connection.execute(
                    "UPDATE staff_identity.users SET last_login_at = clock_timestamp() "
                    "WHERE id = %s",
                    (user["id"],),
                )
                result = self._new_session(
                    connection,
                    user,
                    memberships,
                    "password",
                    old_token=old_token,
                )
                self._audit(connection, "password_login", user["id"])
        if failure:
            raise _invalid_login()
        return result

    def logout(self, identity: Identity) -> None:
        with self._transaction() as connection:
            self._lock_user(connection, identity.user_id)
            connection.execute(
                "UPDATE staff_identity.sessions SET revoked_at = clock_timestamp() "
                "WHERE id = %s AND user_id = %s",
                (identity.session_id, identity.user_id),
            )
            connection.execute(
                "DELETE FROM staff_identity.challenges WHERE session_id = %s",
                (identity.session_id,),
            )
            self._audit(connection, "logout", identity.user_id)

    def change_tenant(self, identity: Identity, tenant_id: str) -> tuple[Identity, str]:
        with self._transaction() as connection:
            user, session, memberships = self._require_session(connection, identity)
            if not any(m["id"] == tenant_id for m in memberships):
                raise _error(403, "tenant_denied", "This account cannot access that tenant.")
            result = self._new_session(
                connection,
                user,
                memberships,
                session["auth_method"],
                tenant_id=tenant_id,
                previous=session,
            )
            self._audit(connection, "tenant_changed", user["id"])
            return result

    @staticmethod
    def _replace_password(connection, user_id, new_password: str) -> None:
        connection.execute(
            "UPDATE staff_identity.users SET password_hash = %s, "
            "password_changed_at = clock_timestamp() WHERE id = %s",
            (_hash_password(new_password), user_id),
        )
        connection.execute(
            "UPDATE staff_identity.sessions SET revoked_at = clock_timestamp() WHERE user_id = %s",
            (user_id,),
        )
        connection.execute("DELETE FROM staff_identity.challenges WHERE user_id = %s", (user_id,))

    def change_password(
        self, identity: Identity, current_password: str, new_password: str, source: str
    ) -> tuple[Identity, str]:
        validate_new_password(new_password)
        self._password_rate(identity.username, source)
        with self._transaction() as connection:
            user, session, memberships = self._require_session(connection, identity)
            if not _password_matches(user["password_hash"], current_password):
                raise _invalid_login()
            self._replace_password(connection, user["id"], new_password)
            result = self._new_session(
                connection,
                user,
                memberships,
                "password",
                tenant_id=session["tenant_id"],
            )
            self._audit(connection, "password_changed", user["id"])
            return result

    def reauthenticate(self, identity: Identity, password: str, source: str) -> None:
        self._password_rate(identity.username, source)
        with self._transaction() as connection:
            user, _, _ = self._require_session(connection, identity)
            if not _password_matches(user["password_hash"], password):
                raise _invalid_login()
            connection.execute(
                "UPDATE staff_identity.sessions SET recent_auth_at = clock_timestamp() "
                "WHERE id = %s",
                (identity.session_id,),
            )
            self._audit(connection, "reauthenticated", user["id"])

    def list_passkeys(self, identity: Identity) -> list[dict]:
        with self._transaction() as connection:
            self._require_session(connection, identity)
            rows = connection.execute(
                "SELECT id, name, created_at, last_used_at, backed_up, device_type "
                "FROM staff_identity.credentials WHERE user_id = %s ORDER BY created_at, id",
                (identity.user_id,),
            ).fetchall()
            for row in rows:
                row["id"] = str(row["id"])
            return rows

    def delete_passkey(self, identity: Identity, credential_id: str) -> None:
        try:
            key_id = UUID(credential_id)
        except ValueError:
            raise _error(404, "passkey_not_found", "The passkey does not exist.") from None
        with self._transaction() as connection:
            self._require_session(connection, identity, recent=True)
            row = connection.execute(
                "DELETE FROM staff_identity.credentials "
                "WHERE id = %s AND user_id = %s RETURNING id",
                (key_id, identity.user_id),
            ).fetchone()
            if not row:
                raise _error(404, "passkey_not_found", "The passkey does not exist.")
            self._audit(connection, "passkey_removed", identity.user_id, credential_id=key_id)

    def registration_options(self, identity: Identity, settings) -> dict:
        with self._transaction() as connection:
            user, _, _ = self._require_session(connection, identity, recent=True)
            credentials = connection.execute(
                "SELECT credential_id FROM staff_identity.credentials WHERE user_id = %s",
                (identity.user_id,),
            ).fetchall()
            if len(credentials) >= MAX_CREDENTIALS:
                raise _error(409, "passkey_limit", "Remove a passkey before adding another.")
            challenge = secrets.token_bytes(32)
            connection.execute(
                "DELETE FROM staff_identity.challenges WHERE session_id = %s",
                (identity.session_id,),
            )
            connection.execute(
                "INSERT INTO staff_identity.challenges"
                "(id, purpose, challenge, user_id, session_id) "
                "VALUES (%s, 'registration', %s, %s, %s)",
                (uuid4(), challenge, user["id"], identity.session_id),
            )
            options = generate_registration_options(
                rp_id=settings.rp_id,
                rp_name=settings.rp_name,
                user_id=user["id"].bytes,
                user_name=user["username"],
                user_display_name=user["display_name"],
                challenge=challenge,
                timeout=300000,
                attestation=AttestationConveyancePreference.NONE,
                authenticator_selection=AuthenticatorSelectionCriteria(
                    resident_key=ResidentKeyRequirement.REQUIRED,
                    require_resident_key=True,
                    user_verification=UserVerificationRequirement.REQUIRED,
                ),
                exclude_credentials=[
                    PublicKeyCredentialDescriptor(id=r["credential_id"]) for r in credentials
                ],
            )
            return json.loads(options_to_json(options))

    def _consume_challenge(
        self, *, identity: Identity | None = None, flow_token: str | None = None
    ) -> dict:
        # Separate transaction is essential: failed verification cannot roll back consumption.
        with self._transaction() as connection:
            if identity:
                row = connection.execute(
                    "DELETE FROM staff_identity.challenges WHERE purpose = 'registration' "
                    "AND session_id = %s AND user_id = %s "
                    "RETURNING *, expires_at > clock_timestamp() AS valid",
                    (identity.session_id, identity.user_id),
                ).fetchone()
            elif _valid_token(flow_token):
                row = connection.execute(
                    "DELETE FROM staff_identity.challenges WHERE purpose = 'authentication' "
                    "AND flow_hash = %s RETURNING *, expires_at > clock_timestamp() AS valid",
                    (_digest(flow_token),),
                ).fetchone()
            else:
                row = None
        if not row or not row["valid"]:
            raise _invalid_ceremony()
        return row

    def registration_verify(
        self, identity: Identity, credential: dict, name: str, settings
    ) -> None:
        challenge = self._consume_challenge(identity=identity)
        _text(name, 80, "Passkey name")
        try:
            parsed = parse_registration_credential_json(credential)
            _client_data(parsed.response.client_data_json)
            verified = verify_registration_response(
                credential=parsed,
                expected_challenge=challenge["challenge"],
                expected_origin=settings.origin,
                expected_rp_id=settings.rp_id,
                require_user_presence=True,
                require_user_verification=True,
            )
            if not 1 <= len(verified.credential_id) <= 1024 or (
                verified.credential_id != parsed.raw_id
            ):
                raise _invalid_ceremony()
        except (WebAuthnException, ValueError, TypeError, KeyError):
            raise _invalid_ceremony() from None
        try:
            with self._transaction() as connection:
                self._require_session(connection, identity, recent=True)
                count = connection.execute(
                    "SELECT count(*) AS count FROM staff_identity.credentials WHERE user_id = %s",
                    (identity.user_id,),
                ).fetchone()["count"]
                if count >= MAX_CREDENTIALS:
                    raise _error(409, "passkey_limit", "Remove a passkey before adding another.")
                key_id = uuid4()
                connection.execute(
                    "INSERT INTO staff_identity.credentials"
                    "(id, user_id, credential_id, public_key, sign_count, name, "
                    "device_type, backed_up) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        key_id,
                        identity.user_id,
                        verified.credential_id,
                        verified.credential_public_key,
                        verified.sign_count,
                        name,
                        verified.credential_device_type.value,
                        verified.credential_backed_up,
                    ),
                )
                self._audit(
                    connection, "passkey_registered", identity.user_id, credential_id=key_id
                )
        except psycopg.errors.UniqueViolation:
            raise _error(409, "passkey_exists", "This passkey is already registered.") from None

    def authentication_options(
        self, source: str, settings, *, old_flow_token: str | None = None
    ) -> tuple[dict, str]:
        self._rate_limit("anonymous", source, 120)
        flow_token, challenge = secrets.token_urlsafe(32), secrets.token_bytes(32)
        with self._transaction() as connection:
            # Short global lock caps anonymous outstanding state across all replicas.
            connection.execute("SELECT pg_advisory_xact_lock(731624091)")
            connection.execute(
                "DELETE FROM staff_identity.challenges WHERE expires_at <= clock_timestamp()"
            )
            if _valid_token(old_flow_token):
                connection.execute(
                    "DELETE FROM staff_identity.challenges WHERE flow_hash = %s",
                    (_digest(old_flow_token),),
                )
            count = connection.execute(
                "SELECT count(*) AS count FROM staff_identity.challenges "
                "WHERE purpose = 'authentication'"
            ).fetchone()["count"]
            if count >= MAX_CHALLENGES:
                raise _error(429, "rate_limited", "Too many attempts. Try again later.")
            connection.execute(
                "INSERT INTO staff_identity.challenges(id, purpose, challenge, flow_hash) "
                "VALUES (%s, 'authentication', %s, %s)",
                (uuid4(), challenge, _digest(flow_token)),
            )
        options = generate_authentication_options(
            rp_id=settings.rp_id,
            challenge=challenge,
            timeout=300000,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return json.loads(options_to_json(options)), flow_token

    def authentication_verify(
        self,
        credential: dict,
        flow_token: str | None,
        source: str,
        settings,
        *,
        old_token: str | None = None,
    ) -> tuple[Identity, str]:
        self._rate_limit("anonymous", source, 120)
        challenge = self._consume_challenge(flow_token=flow_token)
        try:
            parsed = parse_authentication_credential_json(credential)
            _client_data(parsed.response.client_data_json)
            with self._transaction() as connection:
                owner = connection.execute(
                    "SELECT user_id FROM staff_identity.credentials WHERE credential_id = %s",
                    (parsed.raw_id,),
                ).fetchone()
                if not owner:
                    raise _invalid_ceremony()
                user = self._lock_login_user(connection, owner["user_id"], old_token)
                stored = connection.execute(
                    "SELECT * FROM staff_identity.credentials WHERE credential_id = %s FOR UPDATE",
                    (parsed.raw_id,),
                ).fetchone()
                if (
                    not user
                    or not user["enabled"]
                    or not stored
                    or parsed.response.user_handle != user["id"].bytes
                ):
                    raise _invalid_ceremony()
                verified = verify_authentication_response(
                    credential=parsed,
                    expected_challenge=challenge["challenge"],
                    expected_origin=settings.origin,
                    expected_rp_id=settings.rp_id,
                    credential_public_key=stored["public_key"],
                    credential_current_sign_count=stored["sign_count"],
                    require_user_verification=True,
                )
                if verified.credential_device_type.value != stored["device_type"]:
                    raise _invalid_ceremony()
                connection.execute(
                    "UPDATE staff_identity.credentials SET sign_count = %s, backed_up = %s, "
                    "last_used_at = clock_timestamp() WHERE id = %s",
                    (verified.new_sign_count, verified.credential_backed_up, stored["id"]),
                )
                result = self._new_session(
                    connection,
                    user,
                    self._memberships(connection, user["id"]),
                    "passkey",
                    old_token=old_token,
                )
                connection.execute(
                    "UPDATE staff_identity.users SET last_login_at = clock_timestamp() "
                    "WHERE id = %s",
                    (user["id"],),
                )
                self._audit(connection, "passkey_login", user["id"], credential_id=stored["id"])
                return result
        except StoreError as error:
            if error.code not in {"invalid_passkey", "invalid_credentials"}:
                raise
            with self._transaction() as connection:
                self._audit(connection, "passkey_login_failed", None, actor="anonymous")
            raise _invalid_ceremony() from None
        except (WebAuthnException, ValueError, TypeError, KeyError):
            with self._transaction() as connection:
                self._audit(connection, "passkey_login_failed", None, actor="anonymous")
            raise _invalid_ceremony() from None
