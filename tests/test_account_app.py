import sys
from datetime import UTC, datetime
from types import ModuleType

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.identity import Identity
from backend.models import OptimizeRequest, RunAccepted
from backend.seed import demo_snapshot
from backend.store import Store, StoreError

ORIGIN = "https://staff.example"


class AdapterIdentityFixture:
    def initialize(self):
        pass

    def resolve_session(self, token):
        accounts = {
            "a" * 43: ("tenant-a", "coordinator"),
            "b" * 43: ("tenant-b", "coordinator"),
            "v" * 43: ("tenant-a", "viewer"),
        }
        if token not in accounts:
            return None
        tenant, role = accounts[token]
        return Identity(
            user_id="synthetic", username="synthetic", display_name="synthetic",
            tenant_id=tenant, role=role, session_id="synthetic",
            authenticated_at=datetime.now(UTC), auth_method="password",
        )


class RecordingMatchingStore(Store):
    def __init__(self, path):
        super().__init__(path)
        self.calls = []

    def create_run(self, tenant, config):
        self.calls.append(("create", tenant))
        return RunAccepted(run_id=tenant, status="queued"), True

    def start_run(self, tenant, run_id):
        assert run_id == tenant
        self.calls.append(("start", tenant))
        return (
            demo_snapshot().model_copy(update={"tenant_id": tenant}),
            OptimizeRequest(idempotency_key="adapter-test", time_limit_seconds=0.1),
        )

    def publish_run(self, tenant, run_id, result):
        assert run_id == tenant
        assert result.optimized.filled_slots > 0
        self.calls.append(("publish", tenant))


def test_accounts_adapter_captures_tenant_for_each_background_run(tmp_path, monkeypatch):
    for key, value in {
        "POC_AUTH_MODE": "accounts", "POC_AUTH_ORIGIN": ORIGIN,
        "PGHOST": "synthetic.invalid", "PGDATABASE": "synthetic",
        "PGUSER": "synthetic", "PGPASSWORD": "synthetic", "PGSSLMODE": "verify-full",
    }.items():
        monkeypatch.setenv(key, value)
    static = tmp_path / "web"
    static.mkdir()
    (static / "assets").mkdir()
    (static / "index.html").write_text("<h1>synthetic</h1>")
    monkeypatch.setenv("STATIC_DIR", str(static))
    store = RecordingMatchingStore(tmp_path / "adapter.sqlite3")
    app = create_app(mode="azure-poc", store=store, identity_store=AdapterIdentityFixture())
    with TestClient(app, base_url=ORIGIN) as client:
        for path in (
            "/api/health", "/api/workspace", "/health", "/staff", "/docs", "/openapi.json",
        ):
            assert client.get(path).status_code == 401
        invalid_password = "secret-input-that-must-not-be-echoed" * 5
        invalid = client.post(
            "/api/auth/login", json={"username": "synthetic", "password": invalid_password},
            headers={"Origin": ORIGIN, "X-Requested-With": "staff-matching"},
        )
        assert invalid.status_code == 422
        assert invalid_password not in invalid.text
        for token, tenant in (("a" * 43, "tenant-a"), ("b" * 43, "tenant-b")):
            client.cookies.set("__Host-staff-session", token, domain="staff.example", path="/")
            assert client.get("/api/health").json()["tenant_id"] == tenant
            assert client.get("/api/workspace").json() == {"kind": "legacy"}
            assert client.get("/health").json()["tenant_id"] == tenant
            assert client.get("/staff", headers={"X-Tenant-ID": "foreign"}).status_code == 409
            oversized = client.post(
                "/api/saas/matching/runs", content=b"x" * (262144 + 1),
                headers={"Origin": ORIGIN, "X-Requested-With": "staff-matching"},
            )
            assert oversized.status_code == 413
            assert oversized.json()["detail"]["code"] == "request_too_large"
            result = client.post(
                "/api/matching/optimize", json={"idempotency_key": "adapter-test"},
                headers={"Origin": ORIGIN, "X-Requested-With": "staff-matching",
                         "X-Tenant-ID": tenant},
            )
            assert result.status_code == 202
        client.cookies.set("__Host-staff-session", "v" * 43, domain="staff.example", path="/")
        for path in ("/api/matching/optimize", "/matching/optimize"):
            assert client.post(
                path, json={"idempotency_key": "adapter-test"},
                headers={"Origin": ORIGIN, "X-Requested-With": "staff-matching"},
            ).status_code == 403
        assert client.get("/docs", follow_redirects=False).headers["location"] == "/openapi.json"
        assert client.get("/openapi.json").status_code == 200
        def busy(_token):
            raise StoreError(503, "database_busy", "Temporarily unavailable.")

        monkeypatch.setattr(app.state.identity_store, "resolve_session", busy)
        unavailable = client.get("/api/workspace")
        assert unavailable.status_code == 503
        assert unavailable.json()["detail"]["code"] == "database_busy"
    assert sorted(store.calls) == sorted(
        (phase, tenant)
        for phase in ("create", "start", "publish") for tenant in ("tenant-a", "tenant-b")
    )


