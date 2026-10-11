"""Opt-in real PostgreSQL checks against a disposable acceptance database.

Requires SAAS_TEST_ADMIN_DSN, SAAS_TEST_APP_DSN and SAAS_TEST_WORKER_DSN.
Roles/extensions must exist; tests apply only db/saas/001_saas.sql. Every test
uses unique tenants and removes only its own synthetic records. No identity or
legacy migration/table is touched. Routes are deterministic unit routes here;
the parent geographic acceptance tests validate the real network separately.
"""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from test_saas_domain import commute, fixture_dataset

from backend.saas_models import (
    AppointmentRequest,
    MatchRequest,
    ShortlistRequest,
    SimulationRequest,
)
from backend.saas_models import Versioned as Version
from backend.saas_store import SaasStore, grant_demo_simulation, load_dataset, tenant_context
from backend.saas_worker import LeaseLost, SaasWorker
from backend.store import StoreError

DSNS = [os.environ.get(f"SAAS_TEST_{role}_DSN") for role in ("ADMIN", "APP", "WORKER")]
pytestmark = pytest.mark.skipif(not all(DSNS), reason="Opt-in SaaS PostgreSQL DSNs not configured")


def key():
    return uuid4().hex


@pytest.fixture
def live(monkeypatch):
    from backend import saas_routing

    monkeypatch.setattr(saas_routing, "estimate_commute", lambda *_: commute())
    tenant_ids = ["saas-test-" + uuid4().hex for _ in range(2)]
    with psycopg.connect(DSNS[0]) as admin:
        admin.execute(Path("db/saas/001_saas.sql").read_text())
        for tenant in tenant_ids:
            dataset = fixture_dataset(tenant)
            dataset.people.append(dataset.people[0].model_copy(update={"id": "alternative"}))
            dataset.jobs.append(
                dataset.jobs[0].model_copy(
                    update={
                        "id": "dispatch",
                        "business_type": "dispatch",
                    }
                )
            )
            load_dataset(admin, dataset)
            grant_demo_simulation(admin, tenant, "actor")
    store, worker = SaasStore(DSNS[1]), SaasWorker(DSNS[2])
    store.initialize()
    worker.initialize()
    try:
        yield store, worker, tenant_ids
    finally:
        with psycopg.connect(DSNS[0]) as admin:
            for tenant in tenant_ids:
                tenant_context(admin, tenant)
                for table in (
                    "notifications",
                    "outbox",
                    "audit_events",
                    "idempotency",
                    "appointments",
                    "placements",
                    "introductions",
                    "candidates",
                    "runs",
                    "simulation_grants",
                    "jobs",
                    "people",
                    "sites",
                    "companies",
                    "dataset_manifests",
                    "tenants",
                    "dispatch",
                    "outbox_dispatch",
                ):
                    admin.execute(f"DELETE FROM staff_saas.{table} WHERE tenant_id=%s", (tenant,))


def completed(live, job_id="job"):
    store, worker, (tenant, _) = live
    run = store.create_run(tenant, "actor", MatchRequest(idempotency_key=key(), job_ids=[job_id]))
    claim = worker.claim()
    assert claim["tenant_id"] == tenant
    worker.process(claim)
    assert store.run(tenant, run.id).status == "completed"
    candidates = store.list_candidates(tenant, run.id, job_id=job_id).items
    assert len(candidates) == 2
    return run, candidates


def shortlisted(live, job_id="job"):
    store, _, (tenant, _) = live
    run, candidates = completed(live, job_id)
    candidate = next(c for c in candidates if not c.recommended)
    intro = store.shortlist(
        tenant,
        "actor",
        ShortlistRequest(
            run_id=run.id,
            job_id=job_id,
            person_id=candidate.person_id,
            expected_job_version=1,
            expected_person_version=1,
            idempotency_key=key(),
        ),
    )
    return intro


def simulate(store, tenant, intro, action):
    return store.simulate(
        tenant,
        "actor",
        intro.id,
        SimulationRequest(
            expected_version=intro.version,
            idempotency_key=key(),
            action=action,
            simulated=True,
        ),
        True,
    )


