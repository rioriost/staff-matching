"""PostgreSQL lifecycle persistence; schema installation belongs to postgres_setup.

Every operation uses a transaction-local tenant context and a tenant row lock.
Runs have an instance owner and a 45-second lease (solver budgets are at most
10 seconds). Startup and subsequent tenant requests recover only expired leases;
rolling revisions never invalidate another instance's live work.
"""

import hashlib
import json
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg.types.range import Range

from backend.database import connect
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
from backend.postgres_features import enriched_evidence, skill_vector
from backend.store import StoreError

MIGRATION_VERSION = 3
RUN_LEASE_SECONDS = 45
APPLICATION_TABLES = (
    "tenants",
    "staff",
    "orders",
    "evidence",
    "history_source",
    "runs",
    "proposals",
    "assignments",
    "response_events",
    "audit_events",
    "idempotency",
)


def _json(model) -> Jsonb:
    return Jsonb(model.model_dump(mode="json"))


class PostgresStore:
    def __init__(
        self,
        conninfo: str | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        proposal_ttl_seconds: int = 1800,
    ):
        self._conninfo = conninfo or ""
        self.clock = clock or (lambda: datetime.now(UTC))
        self.proposal_ttl_seconds = proposal_ttl_seconds
        self.owner_id = uuid4().hex

    def connect(self) -> AbstractContextManager[psycopg.Connection]:
        return connect(
            self._conninfo,
            row_factory=dict_row,
            connect_timeout=10,
            application_name="staff-matching",
        )

    @contextmanager
    def _connection(self) -> Iterator[psycopg.Connection]:
        with self.connect() as connection:
            yield connection

    def initialize(self) -> None:
        """Readiness checks only: no schema, seed, extension, role, or grant writes."""
        with self._connection() as connection:
            role = connection.execute(
                "SELECT rolsuper, rolbypassrls FROM pg_catalog.pg_roles "
                "WHERE rolname = current_user"
            ).fetchone()
            if role["rolsuper"] or role["rolbypassrls"]:
                raise RuntimeError("PostgreSQL runtime role must not be superuser or BYPASSRLS.")
            checks = connection.execute(
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, "
                "pg_catalog.pg_has_role(current_user, c.relowner, 'MEMBER') AS owns_table "
                "FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'staff_matching' AND c.relname = ANY(%s)",
                (list(APPLICATION_TABLES),),
            ).fetchall()
            if len(checks) != len(APPLICATION_TABLES) or any(
                not row["relrowsecurity"] or not row["relforcerowsecurity"] or row["owns_table"]
                for row in checks
            ):
                raise RuntimeError("PostgreSQL requires migrated FORCE RLS tables and a non-owner.")
            version = connection.execute(
                "SELECT max(version) AS version FROM staff_matching.schema_migrations"
            ).fetchone()["version"]
            if version != MIGRATION_VERSION:
                raise RuntimeError("PostgreSQL migration version does not match this application.")
            extensions = connection.execute(
                "SELECT extname FROM pg_catalog.pg_extension "
                "WHERE extname IN ('postgis', 'vector', 'btree_gist', 'age')"
            ).fetchall()
            if len(extensions) != 4:
                raise RuntimeError("Required PostgreSQL extensions are not installed.")
            if connection.execute(
                "SELECT has_schema_privilege(current_user, 'ag_catalog', 'USAGE') AS raw_age"
            ).fetchone()["raw_age"]:
                raise RuntimeError("Runtime role must not have direct AGE access.")

    @contextmanager
    def server_lock(self) -> Iterator[None]:
        """Rolling revisions are safe; PostgreSQL row locks replace process/file locks."""
        yield

    @staticmethod
    def _set_context(connection: psycopg.Connection, tenant_id: str) -> None:
        connection.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))
        connection.execute(
            "SELECT set_config('search_path', 'staff_matching,public,pg_catalog', true), "
            "set_config('lock_timeout', '10s', true), "
            "set_config('statement_timeout', '25s', true)"
        )

    @contextmanager
    def _transaction(self, tenant_id: str) -> Iterator[psycopg.Connection]:
        rejected = None
        with self._connection() as connection:
            with connection.transaction():
                self._set_context(connection, tenant_id)
                self._tenant(connection, tenant_id)
                self._expire(connection, tenant_id)
                self._recover_stale(connection, tenant_id)
                try:
                    # A rejected action must not roll back materialized expiry/recovery.
                    with connection.transaction():
                        yield connection
                except StoreError as error:
                    rejected = error
        if rejected is not None:
            raise rejected

    @staticmethod
    def _tenant(connection: psycopg.Connection, tenant_id: str) -> dict:
        row = connection.execute(
            "SELECT * FROM tenants WHERE id = %s FOR UPDATE", (tenant_id,)
        ).fetchone()
        if row is None:
            raise StoreError(404, "tenant_not_found", "Tenant not found.")
        return row

    @staticmethod
    def _revision(connection: psycopg.Connection, tenant_id: str) -> None:
        connection.execute("UPDATE tenants SET revision = revision + 1 WHERE id = %s", (tenant_id,))

    def _expire(self, connection: psycopg.Connection, tenant_id: str) -> None:
        now = self.clock()
        rows = connection.execute(
            "UPDATE proposals SET status = 'expired', version = version + 1 "
            "WHERE tenant_id = %s AND status IN ('proposed', 'consented') "
            "AND expires_at <= %s RETURNING id",
            (tenant_id, now),
        ).fetchall()
        for row in rows:
            connection.execute(
                "INSERT INTO audit_events(tenant_id, event, proposal_id, created_at) "
                "VALUES (%s, 'expired', %s, %s)",
                (tenant_id, row["id"], now),
            )
        if rows:
            self._revision(connection, tenant_id)

    @staticmethod
    def _recover_stale(connection: psycopg.Connection, tenant_id: str) -> None:
        connection.execute(
            "UPDATE runs SET status = 'failed', error = %s "
            "WHERE tenant_id = %s AND status IN ('queued', 'running') "
            "AND lease_expires_at <= clock_timestamp()",
            ("Run lease expired after interruption or restart; submit a new run.", tenant_id),
        )

    def recover_interrupted_runs(self, tenant_id: str) -> None:
        try:
            with self._transaction(tenant_id):
                pass
        except StoreError as error:
            if error.code != "tenant_not_found":
                raise

    @staticmethod
    def _features(connection: psycopg.Connection) -> dict:
        return connection.execute("SELECT staff_matching.feature_evidence() AS report").fetchone()[
            "report"
        ]

    def extension_report(self, tenant_id: str) -> dict:
        with self._transaction(tenant_id) as connection:
            report = self._features(connection)
            report["sample"] = report["pairs"][0] if report["pairs"] else None
            report["run_lease_seconds"] = RUN_LEASE_SECONDS
            return report

    def _snapshot(self, connection: psycopg.Connection, tenant_id: str) -> Snapshot:
        revision = self._tenant(connection, tenant_id)["revision"]
        staff = [
            Staff.model_validate(row["body"])
            for row in connection.execute(
                "SELECT body FROM staff WHERE tenant_id = %s ORDER BY id", (tenant_id,)
            )
        ]
        orders = [
            Order.model_validate(row["body"])
            for row in connection.execute(
                "SELECT body FROM orders WHERE tenant_id = %s ORDER BY id", (tenant_id,)
            )
        ]
        feature_pairs = {
            (pair["staff_id"], pair["order_id"]): pair
            for pair in self._features(connection)["pairs"]
        }
        evidence = [
            enriched_evidence(
                PairEvidence.model_validate(row["body"]),
                feature_pairs[(row["staff_id"], row["order_id"])],
            )
            for row in connection.execute(
                "SELECT staff_id, order_id, body FROM evidence "
                "WHERE tenant_id = %s ORDER BY order_id, staff_id",
                (tenant_id,),
            )
        ]
        assignments = [
            Assignment.model_validate(row["body"])
            for row in connection.execute(
                "SELECT body FROM assignments WHERE tenant_id = %s ORDER BY sequence", (tenant_id,)
            )
        ]
        return Snapshot(
            tenant_id=tenant_id,
            revision=revision,
            staff=staff,
            orders=orders,
            evidence=evidence,
            assignments=assignments,
        )

    def snapshot(self, tenant_id: str) -> Snapshot:
        with self._transaction(tenant_id) as connection:
            return self._snapshot(connection, tenant_id)

    def update_domain(self, tenant_id: str, item: Staff | Order | PairEvidence) -> None:
        """Versioned synthetic edits; source history/graph writes remain admin-only."""
        with self._transaction(tenant_id) as connection:
            if isinstance(item, PairEvidence):
                row = connection.execute(
                    "SELECT body FROM evidence "
                    "WHERE tenant_id = %s AND staff_id = %s AND order_id = %s",
                    (tenant_id, item.staff_id, item.order_id),
                ).fetchone()
            else:
                table = "staff" if isinstance(item, Staff) else "orders"
                row = connection.execute(
                    psycopg.sql.SQL("SELECT body FROM {} WHERE tenant_id = %s AND id = %s").format(
                        psycopg.sql.Identifier(table)
                    ),
                    (tenant_id, item.id),
                ).fetchone()
            if row is None:
                raise StoreError(404, "not_found", "Domain record not found.")
            if item.version <= row["body"]["version"]:
                raise StoreError(409, "stale_version", "Domain version must increase.")
            if isinstance(item, PairEvidence):
                connection.execute(
                    "UPDATE evidence SET body = %s "
                    "WHERE tenant_id = %s AND staff_id = %s AND order_id = %s",
                    (_json(item), tenant_id, item.staff_id, item.order_id),
                )
            else:
                connection.execute(
                    psycopg.sql.SQL(
                        "UPDATE {} SET body = %s, skill_vector = %s::public.vector "
                        "WHERE tenant_id = %s AND id = %s"
                    ).format(psycopg.sql.Identifier(table)),
                    (
                        _json(item),
                        skill_vector(
                            item.skills if isinstance(item, Staff) else item.required_skills
                        ),
                        tenant_id,
                        item.id,
                    ),
                )
            self._revision(connection, tenant_id)

    def add_mock_tenant(self, snapshot: Snapshot) -> None:
        """Admin-only fixture import, not available to the runtime database role."""
        from backend.postgres_setup import seed_tenant

        with self._connection() as connection:
            seed_tenant(connection, snapshot)

    @staticmethod
    def _fingerprint(payload: dict) -> str:
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @classmethod
    def _replay(cls, connection, tenant_id, operation, key, payload) -> dict | None:
        row = connection.execute(
            "SELECT fingerprint, response FROM idempotency "
            "WHERE tenant_id = %s AND operation = %s AND key = %s",
            (tenant_id, operation, key),
        ).fetchone()
        if row is None:
            return None
        if row["fingerprint"] != cls._fingerprint(payload):
            raise StoreError(409, "idempotency_conflict", "Key already used for another request.")
        return row["response"]

    @classmethod
    def _remember(cls, connection, tenant_id, operation, key, payload, response) -> None:
        connection.execute(
            "INSERT INTO idempotency(tenant_id, operation, key, fingerprint, response) "
            "VALUES (%s, %s, %s, %s, %s)",
            (tenant_id, operation, key, cls._fingerprint(payload), _json(response)),
        )

    @staticmethod
    def _active_batch(connection, tenant_id: str, exclude_run_id: str = "") -> bool:
        return bool(
            connection.execute(
                "SELECT 1 FROM runs WHERE tenant_id = %s AND status IN ('queued', 'running') "
                "AND id != %s LIMIT 1",
                (tenant_id, exclude_run_id),
            ).fetchone()
            or connection.execute(
                "SELECT 1 FROM proposals WHERE tenant_id = %s "
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
                return RunAccepted.model_validate(replay), False
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
                "snapshot, algorithm_config, owner_id, lease_expires_at) "
                "VALUES (%s, %s, 'queued', %s, %s, %s, %s, %s, %s, "
                "clock_timestamp() + %s * interval '1 second')",
                (
                    tenant_id,
                    run_id,
                    self.clock(),
                    snapshot.revision,
                    _json(request),
                    _json(snapshot),
                    Jsonb(
                        {
                            "weights_version": "mock-rules-v1",
                            "seed": 0,
                            "workers": 1,
                            "score_scale": 10000,
                            "scheduling_mode": request.scheduling_mode,
                            "time_limit_seconds": request.time_limit_seconds,
                            "features_version": "synthetic-tokyo-structured-v1",
                        }
                    ),
                    self.owner_id,
                    RUN_LEASE_SECONDS,
                ),
            )
            accepted = RunAccepted(run_id=run_id, status="queued")
            self._remember(
                connection, tenant_id, "optimize", request.idempotency_key, payload, accepted
            )
            return accepted, True

    @staticmethod
    def _run(connection, tenant_id: str, run_id: str) -> dict:
        row = connection.execute(
            "SELECT * FROM runs WHERE tenant_id = %s AND id = %s",
            (tenant_id, run_id),
        ).fetchone()
        if row is None:
            raise StoreError(404, "run_not_found", "Run not found.")
        return row

    def _require_owner(self, row: dict) -> None:
        if row["owner_id"] != self.owner_id:
            raise StoreError(409, "run_owner", "Run belongs to another live application instance.")

    def start_run(self, tenant_id: str, run_id: str) -> tuple[Snapshot, OptimizeRequest]:
        with self._transaction(tenant_id) as connection:
            row = self._run(connection, tenant_id, run_id)
            if row["status"] != "queued":
                raise StoreError(409, "run_state", "Run is not queued.")
            self._require_owner(row)
            connection.execute(
                "UPDATE runs SET status = 'running', "
                "lease_expires_at = clock_timestamp() + %s * interval '1 second' "
                "WHERE tenant_id = %s AND id = %s",
                (RUN_LEASE_SECONDS, tenant_id, run_id),
            )
            return Snapshot.model_validate(row["snapshot"]), OptimizeRequest.model_validate(
                row["config"]
            )

    def fail_run(self, tenant_id: str, run_id: str, error: str) -> None:
        with self._transaction(tenant_id) as connection:
            self._run(connection, tenant_id, run_id)
            connection.execute(
                "UPDATE runs SET status = 'failed', error = %s WHERE tenant_id = %s AND id = %s "
                "AND status IN ('queued', 'running') AND owner_id = %s",
                (error, tenant_id, run_id, self.owner_id),
            )

    def publish_run(self, tenant_id: str, run_id: str, result: OptimizationResult) -> bool:
        from backend.engine import evaluate_candidate, validate_snapshot

        with self._transaction(tenant_id) as connection:
            row = self._run(connection, tenant_id, run_id)
            if row["status"] != "running":
                raise StoreError(409, "run_state", "Run is not running.")
            self._require_owner(row)
            revision = self._tenant(connection, tenant_id)["revision"]
            if revision != row["input_revision"] or self._active_batch(
                connection, tenant_id, run_id
            ):
                connection.execute(
                    "UPDATE runs SET status = 'failed', error = %s "
                    "WHERE tenant_id = %s AND id = %s",
                    (
                        "Input revision or active batch changed before publication.",
                        tenant_id,
                        run_id,
                    ),
                )
                return False
            snapshot = Snapshot.model_validate(row["snapshot"])
            if result.input_revision != snapshot.revision:
                raise ValueError("Solver result does not match the persisted snapshot revision.")
            staff = {item.id: item for item in snapshot.staff}
            orders = {item.id: item for item in snapshot.orders}
            evidence = {(item.staff_id, item.order_id): item for item in snapshot.evidence}
            # Defend against a malformed solver result, not only against stale versions.
            trial_assignments = list(snapshot.assignments)
            for pair in result.optimized.pairs:
                candidate = evaluate_candidate(
                    staff[pair.staff_id],
                    orders[pair.order_id],
                    evidence[(pair.staff_id, pair.order_id)],
                    trial_assignments,
                )
                if candidate.eligibility != "eligible":
                    raise ValueError("Solver selected an ineligible or conflicting assignment.")
                trial_assignments.append(
                    Assignment(
                        id=uuid4().hex,
                        staff_id=pair.staff_id,
                        order_id=pair.order_id,
                        period=orders[pair.order_id].period,
                        source_run_id=run_id,
                        proposal_id=None,
                        confirmed_at=self.clock(),
                    )
                )
            validate_snapshot(snapshot.model_copy(update={"assignments": trial_assignments}))
            expiry = self.clock() + timedelta(seconds=self.proposal_ttl_seconds)
            for pair in result.optimized.pairs:
                connection.execute(
                    "INSERT INTO proposals(tenant_id, id, run_id, staff_id, order_id, status, "
                    "version, input_revision, staff_version, order_version, evidence_version, "
                    "expires_at) VALUES (%s, %s, %s, %s, %s, 'proposed', 1, %s, %s, %s, %s, %s)",
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
                "UPDATE runs SET status = 'completed', result = %s "
                "WHERE tenant_id = %s AND id = %s",
                (_json(result), tenant_id, run_id),
            )
            return True

    @staticmethod
    def _proposal(connection, tenant_id: str, proposal_id: str) -> Proposal:
        row = connection.execute(
            "SELECT * FROM proposals WHERE tenant_id = %s AND id = %s",
            (tenant_id, proposal_id),
        ).fetchone()
        if row is None:
            raise StoreError(404, "proposal_not_found", "Proposal not found.")
        return Proposal.model_validate({field: row[field] for field in Proposal.model_fields})

    def get_proposal(self, tenant_id: str, proposal_id: str) -> Proposal:
        with self._transaction(tenant_id) as connection:
            return self._proposal(connection, tenant_id, proposal_id)

    @staticmethod
    def _run_view(connection, tenant_id: str, row: dict) -> RunView:
        proposals = [
            Proposal.model_validate({field: item[field] for field in Proposal.model_fields})
            for item in connection.execute(
                "SELECT * FROM proposals WHERE tenant_id = %s AND run_id = %s ORDER BY sequence",
                (tenant_id, row["id"]),
            )
        ]
        return RunView(
            id=row["id"],
            status=row["status"],
            created_at=row["created_at"],
            input_revision=row["input_revision"],
            config=OptimizeRequest.model_validate(row["config"]),
            error=row["error"],
            result=OptimizationResult.model_validate(row["result"]) if row["result"] else None,
            proposals=proposals,
        )

    def get_run(self, tenant_id: str, run_id: str) -> RunView:
        with self._transaction(tenant_id) as connection:
            return self._run_view(connection, tenant_id, self._run(connection, tenant_id, run_id))

    def list_runs(self, tenant_id: str) -> list[RunView]:
        with self._transaction(tenant_id) as connection:
            rows = connection.execute(
                "SELECT * FROM runs WHERE tenant_id = %s "
                "ORDER BY created_at DESC, sequence DESC LIMIT 20",
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
                return Proposal.model_validate(replay)
            if request.expected_version != proposal.version:
                raise StoreError(409, "stale_version", "Proposal version has changed.")
            if proposal.status not in ("proposed", "consented") or (
                proposal.status == "consented" and request.decision != "decline"
            ):
                raise StoreError(409, "proposal_state", "Proposal cannot accept this response.")
            status = "consented" if request.decision == "consent" else "declined"
            now = self.clock()
            connection.execute(
                "UPDATE proposals SET status = %s, version = version + 1, responded_at = %s "
                "WHERE tenant_id = %s AND id = %s",
                (status, now, tenant_id, proposal_id),
            )
            connection.execute(
                "INSERT INTO response_events(tenant_id, proposal_id, decision, from_status, "
                "to_status, proposal_version, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
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
                connection, tenant_id, "respond", request.idempotency_key, payload, updated
            )
            return updated

    @staticmethod
    def _insert_assignment(connection, tenant_id: str, assignment: Assignment) -> None:
        connection.execute(
            "INSERT INTO assignments(tenant_id, id, staff_id, order_id, proposal_id, "
            "source_run_id, occupied, body) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                tenant_id,
                assignment.id,
                assignment.staff_id,
                assignment.order_id,
                assignment.proposal_id,
                assignment.source_run_id,
                Range(assignment.period.start, assignment.period.end, "[)"),
                _json(assignment),
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
                return Assignment.model_validate(replay)
            if proposal.status == "confirmed":
                if request.expected_version not in (proposal.version, proposal.version - 1):
                    raise StoreError(409, "stale_version", "Proposal version has changed.")
                row = connection.execute(
                    "SELECT body FROM assignments WHERE tenant_id = %s AND proposal_id = %s",
                    (tenant_id, proposal_id),
                ).fetchone()
                if row is None:
                    raise StoreError(409, "assignment_missing", "Confirmed assignment is missing.")
                assignment = Assignment.model_validate(row["body"])
            else:
                if request.expected_version != proposal.version:
                    raise StoreError(409, "stale_version", "Proposal version has changed.")
                if proposal.status != "consented":
                    raise StoreError(409, "consent_required", "An unexpired consent is required.")
                connection.execute(
                    "SELECT id FROM orders WHERE tenant_id = %s AND id = %s FOR UPDATE",
                    (tenant_id, proposal.order_id),
                )
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
                if (
                    sum(item.order_id == order.id for item in snapshot.assignments)
                    >= order.headcount
                ):
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
                try:
                    with connection.transaction():
                        self._insert_assignment(connection, tenant_id, assignment)
                except psycopg.errors.ExclusionViolation as error:
                    raise StoreError(
                        409, "period_conflict", "Staff already has overlapping work."
                    ) from error
                except psycopg.errors.CheckViolation as error:
                    raise StoreError(
                        409, "capacity_conflict", "Assignment capacity or period changed."
                    ) from error
                connection.execute(
                    "UPDATE proposals SET status = 'confirmed', version = version + 1, "
                    "confirmed_assignment_id = %s WHERE tenant_id = %s AND id = %s",
                    (assignment.id, tenant_id, proposal_id),
                )
                connection.execute(
                    "INSERT INTO audit_events(tenant_id, event, proposal_id, assignment_id, "
                    "created_at) VALUES (%s, 'confirmed', %s, %s, %s)",
                    (tenant_id, proposal_id, assignment.id, self.clock()),
                )
                self._revision(connection, tenant_id)
            self._remember(
                connection, tenant_id, "confirm", request.idempotency_key, payload, assignment
            )
            return assignment
