from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from test_saas_domain import fixture_dataset

from backend.saas_models import MatchRequest, PersonSummary
from backend.saas_store import SaasStore
from backend.store import StoreError


class Result:
    def __init__(self, *, one=None, rows=()):
        self.one, self.rows = one, list(rows)

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.rows


class CatalogConnection:
    def __init__(self, table="people"):
        self.calls, self.events = [], []
        self.tenant = {"tenant_id": "tenant", "cursor_secret": b"x" * 32}
        dataset = fixture_dataset()
        body = (
            dataset.people[0].public().model_dump(mode="json", exclude={"employment_history"})
            if table == "people"
            else dataset.jobs[0].model_dump(mode="json")
        )
        self.rows = [
            {"id": f"record-{index:04}", "body": {**body, "id": f"record-{index:04}"}}
            for index in range(51)
        ]

    def execute(self, statement, params=()):
        self.calls.append((statement, params))
        if statement.startswith("SELECT set_config"):
            return Result()
        if statement.startswith("SELECT * FROM tenants"):
            return Result(one=self.tenant)
        if statement.startswith("SELECT count(*)"):
            return Result(one={"n": 1000})
        if statement.startswith("SELECT id,"):
            assert params[-1] == 51
            return Result(rows=self.rows)
        raise AssertionError(f"Unexpected catalog query: {statement}")

    @contextmanager
    def pipeline(self):
        self.events.append("pipeline-enter")
        yield
        self.events.append("pipeline-flush")

    @contextmanager
    def transaction(self):
        self.events.append("savepoint")
        try:
            yield
        except StoreError:
            self.events.append("savepoint-rollback")
            raise
        else:
            self.events.append("savepoint-release")


def catalog_store(connection):
    store = SaasStore()

    @contextmanager
    def connect():
        try:
            yield connection
        except StoreError:
            connection.events.append("transaction-rollback")
            raise
        else:
            connection.events.append("transaction-commit")

    store.connect = connect
    return store


@pytest.mark.parametrize("table", ["people", "jobs"])
def test_catalog_pages_have_no_expiry_write_tenant_lock_or_savepoint(table):
    connection = CatalogConnection(table)
    store = catalog_store(connection)
    page = getattr(store, f"list_{table}")("tenant")
    assert len(page.items) == 50 and page.total == 1000 and page.next_cursor
    assert len(connection.calls) == 4
    assert all(statement.startswith("SELECT ") for statement, _ in connection.calls)
    assert all("FOR UPDATE" not in statement for statement, _ in connection.calls)
    assert "savepoint" not in connection.events
    assert connection.events == ["pipeline-enter", "pipeline-flush", "transaction-commit"]
    count_query, count_params = connection.calls[-2]
    assert count_query == f"SELECT count(*) AS n FROM {table} WHERE tenant_id=%s"
    assert count_params == ("tenant",)
    query, params = connection.calls[-1]
    assert "ORDER BY id LIMIT %s" in query and params == ("tenant", 51)


def test_people_page_validates_only_fifty_summaries_and_never_fetches_history(monkeypatch):
    connection = CatalogConnection()
    store = catalog_store(connection)
    validate = Mock(wraps=PersonSummary.model_validate)
    monkeypatch.setattr(PersonSummary, "model_validate", validate)
    page = store.list_people("tenant")
    assert validate.call_count == 50
    assert all("history" not in query and "private_" not in query for query, _ in connection.calls)
    assert "employment_history" not in page.model_dump_json()


def test_search_remains_filtered_and_cursor_does_not_change_total_count():
    connection = CatalogConnection()
    store = catalog_store(connection)
    first = store.list_people("tenant", q="office")
    store.list_people("tenant", q="office", cursor=first.next_cursor)
    count_query, count_params = connection.calls[-2]
    page_query, page_params = connection.calls[-1]
    assert "strpos(summary->>'name',%s)" in count_query
    assert "AND id > %s" not in count_query
    assert count_params == ("tenant", "office", "office")
    assert "AND id > %s" in page_query
    assert page_params == ("tenant", "office", "office", "record-0049", 51)
    before = len(connection.calls)
    with pytest.raises(StoreError, match="Cursor is invalid"):
        store.list_people("tenant", q="different", cursor=first.next_cursor)
    assert len(connection.calls) == before + 2  # Context/tenant, never COUNT or a page query.


