import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest

from backend.engine import optimize
from backend.models import ConfirmRequest, OptimizeRequest, RespondRequest
from backend.seed import demo_snapshot
from backend.store import Store, StoreError


@pytest.fixture
def store():
    directory = Path(".test-data")
    directory.mkdir(exist_ok=True)
    path = directory / f"store-{uuid4().hex}.sqlite3"
    instance = Store(path)
    instance.initialize()
    yield instance
    for suffix in ("", "-wal", "-shm"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)


def completed_run(store, key=None):
    accepted, created = store.create_run(
        "demo", OptimizeRequest(idempotency_key=key or uuid4().hex)
    )
    assert created
    snapshot, request = store.start_run("demo", accepted.run_id)
    result = optimize(snapshot, time_limit_seconds=request.time_limit_seconds)
    assert store.publish_run("demo", accepted.run_id, result)
    return store.get_run("demo", accepted.run_id)


def consent(store, proposal):
    return store.respond(
        "demo",
        proposal.id,
        RespondRequest(
            decision="consent", expected_version=proposal.version, idempotency_key=uuid4().hex
        ),
    )


def confirmation(proposal, key=None, version=None):
    return ConfirmRequest(
        expected_version=version if version is not None else proposal.version,
        idempotency_key=key or uuid4().hex,
    )


def assert_conflict(call, code=None):
    with pytest.raises(StoreError) as caught:
        call()
    assert caught.value.status == 409
    if code:
        assert caught.value.code == code


def test_seed_is_deterministic_and_persists(store):
    snapshot = store.snapshot("demo")
    assert snapshot == demo_snapshot()
    assert len(snapshot.assignments) == 1
    assert snapshot.assignments[0].order_id == "o03"
    assert all(item.name.startswith("合成スタッフ") for item in snapshot.staff)
    assert all(item.synthetic for item in snapshot.evidence)
    revised = snapshot.staff[0].model_copy(update={"version": 2})
    store.update_domain("demo", revised)
    reopened = Store(store.path)
    reopened.initialize()
    assert reopened.snapshot("demo").staff[0].version == 2
    assert reopened.snapshot("demo").revision == 2
    with store._connection() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_run_snapshot_config_result_and_confirmation_persist(store):
    run = completed_run(store)
    assert run.status == "completed"
    assert run.result.optimized.filled_slots > run.result.greedy.filled_slots
    proposal = run.proposals[0]
    explanation = store.explanation("demo", run.id, proposal.order_id, proposal.staff_id)
    assert explanation.selected
    with store._connection() as connection:
        row = connection.execute(
            "SELECT snapshot, algorithm_config FROM runs WHERE tenant_id = ? AND id = ?",
            ("demo", run.id),
        ).fetchone()
        assert json.loads(row["snapshot"])["revision"] == run.input_revision
        assert json.loads(row["algorithm_config"])["workers"] == 1
        assert json.loads(row["algorithm_config"])["seed"] == 0
    proposal = consent(store, proposal)
    assignment = store.confirm("demo", proposal.id, confirmation(proposal))
    reopened = Store(store.path)
    reopened.initialize()
    assert assignment in reopened.snapshot("demo").assignments
    assert reopened.get_proposal("demo", proposal.id).status == "confirmed"
    assert reopened.explanation("demo", run.id, proposal.order_id, proposal.staff_id) == explanation
    with store._connection() as connection:
        events = connection.execute(
            "SELECT event FROM audit_events WHERE tenant_id = ? AND proposal_id = ?",
            ("demo", proposal.id),
        ).fetchall()
    assert [row["event"] for row in events] == ["confirmed"]


