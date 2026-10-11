"""Private-network acceptance using real account middleware, PostgreSQL, and worker."""

import json
import os
import secrets
import subprocess
import sys
import time
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
from psycopg import sql

from backend.account_seed import ACCOUNTS
from backend.poc_accounts_verify import _response
from backend.postgres_setup import runtime_conninfo
from backend.saas_setup import worker_conninfo


def _passed(check: str, **measurements) -> None:
    print(json.dumps({"status": "PASS", "check": check, **measurements}), flush=True)


def _page(client, path: str, **filters) -> list[dict]:
    items = []
    cursor = None
    while True:
        result = _response(client.get(path, params={"limit": 100, **filters, **(
            {"cursor": cursor} if cursor else {}
        )}))
        items.extend(result["items"])
        cursor = result["next_cursor"]
        if cursor is None:
            return items
        if len(items) > 20000:
            raise AssertionError("Pagination did not terminate within the acceptance budget.")


def _wait_run(client, run_id: str, worker: subprocess.Popen, timeout: int = 180) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if worker.poll() is not None:
            raise RuntimeError(f"Acceptance worker exited unexpectedly: {worker.returncode}.")
        run = _response(client.get(f"/api/saas/matching/runs/{run_id}"))
        if run["status"] in ("failed", "cancelled"):
            raise AssertionError(f"Acceptance matching ended as {run['status']}.")
        if run["status"] == "completed":
            return run
        time.sleep(0.5)
    raise TimeoutError("Acceptance matching did not complete within its bounded budget.")


