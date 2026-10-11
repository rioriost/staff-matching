from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from psycopg.pq import TransactionStatus

from backend import saas_data, saas_setup, saas_store

OLD_VERSION = "saas-jp-original-20261006-v1"


@pytest.fixture
def replacement(monkeypatch):
    connection = MagicMock()
    connection.info.transaction_status = TransactionStatus.IDLE
    cursor = connection.cursor.return_value.__enter__.return_value
    queries = []
    versions = dict.fromkeys(saas_data.TENANT_IDS, OLD_VERSION)

    def execute(statement, parameters=None):
        text = statement if isinstance(statement, str) else statement.as_string()
        queries.append((text, parameters))
        result = MagicMock()
        if text.startswith("SELECT dataset_version"):
            version = versions.get(parameters[0])
            result.fetchone.return_value = {"dataset_version": version} if version else None
        elif "count(DISTINCT split_part" in text:
            result.fetchall.return_value = [
                {"occupation": str(index), "people": 50, "surnames": 26} for index in range(20)
            ]
        elif "body->>'status'='open'" in text:
            result.fetchone.return_value = {"count": 970}
        return result

    cursor.execute.side_effect = execute
    generator = MagicMock(
        side_effect=lambda tenant: SimpleNamespace(
            tenant_id=tenant,
            version=saas_data.DATASET_VERSION,
        )
    )
    loader = MagicMock(return_value={"companies": 20, "sites": 60, "jobs": 1000, "people": 1000})
    identity = MagicMock(return_value={"users": {"count": 15, "fingerprint": "unchanged"}})
    monkeypatch.setattr(saas_data, "generate_tenant", generator)
    monkeypatch.setattr(saas_store, "load_dataset", loader)
    monkeypatch.setattr(saas_setup, "_identity_snapshot", identity)
    return SimpleNamespace(
        connection=connection,
        queries=queries,
        versions=versions,
        generator=generator,
        loader=loader,
        identity=identity,
    )


def replace(fixture, **overrides):
    arguments = {"from_version": OLD_VERSION, "confirm_version": saas_data.DATASET_VERSION}
    return saas_setup.replace_demo_data(fixture.connection, **(arguments | overrides))


@pytest.mark.parametrize(
    "arguments",
    [
        {"from_version": ""},
        {"from_version": saas_data.DATASET_VERSION},
        {"confirm_version": "unconfirmed"},
    ],
)
def test_replacement_requires_distinct_explicit_versions_before_database_access(
    replacement, arguments
):
    with pytest.raises(saas_setup.ReplacementError, match="explicit_source_and_target"):
        replace(replacement, **arguments)
    replacement.generator.assert_not_called()
    replacement.connection.cursor.assert_not_called()


def test_replacement_requires_ownership_of_the_outer_transaction(replacement):
    replacement.connection.info.transaction_status = TransactionStatus.INTRANS
    with pytest.raises(saas_setup.ReplacementError, match="connection_must_be_idle"):
        replace(replacement)
    replacement.connection.transaction.assert_not_called()


@pytest.mark.parametrize("version", [None, saas_data.DATASET_VERSION, "unexpected"])
def test_every_tenant_version_is_checked_before_the_first_delete(replacement, version):
    replacement.versions[saas_data.TENANT_IDS[-1]] = version
    with pytest.raises(saas_setup.ReplacementError, match="source_dataset_version_mismatch"):
        replace(replacement)
    assert not any(text.startswith(("DELETE", "UPDATE")) for text, _ in replacement.queries)
    replacement.loader.assert_not_called()
    replacement.connection.transaction.return_value.__exit__.assert_called_once()


