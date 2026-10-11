import base64
import secrets
import shutil
import sys
import time
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.cloud_auth import CoordinatorCredentials
from backend.engine import optimize
from backend.store import Store


@pytest.fixture
def cloud_environment(monkeypatch):
    directory = Path(".test-data") / f"cloud-{uuid4().hex}"
    assets = directory / "assets"
    assets.mkdir(parents=True)
    (directory / "index.html").write_text(
        '<!doctype html><html><body><div id="root">Synthetic PoC</div></body></html>',
        encoding="utf-8",
    )
    (assets / "app.js").write_text('console.log("synthetic");', encoding="utf-8")
    environment = {
        "APP_MODE": "azure-poc",
        "POC_AUTH_USERNAME": "coordinator",
        "POC_AUTH_PASSWORD": secrets.token_urlsafe(40),
        "STAFF_MATCHING_TENANT": "demo",
        "PGHOST": "postgres.example.invalid",
        "PGDATABASE": "staff_matching",
        "PGUSER": "app_role",
        "PGPASSWORD": secrets.token_urlsafe(40),
        "PGSSLMODE": "verify-full",
        "STATIC_DIR": str(directory),
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    try:
        yield environment
    finally:
        shutil.rmtree(directory)


@pytest.fixture
def injected_store(cloud_environment):
    return Store(Path(cloud_environment["STATIC_DIR"]) / "test.sqlite3")


def cloud_client(environment, store, **kwargs):
    client = TestClient(
        create_app(
            store=store,
            extension_report=lambda tenant: {
                "persistence": "postgresql",
                "tenant_id": tenant,
                "extensions": [{"name": "vector", "version": "test-only"}],
            },
            **kwargs,
        ),
        base_url="https://cloud.test",
    )
    client.auth = (environment["POC_AUTH_USERNAME"], environment["POC_AUTH_PASSWORD"])
    return client


def csrf_headers():
    return {"Origin": "https://cloud.test", "X-Requested-With": "staff-matching"}


def wait_run(client, run_id):
    for _ in range(200):
        response = client.get(f"/api/matching/runs/{run_id}")
        assert response.status_code == 200
        run = response.json()
        if run["status"] in ("completed", "failed"):
            return run
        time.sleep(0.025)
    pytest.fail("Background cloud solver did not finish.")


def test_cloud_ui_api_docs_assets_and_unknown_paths_require_auth(cloud_environment, injected_store):
    with cloud_client(cloud_environment, injected_store) as client:
        for path in (
            "/", "/assets/app.js", "/docs", "/redoc", "/openapi.json", "/api/health",
            "/api/extensions", "/api/staff", "/health", "/staff", "/not-found", "/api/not-found",
        ):
            missing = client.get(path, auth=None)
            wrong = client.get(path, auth=("incorrect", secrets.token_urlsafe(40)))
            for response in (missing, wrong):
                assert response.status_code == 401, path
                assert response.headers["www-authenticate"].startswith("Basic ")
                assert response.headers["cache-control"] == "no-store"
                assert "demo" not in response.text
                assert cloud_environment["POC_AUTH_PASSWORD"] not in response.text
                assert cloud_environment["PGPASSWORD"] not in response.text


def test_public_liveness_is_sanitized_but_business_health_is_authenticated(
    cloud_environment, injected_store
):
    with cloud_client(cloud_environment, injected_store) as client:
        response = client.get("/healthz", auth=None)
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert client.head("/healthz", auth=None).status_code == 200
        assert client.get("/api/healthz", auth=None).status_code == 401
        assert client.post("/healthz", auth=None).status_code == 401
        assert client.get("/api/health").json() == {
            "status": "ok",
            "mode": "azure-poc",
            "tenant_id": "demo",
            "persistence": "postgresql",
        }


def test_static_and_legacy_routes_work_without_swallowing_api_404s(
    cloud_environment, injected_store
):
    with cloud_client(cloud_environment, injected_store) as client:
        assert "Synthetic PoC" in client.get("/").text
        asset = client.get("/assets/app.js")
        assert asset.status_code == 200
        assert asset.headers["cache-control"] == "no-store"
        for path in ("/staff", "/api/staff"):
            assert len(client.get(path).json()) == 6
        for path in ("/api/does-not-exist", "/api", "/does-not-exist"):
            response = client.get(path)
            assert response.status_code == 404
            assert "Synthetic PoC" not in response.text
        assert client.get("/docs").status_code == 200
        assert client.get("/openapi.json").status_code == 200
        assert client.get("/api/extensions").json()["extensions"][0]["name"] == "vector"


@pytest.mark.parametrize("authorization", [
    "Bearer token", "Basic invalid", "Basic ****",
    "Basic " + base64.b64encode(b"no-separator").decode(),
])
def test_malformed_authorization_is_rejected(cloud_environment, injected_store, authorization):
    with cloud_client(cloud_environment, injected_store) as client:
        response = client.get("/api/staff", auth=None, headers={"Authorization": authorization})
        assert response.status_code == 401


def test_both_basic_credentials_are_compared_even_when_username_is_wrong(
    cloud_environment, monkeypatch
):
    credentials = CoordinatorCredentials.from_environment()
    comparisons = []
    compare = secrets.compare_digest

    def record(left, right):
        comparisons.append((left, right))
        return compare(left, right)

    monkeypatch.setattr("backend.cloud_auth.secrets.compare_digest", record)
    authorization = "Basic " + base64.b64encode(b"wrong:incorrect").decode()
    assert not credentials.accepts(authorization)
    assert len(comparisons) == 2
    assert cloud_environment["POC_AUTH_PASSWORD"] not in repr(credentials)


@pytest.mark.parametrize("headers", [
    {},
    {"Origin": "https://cloud.test"},
    {"X-Requested-With": "staff-matching"},
    {"Origin": "https://cloud.test", "X-Requested-With": "wrong"},
])
def test_unsafe_requests_require_csrf_header_and_origin(
    cloud_environment, injected_store, headers
):
    with cloud_client(cloud_environment, injected_store) as client:
        for path in ("/api/matching/optimize", "/matching/optimize"):
            response = client.post(
                path, json={"idempotency_key": "cloud-csrf-check"}, headers=headers
            )
            assert response.status_code == 403
            assert response.json()["detail"]["code"] == "csrf_rejected"
        assert client.get("/api/matching/runs").json() == []


@pytest.mark.parametrize("origin", [
    "https://evil.test", "https://cloud.test.evil.test", "http://cloud.test", "null",
    "https://cloud.test:8443", "https://cloud.test/path", "https://user@cloud.test",
])
def test_cloud_rejects_cross_origin_get_post_and_preflight(
    cloud_environment, injected_store, origin
):
    with cloud_client(cloud_environment, injected_store) as client:
        headers = {**csrf_headers(), "Origin": origin}
        for method, path in (
            ("GET", "/"), ("GET", "/api/staff"), ("POST", "/api/matching/optimize"),
            ("OPTIONS", "/api/matching/optimize"),
        ):
            response = client.request(method, path, headers=headers)
            assert response.status_code == 403
            assert response.json()["detail"]["code"] == "cross_origin_denied"
            assert "access-control-allow-origin" not in response.headers


def test_cloud_rejects_browser_cross_site_metadata(cloud_environment, injected_store):
    with cloud_client(cloud_environment, injected_store) as client:
        for value in ("cross-site", "same-site"):
            assert client.get("/", headers={"Sec-Fetch-Site": value}).status_code == 403
        same_origin = client.get("/api/staff", headers={"Origin": "https://cloud.test:443"})
        assert same_origin.status_code == 200


def test_valid_auth_retains_async_runs_tenant_scope_and_consent_lifecycle(
    cloud_environment, injected_store
):
    entered, release = Event(), Event()
    calls = []

    def blocked_solver(snapshot, time_limit_seconds):
        calls.append(snapshot.tenant_id)
        entered.set()
        assert release.wait(timeout=10)
        return optimize(snapshot, time_limit_seconds=time_limit_seconds)

    with cloud_client(cloud_environment, injected_store, optimizer=blocked_solver) as client:
        headers = {**csrf_headers(), "X-Tenant-ID": "attacker"}
        body = {"idempotency_key": "cloud-async-run"}
        try:
            response = client.post("/api/matching/optimize", json=body, headers=headers)
            assert response.status_code == 202
            accepted = response.json()
            assert entered.wait(timeout=3)
            running = client.get(f"/api/matching/runs/{accepted['run_id']}")
            assert running.json()["status"] == "running"
            retry = client.post("/api/matching/optimize", json=body, headers=headers)
            assert retry.json() == accepted
            health = client.get("/api/health", headers={"X-Tenant-ID": "attacker"})
            assert health.json()["tenant_id"] == "demo"
        finally:
            release.set()
        run = wait_run(client, accepted["run_id"])
        assert run["status"] == "completed", run["error"]
        assert calls == ["demo"]
        proposal = run["proposals"][0]
        endpoint = f"/api/proposals/{proposal['id']}"
        refused = client.post(
            endpoint + "/confirm", headers=headers,
            json={"expected_version": proposal["version"], "idempotency_key": "before-consent"},
        )
        assert refused.status_code == 409
        consented = client.post(
            endpoint + "/respond", headers=headers,
            json={
                "decision": "consent",
                "expected_version": proposal["version"],
                "idempotency_key": "cloud-consent",
            },
        )
        assert consented.status_code == 200
        confirmation = {
            "expected_version": consented.json()["version"],
            "idempotency_key": "cloud-confirm",
        }
        assigned = client.post(endpoint + "/confirm", headers=headers, json=confirmation)
        assert assigned.status_code == 200
        retry = client.post(endpoint + "/confirm", headers=headers, json=confirmation)
        assert retry.json() == assigned.json()
        assert len(client.get("/api/assignments").json()) == 2


def test_cloud_worker_failures_remain_sanitized_and_retryable(cloud_environment, injected_store):
    def failing_solver(snapshot, time_limit_seconds):
        raise ValueError("private-input-not-for-client")

    with cloud_client(cloud_environment, injected_store, optimizer=failing_solver) as client:
        response = client.post(
            "/api/matching/optimize", headers=csrf_headers(),
            json={"idempotency_key": "cloud-failing-run"},
        )
        run = wait_run(client, response.json()["run_id"])
        assert run["status"] == "failed"
        assert "ValueError" in run["error"]
        assert "private-input" not in run["error"]
        assert client.post(
            "/api/matching/optimize", headers=csrf_headers(),
            json={"idempotency_key": "cloud-retry-run"},
        ).status_code == 202


@pytest.mark.parametrize("missing", [
    "POC_AUTH_USERNAME", "POC_AUTH_PASSWORD", "STAFF_MATCHING_TENANT",
    "PGHOST", "PGDATABASE", "PGUSER", "PGPASSWORD", "PGSSLMODE",
])
def test_cloud_fails_closed_on_missing_configuration(
    cloud_environment, injected_store, monkeypatch, missing
):
    monkeypatch.delenv(missing)
    with pytest.raises(RuntimeError, match=missing):
        with cloud_client(cloud_environment, injected_store):
            pass
    assert not injected_store.path.exists()


@pytest.mark.parametrize(("name", "value"), [
    ("POC_AUTH_PASSWORD", "too-short"),
    ("POC_AUTH_USERNAME", "invalid:username"),
    ("PGSSLMODE", "require"),
    ("PGSSLMODE", "disable"),
])
def test_cloud_rejects_weak_auth_and_database_tls(
    cloud_environment, injected_store, monkeypatch, name, value
):
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match=name):
        with cloud_client(cloud_environment, injected_store):
            pass
    assert not injected_store.path.exists()


