import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.engine import optimize
from backend.models import OptimizeRequest
from backend.seed import demo_snapshot
from backend.store import Store


@pytest.fixture
def database_path():
    directory = Path(".test-data")
    directory.mkdir(exist_ok=True)
    path = directory / f"api-{uuid4().hex}.sqlite3"
    yield path
    for suffix in ("", "-wal", "-shm", "-lock"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)


@pytest.fixture
def client(database_path):
    with TestClient(create_app(database_path=database_path, tenant_id="demo")) as active:
        yield active


def wait_run(client, run_id):
    for _ in range(200):
        response = client.get(f"/matching/runs/{run_id}")
        assert response.status_code == 200
        run = response.json()
        if run["status"] in ("completed", "failed"):
            return run
        time.sleep(0.025)
    pytest.fail("Background local solver did not finish.")


def submit(client, key=None):
    response = client.post("/matching/optimize", json={"idempotency_key": key or uuid4().hex})
    assert response.status_code == 202
    return response.json()


def completed(client):
    run = wait_run(client, submit(client)["run_id"])
    assert run["status"] == "completed", run["error"]
    return run


def test_health_plain_arrays_candidates_and_missing_errors(client):
    assert client.get("/health").json() == {
        "status": "ok",
        "mode": "local-mock",
        "tenant_id": "demo",
        "persistence": "sqlite-mock",
    }
    for route, length in (
        ("/staff", 6),
        ("/orders", 5),
        ("/assignments", 1),
        ("/matching/runs", 0),
    ):
        response = client.get(route)
        assert response.status_code == 200
        assert isinstance(response.json(), list)
        assert len(response.json()) == length
    candidates = client.get("/orders/o03/candidates").json()
    assert {item["staff_id"]: item["eligibility"] for item in candidates} == {
        "s04": "eligible",
        "s05": "pending",
        "s06": "ineligible",
    }
    assert all(item["staff_id"] != "s03" for item in candidates)
    for route in (
        "/orders/absent/candidates",
        "/matching/runs/absent",
        "/matching/runs/absent/explanations/o01/s01",
    ):
        response = client.get(route)
        assert response.status_code == 404
        assert set(response.json()["detail"]) == {"code", "message"}
    assert client.post("/matching/optimize", json={"idempotency_key": "short"}).status_code == 422


def test_async_optimize_returns_before_solver_and_deduplicates_work(database_path):
    entered, release = Event(), Event()
    calls = []

    def slow_solver(snapshot, time_limit_seconds):
        calls.append(snapshot.revision)
        entered.set()
        assert release.wait(timeout=10)
        return optimize(snapshot, time_limit_seconds=time_limit_seconds)

    with TestClient(create_app(database_path=database_path, optimizer=slow_solver)) as client:
        try:
            accepted = submit(client, "async-idempotency")
            assert entered.wait(timeout=3)
            assert not release.is_set()
            assert client.get(f"/matching/runs/{accepted['run_id']}").json()["status"] == "running"
            assert submit(client, "async-idempotency") == accepted
            conflict = client.post("/matching/optimize", json={"idempotency_key": "new-run-key"})
            assert conflict.status_code == 409
            assert conflict.json()["detail"]["code"] == "active_batch"
            assert client.get("/health").status_code == 200
        finally:
            release.set()
        run = wait_run(client, accepted["run_id"])
        assert run["status"] == "completed"
        assert len(calls) == 1


