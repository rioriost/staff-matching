"""Offline contract tests; these do not claim to execute a PostgreSQL server."""

import inspect
import json
from pathlib import Path
from unittest.mock import Mock

import psycopg
import pytest

from backend.postgres_features import enriched_evidence, skill_vector, synthetic_point
from backend.postgres_store import APPLICATION_TABLES, RUN_LEASE_SECONDS, PostgresStore
from backend.seed import demo_snapshot
from backend.store import Store, StoreError


def test_postgres_supports_store_public_contract():
    methods = {
        name
        for name, method in inspect.getmembers(Store, predicate=callable)
        if not name.startswith("_")
    }
    assert methods <= set(dir(PostgresStore))
    assert 10 < RUN_LEASE_SECONDS <= 60
    assert inspect.signature(PostgresStore).parameters["conninfo"].default is None


def test_tenant_context_is_transaction_local_and_parameterized():
    connection = Mock()
    tenant = "demo'; SET ROLE malicious; --"
    PostgresStore._set_context(connection, tenant)
    statement, values = connection.execute.call_args_list[0].args
    assert statement == "SELECT set_config('app.tenant_id', %s, true)"
    assert values == (tenant,)
    assert tenant not in statement


def test_rolling_revisions_do_not_take_a_process_lock():
    first, second = PostgresStore(), PostgresStore()
    assert first.owner_id != second.owner_id
    with first.server_lock(), second.server_lock():
        pass
    with pytest.raises(StoreError, match="another live") as caught:
        first._require_owner({"owner_id": second.owner_id})
    assert caught.value.code == "run_owner"


def test_initialize_is_readiness_only():
    source = inspect.getsource(PostgresStore.initialize)
    for forbidden in ("CREATE ", "INSERT ", "UPDATE ", "GRANT ", "ALTER ", "demo_snapshot"):
        assert forbidden not in source
    assert "rolbypassrls" in source
    assert "relforcerowsecurity" in source
    assert "owns_table" in source


def test_structured_vectors_are_deterministic_not_language_embeddings():
    assert skill_vector({"office": 3, "packing": 2}) == "[3,0,2,0]"
    assert skill_vector(None) == "[0,0,0,0]"
    assert synthetic_point(2) == synthetic_point(2)
    longitude, latitude = synthetic_point(2, site=True)
    assert 139.6 < longitude < 139.9 and 35.5 < latitude < 35.9


def test_snapshot_evidence_consumes_spatial_vector_and_graph_results():
    evidence = demo_snapshot().evidence[0]
    feature = {
        "spatial_distance_m": 7000.0,
        "vector_distance": 0.25,
        "history_fit": 0.7,
        "rated_assignments": 3,
    }
    enriched = enriched_evidence(evidence, feature)
    assert enriched.commute_minutes == 25
    assert enriched.skill_fit == pytest.approx(0.9 * evidence.skill_fit + 0.1 * 0.75)
    assert enriched.history_fit == 0.7
    assert enriched.rated_assignments == 3
    assert enriched.synthetic is True
    assert evidence.history_fit is None


@pytest.mark.parametrize("distance", [None, float("nan")])
def test_zero_vectors_and_unknown_commutes_stay_safe(distance):
    pending = next(item for item in demo_snapshot().evidence if not item.commute_verified)
    enriched = enriched_evidence(
        pending,
        {
            "spatial_distance_m": 100.0,
            "vector_distance": distance,
            "history_fit": None,
            "rated_assignments": 0,
        },
    )
    assert enriched.skill_fit == pytest.approx(0.9 * pending.skill_fit)
    assert enriched.commute_minutes is None
    assert not enriched.commute_verified
    assert enriched.history_fit is None


def test_migration_declares_tenant_constraints_and_controlled_real_extension_operations():
    migration = (Path(__file__).resolve().parents[1] / "db" / "001_postgres.sql").read_text()
    for table in APPLICATION_TABLES:
        assert f"CREATE TABLE staff_matching.{table} " in migration
        assert f"'{table}'" in migration
    assert "ENABLE ROW LEVEL SECURITY" in migration
    assert "FORCE ROW LEVEL SECURITY" in migration
    assert "EXCLUDE USING gist (tenant_id WITH =, staff_id WITH =, occupied WITH &&)" in migration
    assert "FOREIGN KEY (tenant_id, proposal_id)" in migration
    assert "FOREIGN KEY (tenant_id, staff_id, order_id)" in migration
    assert "WHERE id = NEW.tenant_id FOR UPDATE" in migration
    assert "CREATE FUNCTION staff_matching.feature_evidence() RETURNS jsonb" in migration
    assert "SECURITY DEFINER" in migration
    assert "SET search_path = pg_catalog, ag_catalog, staff_matching, public, pg_temp" in migration
    assert "graph_name := 'sm_' || md5(tenant)" in migration
    assert "public.ST_Distance(s.origin, o.site)" in migration
    assert "OPERATOR(public.<=>)" in migration
    assert "count(h), avg(h.rating)" in migration
    assert "FROM ag_catalog.cypher(%L, %L)" in migration
    assert "source.rated_assignments IS DISTINCT FROM projected.rated_assignments" in migration
    assert "REVOKE ALL ON FUNCTION staff_matching.feature_evidence() FROM PUBLIC" in migration


def test_setup_never_prints_connection_or_exception_text():
    from backend import postgres_setup

    source = inspect.getsource(postgres_setup.main)
    assert "str(error)" not in source and "repr(error)" not in source
    assert "_failure_payload(error)" in source
    assert "print(connection" not in source
    assert "POC_APP_DB_PASSWORD" in source


