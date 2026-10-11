"""Admin-only, additive PostgreSQL bootstrap and cloud verification.

Run with admin PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD/PGSSLMODE and
POC_APP_DB_PASSWORD. The database already exists. Azure must allow postgis,
vector, btree_gist, age and preload age before this command is run.
No LOAD, database creation, data deletion, or application-startup DDL is used.
`--verify` adds a uniquely named synthetic validation tenant and retains it.
"""

import argparse
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from backend.models import Assignment, ConfirmRequest, OptimizeRequest, RespondRequest, Snapshot
from backend.postgres_features import FEATURE_VERSION, skill_vector, synthetic_point
from backend.postgres_store import APPLICATION_TABLES, MIGRATION_VERSION, PostgresStore
from backend.seed import demo_snapshot
from backend.store import StoreError

RUNTIME_ROLE = "staff_app"
_stage = "configuration"
_operation: str | None = None
_safe_diagnostics = False


def _check(name: str) -> None:
    global _stage, _operation, _safe_diagnostics
    _stage = name
    _operation = None
    _safe_diagnostics = False


def _seed_operation(name: str) -> None:
    global _operation, _safe_diagnostics
    _operation = name
    _safe_diagnostics = True


def _failure_payload(error: Exception) -> dict:
    payload = {
        "status": "FAIL",
        "check": _stage,
        "category": type(error).__name__,
        "sqlstate": getattr(error, "sqlstate", None),
    }
    if _operation:
        payload["operation"] = _operation
    if _safe_diagnostics and isinstance(error, psycopg.Error):
        # Only synthetic seed/feature operations opt in. Never include SQL,
        # detail/context, internal_query, or diagnostics from credential DDL.
        message = error.diag.message_primary
        if message:
            for name in ("PGPASSWORD", "POC_APP_DB_PASSWORD", "PGUSER", "TEST_POSTGRES_DSN"):
                secret = os.getenv(name)
                if secret:
                    message = message.replace(secret, "[redacted]")
            payload["message_primary"] = message.replace(RUNTIME_ROLE, "[runtime-role]")[:400]
        position = error.diag.statement_position or error.diag.internal_position
        if position and str(position).isdigit():
            payload["statement_position"] = int(position)
    return payload


def _passed(detail: str) -> None:
    print(json.dumps({"status": "PASS", "check": _stage, "detail": detail}), flush=True)


def _cypher_statement(graph: str, query: str) -> sql.Composed:
    # AGE explicitly rejects ordinary single-quoted Cypher string constants.
    # Select a delimiter absent from the entire body, including fixture values.
    delimiter = "$staff_cypher$"
    while delimiter in query:
        delimiter = delimiter[:-1] + "_$"
    return sql.SQL("SELECT * FROM ag_catalog.cypher({}, {}) AS (value ag_catalog.agtype)").format(
        sql.Literal(graph), sql.SQL(delimiter + query + delimiter)
    )


def _cypher(connection, graph: str, query: str, *, operation: str) -> None:
    _seed_operation(operation)
    connection.execute(
        _cypher_statement(graph, query),
        prepare=False,
    ).fetchall()