def test_rls_private_lists_cursor_and_reused_connection(live):
    store, _, (first, second) = live
    assert store.list_people(first).total == 2
    serialized = store.list_people(first).model_dump_json()
    assert "employment_history" not in serialized
    assert "private_address" not in serialized
    assert "contact_email" not in serialized
    page = store.list_people(first, limit=1)
    assert (
        store.list_people(first, limit=1, cursor=page.next_cursor).items[0].id != page.items[0].id
    )
    with pytest.raises(StoreError) as error:
        store.list_people(second, cursor=page.next_cursor)
    assert error.value.code == "invalid_cursor"
    with store.connect() as connection:
        for tenant in (first, second):
            with connection.transaction():
                tenant_context(connection, tenant)
                assert connection.execute(
                    "SELECT DISTINCT tenant_id FROM staff_saas.people",
                ).fetchall() == [{"tenant_id": tenant}]
        connection.commit()
        assert (
            connection.execute("SELECT count(*) AS n FROM staff_saas.people").fetchone()["n"] == 0
        )


def test_worker_lease_restart_fencing_cancellation_and_fairness(live):
    store, worker, (first, second) = live
    a = store.create_run(first, "actor", MatchRequest(idempotency_key=key(), job_ids=["job"]))
    b = store.create_run(second, "actor", MatchRequest(idempotency_key=key(), job_ids=["job"]))
    claim = worker.claim()
    assert claim["tenant_id"] == first
    worker._snapshot(claim)
    with psycopg.connect(DSNS[0]) as admin:
        admin.execute(
            "UPDATE staff_saas.dispatch SET lease_until=clock_timestamp()-interval '1 second' "
            "WHERE tenant_id=%s AND run_id=%s",
            (first, a.id),
        )
    next_claim = worker.claim()
    assert next_claim["tenant_id"] == second
    worker.process(next_claim)
    assert store.run(second, b.id).status == "completed"
    replacement = SaasWorker(DSNS[2])
    fresh = replacement.claim()
    assert fresh["attempt"] == 2
    with pytest.raises(LeaseLost):
        worker.heartbeat(claim)
    store.cancel_run(first, "actor", a.id, {"idempotency_key": key()})
    with pytest.raises(LeaseLost):
        replacement.heartbeat(fresh)
    replacement.process(fresh)
    assert store.run(first, a.id).status == "cancelled"


