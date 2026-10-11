"""Tenant-scoped SQLite persistence for the single-process local mock."""

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from backend.models import (
    Assignment,
    Candidate,
    ConfirmRequest,
    OptimizationResult,
    OptimizeRequest,
    Order,
    PairEvidence,
    Proposal,
    RespondRequest,
    RunAccepted,
    RunView,
    Snapshot,
    Staff,
)
from backend.seed import demo_snapshot


class StoreError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS staff (
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    id TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE IF NOT EXISTS orders (
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    id TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE IF NOT EXISTS evidence (
    tenant_id TEXT NOT NULL,
    staff_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (tenant_id, staff_id, order_id),
    FOREIGN KEY (tenant_id, staff_id) REFERENCES staff(tenant_id, id),
    FOREIGN KEY (tenant_id, order_id) REFERENCES orders(tenant_id, id)
);
CREATE TABLE IF NOT EXISTS runs (
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'completed', 'failed')),
    created_at TEXT NOT NULL,
    input_revision INTEGER NOT NULL,
    config TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    algorithm_config TEXT NOT NULL,
    error TEXT,
    result TEXT,
    PRIMARY KEY (tenant_id, id)
);
CREATE TABLE IF NOT EXISTS proposals (
    tenant_id TEXT NOT NULL,
    id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    staff_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(
        status IN ('proposed', 'consented', 'declined', 'expired', 'confirmed')
    ),
    version INTEGER NOT NULL,
    input_revision INTEGER NOT NULL,
    staff_version INTEGER NOT NULL,
    order_version INTEGER NOT NULL,
    evidence_version INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    responded_at TEXT,
    confirmed_assignment_id TEXT,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, run_id, staff_id, order_id),
    FOREIGN KEY (tenant_id, run_id) REFERENCES runs(tenant_id, id),
    FOREIGN KEY (tenant_id, staff_id, order_id)
        REFERENCES evidence(tenant_id, staff_id, order_id),
    FOREIGN KEY (tenant_id, confirmed_assignment_id)
        REFERENCES assignments(tenant_id, id)
);
CREATE TABLE IF NOT EXISTS assignments (
    tenant_id TEXT NOT NULL,
    id TEXT NOT NULL,
    staff_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    proposal_id TEXT,
    source_run_id TEXT,
    body TEXT NOT NULL,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, proposal_id),
    FOREIGN KEY (tenant_id, staff_id) REFERENCES staff(tenant_id, id),
    FOREIGN KEY (tenant_id, order_id) REFERENCES orders(tenant_id, id),
    FOREIGN KEY (tenant_id, proposal_id) REFERENCES proposals(tenant_id, id),
    FOREIGN KEY (tenant_id, source_run_id) REFERENCES runs(tenant_id, id)
);
CREATE TABLE IF NOT EXISTS response_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    proposal_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (tenant_id, proposal_id) REFERENCES proposals(tenant_id, id)
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    event TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    assignment_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (tenant_id, proposal_id) REFERENCES proposals(tenant_id, id),
    FOREIGN KEY (tenant_id, assignment_id) REFERENCES assignments(tenant_id, id)
);
CREATE TABLE IF NOT EXISTS idempotency (
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    operation TEXT NOT NULL,
    key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    response TEXT NOT NULL,
    PRIMARY KEY (tenant_id, operation, key)
);
CREATE INDEX IF NOT EXISTS runs_recent ON runs(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS proposals_active ON proposals(tenant_id, status, expires_at);
"""


class Store:
    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
        proposal_ttl_seconds: int = 1800,
    ):
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.proposal_ttl_seconds = proposal_ttl_seconds

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
            connection.execute("BEGIN IMMEDIATE")
            try:
                if not connection.execute("SELECT 1 FROM tenants LIMIT 1").fetchone():
                    self._insert_snapshot(connection, demo_snapshot())
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @contextmanager
    def server_lock(self) -> Iterator[None]:
        """A POSIX advisory lock prevents another server from interrupting live runs."""
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + "-lock")
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError(
                    "This local SQLite mock permits only one server process per database."
                ) from error
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self, tenant_id: str) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._tenant(connection, tenant_id)
                self._expire(connection, tenant_id)
                connection.execute("SAVEPOINT action")
                try:
                    yield connection
                except StoreError:
                    # Keep expiry materialization even when a lifecycle action is rejected.
                    connection.execute("ROLLBACK TO action")
                    connection.commit()
                    raise
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _tenant(connection: sqlite3.Connection, tenant_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
        if row is None:
            raise StoreError(404, "tenant_not_found", "Local mock tenant not found.")
        return row

    @staticmethod
    def _revision(connection: sqlite3.Connection, tenant_id: str) -> None:
        connection.execute("UPDATE tenants SET revision = revision + 1 WHERE id = ?", (tenant_id,))

    def _expire(self, connection: sqlite3.Connection, tenant_id: str) -> None:
        now = self.clock().isoformat()
        rows = connection.execute(
            "SELECT id FROM proposals WHERE tenant_id = ? "
            "AND status IN ('proposed', 'consented') AND expires_at <= ?",
            (tenant_id, now),
        ).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE proposals SET status = 'expired', version = version + 1 "
                "WHERE tenant_id = ? AND id = ?",
                (tenant_id, row["id"]),
            )
            connection.execute(
                "INSERT INTO audit_events(tenant_id, event, proposal_id, created_at) "
                "VALUES (?, 'expired', ?, ?)",
                (tenant_id, row["id"], now),
            )
        if rows:
            self._revision(connection, tenant_id)

    @staticmethod
    def _insert_snapshot(connection: sqlite3.Connection, snapshot: Snapshot) -> None:
        tenant_id = snapshot.tenant_id
        connection.execute(
            "INSERT INTO tenants(id, revision) VALUES (?, ?)", (tenant_id, snapshot.revision)
        )
        for table, models in (("staff", snapshot.staff), ("orders", snapshot.orders)):
            connection.executemany(
                f"INSERT INTO {table}(tenant_id, id, body) VALUES (?, ?, ?)",
                [(tenant_id, item.id, item.model_dump_json()) for item in models],
            )
        connection.executemany(
            "INSERT INTO evidence(tenant_id, staff_id, order_id, body) VALUES (?, ?, ?, ?)",
            [
                (tenant_id, item.staff_id, item.order_id, item.model_dump_json())
                for item in snapshot.evidence
            ],
        )
        for assignment in snapshot.assignments:
            Store._insert_assignment(connection, tenant_id, assignment)

    def add_mock_tenant(self, snapshot: Snapshot) -> None:
        """Fixture-only domain import; deliberately not exposed through HTTP."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._insert_snapshot(connection, snapshot)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def _snapshot(connection: sqlite3.Connection, tenant_id: str) -> Snapshot:
        revision = Store._tenant(connection, tenant_id)["revision"]
        collections = {}
        for table, model in (
            ("staff", Staff),
            ("orders", Order),
            ("evidence", PairEvidence),
            ("assignments", Assignment),
        ):
            collections[table] = [
                model.model_validate_json(row["body"])
                for row in connection.execute(
                    f"SELECT body FROM {table} WHERE tenant_id = ? ORDER BY rowid", (tenant_id,)
                )
            ]
        return Snapshot(tenant_id=tenant_id, revision=revision, **collections)

    def snapshot(self, tenant_id: str) -> Snapshot:
        with self._transaction(tenant_id) as connection:
            return self._snapshot(connection, tenant_id)

    def update_domain(self, tenant_id: str, item: Staff | Order | PairEvidence) -> None:
        """Local fixture edits must use increasing versions and invalidate in-flight runs."""
        with self._transaction(tenant_id) as connection:
            if isinstance(item, PairEvidence):
                table, predicate = "evidence", "staff_id = ? AND order_id = ?"
                identifiers = (item.staff_id, item.order_id)
            else:
                table = "staff" if isinstance(item, Staff) else "orders"
                predicate, identifiers = "id = ?", (item.id,)
            row = connection.execute(
                f"SELECT body FROM {table} WHERE tenant_id = ? AND {predicate}",
                (tenant_id, *identifiers),
            ).fetchone()
            if row is None:
                raise StoreError(404, "not_found", "Domain record not found.")
            if item.version <= json.loads(row["body"])["version"]:
                raise StoreError(409, "stale_version", "Domain version must increase.")
            connection.execute(
                f"UPDATE {table} SET body = ? WHERE tenant_id = ? AND {predicate}",
                (item.model_dump_json(), tenant_id, *identifiers),
            )
            self._revision(connection, tenant_id)

    @staticmethod
    def _fingerprint(payload: dict) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _replay(
        connection: sqlite3.Connection,
        tenant_id: str,
        operation: str,
        key: str,
        payload: dict,
    ) -> str | None:
        row = connection.execute(
            "SELECT fingerprint, response FROM idempotency "
            "WHERE tenant_id = ? AND operation = ? AND key = ?",
            (tenant_id, operation, key),
        ).fetchone()
        if row is None:
            return None
        if row["fingerprint"] != Store._fingerprint(payload):
            raise StoreError(409, "idempotency_conflict", "Key already used for another request.")
        return row["response"]

    @staticmethod
    def _remember(
        connection: sqlite3.Connection,
        tenant_id: str,
        operation: str,
        key: str,
        payload: dict,
        response: str,
    ) -> None:
        connection.execute(
            "INSERT INTO idempotency VALUES (?, ?, ?, ?, ?)",
            (tenant_id, operation, key, Store._fingerprint(payload), response),
        )

    @staticmethod
    def _active_batch(
        connection: sqlite3.Connection, tenant_id: str, exclude_run_id: str = ""
    ) -> bool:
        return bool(
            connection.execute(
                "SELECT 1 FROM runs WHERE tenant_id = ? AND status IN ('queued', 'running') "
                "AND id != ? LIMIT 1",
                (tenant_id, exclude_run_id),
            ).fetchone()
            or connection.execute(
                "SELECT 1 FROM proposals WHERE tenant_id = ? "
                "AND status IN ('proposed', 'consented') LIMIT 1",
                (tenant_id,),
            ).fetchone()
        )

    def create_run(self, tenant_id: str, request: OptimizeRequest) -> tuple[RunAccepted, bool]:
        with self._transaction(tenant_id) as connection:
            payload = request.model_dump(mode="json")
            replay = self._replay(
                connection, tenant_id, "optimize", request.idempotency_key, payload
            )
            if replay is not None:
                return RunAccepted.model_validate_json(replay), False
            if self._active_batch(connection, tenant_id):
                raise StoreError(
                    409,
                    "active_batch",
                    "Resolve all proposals before rerunning; "
                    "consent carry-forward is not supported.",
                )
            snapshot = self._snapshot(connection, tenant_id)
            run_id = f"run-{uuid4().hex}"
            connection.execute(
                "INSERT INTO runs(tenant_id, id, status, created_at, input_revision, config, "
                "snapshot, algorithm_config) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?)",
                (
                    tenant_id,
                    run_id,
                    self.clock().isoformat(),
                    snapshot.revision,
                    request.model_dump_json(),
                    snapshot.model_dump_json(),
                    json.dumps(
                        {
                            "weights_version": "mock-rules-v1",
                            "seed": 0,
                            "workers": 1,
                            "score_scale": 10000,
                            "scheduling_mode": request.scheduling_mode,
                            "time_limit_seconds": request.time_limit_seconds,
                        }
                    ),
                ),
            )
            accepted = RunAccepted(run_id=run_id, status="queued")
            self._remember(
                connection,
                tenant_id,
                "optimize",
                request.idempotency_key,
                payload,
                accepted.model_dump_json(),
            )
            return accepted, True

    @staticmethod
    def _run(connection: sqlite3.Connection, tenant_id: str, run_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM runs WHERE tenant_id = ? AND id = ?", (tenant_id, run_id)
        ).fetchone()
        if row is None:
            raise StoreError(404, "run_not_found", "Run not found.")
        return row

    def start_run(self, tenant_id: str, run_id: str) -> tuple[Snapshot, OptimizeRequest]:
        with self._transaction(tenant_id) as connection:
            row = self._run(connection, tenant_id, run_id)
            if row["status"] != "queued":
                raise StoreError(409, "run_state", "Run is not queued.")
            connection.execute(
                "UPDATE runs SET status = 'running' WHERE tenant_id = ? AND id = ?",
                (tenant_id, run_id),
            )
            return (
                Snapshot.model_validate_json(row["snapshot"]),
                OptimizeRequest.model_validate_json(row["config"]),
            )

    def fail_run(self, tenant_id: str, run_id: str, error: str) -> None:
        with self._transaction(tenant_id) as connection:
            self._run(connection, tenant_id, run_id)
            connection.execute(
                "UPDATE runs SET status = 'failed', error = ? WHERE tenant_id = ? AND id = ? "
                "AND status IN ('queued', 'running')",
                (error, tenant_id, run_id),
            )

    def recover_interrupted_runs(self, tenant_id: str) -> None:
        # An unconfigured tenant has no jobs to recover and never inherits demo data.
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE runs SET status = 'failed', error = ? "
                "WHERE tenant_id = ? AND status IN ('queued', 'running')",
                ("Interrupted by application restart; submit a new local run.", tenant_id),
            )
            connection.commit()

    def publish_run(self, tenant_id: str, run_id: str, result: OptimizationResult) -> bool:
        with self._transaction(tenant_id) as connection:
            row = self._run(connection, tenant_id, run_id)
            if row["status"] != "running":
                raise StoreError(409, "run_state", "Run is not running.")
            revision = self._tenant(connection, tenant_id)["revision"]
            if revision != row["input_revision"] or self._active_batch(
                connection, tenant_id, exclude_run_id=run_id
            ):
                connection.execute(
                    "UPDATE runs SET status = 'failed', error = ? WHERE tenant_id = ? AND id = ?",
                    (
                        "Input revision or active batch changed before publication.",
                        tenant_id,
                        run_id,
                    ),
                )
                return False
            snapshot = Snapshot.model_validate_json(row["snapshot"])
            staff = {item.id: item for item in snapshot.staff}
            orders = {item.id: item for item in snapshot.orders}
            evidence = {(item.staff_id, item.order_id): item for item in snapshot.evidence}
            if result.input_revision != snapshot.revision:
                raise ValueError("Solver result does not match the persisted snapshot revision.")
            expiry = (self.clock() + timedelta(seconds=self.proposal_ttl_seconds)).isoformat()
            for pair in result.optimized.pairs:
                connection.execute(
                    "INSERT INTO proposals(tenant_id, id, run_id, staff_id, order_id, status, "
                    "version, input_revision, staff_version, order_version, evidence_version, "
                    "expires_at) VALUES (?, ?, ?, ?, ?, 'proposed', 1, ?, ?, ?, ?, ?)",
                    (
                        tenant_id,
                        f"proposal-{uuid4().hex}",
                        run_id,
                        pair.staff_id,
                        pair.order_id,
                        snapshot.revision,
                        staff[pair.staff_id].version,
                        orders[pair.order_id].version,
                        evidence[(pair.staff_id, pair.order_id)].version,
                        expiry,
                    ),
                )
            connection.execute(
                "UPDATE runs SET status = 'completed', result = ? WHERE tenant_id = ? AND id = ?",
                (result.model_dump_json(), tenant_id, run_id),
            )
            return True

    @staticmethod
    def _proposal(connection: sqlite3.Connection, tenant_id: str, proposal_id: str) -> Proposal:
        row = connection.execute(
            "SELECT * FROM proposals WHERE tenant_id = ? AND id = ?", (tenant_id, proposal_id)
        ).fetchone()
        if row is None:
            raise StoreError(404, "proposal_not_found", "Proposal not found.")
        return Proposal.model_validate({field: row[field] for field in Proposal.model_fields})

    def get_proposal(self, tenant_id: str, proposal_id: str) -> Proposal:
        with self._transaction(tenant_id) as connection:
            return self._proposal(connection, tenant_id, proposal_id)

    @staticmethod
    def _run_view(connection: sqlite3.Connection, tenant_id: str, row: sqlite3.Row) -> RunView:
        proposals = [
            Store._proposal(connection, tenant_id, item["id"])
            for item in connection.execute(
                "SELECT id FROM proposals WHERE tenant_id = ? AND run_id = ? ORDER BY rowid",
                (tenant_id, row["id"]),
            )
        ]
        return RunView(
            id=row["id"],
            status=row["status"],
            created_at=row["created_at"],
            input_revision=row["input_revision"],
            config=OptimizeRequest.model_validate_json(row["config"]),
            error=row["error"],
            result=OptimizationResult.model_validate_json(row["result"]) if row["result"] else None,
            proposals=proposals,
        )

    def get_run(self, tenant_id: str, run_id: str) -> RunView:
        with self._transaction(tenant_id) as connection:
            return self._run_view(connection, tenant_id, self._run(connection, tenant_id, run_id))

    def list_runs(self, tenant_id: str) -> list[RunView]:
        with self._transaction(tenant_id) as connection:
            rows = connection.execute(
                "SELECT * FROM runs WHERE tenant_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 20",
                (tenant_id,),
            ).fetchall()
            return [self._run_view(connection, tenant_id, row) for row in rows]

    def explanation(self, tenant_id: str, run_id: str, order_id: str, staff_id: str) -> Candidate:
        run = self.get_run(tenant_id, run_id)
        if run.result:
            for candidate in run.result.candidates:
                if candidate.order_id == order_id and candidate.staff_id == staff_id:
                    return candidate
        raise StoreError(404, "explanation_not_found", "Persisted candidate explanation not found.")

    def respond(self, tenant_id: str, proposal_id: str, request: RespondRequest) -> Proposal:
        with self._transaction(tenant_id) as connection:
            proposal = self._proposal(connection, tenant_id, proposal_id)
            payload = {"proposal_id": proposal_id, **request.model_dump(mode="json")}
            replay = self._replay(
                connection, tenant_id, "respond", request.idempotency_key, payload
            )
            if replay is not None:
                return Proposal.model_validate_json(replay)
            if request.expected_version != proposal.version:
                raise StoreError(409, "stale_version", "Proposal version has changed.")
            if proposal.status not in ("proposed", "consented") or (
                proposal.status == "consented" and request.decision != "decline"
            ):
                raise StoreError(409, "proposal_state", "Proposal cannot accept this response.")
            status = "consented" if request.decision == "consent" else "declined"
            now = self.clock().isoformat()
            connection.execute(
                "UPDATE proposals SET status = ?, version = version + 1, responded_at = ? "
                "WHERE tenant_id = ? AND id = ?",
                (status, now, tenant_id, proposal_id),
            )
            connection.execute(
                "INSERT INTO response_events(tenant_id, proposal_id, decision, from_status, "
                "to_status, proposal_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    tenant_id,
                    proposal_id,
                    request.decision,
                    proposal.status,
                    status,
                    proposal.version + 1,
                    now,
                ),
            )
            self._revision(connection, tenant_id)
            updated = self._proposal(connection, tenant_id, proposal_id)
            self._remember(
                connection,
                tenant_id,
                "respond",
                request.idempotency_key,
                payload,
                updated.model_dump_json(),
            )
            return updated

    @staticmethod
    def _insert_assignment(
        connection: sqlite3.Connection, tenant_id: str, assignment: Assignment
    ) -> None:
        connection.execute(
            "INSERT INTO assignments(tenant_id, id, staff_id, order_id, proposal_id, "
            "source_run_id, body) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                tenant_id,
                assignment.id,
                assignment.staff_id,
                assignment.order_id,
                assignment.proposal_id,
                assignment.source_run_id,
                assignment.model_dump_json(),
            ),
        )

    def confirm(self, tenant_id: str, proposal_id: str, request: ConfirmRequest) -> Assignment:
        from backend.engine import evaluate_candidate

        with self._transaction(tenant_id) as connection:
            proposal = self._proposal(connection, tenant_id, proposal_id)
            payload = {"proposal_id": proposal_id, **request.model_dump(mode="json")}
            replay = self._replay(
                connection, tenant_id, "confirm", request.idempotency_key, payload
            )
            if replay is not None:
                return Assignment.model_validate_json(replay)
            if proposal.status == "confirmed":
                if request.expected_version not in (proposal.version, proposal.version - 1):
                    raise StoreError(409, "stale_version", "Proposal version has changed.")
                row = connection.execute(
                    "SELECT body FROM assignments WHERE tenant_id = ? AND proposal_id = ?",
                    (tenant_id, proposal_id),
                ).fetchone()
                if row is None:
                    raise StoreError(409, "assignment_missing", "Confirmed assignment is missing.")
                assignment = Assignment.model_validate_json(row["body"])
            else:
                if request.expected_version != proposal.version:
                    raise StoreError(409, "stale_version", "Proposal version has changed.")
                if proposal.status != "consented":
                    raise StoreError(409, "consent_required", "An unexpired consent is required.")
                snapshot = self._snapshot(connection, tenant_id)
                staff = next(item for item in snapshot.staff if item.id == proposal.staff_id)
                order = next(item for item in snapshot.orders if item.id == proposal.order_id)
                evidence = next(
                    (
                        item
                        for item in snapshot.evidence
                        if item.staff_id == staff.id and item.order_id == order.id
                    ),
                    None,
                )
                if (
                    staff.version != proposal.staff_version
                    or order.version != proposal.order_version
                    or evidence is None
                    or evidence.version != proposal.evidence_version
                ):
                    raise StoreError(409, "stale_input", "Profile, order, or evidence has changed.")
                assigned_count = sum(item.order_id == order.id for item in snapshot.assignments)
                if assigned_count >= order.headcount:
                    raise StoreError(409, "capacity_conflict", "Order capacity is exhausted.")
                if any(
                    item.staff_id == staff.id
                    and item.period.start < order.period.end
                    and order.period.start < item.period.end
                    for item in snapshot.assignments
                ):
                    raise StoreError(409, "period_conflict", "Staff already has overlapping work.")
                candidate = evaluate_candidate(staff, order, evidence, snapshot.assignments)
                if candidate.eligibility != "eligible":
                    raise StoreError(
                        409, "ineligible", "Current eligibility does not permit assignment."
                    )
                assignment = Assignment(
                    id=f"assignment-{uuid4().hex}",
                    staff_id=staff.id,
                    order_id=order.id,
                    period=order.period,
                    source_run_id=proposal.run_id,
                    proposal_id=proposal.id,
                    confirmed_at=self.clock(),
                )
                self._insert_assignment(connection, tenant_id, assignment)
                connection.execute(
                    "UPDATE proposals SET status = 'confirmed', version = version + 1, "
                    "confirmed_assignment_id = ? WHERE tenant_id = ? AND id = ?",
                    (assignment.id, tenant_id, proposal_id),
                )
                connection.execute(
                    "INSERT INTO audit_events(tenant_id, event, proposal_id, assignment_id, "
                    "created_at) VALUES (?, 'confirmed', ?, ?, ?)",
                    (tenant_id, proposal_id, assignment.id, self.clock().isoformat()),
                )
                self._revision(connection, tenant_id)
            self._remember(
                connection,
                tenant_id,
                "confirm",
                request.idempotency_key,
                payload,
                assignment.model_dump_json(),
            )
            return assignment
