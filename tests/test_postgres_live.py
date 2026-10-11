"""Opt-in real-server tests.

TEST_POSTGRES_DSN is an admin DSN for an already bootstrapped database.
POC_APP_DB_PASSWORD supplies the restricted staff_app password. Each test adds
a separate synthetic tenant; no demo records or existing tenant data are deleted.
"""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.engine import optimize
from backend.models import ConfirmRequest, OptimizeRequest, RespondRequest
from backend.postgres_setup import runtime_conninfo, seed_tenant, verify
from backend.postgres_store import PostgresStore
from backend.seed import demo_snapshot
from backend.store import StoreError

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_DSN"), reason="TEST_POSTGRES_DSN not configured; no live PG claim"
)


@pytest.fixture
def admin():
    if not os.getenv("POC_APP_DB_PASSWORD"):
        pytest.fail("Real PG tests require POC_APP_DB_PASSWORD for the restricted runtime role.")
    with psycopg.connect(os.environ["TEST_POSTGRES_DSN"], row_factory=dict_row) as connection:
        yield connection


@pytest.fixture
def store(admin):
    tenant = "pytest-" + uuid4().hex
    seed_tenant(admin, demo_snapshot().model_copy(update={"tenant_id": tenant}))
    admin.commit()
    instance = PostgresStore(runtime_conninfo(admin, os.environ["POC_APP_DB_PASSWORD"]))
    instance.initialize()
    return instance, tenant


def completed(store, tenant):
    accepted, _ = store.create_run(tenant, OptimizeRequest(idempotency_key=uuid4().hex))
    snapshot, config = store.start_run(tenant, accepted.run_id)
    assert store.publish_run(
        tenant, accepted.run_id, optimize(snapshot, time_limit_seconds=config.time_limit_seconds)
    )
    return store.get_run(tenant, accepted.run_id)


def test_cloud_verification_real_extensions_rls_and_concurrent_lifecycle(admin):
    verify(admin, os.environ["POC_APP_DB_PASSWORD"])


@pytest.mark.parametrize("kind", ["staff", "order", "evidence"])
def test_real_pg_input_versions_block_confirmation(store, kind):
    instance, tenant = store
    proposal = completed(instance, tenant).proposals[0]
    consented = instance.respond(
        tenant,
        proposal.id,
        RespondRequest(
            decision="consent", expected_version=proposal.version, idempotency_key=uuid4().hex
        ),
    )
    snapshot = instance.snapshot(tenant)
    if kind == "staff":
        item = next(item for item in snapshot.staff if item.id == proposal.staff_id)
    elif kind == "order":
        item = next(item for item in snapshot.orders if item.id == proposal.order_id)
    else:
        item = next(
            item
            for item in snapshot.evidence
            if (item.staff_id, item.order_id) == (proposal.staff_id, proposal.order_id)
        )
    instance.update_domain(tenant, item.model_copy(update={"version": item.version + 1}))
    with pytest.raises(StoreError) as caught:
        instance.confirm(
            tenant,
            consented.id,
            ConfirmRequest(expected_version=consented.version, idempotency_key=uuid4().hex),
        )
    assert caught.value.code == "stale_input"
    assert len(instance.snapshot(tenant).assignments) == 1


def test_real_pg_expiry_commits_when_action_is_rejected(store):
    instance, tenant = store
    now = datetime(2026, 10, 5, tzinfo=UTC)
    instance.clock = lambda: now
    proposal = completed(instance, tenant).proposals[0]
    proposal = instance.respond(
        tenant,
        proposal.id,
        RespondRequest(
            decision="consent", expected_version=proposal.version, idempotency_key=uuid4().hex
        ),
    )
    before = instance.snapshot(tenant).revision
    now += timedelta(minutes=31)
    with pytest.raises(StoreError):
        instance.confirm(
            tenant,
            proposal.id,
            ConfirmRequest(expected_version=proposal.version, idempotency_key=uuid4().hex),
        )
    assert instance.get_proposal(tenant, proposal.id).status == "expired"
    assert instance.snapshot(tenant).revision == before + 1


def test_real_pg_owner_and_publication_revision_guards(store):
    instance, tenant = store
    accepted, _ = instance.create_run(tenant, OptimizeRequest(idempotency_key=uuid4().hex))
    other = PostgresStore(instance._conninfo)
    other.recover_interrupted_runs(tenant)
    with pytest.raises(StoreError) as caught:
        other.start_run(tenant, accepted.run_id)
    assert caught.value.code == "run_owner"
    snapshot, _ = instance.start_run(tenant, accepted.run_id)
    instance.update_domain(tenant, snapshot.staff[0].model_copy(update={"version": 2}))
    assert not instance.publish_run(tenant, accepted.run_id, optimize(snapshot))
    assert instance.get_run(tenant, accepted.run_id).status == "failed"
    assert instance.get_run(tenant, accepted.run_id).proposals == []


def test_real_pg_composite_foreign_keys_and_rls_reject_cross_tenant_writes(store):
    instance, tenant = store
    with instance._transaction(tenant) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with connection.transaction():
                connection.execute(
                    "UPDATE staff_matching.staff SET tenant_id = 'demo' "
                    "WHERE tenant_id = %s AND id = 's01'",
                    (tenant,),
                )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with connection.transaction():
                connection.execute(
                    "INSERT INTO staff_matching.proposals"
                    "(tenant_id, id, run_id, staff_id, order_id, "
                    "status, version, input_revision, staff_version, order_version, "
                    "evidence_version, "
                    "expires_at) VALUES (%s, 'invalid-foreign-run', 'absent-run', 's01', 'o01', "
                    "'proposed', 1, 1, 1, 1, 1, clock_timestamp() + interval '30 minutes')",
                    (tenant,),
                )


def test_real_pg_restart_recovers_running_lease_only_when_expired(store, admin):
    instance, tenant = store
    accepted, _ = instance.create_run(tenant, OptimizeRequest(idempotency_key=uuid4().hex))
    instance.start_run(tenant, accepted.run_id)
    other = PostgresStore(instance._conninfo)
    other.recover_interrupted_runs(tenant)
    assert other.get_run(tenant, accepted.run_id).status == "running"
    with admin.transaction():
        PostgresStore._set_context(admin, tenant)
        admin.execute(
            "UPDATE staff_matching.runs "
            "SET lease_expires_at = clock_timestamp() - interval '1 second' "
            "WHERE tenant_id = %s AND id = %s",
            (tenant, accepted.run_id),
        )
    admin.commit()
    assert other.list_runs(tenant)[0].status == "failed"