def test_concurrent_admission_and_idempotency(live):
    store, _, (tenant, _) = live
    request = MatchRequest(idempotency_key=key(), job_ids=["job"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        runs = list(pool.map(lambda _: store.create_run(tenant, "actor", request), range(2)))
    assert runs[0].id == runs[1].id
    with pytest.raises(StoreError) as error:
        store.create_run(tenant, "actor", MatchRequest(idempotency_key=key(), job_ids=["job"]))
    assert error.value.status == 429
    with pytest.raises(StoreError) as error:
        store.create_run(tenant, "actor", request.model_copy(update={"job_ids": ["dispatch"]}))
    assert error.value.code == "idempotency_conflict"


def test_full_placement_with_simulated_permissions_and_deduplicated_outbox(live):
    store, worker, (tenant, _) = live
    intro = shortlisted(live)
    request = SimulationRequest(
        expected_version=intro.version,
        idempotency_key=key(),
        action="company_request",
        simulated=True,
    )
    for user, enabled in (("actor", False), ("ungranted", True)):
        with pytest.raises(StoreError) as error:
            store.simulate(tenant, user, intro.id, request, enabled)
        assert error.value.status == 403
    intro = store.simulate(tenant, "actor", intro.id, request, True)
    assert store.simulate(tenant, "actor", intro.id, request, True).id == intro.id
    intro = simulate(store, tenant, intro, "person_consent")
    start = datetime.now(UTC) + timedelta(days=1)
    appointment = store.propose_appointment(
        tenant,
        "actor",
        intro.id,
        AppointmentRequest(
            expected_version=intro.version,
            idempotency_key=key(),
            start_at=start,
            end_at=start + timedelta(hours=1),
            site_id="site",
        ),
    )
    accepted = store.change_appointment(
        tenant,
        "actor",
        appointment.id,
        Version(
            expected_version=appointment.version,
            idempotency_key=key(),
        ),
        "accept",
        True,
    )
    assert accepted.status == "accepted"
    intro = next(i for i in store.list_introductions(tenant).items if i.id == intro.id)
    placed = store.place(
        tenant,
        "actor",
        intro.id,
        Version(
            expected_version=intro.version,
            idempotency_key=key(),
        ),
    )
    assert placed.status == "placed"
    while worker.deliver_outbox():
        pass
    notices = store.list_notifications(tenant, "actor", audience="company", allow_simulation=True)
    assert all(n.simulated and "[SIMULATED" in n.title for n in notices.items)
    assert all("private" not in n.message for n in notices.items)
    assert all("架空候補" not in n.message for n in notices.items if n.kind == "shortlisted")
    assert any(
        "架空候補" in n.message for n in notices.items if n.kind == "simulated_person_consent"
    )
    before = notices.total
    worker.deliver_outbox()
    assert (
        store.list_notifications(
            tenant,
            "actor",
            audience="company",
            allow_simulation=True,
        ).total
        == before
    )


def test_dispatch_rejects_company_selection_and_interviews(live):
    store, _, (tenant, _) = live
    intro = shortlisted(live, "dispatch")
    assert intro.status == "requested"
    with pytest.raises(StoreError) as error:
        simulate(store, tenant, intro, "company_request")
    assert error.value.code == "dispatch_selection_forbidden"
    intro = simulate(store, tenant, intro, "person_consent")
    start = datetime.now(UTC) + timedelta(days=1)
    with pytest.raises(StoreError) as error:
        store.propose_appointment(
            tenant,
            "actor",
            intro.id,
            AppointmentRequest(
                expected_version=intro.version,
                idempotency_key=key(),
                start_at=start,
                end_at=start + timedelta(hours=1),
                online=True,
            ),
        )
    assert error.value.code == "dispatch_interview_forbidden"
    assert (
        store.place(
            tenant,
            "actor",
            intro.id,
            Version(
                expected_version=intro.version,
                idempotency_key=key(),
            ),
        ).status
        == "placed"
    )


def test_additive_import_preserves_edits_and_worker_has_no_identity_privilege(live):
    store, worker, (tenant, _) = live
    with psycopg.connect(DSNS[0], row_factory=dict_row) as admin:
        tenant_context(admin, tenant)
        admin.execute(
            "UPDATE staff_saas.jobs SET body=jsonb_set(body,'{title}','\"operator edit\"') "
            "WHERE tenant_id=%s AND id='job'",
            (tenant,),
        )
        counts = load_dataset(admin, fixture_dataset(tenant))
        assert counts["jobs"] == 2
    assert store.job(tenant, "job").title == "operator edit"
    with worker.store.connect() as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("SELECT * FROM staff_saas.dispatch")
    with worker.store.connect() as connection:
        row = connection.execute(
            "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname='staff_identity' "
            "AND has_schema_privilege(current_user,oid,'USAGE')) AS identity_access",
        ).fetchone()
        assert not row["identity_access"]


def test_concurrent_site_reservation_reschedule_and_consent_revocation(live):
    store, worker, (tenant, _) = live
    run, candidates = completed(live)
    intros = []
    for candidate in candidates:
        intro = store.shortlist(
            tenant,
            "actor",
            ShortlistRequest(
                run_id=run.id,
                job_id="job",
                person_id=candidate.person_id,
                expected_job_version=1,
                expected_person_version=1,
                idempotency_key=key(),
            ),
        )
        intro = simulate(store, tenant, intro, "company_request")
        intros.append(simulate(store, tenant, intro, "person_consent"))
    start = datetime.now(UTC) + timedelta(days=1)

    def reserve(intro):
        try:
            return store.propose_appointment(
                tenant,
                "actor",
                intro.id,
                AppointmentRequest(
                    expected_version=intro.version,
                    idempotency_key=key(),
                    start_at=start,
                    end_at=start + timedelta(hours=1),
                    site_id="site",
                ),
            )
        except StoreError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, intros))
    assert sum(isinstance(result, StoreError) for result in results) == 1
    appointment = next(result for result in results if not isinstance(result, StoreError))
    moved = store.change_appointment(
        tenant,
        "actor",
        appointment.id,
        AppointmentRequest(
            expected_version=appointment.version,
            idempotency_key=key(),
            start_at=start + timedelta(hours=2),
            end_at=start + timedelta(hours=3),
            online=True,
        ),
        "reschedule",
    )
    assert moved.version == 2 and moved.status == "proposed"
    with pytest.raises(StoreError) as error:
        store.change_appointment(
            tenant,
            "actor",
            moved.id,
            Version(
                expected_version=1,
                idempotency_key=key(),
            ),
            "accept",
            True,
        )
    assert error.value.code == "stale_version"
    worker.deliver_outbox()
    intro = next(i for i in store.list_introductions(tenant).items if i.id == moved.introduction_id)
    simulate(store, tenant, intro, "withdraw_consent")
    cancelled = next(a for a in store.list_appointments(tenant).items if a.id == moved.id)
    assert cancelled.status == "cancelled"
    notices = store.list_notifications(tenant, "actor", audience="company", allow_simulation=True)
    assert all(
        "公開プロフィール" not in n.message for n in notices.items if n.introduction_id == intro.id
    )