def verify_saas(admin_connection, app_password: str, worker_password: str) -> None:
    os.environ.update(
        APP_MODE="azure-poc", POC_AUTH_MODE="accounts",
        SAAS_ENABLED="true", SAAS_DEMO_SIMULATION="true",
    )
    from backend.app import create_app
    from backend.identity import IdentityStore
    from backend.postgres_store import PostgresStore
    from backend.saas_domain_verify import verify_domain, workflow_dataset
    from backend.saas_routing import commute_allowed
    from backend.saas_store import SaasStore, grant_demo_simulation, load_dataset

    tenants = [account.tenant_id for account in ACCOUNTS if account.role == "coordinator"]
    for tenant in tenants:
        with admin_connection.transaction():
            admin_connection.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant,))
            for table in ("jobs", "people"):
                count = admin_connection.execute(sql.SQL(
                    "SELECT count(*) AS count FROM staff_saas.{} WHERE tenant_id = %s"
                ).format(sql.Identifier(table)), (tenant,)).fetchone()["count"]
                assert count == 1000
    _passed("saas-exact-database-counts", tenants=10, jobs=10000, people=10000)
    assert commute_allowed(10799, 180)
    assert not commute_allowed(10800, 240)
    assert not commute_allowed(10801, 240)
    assert not commute_allowed(10799, 179)
    assert not commute_allowed(None, 180)
    _passed("saas-unrounded-commute-boundary")

    admin = IdentityStore(admin_connection.info.dsn)
    dsn = runtime_conninfo(admin_connection, app_password)
    admin_connection.commit()
    verification = verify_domain(
        admin_connection, dsn, worker_conninfo(admin_connection, worker_password),
    )
    _passed("saas-database-isolation-and-durable-leases", report=verification)
    suffix = uuid4().hex[:16]
    tenant_a, tenant_b = (f"validation-saas-{suffix}-{side}" for side in ("a", "b"))
    users = [
        (f"saas-a-{suffix}", tenant_a, "coordinator"),
        (f"saas-b-{suffix}", tenant_b, "coordinator"),
        (f"saas-v-{suffix}", tenant_a, "viewer"),
    ]
    password = secrets.token_urlsafe(32)
    origin = os.environ["POC_AUTH_ORIGIN"]
    worker = None
    provisioned = []
    try:
        for username, tenant, role in users:
            admin.provision_user(
                username=username, display_name=username, password=password,
                tenant_id=tenant, tenant_name=tenant, role=role,
            )
            provisioned.append(username)
        datasets = [workflow_dataset(tenant) for tenant in (tenant_a, tenant_b)]
        for dataset in datasets:
            load_dataset(admin_connection, dataset)
            load_dataset(admin_connection, dataset)
            admin_connection.commit()
        with admin_connection.transaction():
            user = admin_connection.execute(
                "SELECT id FROM staff_identity.users WHERE username = %s", (users[0][0],),
            ).fetchone()
            grant_demo_simulation(admin_connection, tenant_a, str(user["id"]))

        def app():
            return create_app(
                mode="azure-poc", store=PostgresStore(dsn), identity_store=IdentityStore(dsn),
                saas_store=SaasStore(dsn),
            )

        with ExitStack() as stack:
            clients = [
                stack.enter_context(TestClient(app(), base_url=origin)) for _ in users
            ]
            first, second, viewer = clients
            for client, (username, tenant, _role) in zip(clients, users, strict=True):
                client.headers.update({"Origin": origin, "X-Requested-With": "staff-matching"})
                _response(client.get("/api/saas/overview"), 401)
                session = _response(client.post("/api/auth/login", json={
                    "username": username, "password": password,
                }))
                assert session["tenant"]["id"] == tenant
                client.headers["X-Tenant-ID"] = tenant
                assert _response(client.get("/api/workspace")) == {"kind": "saas"}
                overview = _response(client.get("/api/saas/overview"))
                assert overview["tenant_id"] == tenant
                assert overview["jobs"] == overview["people"] == 30
            assert _response(first.get("/api/saas/overview"))["can_simulate"]
            assert not _response(second.get("/api/saas/overview"))["can_simulate"]
            assert not _response(viewer.get("/api/saas/overview"))["can_simulate"]
            _response(first.get("/api/saas/jobs", headers={"X-Tenant-ID": tenant_b}), 409)
            _response(first.post("/api/auth/tenant", json={"tenant_id": tenant_b}), 403)
            _response(viewer.post("/api/saas/matching/runs", json={
                "idempotency_key": uuid4().hex,
            }), 403)
            people = _page(first, "/api/saas/people")
            assert len(people) == 30
            assert "employment_history" not in people[0]
            person = _response(first.get(f"/api/saas/people/{people[0]['id']}"))
            assert "employment_history" in person
            for value in (people, person):
                encoded = json.dumps(value, ensure_ascii=False)
                for forbidden in ("private_location", "private_address", "contact_email"):
                    assert forbidden not in encoded
            _response(first.get("/api/saas/geo/basemap"))
            _response(first.get("/api/saas/geo/manifest"))
            _passed("saas-authenticated-tenancy-and-public-person-dtos")

            payload = {"idempotency_key": uuid4().hex}
            accepted = _response(first.post("/api/saas/matching/runs", json=payload), 202)
            replay = _response(first.post("/api/saas/matching/runs", json=payload), 202)
            assert accepted["id"] == replay["id"]
            _response(second.get(f"/api/saas/matching/runs/{accepted['id']}"), 404)
            other = _response(second.post("/api/saas/matching/runs", json={
                "idempotency_key": uuid4().hex,
            }), 202)
            # Work was accepted before this separate, least-privileged process existed.
            parameters = admin_connection.info.get_parameters()
            worker_env = {
                key: value for key, value in os.environ.items()
                if not key.startswith(("PG", "POC_", "SAAS_"))
            }
            for key, pg_key in (
                ("host", "PGHOST"), ("port", "PGPORT"), ("dbname", "PGDATABASE"),
                ("sslmode", "PGSSLMODE"), ("sslrootcert", "PGSSLROOTCERT"),
            ):
                if parameters.get(key):
                    worker_env[pg_key] = parameters[key]
            worker_env.update(PGUSER="staff_worker", PGPASSWORD=worker_password)
            worker = subprocess.Popen(
                [sys.executable, "-m", "backend.saas_worker"], env=worker_env,
            )
            for client, run_id in ((first, accepted["id"]), (second, other["id"])):
                completed = _wait_run(client, run_id, worker)
                assert completed["recommended_count"] > 0
                assert completed["coverage"]["jobs"] == 30
            _passed("saas-durable-two-tenant-matching")
            candidates = _page(
                first, f"/api/saas/matching/runs/{accepted['id']}/candidates",
            )
            jobs = {job["id"]: job for job in _page(first, "/api/saas/jobs")}
            eligible = [item for item in candidates if item["eligible"]]
            assert eligible
            placement_choices = sorted(
                [item for item in eligible if jobs[item["job_id"]]["business_type"] == "placement"],
                key=lambda item: item["recommended"],
            )
            assert placement_choices, "No eligible placement candidate in the acceptance fixture."
            placement = placement_choices[0]

            def shortlist(candidate):
                body = {
                    "run_id": accepted["id"], "job_id": candidate["job_id"],
                    "person_id": candidate["person_id"],
                    "expected_job_version": candidate["job_version"],
                    "expected_person_version": candidate["person_version"],
                    "idempotency_key": uuid4().hex,
                }
                intro = _response(first.post("/api/saas/introductions", json=body), 201)
                assert _response(first.post("/api/saas/introductions", json=body), 201) == intro
                return intro

            introduction = shortlist(placement)
            introduction_path = f"/api/saas/introductions/{introduction['id']}"
            for action in ("company_request", "person_consent"):
                body = {
                    "expected_version": introduction["version"], "idempotency_key": uuid4().hex,
                    "action": action, "simulated": True,
                }
                introduction = _response(first.post(introduction_path + "/simulate", json=body))
            assert introduction["status"] == "consented"
            start = (datetime.now(UTC) + timedelta(days=2)).replace(
                hour=2, minute=0, second=0, microsecond=0,
            )
            appointment = _response(first.post(introduction_path + "/appointments", json={
                "expected_version": introduction["version"], "idempotency_key": uuid4().hex,
                "start_at": start.isoformat(),
                "end_at": (start + timedelta(minutes=30)).isoformat(),
                "online": True,
            }), 201)
            appointment = _response(first.post(
                f"/api/saas/appointments/{appointment['id']}/accept",
                json={"expected_version": appointment["version"], "idempotency_key": uuid4().hex},
            ))
            assert appointment["status"] == "accepted"
            introduction = next(
                item for item in _page(first, "/api/saas/introductions")
                if item["id"] == introduction["id"]
            )
            placed = _response(first.post(introduction_path + "/place", json={
                "expected_version": introduction["version"], "idempotency_key": uuid4().hex,
            }))
            assert placed["status"] == "placed"
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                notifications = [
                    item
                    for audience in ("management", "company", "person")
                    for item in _page(first, "/api/saas/notifications", audience=audience)
                ]
                if {item["audience"] for item in notifications} == {
                    "management", "company", "person",
                }:
                    break
                time.sleep(0.5)
            else:
                raise TimeoutError("Transactional inbox delivery did not complete.")
            _passed("saas-shortlist-consent-interview-placement-inboxes")
    finally:
        try:
            if worker is not None:
                worker.terminate()
                try:
                    worker.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    worker.wait(timeout=5)
                    raise RuntimeError("Acceptance worker did not shut down gracefully.") from None
        finally:
            for username in provisioned:
                admin.recover_password(
                    username, secrets.token_urlsafe(48), revoke_passkeys=True,
                    actor="saas-verification-cleanup",
                )
    _passed("saas-private-acceptance", worker_role="staff_worker")
