from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from test_saas_domain import fixture_dataset

from backend.saas_api import create_saas_router
from backend.saas_models import Notification, Page, PersonSummary
from backend.saas_store import SaasStore
from backend.saas_worker import LeaseLost, SaasWorker
from backend.store import StoreError


def test_cursors_are_authenticated_tenant_and_filter_bound():
    secret = b"x" * 32
    token = SaasStore._cursor(secret, "a", {"q": "office"}, after="p20")
    assert SaasStore._cursor(secret, "a", {"q": "office"}, cursor=token) == "p20"
    for tenant, scope, supplied in [
        ("b", {"q": "office"}, token),
        ("a", {"q": ""}, token),
        ("a", {}, "bad"),
        ("a", {"q": "office"}, token[:-2] + "xx"),
    ]:
        with pytest.raises(StoreError) as error:
            SaasStore._cursor(secret, tenant, scope, cursor=supplied)
        assert error.value.status == 400


def test_fence_rejects_expired_or_replaced_attempt():
    connection = Mock()
    connection.execute.return_value.fetchone.return_value = {"ok": False}
    claim = {"tenant_id": "a", "run_id": "r", "token": "old", "attempt": 1}
    with pytest.raises(LeaseLost):
        SaasWorker._fence(connection, claim)
    connection.execute.return_value.fetchone.return_value = {"ok": True}
    SaasWorker._fence(connection, claim)


def test_runtime_readiness_is_read_only():
    connection = Mock()

    @contextmanager
    def connect():
        yield connection

    store = SaasStore()
    store.connect = connect
    connection.execute.return_value.fetchone.return_value = {"rolsuper": True, "rolbypassrls": True}
    with pytest.raises(RuntimeError):
        store.initialize()
    statements = [call.args[0].upper() for call in connection.execute.call_args_list]
    assert statements[0] == "SET TRANSACTION READ ONLY"
    assert not any(
        sql.startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER")) for sql in statements
    )


def test_store_and_worker_use_shared_connection_guard(monkeypatch):
    import backend.saas_store as module

    calls = []
    connection = object()

    @contextmanager
    def guarded(conninfo="", **kwargs):
        calls.append((conninfo, kwargs))
        yield connection

    monkeypatch.setattr(module, "connect", guarded)
    store = SaasStore("application-dsn")
    worker = SaasWorker("worker-dsn")
    for instance in (store, worker.store):
        with instance.connect() as actual:
            assert actual is connection
    assert [call[0] for call in calls] == ["application-dsn", "worker-dsn"]
    assert all(call[1]["application_name"] == "staff-saas" for call in calls)
    assert all(call[1]["connect_timeout"] == 10 for call in calls)
    assert not hasattr(store, "_connection_slots")


def test_http_identity_pagination_and_viewer_permissions():
    store = Mock()
    person = fixture_dataset().people[0].public()
    summary = PersonSummary.model_validate(person.model_dump(exclude={"employment_history"}))
    store.list_people.return_value = Page[PersonSummary](items=[summary], next_cursor=None, total=1)
    store.list_notifications.return_value = Page[Notification](items=[], next_cursor=None, total=0)
    app = FastAPI()

    @app.exception_handler(StoreError)
    async def handle(_request, error):
        return JSONResponse(
            status_code=error.status,
            content={"detail": {"code": error.code, "message": error.message}},
        )

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.identity = SimpleNamespace(
            tenant_id="server-tenant",
            user_id="actor",
            role="viewer",
        )
        return await call_next(request)

    app.include_router(create_saas_router(lambda: store, allow_simulation=True))
    with TestClient(app) as client:
        response = client.get("/api/saas/people?tenant_id=evil&limit=100")
        assert response.status_code == 200
        assert "employment_history" not in response.text
        assert "private" not in response.text
        store.list_people.assert_called_once_with("server-tenant", q="", cursor=None, limit=100)
        assert client.get("/api/saas/people?limit=101").status_code == 422
        assert (
            client.post(
                "/api/saas/matching/runs",
                json={
                    "idempotency_key": "viewer-cannot-mutate",
                },
            ).status_code
            == 403
        )
        store.create_run.assert_not_called()
        client.get("/api/saas/notifications?audience=company")
        assert store.list_notifications.call_args.kwargs["allow_simulation"] is False


def test_http_identifier_strings_are_bounded_without_changing_shared_dtos():
    store = Mock()
    app = FastAPI()

    @app.exception_handler(StoreError)
    async def handle(_request, error):
        return JSONResponse(status_code=error.status, content={"detail": {"code": error.code}})

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.identity = SimpleNamespace(
            tenant_id="server-tenant",
            user_id="actor",
            role="coordinator",
        )
        return await call_next(request)

    app.include_router(create_saas_router(lambda: store))
    with TestClient(app) as client:
        assert client.get("/api/saas/people/" + "x" * 129).status_code == 422
        assert client.get("/api/saas/sites?company_id=" + "x" * 129).status_code == 422
        for payload in (
            {"job_ids": ["x" * 129]},
            {"job_ids": [""]},
            {"pins": [["job", "x" * 129]]},
        ):
            response = client.post(
                "/api/saas/matching/runs",
                json={
                    "idempotency_key": "bounded-identifier",
                    **payload,
                },
            )
            assert response.status_code == 422
            assert response.json()["detail"]["code"] == "invalid_identifier"
        response = client.post(
            "/api/saas/introductions",
            json={
                "run_id": "run",
                "job_id": "job",
                "person_id": "x" * 129,
                "expected_job_version": 1,
                "expected_person_version": 1,
                "idempotency_key": "bounded-shortlist",
            },
        )
        assert response.status_code == 422
        store.create_run.assert_not_called()
        store.shortlist.assert_not_called()
        store.person.assert_not_called()


def test_packaged_verifier_fixture_and_admin_transaction_precondition():
    from psycopg.pq import TransactionStatus

    from backend.saas_domain_verify import _fixture, verify_domain

    dataset = _fixture("verify-saas-unit")
    assert len(dataset.jobs) == 1 and len(dataset.people) == 2
    assert dataset.jobs[0].site.company_id == dataset.companies[0].id
    admin = Mock()
    admin.info.transaction_status = TransactionStatus.INTRANS
    with pytest.raises(RuntimeError, match="administrator connection must be idle"):
        verify_domain(admin, "unused-app-dsn", "unused-worker-dsn")
    admin.commit.assert_not_called()
    admin.rollback.assert_not_called()


def test_packaged_workflow_fixture_uses_real_feasible_routes():
    from backend.saas_domain_verify import _workflow_fixture

    dataset, job, secondary, dispatch, person, alternative, dispatch_person = _workflow_fixture(
        "verify-saas-workflow-unit",
    )
    assert len(dataset.jobs) == len(dataset.people) == 31
    assert job.headcount == 1 and job.business_type == "placement"
    assert secondary.id != job.id and secondary.site_id == job.site_id
    assert person.id != alternative.id
    assert dispatch.business_type == "dispatch" and dispatch_person.id.endswith("0006")
