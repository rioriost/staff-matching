"""Opt-in PostgreSQL/ACL/race tests; never substitute SQLite.

TEST_IDENTITY_POSTGRES_DSN: admin connection to an explicitly prepared test database.
TEST_IDENTITY_RUNTIME_DSN: restricted staff_app connection to that same database.
Prepare this isolated test database with db/identity/001_identity.sql first. The
migration-rerun test explicitly reapplies that DDL to verify data preservation.
Tests delete only their own accounts; fixed rate buckets are deliberately not reset.
"""

import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from test_identity import PASSWORD, SETTINGS, Authenticator, unb64

from backend.identity import IdentityStore
from backend.identity_store import _digest
from backend.store import StoreError

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_IDENTITY_POSTGRES_DSN"),
    reason="TEST_IDENTITY_POSTGRES_DSN not configured; no live identity PostgreSQL claim",
)


@pytest.fixture
def accounts():
    runtime_dsn = os.getenv("TEST_IDENTITY_RUNTIME_DSN")
    if not runtime_dsn:
        pytest.fail("Live identity tests require a restricted TEST_IDENTITY_RUNTIME_DSN.")
    admin_dsn = os.environ["TEST_IDENTITY_POSTGRES_DSN"]
    administrator, runtime = IdentityStore(admin_dsn), IdentityStore(runtime_dsn)
    runtime.initialize()
    prefix = "identity-test-" + uuid4().hex
    tenant_a, tenant_b = prefix + "-a", prefix + "-b"
    username, other_username = prefix + "-user", prefix + "-other"
    user_id = administrator.provision_user(username, "Test account", PASSWORD, tenant_a, "Tenant A")
    administrator.provision_user(username, "Ignored", PASSWORD, tenant_b, "Tenant B", "viewer")
    other_id = administrator.provision_user(
        other_username,
        "Other account",
        PASSWORD,
        tenant_b,
        "Tenant B",
    )
    with psycopg.connect(admin_dsn, row_factory=dict_row, autocommit=True) as connection:
        try:
            yield {
                "admin": administrator,
                "runtime": runtime,
                "connection": connection,
                "username": username,
                "other_username": other_username,
                "user_id": user_id,
                "other_id": other_id,
                "tenant_a": tenant_a,
                "tenant_b": tenant_b,
                "source": uuid4().hex,
            }
        finally:
            with connection.transaction():
                user_ids = [UUID(user_id), UUID(other_id)]
                for table in (
                    "audit_events",
                    "challenges",
                    "credentials",
                    "sessions",
                    "memberships",
                ):
                    connection.execute(
                        f"DELETE FROM staff_identity.{table} WHERE user_id = ANY(%s)",
                        (user_ids,),
                    )
                connection.execute(
                    "DELETE FROM staff_identity.users WHERE id = ANY(%s)",
                    (user_ids,),
                )
                connection.execute(
                    "DELETE FROM staff_identity.tenants WHERE id = ANY(%s)",
                    ([tenant_a, tenant_b],),
                )


def login(accounts, *, other=False):
    return accounts["runtime"].login(
        accounts["other_username"] if other else accounts["username"],
        PASSWORD,
        accounts["source"],
    )


def enroll(accounts):
    store = accounts["runtime"]
    identity, token = login(accounts)
    authenticator = Authenticator()
    options = store.registration_options(identity, SETTINGS)
    assert options["attestation"] == "none"
    assert options["authenticatorSelection"]["residentKey"] == "required"
    assert options["authenticatorSelection"]["userVerification"] == "required"
    assert unb64(options["user"]["id"]) == UUID(identity.user_id).bytes
    store.registration_verify(
        identity,
        authenticator.registration(unb64(options["challenge"])),
        "Test key",
        SETTINGS,
    )
    return identity, token, authenticator