def test_consent_required_versions_idempotency_and_no_duplicate_assignment(store):
    run = completed_run(store)
    proposal = run.proposals[0]
    assert_conflict(
        lambda: store.confirm("demo", proposal.id, confirmation(proposal)), "consent_required"
    )
    response_request = RespondRequest(
        decision="consent", expected_version=proposal.version, idempotency_key="response-key"
    )
    consented = store.respond("demo", proposal.id, response_request)
    assert store.respond("demo", proposal.id, response_request) == consented
    assert_conflict(
        lambda: store.respond(
            "demo", proposal.id, response_request.model_copy(update={"decision": "decline"})
        ),
        "idempotency_conflict",
    )
    assert_conflict(
        lambda: store.confirm("demo", proposal.id, confirmation(consented, version=1)),
        "stale_version",
    )
    request = confirmation(consented, key="confirmation-key")
    assignment = store.confirm("demo", proposal.id, request)
    assert store.confirm("demo", proposal.id, request) == assignment
    assert store.confirm("demo", proposal.id, confirmation(consented)) == assignment
    confirmed = store.get_proposal("demo", proposal.id)
    assert store.confirm("demo", proposal.id, confirmation(confirmed)) == assignment
    assert_conflict(
        lambda: store.confirm(
            "demo", proposal.id, request.model_copy(update={"expected_version": 99})
        ),
        "idempotency_conflict",
    )
    assert_conflict(
        lambda: store.confirm("demo", proposal.id, confirmation(confirmed, version=99)),
        "stale_version",
    )
    assert_conflict(
        lambda: store.respond(
            "demo",
            proposal.id,
            RespondRequest(
                decision="decline", expected_version=confirmed.version, idempotency_key=uuid4().hex
            ),
        ),
        "proposal_state",
    )
    assert sum(item.proposal_id == proposal.id for item in store.snapshot("demo").assignments) == 1


@pytest.mark.parametrize("kind", ["staff", "order", "evidence"])
def test_changed_input_versions_block_confirmation(store, kind):
    proposal = consent(store, completed_run(store).proposals[0])
    snapshot = store.snapshot("demo")
    if kind == "staff":
        item = next(item for item in snapshot.staff if item.id == proposal.staff_id)
    elif kind == "order":
        item = next(item for item in snapshot.orders if item.id == proposal.order_id)
    else:
        item = next(
            item
            for item in snapshot.evidence
            if item.staff_id == proposal.staff_id and item.order_id == proposal.order_id
        )
    store.update_domain("demo", item.model_copy(update={"version": item.version + 1}))
    assert_conflict(
        lambda: store.confirm("demo", proposal.id, confirmation(proposal)), "stale_input"
    )
    assert len(store.snapshot("demo").assignments) == 1


def test_withdrawal_preserves_response_history(store):
    run = completed_run(store)
    proposal = consent(store, run.proposals[0])
    declined = store.respond(
        "demo",
        proposal.id,
        RespondRequest(
            decision="decline", expected_version=proposal.version, idempotency_key=uuid4().hex
        ),
    )
    assert declined.status == "declined"
    assert_conflict(
        lambda: store.confirm("demo", declined.id, confirmation(declined)), "consent_required"
    )
    with store._connection() as connection:
        events = connection.execute(
            "SELECT decision, from_status, to_status FROM response_events "
            "WHERE tenant_id = ? AND proposal_id = ? ORDER BY id",
            ("demo", proposal.id),
        ).fetchall()
    assert [tuple(row) for row in events] == [
        ("consent", "proposed", "consented"),
        ("decline", "consented", "declined"),
    ]
    for pending in run.proposals[1:]:
        store.respond(
            "demo",
            pending.id,
            RespondRequest(
                decision="decline", expected_version=pending.version, idempotency_key=uuid4().hex
            ),
        )
    accepted, created = store.create_run("demo", OptimizeRequest(idempotency_key=uuid4().hex))
    assert created and accepted.status == "queued"


def test_expiry_materializes_on_conflict_and_releases_batch(store):
    now = datetime(2026, 10, 5, tzinfo=UTC)
    store.clock = lambda: now
    run = completed_run(store)
    proposal = consent(store, run.proposals[0])
    before = store.snapshot("demo").revision
    now += timedelta(minutes=31)
    assert_conflict(lambda: store.confirm("demo", proposal.id, confirmation(proposal)))
    assert store.get_proposal("demo", proposal.id).status == "expired"
    assert store.snapshot("demo").revision == before + 1
    assert all(item.status == "expired" for item in store.get_run("demo", run.id).proposals)
    accepted, created = store.create_run("demo", OptimizeRequest(idempotency_key=uuid4().hex))
    assert created and accepted.status == "queued"