def _project_history(connection, tenant_id: str) -> None:
    graph = "sm_" + hashlib.md5(tenant_id.encode(), usedforsecurity=False).hexdigest()
    _seed_operation("graph.session-context")
    connection.execute(
        "SELECT set_config('search_path', 'ag_catalog,staff_matching,public,pg_catalog', true)"
    )
    _seed_operation("graph.lookup")
    if not connection.execute(
        "SELECT 1 FROM ag_catalog.ag_graph WHERE name = %s", (graph,)
    ).fetchone():
        _seed_operation("graph.create")
        connection.execute(
            sql.SQL("SELECT ag_catalog.create_graph({})").format(sql.Literal(graph)), prepare=False
        )
    for table, label in (("staff", "Staff"), ("orders", "Order")):
        _seed_operation(f"graph.read-{table}")
        rows = connection.execute(
            sql.SQL("SELECT id FROM staff_matching.{} WHERE tenant_id = %s ORDER BY id").format(
                sql.Identifier(table)
            ),
            (tenant_id,),
        ).fetchall()
        for row in rows:
            _cypher(
                connection,
                graph,
                f"MERGE (n:{label} {{id: {json.dumps(row['id'])}}}) RETURN n",
                operation=f"graph.merge-{table}",
            )
    _seed_operation("graph.read-history-source")
    for row in connection.execute(
        "SELECT id, staff_id, order_id, rating FROM staff_matching.history_source "
        "WHERE tenant_id = %s ORDER BY id",
        (tenant_id,),
    ).fetchall():
        _cypher(
            connection,
            graph,
            f"MATCH (s:Staff {{id: {json.dumps(row['staff_id'])}}}), "
            f"(o:Order {{id: {json.dumps(row['order_id'])}}}) "
            f"MERGE (h:History {{id: {json.dumps(row['id'])}}}) "
            f"SET h.rating = {float(row['rating'])} "
            "MERGE (s)-[:COMPLETED]->(h) MERGE (h)-[:FOR_ORDER]->(o) RETURN h",
            operation="graph.merge-history-path",
        )
    _seed_operation("graph.revoke-schema-access")
    connection.execute(
        sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC, staff_app").format(sql.Identifier(graph))
    )
    _seed_operation("graph.revoke-table-access")
    connection.execute(
        sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM PUBLIC, staff_app").format(
            sql.Identifier(graph)
        )
    )
    # AGE label tables are a projection, not the authoritative store. They also
    # receive FORCE RLS; graph-schema ACLs independently prohibit direct runtime access.
    _seed_operation("graph.list-label-tables")
    for row in connection.execute(
        "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = %s", (graph,)
    ).fetchall():
        table = sql.Identifier(graph, row["tablename"])
        _seed_operation("graph.enable-rls")
        connection.execute(sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(table))
        _seed_operation("graph.force-rls")
        connection.execute(sql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY").format(table))
        _seed_operation("graph.lookup-policy")
        if not connection.execute(
            "SELECT 1 FROM pg_catalog.pg_policies "
            "WHERE schemaname = %s AND tablename = %s AND policyname = 'tenant_scope'",
            (graph, row["tablename"]),
        ).fetchone():
            _seed_operation("graph.create-policy")
            connection.execute(
                sql.SQL(
                    "CREATE POLICY tenant_scope ON {} USING "
                    "(current_setting('app.tenant_id', true) = {}) WITH CHECK "
                    "(current_setting('app.tenant_id', true) = {})"
                ).format(table, sql.Literal(tenant_id), sql.Literal(tenant_id))
            )


def seed_tenant(connection, snapshot: Snapshot) -> bool:
    """Import once under an advisory/tenant lock; never overwrite lifecycle records."""
    from backend.engine import validate_snapshot

    validate_snapshot(snapshot)
    tenant_id = snapshot.tenant_id
    with connection.transaction():
        _seed_operation("seed.tenant-context")
        PostgresStore._set_context(connection, tenant_id)
        _seed_operation("seed.advisory-lock")
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            ("staff-matching-seed:" + tenant_id,),
        )
        _seed_operation("seed.insert-tenant")
        inserted = connection.execute(
            "INSERT INTO staff_matching.tenants(id, revision) VALUES (%s, %s) "
            "ON CONFLICT (id) DO NOTHING RETURNING id",
            (tenant_id, snapshot.revision),
        ).fetchone()
        _seed_operation("seed.lock-tenant")
        connection.execute(
            "SELECT id FROM staff_matching.tenants WHERE id = %s FOR UPDATE", (tenant_id,)
        )
        if inserted:
            for table, models, point_column in (
                ("staff", snapshot.staff, "origin"),
                ("orders", snapshot.orders, "site"),
            ):
                for index, item in enumerate(models):
                    longitude, latitude = synthetic_point(index, site=table == "orders")
                    skills = item.skills if table == "staff" else item.required_skills
                    _seed_operation(f"seed.insert-{table}")
                    connection.execute(
                        sql.SQL(
                            "INSERT INTO staff_matching.{}(tenant_id, id, body, {}, skill_vector) "
                            "VALUES (%s, %s, %s, "
                            "public.ST_SetSRID(public.ST_MakePoint(%s, %s), 4326)"
                            "::public.geography, "
                            "%s::public.vector)"
                        ).format(sql.Identifier(table), sql.Identifier(point_column)),
                        (
                            tenant_id,
                            item.id,
                            Jsonb(item.model_dump(mode="json")),
                            longitude,
                            latitude,
                            skill_vector(skills),
                        ),
                    )
            for item in snapshot.evidence:
                _seed_operation("seed.insert-evidence")
                connection.execute(
                    "INSERT INTO staff_matching.evidence(tenant_id, staff_id, order_id, body) "
                    "VALUES (%s, %s, %s, %s)",
                    (tenant_id, item.staff_id, item.order_id, Jsonb(item.model_dump(mode="json"))),
                )
                for index, offset in enumerate((-0.05, 0.0, 0.05), start=1):
                    rating = round(max(0.0, min(1.0, item.preference_fit + offset)), 6)
                    _seed_operation("seed.insert-history-source")
                    connection.execute(
                        "INSERT INTO staff_matching.history_source"
                        "(tenant_id, id, staff_id, order_id, rating, provenance) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (
                            tenant_id,
                            f"history-{item.staff_id}-{item.order_id}-{index}",
                            item.staff_id,
                            item.order_id,
                            rating,
                            Jsonb(
                                {
                                    "synthetic": True,
                                    "generator": FEATURE_VERSION,
                                    "source": "deterministic fictional prior-assignment rating",
                                    "fixture_preference_fit": item.preference_fit,
                                    "rating_offset": offset,
                                    "rating_scale": "[0,1]",
                                    "not_a_real_employment_record": True,
                                }
                            ),
                        ),
                    )
            for assignment in snapshot.assignments:
                _seed_operation("seed.insert-assignment")
                PostgresStore._insert_assignment(connection, tenant_id, assignment)
        _project_history(connection, tenant_id)
        return bool(inserted)