def test_runtime_acl_and_independent_ledger(accounts):
    store = accounts["runtime"]
    with pytest.raises(RuntimeError, match="restricted"):
        accounts["admin"].initialize()
    with pytest.raises(StoreError) as caught:
        store.provision_user(
            "forbidden-" + uuid4().hex,
            "Forbidden",
            PASSWORD,
            accounts["tenant_a"],
            "A",
        )
    assert caught.value.code == "admin_required"
    with pytest.raises(StoreError):
        store.recover_password(accounts["username"], "new recovery password")
    with store._transaction() as connection:
        assert (
            connection.execute(
                "SELECT max(version) AS version FROM staff_identity.schema_migrations"
            ).fetchone()["version"]
            == 1
        )
        for statement, args in (
            (
                "UPDATE staff_identity.users SET enabled = false WHERE id = %s",
                (accounts["user_id"],),
            ),
            ("DELETE FROM staff_identity.memberships WHERE user_id = %s", (accounts["user_id"],)),
            (
                "INSERT INTO staff_identity.tenants(id, name) VALUES (%s, 'forbidden')",
                ("forbidden-" + uuid4().hex,),
            ),
            ("SELECT * FROM staff_identity.audit_events", ()),
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with connection.transaction():
                    connection.execute(statement, args)


def test_provision_is_idempotent_and_preserves_password_memberships_and_keys(accounts):
    identity, _, _ = enroll(accounts)
    administrator = accounts["admin"]
    original = (
        accounts["connection"]
        .execute(
            "SELECT password_hash FROM staff_identity.users WHERE id = %s",
            (identity.user_id,),
        )
        .fetchone()["password_hash"]
    )
    assert (
        administrator.provision_user(
            accounts["username"].upper(),
            "Changed",
            "A different password!",
            accounts["tenant_a"],
            "Different name",
            "viewer",
        )
        == identity.user_id
    )
    row = (
        accounts["connection"]
        .execute(
            "SELECT password_hash, display_name FROM staff_identity.users WHERE id = %s",
            (identity.user_id,),
        )
        .fetchone()
    )
    assert row["password_hash"] == original and row["display_name"] == "Test account"
    dto = accounts["runtime"].session_dto(identity)
    assert dto["passkey_count"] == 1 and dto["tenant"]["role"] == "coordinator"
    assert len(dto["tenants"]) == 2


def test_migration_reruns_preserve_credentials_sessions_and_ledger(accounts):
    identity, token, _ = enroll(accounts)
    connection, store = accounts["connection"], accounts["runtime"]
    before = connection.execute(
        "SELECT applied_at FROM staff_identity.schema_migrations WHERE version = 1"
    ).fetchone()["applied_at"]
    password_hash = connection.execute(
        "SELECT password_hash FROM staff_identity.users WHERE id = %s",
        (identity.user_id,),
    ).fetchone()["password_hash"]
    migration = (Path(__file__).resolve().parents[1] / "db/identity/001_identity.sql").read_text()
    for _ in range(2):
        with connection.transaction():
            connection.execute("SELECT pg_advisory_xact_lock(731624092)")
            connection.execute(migration)
    store.initialize()
    assert store.resolve_session(token) == identity
    assert store.session_dto(identity)["passkey_count"] == 1
    assert (
        connection.execute(
            "SELECT applied_at FROM staff_identity.schema_migrations WHERE version = 1"
        ).fetchone()["applied_at"]
        == before
    )
    assert (
        connection.execute(
            "SELECT password_hash FROM staff_identity.users WHERE id = %s",
            (identity.user_id,),
        ).fetchone()["password_hash"]
        == password_hash
    )


def test_sessions_are_hashed_rotated_and_revoked_on_logout(accounts):
    store = accounts["runtime"]
    identity, token = login(accounts)
    row = (
        accounts["connection"]
        .execute(
            "SELECT * FROM staff_identity.sessions WHERE id = %s",
            (identity.session_id,),
        )
        .fetchone()
    )
    assert row["token_hash"] == _digest(token)
    assert token not in repr(row) and PASSWORD not in repr(row)
    assert store.resolve_session(token) == identity
    new_identity, new_token = store.login(
        accounts["username"],
        PASSWORD,
        accounts["source"],
        old_token=token,
    )
    assert new_token != token and new_identity.session_id != identity.session_id
    assert store.resolve_session(token) is None
    store.logout(new_identity)
    assert store.resolve_session(new_token) is None


def test_membership_selection_and_disabled_accounts_are_authoritative(accounts):
    store, connection = accounts["runtime"], accounts["connection"]
    identity, token = login(accounts)
    with pytest.raises(StoreError) as caught:
        store.change_tenant(identity, "client-invented-tenant")
    assert caught.value.code == "tenant_denied"
    switched, new_token = store.change_tenant(identity, accounts["tenant_b"])
    assert switched.role == "viewer" and switched.tenant_id == accounts["tenant_b"]
    assert switched.authenticated_at == identity.authenticated_at
    assert store.resolve_session(token) is None
    connection.execute(
        "UPDATE staff_identity.memberships SET enabled = false "
        "WHERE user_id = %s AND tenant_id = %s",
        (identity.user_id, accounts["tenant_b"]),
    )
    assert store.resolve_session(new_token) is None
    fresh, fresh_token = login(accounts)
    connection.execute(
        "UPDATE staff_identity.users SET enabled = false WHERE id = %s", (fresh.user_id,)
    )
    assert store.resolve_session(fresh_token) is None
    for username in (accounts["username"], "unknown-" + uuid4().hex):
        with pytest.raises(StoreError) as caught:
            store.login(username, PASSWORD, accounts["source"])
        assert caught.value.code == "invalid_credentials"


@pytest.mark.parametrize("expiry", ["absolute", "idle"])
def test_absolute_and_idle_expiry_are_server_enforced(accounts, expiry):
    identity, token = login(accounts)
    expression = (
        "expires_at = clock_timestamp() - interval '1 second'"
        if expiry == "absolute"
        else "last_seen_at = clock_timestamp() - interval '31 minutes'"
    )
    accounts["connection"].execute(
        "UPDATE staff_identity.sessions SET " + expression + " WHERE id = %s",
        (identity.session_id,),
    )
    assert accounts["runtime"].resolve_session(token) is None
    with pytest.raises(StoreError):
        accounts["runtime"].session_dto(identity)


def test_recent_authentication_and_password_rotation(accounts):
    store, connection = accounts["runtime"], accounts["connection"]
    identity, token = login(accounts)
    _, other_token = login(accounts)
    connection.execute(
        "UPDATE staff_identity.sessions SET recent_auth_at = "
        "clock_timestamp() - interval '6 minutes' "
        "WHERE id = %s",
        (identity.session_id,),
    )
    with pytest.raises(StoreError) as caught:
        store.registration_options(identity, SETTINGS)
    assert caught.value.code == "reauthentication_required"
    switched, switched_token = store.change_tenant(identity, accounts["tenant_b"])
    with pytest.raises(StoreError):
        store.registration_options(switched, SETTINGS)
    store.reauthenticate(switched, PASSWORD, accounts["source"])
    assert store.registration_options(switched, SETTINGS)["challenge"]
    new_identity, new_token = store.change_password(
        switched,
        PASSWORD,
        "brand new password with spaces ",
        accounts["source"],
    )
    for old in (token, other_token, switched_token):
        assert store.resolve_session(old) is None
    assert store.resolve_session(new_token) == new_identity
    with pytest.raises(StoreError):
        store.login(accounts["username"], PASSWORD, accounts["source"])


@pytest.mark.parametrize("revoke_keys", [False, True])
def test_admin_recovery_revokes_sessions_and_explicitly_controls_keys(accounts, revoke_keys):
    identity, token, _ = enroll(accounts)
    store = accounts["runtime"]
    store.registration_options(identity, SETTINGS)
    accounts["admin"].recover_password(
        accounts["username"],
        "a completely new recovery password",
        revoke_passkeys=revoke_keys,
        actor="pytest-admin",
    )
    assert store.resolve_session(token) is None
    assert (
        accounts["connection"]
        .execute(
            "SELECT count(*) AS count FROM staff_identity.challenges WHERE user_id = %s",
            (identity.user_id,),
        )
        .fetchone()["count"]
        == 0
    )
    assert accounts["connection"].execute(
        "SELECT count(*) AS count FROM staff_identity.credentials WHERE user_id = %s",
        (identity.user_id,),
    ).fetchone()["count"] == (0 if revoke_keys else 1)
    event = (
        accounts["connection"]
        .execute(
            "SELECT event, actor FROM staff_identity.audit_events WHERE user_id = %s "
            "ORDER BY created_at DESC LIMIT 1",
            (identity.user_id,),
        )
        .fetchone()
    )
    assert event["event"] == (
        "password_recovery_with_keys_revoked" if revoke_keys else "password_recovery"
    )
    assert event["actor"] == "pytest-admin"


def test_real_signed_passkey_login_duplicate_enrollment_and_own_key_deletion(accounts):
    identity, token, authenticator = enroll(accounts)
    store = accounts["runtime"]
    for _ in range(2):
        options, flow = store.authentication_options(accounts["source"], SETTINGS)
        assert not options.get("allowCredentials")
        assert options["userVerification"] == "required"
        identity, token = store.authentication_verify(
            authenticator.assertion(unb64(options["challenge"]), identity.user_id),
            flow,
            accounts["source"],
            SETTINGS,
            old_token=token,
        )
        assert store.session_dto(identity)["auth_method"] == "passkey"
    duplicate_options = store.registration_options(identity, SETTINGS)
    with pytest.raises(StoreError) as caught:
        store.registration_verify(
            identity,
            authenticator.registration(unb64(duplicate_options["challenge"])),
            "Duplicate",
            SETTINGS,
        )
    assert caught.value.code == "passkey_exists"
    key = store.list_passkeys(identity)[0]
    other, _ = login(accounts, other=True)
    with pytest.raises(StoreError) as caught:
        store.delete_passkey(other, key["id"])
    assert caught.value.code == "passkey_not_found"
    store.delete_passkey(identity, key["id"])
    assert store.list_passkeys(identity) == []
    assert login(accounts)[0].auth_method == "password"


def test_expired_failed_and_cross_browser_challenges_do_not_replay(accounts):
    identity, _, authenticator = enroll(accounts)
    store, connection = accounts["runtime"], accounts["connection"]
    options, flow = store.authentication_options(accounts["source"], SETTINGS)
    assertion = authenticator.assertion(unb64(options["challenge"]), identity.user_id)
    with pytest.raises(StoreError):
        store.authentication_verify(
            assertion, secrets.token_urlsafe(32), accounts["source"], SETTINGS
        )
    connection.execute(
        "UPDATE staff_identity.challenges SET expires_at = clock_timestamp() - interval '1 second' "
        "WHERE flow_hash = %s",
        (_digest(flow),),
    )
    with pytest.raises(StoreError):
        store.authentication_verify(assertion, flow, accounts["source"], SETTINGS)
    assert (
        connection.execute(
            "SELECT count(*) AS count FROM staff_identity.challenges WHERE flow_hash = %s",
            (_digest(flow),),
        ).fetchone()["count"]
        == 0
    )
    options, flow = store.authentication_options(accounts["source"], SETTINGS)
    assertion = authenticator.assertion(
        unb64(options["challenge"]),
        identity.user_id,
        origin="https://invalid.example.test",
    )
    with pytest.raises(StoreError):
        store.authentication_verify(assertion, flow, accounts["source"], SETTINGS)
    with pytest.raises(StoreError):
        store.authentication_verify(
            authenticator.assertion(unb64(options["challenge"]), identity.user_id),
            flow,
            accounts["source"],
            SETTINGS,
        )


def test_replicas_only_consume_challenge_once(accounts):
    identity, _, authenticator = enroll(accounts)
    store = accounts["runtime"]
    options, flow = store.authentication_options(accounts["source"], SETTINGS)
    assertion = authenticator.assertion(unb64(options["challenge"]), identity.user_id)

    def verify_once(_):
        try:
            IdentityStore(store._conninfo).authentication_verify(
                assertion,
                flow,
                accounts["source"],
                SETTINGS,
            )
        except StoreError as error:
            return error.code
        return "success"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(verify_once, range(2)))
    assert sorted(outcomes) == ["invalid_passkey", "success"]