def test_no_maintenance_transaction_rolls_back_on_rejection():
    connection = CatalogConnection()
    store = catalog_store(connection)
    with pytest.raises(StoreError, match="rejected"):
        with store.transaction("tenant", mutate=True, maintenance=False):
            raise StoreError(409, "rejected", "rejected")
    assert "FOR UPDATE" in connection.calls[-1][0]
    assert connection.events == ["transaction-rollback"]


def test_workflow_maintenance_still_commits_expiry_but_rolls_back_rejected_action():
    connection = CatalogConnection()
    store = catalog_store(connection)
    store._expire = Mock(side_effect=lambda *_: connection.events.append("expiry"))
    with pytest.raises(StoreError, match="rejected"):
        with store.transaction("tenant", mutate=True):
            raise StoreError(409, "rejected", "rejected")
    store._expire.assert_called_once()
    assert "FOR UPDATE" in connection.calls[-1][0]
    assert connection.events == [
        "expiry",
        "savepoint",
        "savepoint-rollback",
        "transaction-commit",
    ]


class AdmissionConnection:
    def __init__(self, jobs):
        self.jobs, self.calls, self.queued_request = jobs, [], None

    def execute(self, statement, params=()):
        self.calls.append((statement, params))
        if statement.startswith("SELECT 1 FROM runs"):
            return Result()
        if statement.startswith("SELECT count(*)"):
            return Result(one={"n": 0})
        if statement.startswith("SELECT id FROM jobs"):
            assert "body->>'status'='open'" in statement
            assert "ORDER BY id LIMIT 1001" in statement
            selected = [
                {"id": job.id}
                for job in self.jobs
                if job.status == "open" and (len(params) == 1 or job.id in params[1])
            ]
            return Result(rows=selected[:1001])
        if statement.startswith("SELECT staff_saas.enqueue"):
            return Result(one={"accepted": True})
        if statement.startswith("INSERT INTO runs"):
            self.queued_request = params[2].obj
            return Result(
                one={
                    "id": params[1],
                    "status": "queued",
                    "created_at": datetime.now(UTC),
                    "progress": 0,
                    "attempt": 0,
                    "error": None,
                    "coverage": {},
                    "recommended_count": 0,
                    "solver_status": None,
                    "objective_bound": None,
                    "model_version": params[3],
                }
            )
        if statement.startswith("INSERT INTO audit_events"):
            return Result()
        raise AssertionError(f"Unexpected admission query: {statement}")


def test_full_tenant_admission_selects_all_open_jobs_not_closed_records():
    from backend.saas_data import generate_tenant

    dataset = generate_tenant("demo", count=1000)
    assert len(dataset.jobs) == 1000
    open_jobs = [job for job in dataset.jobs if job.status == "open"]
    assert 900 < len(open_jobs) < 1000
    connection = AdmissionConnection(dataset.jobs)
    store = SaasStore()
    store._mutate = lambda tenant, user, operation, request, model, callback: callback(connection)
    run = store.create_run("demo", "actor", MatchRequest(idempotency_key="complete-open-scope"))
    assert run.status == "queued"
    assert len(connection.queued_request["job_ids"]) == len(open_jobs)
    assert set(connection.queued_request["job_ids"]) == {
        job.id for job in dataset.jobs if job.status == "open"
    }
    assert sum(job.headcount for job in open_jobs) > len(open_jobs)
    assert not any("SELECT body FROM jobs" in query for query, _ in connection.calls)
    closed = next(job for job in dataset.jobs if job.status == "closed")
    with pytest.raises(StoreError) as error:
        store.create_run(
            "demo",
            "actor",
            MatchRequest(
                idempotency_key="explicit-closed-job",
                job_ids=[closed.id],
            ),
        )
    assert error.value.code == "job_unavailable"