def test_cross_tenant_runs_stale_inputs_expiry_and_retry_limit(live):
    store, worker, (tenant, other) = live
    run, candidates = completed(live)
    with pytest.raises(StoreError) as error:
        store.run(other, run.id)
    assert error.value.status == 404
    candidate = candidates[0]
    request = ShortlistRequest(
        run_id=run.id,
        job_id="job",
        person_id=candidate.person_id,
        expected_job_version=1,
        expected_person_version=1,
        idempotency_key=key(),
    )
    with psycopg.connect(DSNS[0]) as admin:
        tenant_context(admin, tenant)
        admin.execute(
            "UPDATE staff_saas.people SET version=2,summary=jsonb_set(summary,'{version}','2') "
            "WHERE tenant_id=%s AND id=%s",
            (tenant, candidate.person_id),
        )
    with pytest.raises(StoreError) as error:
        store.shortlist(tenant, "actor", request)
    assert error.value.code == "stale_candidate"
    remaining = next(c for c in candidates if c.person_id != candidate.person_id)
    intro = store.shortlist(
        tenant,
        "actor",
        request.model_copy(
            update={
                "person_id": remaining.person_id,
                "idempotency_key": key(),
            }
        ),
    )
    with psycopg.connect(DSNS[0]) as admin:
        tenant_context(admin, tenant)
        admin.execute(
            "UPDATE staff_saas.introductions SET expires_at=clock_timestamp()-interval '1 second' "
            "WHERE tenant_id=%s AND id=%s",
            (tenant, intro.id),
        )
    assert (
        next(i for i in store.list_introductions(tenant).items if i.id == intro.id).status
        == "expired"
    )
    pending = store.create_run(
        tenant,
        "actor",
        MatchRequest(
            idempotency_key=key(),
            job_ids=["dispatch"],
        ),
    )
    with psycopg.connect(DSNS[0]) as admin:
        admin.execute(
            "UPDATE staff_saas.dispatch SET attempt=3 WHERE tenant_id=%s AND run_id=%s",
            (tenant, pending.id),
        )
    claim = worker.claim()
    worker.process(claim)
    failed = store.run(tenant, pending.id)
    assert failed.status == "failed" and failed.error == "retry_limit_exceeded"


def test_packaged_verifier_executes_real_postgresql_checks(live, monkeypatch):
    from backend.saas_domain_verify import verify_domain

    monkeypatch.undo()
    with psycopg.connect(DSNS[0]) as admin:
        report = verify_domain(admin, DSNS[1], DSNS[2])
    assert report["status"] == "passed"
    assert "force_rls_with_check" in report["checks"]
    assert "concurrent_skip_locked_claim" in report["checks"]
    assert "stale_attempt_publication_fencing" in report["checks"]
    assert "restart_persistence" in report["checks"]
    for check in (
        "concurrent_placement_headcount",
        "simultaneous_placement_idempotent_replay",
        "concurrent_appointment_site_overlap",
        "appointment_attendee_overlap",
        "dispatch_company_selection_rejected",
        "dispatch_interview_rejected",
        "consent_withdrawal_blocks_placement_and_cancels_interview",
        "stale_appointment_version",
        "stale_introduction_version",
        "stale_person_version_blocks_placement",
        "expiry_cancels_interview_and_rejects_transition",
    ):
        assert check in report["checks"]


def test_catalog_reads_do_not_wait_for_tenant_mutation_lock(live):
    store, _, (tenant, _) = live
    with psycopg.connect(DSNS[0]) as admin:
        tenant_context(admin, tenant)
        admin.execute(
            "SELECT tenant_id FROM staff_saas.tenants WHERE tenant_id=%s FOR UPDATE",
            (tenant,),
        )
        assert store.list_jobs(tenant, limit=1).total == 2
        first = store.list_people(tenant, limit=1)
        second = store.list_people(tenant, limit=1, cursor=first.next_cursor)
        assert first.total == second.total == 2
        assert first.items[0].id != second.items[0].id