def test_simultaneous_cross_account_logins_do_not_deadlock(accounts):
    store = accounts["runtime"]
    _, token_a = login(accounts)
    _, token_b = login(accounts, other=True)

    def rotate(pair):
        username, old_token = pair
        return IdentityStore(store._conninfo).login(
            username,
            PASSWORD,
            accounts["source"],
            old_token=old_token,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                rotate,
                [
                    (accounts["username"], token_b),
                    (accounts["other_username"], token_a),
                ],
            )
        )
    assert len(results) == 2
    assert store.resolve_session(token_a) is None and store.resolve_session(token_b) is None
    assert all(store.resolve_session(token) is not None for _, token in results)


def test_replicas_serialize_nonzero_credential_counter(accounts):
    identity, _, authenticator = enroll(accounts)
    store = accounts["runtime"]
    ceremonies = [store.authentication_options(accounts["source"], SETTINGS) for _ in range(2)]

    def verify_once(ceremony):
        options, flow = ceremony
        try:
            IdentityStore(store._conninfo).authentication_verify(
                authenticator.assertion(unb64(options["challenge"]), identity.user_id, count=1),
                flow,
                accounts["source"],
                SETTINGS,
            )
        except StoreError as error:
            return error.code
        return "success"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(verify_once, ceremonies))
    assert sorted(outcomes) == ["invalid_passkey", "success"]
    assert (
        accounts["connection"]
        .execute(
            "SELECT sign_count FROM staff_identity.credentials WHERE user_id = %s",
            (identity.user_id,),
        )
        .fetchone()["sign_count"]
        == 1
    )


def test_rate_limit_is_shared_atomic_and_bounded(accounts):
    store = accounts["runtime"]
    key = uuid4().hex

    def attempt(_):
        try:
            IdentityStore(store._conninfo)._rate_limit("password_account", key, 5)
        except StoreError as error:
            return error.code
        return "success"

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(attempt, range(12)))
    assert outcomes.count("success") == 5 and outcomes.count("rate_limited") == 7
    bucket = int.from_bytes(_digest("password_account:" + key)[:4], "big") % 4096
    assert (
        accounts["connection"]
        .execute(
            "SELECT attempts FROM staff_identity.rate_limits WHERE kind = %s AND bucket = %s",
            ("password_account", bucket),
        )
        .fetchone()["attempts"]
        >= 12
    )