def test_replacement_is_tenant_scoped_preserves_identity_and_rotates_cursors(replacement):
    report = replace(replacement)
    assert report["committed"] is True
    assert report["identity_preserved"] is True
    assert report["jobs"] == report["people"] == 10000
    assert len(report["tenants"]) == 10
    assert replacement.generator.call_count == replacement.loader.call_count == 10
    replacement.connection.transaction.assert_called_once_with()
    replacement.connection.commit.assert_not_called()
    replacement.connection.transaction.return_value.__exit__.assert_called_once_with(
        None, None, None
    )
    locks = [text for text, _ in replacement.queries if text.startswith("LOCK TABLE")]
    assert len(locks) == 1
    assert '"staff_saas"."dispatch"' in locks[0]
    assert '"staff_saas"."outbox_dispatch"' in locks[0]
    assert "ACCESS EXCLUSIVE" in locks[0]
    deletes = [
        (text, parameters) for text, parameters in replacement.queries if text.startswith("DELETE")
    ]
    assert len(deletes) == len(saas_setup.REPLACED_TABLES) * 10
    assert {parameters for _, parameters in deletes} == {
        (tenant,) for tenant in saas_data.TENANT_IDS
    }
    assert all("WHERE tenant_id=%s" in text for text, _ in deletes)
    assert not any(
        forbidden in text
        for text, _ in deletes
        for forbidden in ('"tenants"', '"simulation_grants"', "staff_identity", "staff_matching")
    )
    updates = [parameters for text, parameters in replacement.queries if text.startswith("UPDATE")]
    assert len({parameters[1] for parameters in updates}) == 10
    assert all(len(parameters[1]) == 32 for parameters in updates)
    assert replacement.identity.call_count == 2


def test_load_failure_leaves_transaction_context_with_error_for_full_rollback(replacement):
    replacement.loader.side_effect = [
        replacement.loader.return_value,
        replacement.loader.return_value,
        RuntimeError("injected load failure"),
    ]
    with pytest.raises(RuntimeError, match="injected load failure"):
        replace(replacement)
    replacement.connection.commit.assert_not_called()
    rollback = replacement.connection.transaction.return_value.__exit__.call_args.args
    assert rollback[0] is RuntimeError
    assert replacement.identity.call_count == 1


def test_counts_and_identity_must_match_before_committing(replacement):
    replacement.loader.return_value = {"companies": 20, "sites": 60, "jobs": 999, "people": 1000}
    with pytest.raises(saas_setup.ReplacementError, match="replacement_counts_mismatch"):
        replace(replacement)
    assert (
        replacement.connection.transaction.return_value.__exit__.call_args.args[0]
        is saas_setup.ReplacementError
    )


def test_identity_change_aborts_the_complete_replacement(replacement):
    replacement.identity.side_effect = [{"users": "before"}, {"users": "after"}]
    with pytest.raises(saas_setup.ReplacementError, match="identity_changed"):
        replace(replacement)
    assert replacement.loader.call_count == 10
    assert (
        replacement.connection.transaction.return_value.__exit__.call_args.args[0]
        is saas_setup.ReplacementError
    )


def test_additive_loader_refuses_to_mix_different_dataset_versions():
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.execute.return_value.fetchone.return_value = {"dataset_version": OLD_VERSION}
    with pytest.raises(RuntimeError, match="explicit administrator replacement"):
        saas_store.load_dataset(
            connection,
            SimpleNamespace(tenant_id="demo", version=saas_data.DATASET_VERSION),
        )
    cursor.executemany.assert_not_called()


@pytest.mark.parametrize(
    "arguments",
    [
        ["--replace-demo-data"],
        ["--replace-demo-data", "--from-version", OLD_VERSION],
        ["--confirm-version", saas_data.DATASET_VERSION],
        ["--verify", "--replace-demo-data"],
    ],
)
def test_cli_rejects_incomplete_or_conflicting_confirmation(monkeypatch, arguments):
    connect = MagicMock()
    monkeypatch.setattr(saas_setup.psycopg, "connect", connect)
    monkeypatch.setattr("sys.argv", ["saas_setup", *arguments])
    with pytest.raises(SystemExit) as error:
        saas_setup.main()
    assert error.value.code == 2
    connect.assert_not_called()