def test_active_batch_rerun_and_optimize_idempotency(store):
    request = OptimizeRequest(idempotency_key="run-request-key")
    accepted, created = store.create_run("demo", request)
    assert created
    assert store.create_run("demo", request) == (accepted, False)
    assert_conflict(
        lambda: store.create_run("demo", request.model_copy(update={"time_limit_seconds": 1})),
        "idempotency_conflict",
    )
    for stage in ("queued", "running", "completed", "consented"):
        if stage == "running":
            snapshot, _ = store.start_run("demo", accepted.run_id)
        elif stage == "completed":
            store.publish_run("demo", accepted.run_id, optimize(snapshot))
        elif stage == "consented":
            consent(store, store.get_run("demo", accepted.run_id).proposals[0])
        assert_conflict(
            lambda: store.create_run("demo", OptimizeRequest(idempotency_key=uuid4().hex)),
            "active_batch",
        )
        assert store.create_run("demo", request) == (accepted, False)


def test_publish_race_fails_without_proposals(store):
    accepted, _ = store.create_run("demo", OptimizeRequest(idempotency_key=uuid4().hex))
    snapshot, _ = store.start_run("demo", accepted.run_id)
    result = optimize(snapshot)
    store.update_domain("demo", snapshot.staff[0].model_copy(update={"version": 2}))
    assert not store.publish_run("demo", accepted.run_id, result)
    run = store.get_run("demo", accepted.run_id)
    assert run.status == "failed"
    assert "revision" in run.error
    assert run.proposals == []


def test_publication_rejects_another_active_batch(store):
    accepted, _ = store.create_run("demo", OptimizeRequest(idempotency_key=uuid4().hex))
    snapshot, _ = store.start_run("demo", accepted.run_id)
    with store._connection() as connection:
        connection.execute(
            "INSERT INTO runs(tenant_id, id, status, created_at, input_revision, config, "
            "snapshot, algorithm_config) SELECT tenant_id, ?, 'queued', created_at, "
            "input_revision, config, snapshot, algorithm_config FROM runs "
            "WHERE tenant_id = ? AND id = ?",
            ("controlled-other-run", "demo", accepted.run_id),
        )
    assert not store.publish_run("demo", accepted.run_id, optimize(snapshot))
    assert store.get_run("demo", accepted.run_id).status == "failed"


def test_restart_marks_only_interrupted_tenant_jobs_failed(store):
    other = demo_snapshot().model_copy(update={"tenant_id": "tenantB"})
    store.add_mock_tenant(other)
    first, _ = store.create_run("demo", OptimizeRequest(idempotency_key="restart-test"))
    second, _ = store.create_run("tenantB", OptimizeRequest(idempotency_key="restart-test"))
    store.start_run("demo", first.run_id)
    reopened = Store(store.path)
    reopened.initialize()
    reopened.recover_interrupted_runs("demo")
    assert reopened.get_run("demo", first.run_id).status == "failed"
    assert "restart" in reopened.get_run("demo", first.run_id).error
    assert reopened.get_run("tenantB", second.run_id).status == "queued"