@pytest.mark.parametrize(
    "query",
    [
        'MERGE (n:Staff {id: "s01"}) RETURN n',
        'MERGE (n:Staff {id: "$staff_cypher$\' ; SELECT 1; --"}) RETURN n',
        'MERGE (n:Staff {id: "$staff_cypher$ $staff_cypher_$"}) RETURN n',
    ],
)
def test_age_cypher_argument_is_safely_dollar_quoted(query):
    from backend.postgres_setup import _cypher_statement

    rendered = _cypher_statement("sm_synthetic", query).as_string()
    prefix = "SELECT * FROM ag_catalog.cypher('sm_synthetic', "
    suffix = ") AS (value ag_catalog.agtype)"
    assert rendered.startswith(prefix) and rendered.endswith(suffix)
    argument = rendered[len(prefix) : -len(suffix)]
    delimiter = argument[: argument.index("$", 1) + 1]
    assert delimiter.startswith("$staff_cypher")
    assert delimiter not in query
    assert argument == delimiter + query + delimiter


def test_cypher_execution_uses_simple_query_and_operation_label():
    from backend import postgres_setup

    connection = Mock()
    postgres_setup._check("synthetic-seed")
    postgres_setup._cypher(
        connection, "sm_synthetic", "MATCH (n) RETURN n", operation="graph.merge-staff"
    )
    assert connection.execute.call_args.kwargs == {"prepare": False}
    assert postgres_setup._operation == "graph.merge-staff"


def test_seed_diagnostics_include_actionable_fields_but_not_context_or_secrets(monkeypatch):
    from backend import postgres_setup

    monkeypatch.setenv("PGPASSWORD", "diagnostic-secret-placeholder")
    monkeypatch.setenv("PGUSER", "diagnostic-admin-placeholder")
    postgres_setup._check("synthetic-seed")
    postgres_setup._seed_operation("graph.merge-staff")
    error = psycopg.errors.SyntaxError(
        "exception text must never be emitted",
        info={
            77: (
                b"a dollar-quoted string constant is expected "
                b"diagnostic-secret-placeholder diagnostic-admin-placeholder"
            ),
            80: b"69",
            113: b"internal SQL must never be emitted",
            87: b"SQL context must never be emitted",
            68: b"row detail must never be emitted",
        },
    )
    payload = postgres_setup._failure_payload(error)
    assert payload["operation"] == "graph.merge-staff"
    assert payload["sqlstate"] == "42601"
    assert payload["statement_position"] == 69
    assert payload["message_primary"].startswith("a dollar-quoted string constant is expected")
    rendered = json.dumps(payload)
    assert "diagnostic-secret-placeholder" not in rendered
    assert "diagnostic-admin-placeholder" not in rendered
    assert "must never be emitted" not in rendered


def test_credential_phase_resets_and_suppresses_seed_diagnostics():
    from backend import postgres_setup

    postgres_setup._seed_operation("graph.merge-staff")
    postgres_setup._check("restricted-runtime-role")
    payload = postgres_setup._failure_payload(
        psycopg.errors.SyntaxError("credential SQL", info={77: b"credential detail", 80: b"42"})
    )
    assert "operation" not in payload
    assert "message_primary" not in payload
    assert "statement_position" not in payload


def test_feature_migration_repairs_quoting_without_mutating_existing_data():
    from backend.postgres_store import MIGRATION_VERSION

    migration = (
        Path(__file__).resolve().parents[1] / "db" / "002_age_dollar_quoting.sql"
    ).read_text()
    assert MIGRATION_VERSION >= 2
    assert "CREATE OR REPLACE FUNCTION staff_matching.feature_evidence()" in migration
    assert "FROM ag_catalog.cypher(%L, $history$" in migration
    assert "FROM ag_catalog.cypher(%L, %L)" not in migration
    assert "RETURN s.id, o.id, count(h), avg(h.rating)" in migration
    assert "INSERT INTO staff_matching.schema_migrations(version) VALUES (2)" in migration
    for destructive in ("DROP ", "DELETE ", "TRUNCATE ", "UPDATE staff_matching."):
        assert destructive not in migration


def test_scalar_text_migration_encodes_identifiers_and_preserves_feature_contract():
    from backend.postgres_store import MIGRATION_VERSION

    directory = Path(__file__).resolve().parents[1] / "db"
    previous = (directory / "002_age_dollar_quoting.sql").read_text()
    migration = (directory / "003_age_scalar_text.sql").read_text()
    assert MIGRATION_VERSION == 3
    assert "'staff_id', staff_id::text" in migration
    assert "'order_id', order_id::text" in migration
    assert "(staff_id::text)::jsonb" not in migration
    assert "(order_id::text)::jsonb" not in migration
    assert "INSERT INTO staff_matching.schema_migrations(version) VALUES (3)" in migration
    assert "FROM ag_catalog.cypher(%L, $history$" in migration
    # Beyond the two faulty casts, preserve the entire established feature routine.
    start = "CREATE OR REPLACE FUNCTION"
    end = "INSERT INTO staff_matching.schema_migrations"
    expected = (
        previous[previous.index(start) : previous.index(end)]
        .replace("(staff_id::text)::jsonb", "staff_id::text")
        .replace("(order_id::text)::jsonb", "order_id::text")
    )
    assert migration[migration.index(start) : migration.index(end)] == expected
    for destructive in ("DROP ", "DELETE ", "TRUNCATE ", "UPDATE staff_matching."):
        assert destructive not in migration