def test_cloud_fails_before_database_initialization_without_built_ui(
    cloud_environment, injected_store
):
    (Path(cloud_environment["STATIC_DIR"]) / "index.html").unlink()
    with pytest.raises(RuntimeError, match="built frontend"):
        with cloud_client(cloud_environment, injected_store):
            pass
    assert not injected_store.path.exists()


def test_cloud_uses_postgres_constructor_and_never_falls_back_on_db_failure(
    cloud_environment, monkeypatch
):
    constructor_calls = []

    def failing_postgres():
        constructor_calls.append(True)
        raise RuntimeError("database unavailable")

    monkeypatch.setitem(
        sys.modules, "backend.postgres_store", SimpleNamespace(PostgresStore=failing_postgres)
    )
    local_path = Path(cloud_environment["STATIC_DIR"]) / "must-not-exist.sqlite3"
    monkeypatch.setenv("STAFF_MATCHING_DB", str(local_path))
    with pytest.raises(RuntimeError, match="database unavailable"):
        with TestClient(create_app()):
            pass
    assert constructor_calls == [True]
    assert not local_path.exists()


def test_cloud_rejects_sqlite_path_and_unconfigured_tenant_override(
    cloud_environment, injected_store
):
    for arguments in ({"database_path": injected_store.path}, {"tenant_id": "attacker"}):
        with pytest.raises(RuntimeError):
            with cloud_client(cloud_environment, injected_store, **arguments):
                pass
    assert not injected_store.path.exists()


def test_local_api_prefix_keeps_local_mode_without_cloud_secrets(cloud_environment, injected_store):
    with TestClient(create_app(mode="local-mock", store=injected_store)) as client:
        assert client.get("/api/health").json()["mode"] == "local-mock"
        assert client.get("/api/staff").json() == client.get("/staff").json()
        assert client.get("/healthz").json() == {"status": "ok"}