def bootstrap(connection, password: str) -> None:
    if not password:
        raise ValueError("POC_APP_DB_PASSWORD must be configured.")
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(91539965855309)")
        connection.execute("SET LOCAL client_min_messages = warning")
        _check("extensions")
        preloads = connection.execute("SHOW shared_preload_libraries").fetchone()
        if "age" not in [
            value.strip().strip('"') for value in preloads["shared_preload_libraries"].split(",")
        ]:
            raise RuntimeError("AGE must be preloaded by server configuration before bootstrap.")
        for extension in ("postgis", "vector", "btree_gist", "age"):
            connection.execute(
                sql.SQL("CREATE EXTENSION IF NOT EXISTS {}").format(sql.Identifier(extension))
            )
        _passed("postgis, vector, btree_gist, age installed; AGE preloaded")
        _check("restricted-runtime-role")
        role = connection.execute(
            "SELECT rolsuper, rolreplication, rolbypassrls FROM pg_catalog.pg_roles "
            "WHERE rolname = %s",
            (RUNTIME_ROLE,),
        ).fetchone()
        if role is None:
            connection.execute("CREATE ROLE staff_app LOGIN")
        elif role["rolsuper"] or role["rolreplication"] or role["rolbypassrls"]:
            raise RuntimeError("Existing staff_app has privileged role attributes.")
        if connection.execute(
            "SELECT 1 FROM pg_catalog.pg_auth_members WHERE member = 'staff_app'::regrole LIMIT 1"
        ).fetchone():
            raise RuntimeError("Existing staff_app has unexpected role memberships.")
        # Send a SCRAM verifier instead of the cleartext secret in administrative SQL.
        verifier = connection.pgconn.encrypt_password(
            password.encode(), RUNTIME_ROLE.encode(), b"scram-sha-256"
        ).decode()
        connection.execute(
            sql.SQL(
                "ALTER ROLE staff_app LOGIN NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD {}"
            ).format(sql.Literal(verifier))
        )
        connection.execute("REVOKE ALL ON SCHEMA ag_catalog FROM PUBLIC, staff_app")
        connection.execute("REVOKE ALL ON ALL TABLES IN SCHEMA ag_catalog FROM PUBLIC, staff_app")
        connection.execute(
            "REVOKE ALL ON ALL FUNCTIONS IN SCHEMA ag_catalog FROM PUBLIC, staff_app"
        )
        connection.execute("GRANT USAGE ON SCHEMA ag_catalog TO CURRENT_USER")
        connection.execute("GRANT SELECT ON ALL TABLES IN SCHEMA ag_catalog TO CURRENT_USER")
        connection.execute("GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA ag_catalog TO CURRENT_USER")
        _passed("non-owner, NOINHERIT, NOBYPASSRLS runtime role; raw AGE access revoked")
        _check("migration")
        installed = connection.execute(
            "SELECT to_regclass('staff_matching.schema_migrations') AS installed"
        ).fetchone()["installed"]
        version = 0
        if installed:
            version = connection.execute(
                "SELECT coalesce(max(version), 0) AS version FROM staff_matching.schema_migrations"
            ).fetchone()["version"]
        if version > MIGRATION_VERSION:
            raise RuntimeError("Unknown PostgreSQL schema version.")
        migrations = (
            "001_postgres.sql",
            "002_age_dollar_quoting.sql",
            "003_age_scalar_text.sql",
        )
        for filename in migrations[version:]:
            migration = Path(__file__).resolve().parent.parent / "db" / filename
            connection.execute(migration.read_text(), prepare=False)
        connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC, staff_app")
        connection.execute("REVOKE ALL ON SCHEMA staff_matching FROM staff_app")
        connection.execute("REVOKE ALL ON ALL TABLES IN SCHEMA staff_matching FROM staff_app")
        connection.execute("GRANT USAGE ON SCHEMA staff_matching, public TO staff_app")
        connection.execute("GRANT SELECT ON ALL TABLES IN SCHEMA staff_matching TO staff_app")
        connection.execute(
            "GRANT UPDATE ON staff_matching.tenants, staff_matching.staff, staff_matching.orders, "
            "staff_matching.evidence TO staff_app"
        )
        connection.execute(
            "GRANT INSERT, UPDATE ON staff_matching.runs, staff_matching.proposals TO staff_app"
        )
        connection.execute(
            "GRANT INSERT ON staff_matching.assignments, staff_matching.response_events, "
            "staff_matching.audit_events, staff_matching.idempotency TO staff_app"
        )
        connection.execute("GRANT USAGE ON ALL SEQUENCES IN SCHEMA staff_matching TO staff_app")
        connection.execute(
            "GRANT EXECUTE ON FUNCTION staff_matching.feature_evidence() TO staff_app"
        )
        _passed(
            f"migration {MIGRATION_VERSION}; FORCE RLS and tenant-composite foreign keys enabled"
        )
        _check("synthetic-seed")
        created = seed_tenant(connection, demo_snapshot())
        _passed(
            "synthetic demo imported" if created else "existing demo retained; graph reconciled"
        )