def test_tenant_scoped_identifiers_idempotency_and_foreign_keys(store):
    run = completed_run(store, key="same-tenant-key")
    proposal = consent(store, run.proposals[0])
    other = demo_snapshot().model_copy(update={"tenant_id": "tenantB", "assignments": []})
    store.add_mock_tenant(other)
    for action in (
        lambda: store.get_run("tenantB", run.id),
        lambda: store.get_proposal("tenantB", proposal.id),
        lambda: store.explanation("tenantB", run.id, proposal.order_id, proposal.staff_id),
        lambda: store.confirm("tenantB", proposal.id, confirmation(proposal)),
        lambda: store.respond(
            "tenantB",
            proposal.id,
            RespondRequest(
                decision="consent", expected_version=proposal.version, idempotency_key=uuid4().hex
            ),
        ),
    ):
        with pytest.raises(StoreError) as caught:
            action()
        assert caught.value.status == 404
    other_run, created = store.create_run(
        "tenantB", OptimizeRequest(idempotency_key="same-tenant-key")
    )
    assert created and other_run.run_id != run.id
    with store._connection() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO assignments(tenant_id, id, staff_id, order_id, proposal_id, "
                "source_run_id, body) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "tenantB",
                    "invalid-foreign-proposal",
                    proposal.staff_id,
                    proposal.order_id,
                    proposal.id,
                    None,
                    "{}",
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO proposals(tenant_id, id, run_id, staff_id, order_id, status, "
                "version, input_revision, staff_version, order_version, evidence_version, "
                "expires_at) VALUES (?, ?, ?, ?, ?, 'proposed', 1, 1, 1, 1, 1, ?)",
                (
                    "tenantB",
                    "invalid-foreign-run",
                    run.id,
                    proposal.staff_id,
                    proposal.order_id,
                    proposal.expires_at.isoformat(),
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO evidence(tenant_id, staff_id, order_id, body) VALUES (?, ?, ?, ?)",
                ("tenantB", "absent-staff", "o01", "{}"),
            )
    with pytest.raises(StoreError) as caught:
        store.snapshot("unknown")
    assert caught.value.status == 404


def _controlled_competing_proposals(store, pairs):
    """Construct competing historical consents to exercise final transaction defenses."""
    accepted, _ = store.create_run("demo", OptimizeRequest(idempotency_key=uuid4().hex))
    snapshot, _ = store.start_run("demo", accepted.run_id)
    staff = {item.id: item for item in snapshot.staff}
    orders = {item.id: item for item in snapshot.orders}
    evidence = {(item.staff_id, item.order_id): item for item in snapshot.evidence}
    expiry = (datetime.now(UTC) + timedelta(minutes=30)).isoformat()
    with store._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for index, (staff_id, order_id) in enumerate(pairs):
            connection.execute(
                "INSERT INTO proposals(tenant_id, id, run_id, staff_id, order_id, status, "
                "version, input_revision, staff_version, order_version, evidence_version, "
                "expires_at) VALUES (?, ?, ?, ?, ?, 'consented', 2, ?, ?, ?, ?, ?)",
                (
                    "demo",
                    f"controlled-{index}",
                    accepted.run_id,
                    staff_id,
                    order_id,
                    snapshot.revision,
                    staff[staff_id].version,
                    orders[order_id].version,
                    evidence[(staff_id, order_id)].version,
                    expiry,
                ),
            )
        connection.execute(
            "UPDATE runs SET status = 'completed' WHERE tenant_id = ? AND id = ?",
            ("demo", accepted.run_id),
        )
        connection.commit()
    return [store.get_proposal("demo", f"controlled-{index}") for index in range(len(pairs))]


@pytest.mark.parametrize(
    "pairs",
    [
        [("s01", "o01"), ("s02", "o01")],
        [("s04", "o03"), ("s05", "o03")],
        [("s01", "o01"), ("s01", "o02")],
    ],
)
def test_concurrent_confirmations_serialize_capacity_and_worker_conflicts(store, pairs):
    if ("s05", "o03") in pairs:
        evidence = next(
            item
            for item in store.snapshot("demo").evidence
            if item.staff_id == "s05" and item.order_id == "o03"
        )
        store.update_domain(
            "demo",
            evidence.model_copy(
                update={"version": 2, "commute_minutes": 15, "commute_verified": True}
            ),
        )
    proposals = _controlled_competing_proposals(store, pairs)
    barrier = Barrier(2)

    def confirm_separate_connection(proposal):
        worker = Store(store.path)
        barrier.wait(timeout=5)
        try:
            return worker.confirm("demo", proposal.id, confirmation(proposal))
        except StoreError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(confirm_separate_connection, proposals))
    assert sum(not isinstance(result, StoreError) for result in results) == 1
    errors = [result for result in results if isinstance(result, StoreError)]
    assert len(errors) == 1 and errors[0].status == 409
    snapshot = store.snapshot("demo")
    assert len(snapshot.assignments) == 2
    assert len({item.proposal_id for item in snapshot.assignments if item.proposal_id}) == 1


def test_concurrent_optimization_requests_create_only_one_run(store):
    barrier = Barrier(2)

    def submit_separate_connection(_):
        worker = Store(store.path)
        barrier.wait(timeout=5)
        try:
            return worker.create_run("demo", OptimizeRequest(idempotency_key=uuid4().hex))
        except StoreError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit_separate_connection, range(2)))
    assert sum(isinstance(result, tuple) for result in results) == 1
    failures = [result for result in results if isinstance(result, StoreError)]
    assert len(failures) == 1 and failures[0].code == "active_batch"
    assert len(store.list_runs("demo")) == 1


def test_recent_runs_are_limited_and_newest_first(store):
    identifiers = []
    for index in range(22):
        accepted, _ = store.create_run(
            "demo", OptimizeRequest(idempotency_key=f"recent-run-{index:02}")
        )
        store.fail_run("demo", accepted.run_id, "Controlled test failure.")
        identifiers.append(accepted.run_id)
    assert [run.id for run in store.list_runs("demo")] == list(reversed(identifiers))[:20]