def test_consent_confirmation_idempotency_and_restart(client, database_path):
    run = completed(client)
    assert run["result"]["optimized"]["filled_slots"] > run["result"]["greedy"]["filled_slots"]
    proposal = run["proposals"][0]
    endpoint = f"/proposals/{proposal['id']}"
    refused = client.post(
        endpoint + "/confirm",
        json={"expected_version": 1, "idempotency_key": "before-consent"},
    )
    assert refused.status_code == 409
    response = client.post(
        endpoint + "/respond",
        json={"decision": "consent", "expected_version": 1, "idempotency_key": "consent-key"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "consented"
    confirmation = {
        "expected_version": response.json()["version"],
        "idempotency_key": "confirm-key",
    }
    assigned = client.post(endpoint + "/confirm", json=confirmation)
    assert assigned.status_code == 200
    assert client.post(endpoint + "/confirm", json=confirmation).json() == assigned.json()
    retry = {**confirmation, "idempotency_key": "confirm-new-key"}
    assert client.post(endpoint + "/confirm", json=retry).json() == assigned.json()
    mismatch = {**confirmation, "expected_version": 99}
    assert client.post(endpoint + "/confirm", json=mismatch).status_code == 409
    assert len(client.get("/assignments").json()) == 2
    explanation_url = (
        f"/matching/runs/{run['id']}/explanations/{proposal['order_id']}/{proposal['staff_id']}"
    )
    explanation = client.get(explanation_url)
    assert explanation.status_code == 200
    assert explanation.json()["selected"]
    reopened = Store(database_path)
    reopened.initialize()
    assert len(reopened.snapshot("demo").assignments) == 2
    assert reopened.get_run("demo", run["id"]).result.model_dump(mode="json") == run["result"]
    assert reopened.get_proposal("demo", proposal["id"]).status == "confirmed"


def test_response_withdrawal_and_expiry_through_api(database_path):
    now = datetime(2026, 10, 5, tzinfo=UTC)
    store = Store(database_path, clock=lambda: now)
    with TestClient(create_app(store=store)) as client:
        run = completed(client)
        proposal = run["proposals"][0]
        endpoint = f"/proposals/{proposal['id']}"
        body = {"decision": "consent", "expected_version": 1, "idempotency_key": "consent-event"}
        consented = client.post(endpoint + "/respond", json=body)
        assert consented.status_code == 200
        assert client.post(endpoint + "/respond", json=body).json() == consented.json()
        changed = {**body, "decision": "decline"}
        assert client.post(endpoint + "/respond", json=changed).status_code == 409
        withdrawal = client.post(
            endpoint + "/respond",
            json={
                "decision": "decline",
                "expected_version": 2,
                "idempotency_key": "withdraw-event",
            },
        )
        assert withdrawal.json()["status"] == "declined"
        assert (
            client.post(
                endpoint + "/confirm",
                json={"expected_version": 3, "idempotency_key": "declined-confirm"},
            ).status_code
            == 409
        )
        now += timedelta(minutes=31)
        expired = client.get(f"/matching/runs/{run['id']}").json()
        assert expired["proposals"][0]["status"] == "declined"
        assert all(item["status"] == "expired" for item in expired["proposals"][1:])
        assert submit(client)["run_id"] != run["id"]


def test_current_candidates_but_historical_explanations(client):
    run = completed(client)
    proposal = next(item for item in run["proposals"] if item["order_id"] == "o02")
    url = f"/matching/runs/{run['id']}/explanations/o02/{proposal['staff_id']}"
    historical = client.get(url).json()
    store = client.app.state.store
    staff = next(item for item in store.snapshot("demo").staff if item.id == proposal["staff_id"])
    store.update_domain("demo", staff.model_copy(update={"version": 2, "matching_enabled": False}))
    current = client.get("/orders/o02/candidates").json()
    assert current[0]["eligibility"] == "ineligible"
    assert client.get(url).json() == historical
    assert client.get(f"/matching/runs/{run['id']}/explanations/o02/s99").status_code == 404


def test_worker_failure_is_persisted_and_new_run_is_allowed(database_path):
    def failing_solver(snapshot, time_limit_seconds):
        raise ValueError("Sensitive input must not appear in HTTP failure details")

    with TestClient(create_app(database_path=database_path, optimizer=failing_solver)) as client:
        run = wait_run(client, submit(client)["run_id"])
        assert run["status"] == "failed"
        assert "ValueError" in run["error"]
        assert "Sensitive" not in run["error"]
        assert run["proposals"] == []
        assert submit(client)["run_id"] != run["id"]


def test_restart_interrupts_stale_run_and_preserves_history(database_path):
    store = Store(database_path)
    store.initialize()
    accepted, _ = store.create_run("demo", OptimizeRequest(idempotency_key="restart-stale-run"))
    store.start_run("demo", accepted.run_id)
    with TestClient(create_app(database_path=database_path)) as client:
        old_run = client.get(f"/matching/runs/{accepted.run_id}").json()
        assert old_run["status"] == "failed"
        assert "restart" in old_run["error"]
        new_run = completed(client)
        assert new_run["id"] != accepted.run_id
    with TestClient(create_app(database_path=database_path)) as client:
        assert client.get(f"/matching/runs/{new_run['id']}").json()["status"] == "completed"
        assert (
            client.post(
                "/matching/optimize", json={"idempotency_key": "still-active-batch"}
            ).status_code
            == 409
        )


@pytest.mark.parametrize("mode", ["production", "azure", "", "test"])
def test_non_mock_mode_rejected_before_creating_database(database_path, mode):
    with pytest.raises(RuntimeError, match="local-mock"):
        with TestClient(create_app(database_path=database_path, mode=mode)):
            pass
    assert not database_path.exists()


def test_environment_defaults_are_local_and_other_mode_is_rejected(database_path, monkeypatch):
    monkeypatch.setenv("STAFF_MATCHING_DB", str(database_path))
    monkeypatch.delenv("APP_MODE", raising=False)
    monkeypatch.delenv("LOCAL_TENANT_ID", raising=False)
    with TestClient(create_app()) as client:
        assert client.get("/health").json()["tenant_id"] == "demo"
    monkeypatch.setenv("APP_MODE", "production")
    with pytest.raises(RuntimeError, match="local-mock"):
        with TestClient(create_app()):
            pass


def test_tenant_is_server_configured_and_ids_do_not_cross_tenants(database_path):
    with TestClient(create_app(database_path=database_path, tenant_id="demo")) as client:
        run = completed(client)
        proposal = run["proposals"][0]
        assert len(client.get("/staff", headers={"X-Tenant-ID": "tenantB"}).json()) == 6
        store = client.app.state.store
        other = demo_snapshot().model_copy(update={"tenant_id": "tenantB", "assignments": []})
        store.add_mock_tenant(other)
    with TestClient(create_app(database_path=database_path, tenant_id="tenantB")) as client:
        assert client.get("/assignments").json() == []
        assert client.get(f"/matching/runs/{run['id']}").status_code == 404
        assert (
            client.get(
                f"/matching/runs/{run['id']}/explanations/{proposal['order_id']}/{proposal['staff_id']}"
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"/proposals/{proposal['id']}/respond",
                json={
                    "decision": "consent",
                    "expected_version": 1,
                    "idempotency_key": "tenant-response",
                },
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"/proposals/{proposal['id']}/confirm",
                json={"expected_version": 1, "idempotency_key": "tenant-confirm"},
            ).status_code
            == 404
        )
    with TestClient(create_app(database_path=database_path, tenant_id="absent")) as client:
        assert client.get("/health").json()["tenant_id"] == "absent"
        assert client.get("/staff").status_code == 404
        assert client.get("/matching/runs").status_code == 404
        assert (
            client.post(
                "/matching/optimize", json={"idempotency_key": "unknown-tenant-run"}
            ).status_code
            == 404
        )


def test_cors_is_limited_to_local_frontend_origins(client):
    for origin, allowed in (
        ("http://localhost:5173", True),
        ("http://127.0.0.1:5173", True),
        ("https://example.com", False),
        ("http://localhost:9999", False),
    ):
        response = client.options(
            "/matching/optimize",
            headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
        )
        assert (response.headers.get("access-control-allow-origin") == origin) is allowed


def test_second_server_is_rejected_before_interrupting_active_run(database_path):
    entered, release = Event(), Event()

    def slow_solver(snapshot, time_limit_seconds):
        entered.set()
        assert release.wait(timeout=10)
        return optimize(snapshot, time_limit_seconds=time_limit_seconds)

    with TestClient(create_app(database_path=database_path, optimizer=slow_solver)) as client:
        try:
            accepted = submit(client)
            assert entered.wait(timeout=3)
            with pytest.raises(RuntimeError, match="one server process"):
                with TestClient(create_app(database_path=database_path)):
                    pass
            assert client.get(f"/matching/runs/{accepted['run_id']}").json()["status"] == "running"
        finally:
            release.set()
        assert wait_run(client, accepted["run_id"])["status"] == "completed"
    with TestClient(create_app(database_path=database_path)) as client:
        assert client.get("/health").status_code == 200


def test_shutdown_drains_running_solver_before_releasing_database(database_path):
    entered, release, submitted, closed = Event(), Event(), Event(), Event()

    def slow_solver(snapshot, time_limit_seconds):
        entered.set()
        assert release.wait(timeout=10)
        return optimize(snapshot, time_limit_seconds=time_limit_seconds)

    def run_and_close():
        with TestClient(create_app(database_path=database_path, optimizer=slow_solver)) as client:
            accepted = submit(client)
            submitted.set()
        closed.set()
        return accepted["run_id"]

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(run_and_close)
        try:
            assert submitted.wait(timeout=3)
            assert entered.wait(timeout=3)
            assert not closed.wait(timeout=0.05)
        finally:
            release.set()
        run_id = future.result(timeout=10)
    assert closed.is_set()
    store = Store(database_path)
    assert store.get_run("demo", run_id).status == "completed"