def runtime_conninfo(connection, password: str) -> str:
    parameters = connection.info.get_parameters()
    parameters.update(user=RUNTIME_ROLE, password=password)
    return make_conninfo(**parameters)


def verify(connection, password: str) -> None:
    """Exercise real extension, RLS, lease, and lifecycle SQL without touching demo data."""
    from backend.engine import optimize

    _check("validation-tenant")
    tenant_id = "validation-" + uuid4().hex
    with connection.transaction():
        seed_tenant(connection, demo_snapshot().model_copy(update={"tenant_id": tenant_id}))
    # Finish the admin transaction before independent runtime sessions use the tenant.
    connection.commit()
    dsn = runtime_conninfo(connection, password)
    store = PostgresStore(dsn)
    store.initialize()
    _passed("isolated synthetic validation tenant created; restricted-role readiness passed")

    _check("extension-operations")
    _seed_operation("features.extension-report")
    report = store.extension_report(tenant_id)
    assert set(report["extensions"]) == {"postgis", "vector", "btree_gist", "age"}
    assert {(pair["staff_id"], pair["order_id"]) for pair in report["pairs"]} == {
        (pair.staff_id, pair.order_id) for pair in demo_snapshot().evidence
    }
    assert report["graph_history_count"] == report["history_source_count"] == 27
    assert report["sample"]["spatial_distance_m"] > 0
    assert 0 <= report["sample"]["vector_distance"] <= 2
    _seed_operation("features.snapshot-evidence")
    assert all(item.rated_assignments == 3 for item in store.snapshot(tenant_id).evidence)
    _passed("PostGIS distance, pgvector cosine, AGE Cypher count/avg and 27 raw source records")

    _check("runtime-rls")
    with store._connection() as runtime:
        for table in APPLICATION_TABLES:
            count = runtime.execute(
                sql.SQL("SELECT count(*) AS n FROM staff_matching.{}").format(sql.Identifier(table))
            ).fetchone()["n"]
            assert count == 0
        PostgresStore._set_context(runtime, tenant_id)
        assert runtime.execute("SELECT count(*) AS n FROM staff").fetchone()["n"] == 6
        assert (
            runtime.execute("SELECT count(*) AS n FROM staff WHERE tenant_id = 'demo'").fetchone()[
                "n"
            ]
            == 0
        )
        for statement in (
            "SELECT count(*) FROM ag_catalog.ag_graph",
            "INSERT INTO staff_matching.tenants(id, revision) VALUES ('forbidden-runtime', 1)",
            "UPDATE staff_matching.history_source SET rating = 0",
        ):
            try:
                with runtime.transaction():
                    runtime.execute(statement)
            except psycopg.errors.InsufficientPrivilege:
                pass
            else:
                raise AssertionError("Runtime privilege boundary was not enforced.")
    _passed("unscoped reads empty; cross-tenant reads empty; raw AGE/source writes/import denied")

    _check("database-overlap-constraint")
    with store._transaction(tenant_id) as runtime:
        duplicate = (
            store._snapshot(runtime, tenant_id)
            .assignments[0]
            .model_copy(update={"id": "validation-overlap"})
        )
        try:
            with runtime.transaction():
                store._insert_assignment(runtime, tenant_id, duplicate)
        except psycopg.errors.ExclusionViolation:
            pass
        else:
            raise AssertionError("btree_gist exclusion did not reject overlapping work.")
    _passed("btree_gist daterange exclusion rejected overlapping assignment")

    _check("optimize-idempotency")
    request = OptimizeRequest(idempotency_key=uuid4().hex)
    workers = [PostgresStore(dsn), PostgresStore(dsn)]
    barrier = Barrier(2)

    def submit(worker):
        barrier.wait(timeout=15)
        return worker, worker.create_run(tenant_id, request)

    with ThreadPoolExecutor(max_workers=2) as executor:
        submissions = list(executor.map(submit, workers))
    assert sum(result[1] for _, result in submissions) == 1
    assert len({result[0].run_id for _, result in submissions}) == 1
    owner, (accepted, _) = next(item for item in submissions if item[1][1])
    snapshot, config = owner.start_run(tenant_id, accepted.run_id)
    assert owner.publish_run(
        tenant_id, accepted.run_id, optimize(snapshot, time_limit_seconds=config.time_limit_seconds)
    )
    run = store.get_run(tenant_id, accepted.run_id)
    _passed("concurrent duplicate optimization created exactly one durable run")

    _check("confirmation-capacity-replay")
    original = next(proposal for proposal in run.proposals if proposal.order_id == "o01")
    original = store.respond(
        tenant_id,
        original.id,
        RespondRequest(
            decision="consent", expected_version=original.version, idempotency_key=uuid4().hex
        ),
    )
    competitor_id = "validation-competitor"
    other_staff = "s01" if original.staff_id == "s02" else "s02"
    # Deliberately construct a competing historical consent in this validation
    # tenant, so final confirmation (not solver filtering) must protect capacity.
    with store._transaction(tenant_id) as runtime:
        runtime.execute(
            "INSERT INTO proposals(tenant_id, id, run_id, staff_id, order_id, status, version, "
            "input_revision, staff_version, order_version, evidence_version, expires_at) "
            "VALUES (%s, %s, %s, %s, 'o01', 'consented', 2, %s, 1, 1, 1, %s)",
            (
                tenant_id,
                competitor_id,
                run.id,
                other_staff,
                snapshot.revision,
                store.clock() + timedelta(minutes=30),
            ),
        )
    proposals = [original, store.get_proposal(tenant_id, competitor_id)]
    barrier = Barrier(2)

    def confirm(proposal):
        worker = PostgresStore(dsn)
        confirmation = ConfirmRequest(
            expected_version=proposal.version, idempotency_key=uuid4().hex
        )
        barrier.wait(timeout=15)
        try:
            return proposal, confirmation, worker.confirm(tenant_id, proposal.id, confirmation)
        except StoreError as error:
            return proposal, confirmation, error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(confirm, proposals))
    successes = [item for item in results if isinstance(item[2], Assignment)]
    failures = [item for item in results if isinstance(item[2], StoreError)]
    assert len(successes) == len(failures) == 1
    assert failures[0][2].code == "capacity_conflict"
    proposal, confirmation, assignment = successes[0]
    assert store.confirm(tenant_id, proposal.id, confirmation) == assignment
    assert sum(item.order_id == "o01" for item in store.snapshot(tenant_id).assignments) == 1
    _passed("competing confirmations produced one assignment; replay returned the same assignment")

    _check("rolling-revision-lease")
    for proposal in store.get_run(tenant_id, run.id).proposals:
        if proposal.status in ("proposed", "consented"):
            store.respond(
                tenant_id,
                proposal.id,
                RespondRequest(
                    decision="decline",
                    expected_version=proposal.version,
                    idempotency_key=uuid4().hex,
                ),
            )
    queued, _ = store.create_run(tenant_id, OptimizeRequest(idempotency_key=uuid4().hex))
    another_revision = PostgresStore(dsn)
    another_revision.recover_interrupted_runs(tenant_id)
    assert another_revision.get_run(tenant_id, queued.run_id).status == "queued"
    another_revision.fail_run(tenant_id, queued.run_id, "Must not affect another owner's live run.")
    assert store.get_run(tenant_id, queued.run_id).status == "queued"
    with connection.transaction():
        PostgresStore._set_context(connection, tenant_id)
        connection.execute(
            "UPDATE staff_matching.runs "
            "SET lease_expires_at = clock_timestamp() - interval '1 second' "
            "WHERE tenant_id = %s AND id = %s",
            (tenant_id, queued.run_id),
        )
    connection.commit()
    assert another_revision.get_run(tenant_id, queued.run_id).status == "failed"
    _passed("live owner protected; stale lease recovered by a subsequent read within the 45s bound")

    _check("rerunnable-seed")
    before = store.snapshot(tenant_id)
    with connection.transaction():
        assert not seed_tenant(
            connection, demo_snapshot().model_copy(update={"tenant_id": tenant_id})
        )
    connection.commit()
    assert store.snapshot(tenant_id) == before
    assert store.get_run(tenant_id, run.id).status == "completed"
    _passed("repeat seed retained profiles, proposals, assignments and extension-derived evidence")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="run additive isolated cloud checks")
    arguments = parser.parse_args()
    try:
        password = os.environ["POC_APP_DB_PASSWORD"]
        with psycopg.connect(
            "",
            row_factory=dict_row,
            connect_timeout=15,
            application_name="staff-matching-bootstrap",
        ) as connection:
            if arguments.verify:
                verify(connection, password)
            else:
                bootstrap(connection, password)
        return 0
    except (KeyError, ValueError, RuntimeError, AssertionError, StoreError, psycopg.Error) as error:
        print(json.dumps(_failure_payload(error)), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