@pytest.mark.parametrize(("settings", "message"), [
    ({"SAAS_ENABLED": "true"}, "requires Azure PostgreSQL"),
    ({"SAAS_ENABLED": "yes"}, "SAAS_ENABLED must"),
    ({"SAAS_DEMO_SIMULATION": "true"}, "requires the explicit SaaS"),
])
def test_saas_configuration_never_silently_selects_legacy(settings, message, monkeypatch):
    monkeypatch.delenv("SAAS_ENABLED", raising=False)
    monkeypatch.delenv("SAAS_DEMO_SIMULATION", raising=False)
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match=message):
        create_app(mode="local-mock")


def test_saas_lifecycle_and_capability_are_wired_to_the_authenticated_workspace(
    tmp_path, monkeypatch,
):
    for key, value in {
        "POC_AUTH_MODE": "accounts", "POC_AUTH_ORIGIN": ORIGIN,
        "PGHOST": "synthetic.invalid", "PGDATABASE": "synthetic",
        "PGUSER": "synthetic", "PGPASSWORD": "synthetic", "PGSSLMODE": "verify-full",
        "SAAS_ENABLED": "true", "SAAS_DEMO_SIMULATION": "false",
    }.items():
        monkeypatch.setenv(key, value)
    static = tmp_path / "web"
    static.mkdir()
    (static / "assets").mkdir()
    (static / "index.html").write_text("<h1>synthetic</h1>")
    monkeypatch.setenv("STATIC_DIR", str(static))

    class InjectedSaasStore:
        ready = False

        def initialize(self):
            self.ready = True

    store_module = ModuleType("backend.saas_store")
    store_module.SaasStore = InjectedSaasStore
    api_module = ModuleType("backend.saas_api")

    def router_factory(*, store, allow_simulation):
        assert not allow_simulation
        router = APIRouter(prefix="/api/saas")

        @router.get("/integration-check")
        def integration_check():
            return {"ready": store().ready}

        return router

    api_module.create_saas_router = router_factory
    monkeypatch.setitem(sys.modules, "backend.saas_store", store_module)
    monkeypatch.setitem(sys.modules, "backend.saas_api", api_module)
    domain = InjectedSaasStore()
    app = create_app(
        mode="azure-poc", store=Store(tmp_path / "gateway.sqlite3"),
        identity_store=AdapterIdentityFixture(), saas_store=domain,
    )
    with TestClient(app, base_url=ORIGIN) as client:
        assert domain.ready
        for path in ("/api/workspace", "/api/saas/integration-check", "/api/saas/geo/manifest"):
            assert client.get(path).status_code == 401
        assert client.get("/assets/geography/private-people.json").status_code == 404
        client.cookies.set("__Host-staff-session", "a" * 43, domain="staff.example", path="/")
        assert client.get("/api/workspace").json() == {"kind": "saas"}
        assert client.get("/api/saas/integration-check").json() == {"ready": True}
        assert client.get("/api/workspace", headers={"X-Tenant-ID": "tenant-b"}).status_code == 409
