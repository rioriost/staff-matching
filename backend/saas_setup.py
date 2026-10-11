"""Private-network SaaS administration; replacement requires explicit version confirmation."""

import argparse
import json
import os
import secrets
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

from backend.account_seed import ACCOUNTS
from backend.poc_accounts import setup_accounts
from backend.postgres_setup import runtime_conninfo

REPLACED_TABLES = (
    "notifications",
    "outbox",
    "audit_events",
    "idempotency",
    "appointments",
    "placements",
    "introductions",
    "candidates",
    "runs",
    "jobs",
    "people",
    "sites",
    "companies",
    "dataset_manifests",
    "dispatch",
    "outbox_dispatch",
)


class ReplacementError(RuntimeError):
    """Safe operator-facing reason codes, never database exception text."""


def _identity_snapshot(connection):
    with connection.cursor(row_factory=dict_row) as cursor:
        tables = cursor.execute(
            "SELECT tablename FROM pg_catalog.pg_tables "
            "WHERE schemaname='staff_identity' ORDER BY tablename"
        ).fetchall()
        if not tables:
            raise ReplacementError("identity_schema_missing")
        identifiers = [sql.Identifier("staff_identity", row["tablename"]) for row in tables]
        cursor.execute(
            sql.SQL("LOCK TABLE {} IN SHARE MODE").format(sql.SQL(",").join(identifiers))
        )
        return {
            row["tablename"]: cursor.execute(
                sql.SQL(
                    "SELECT count(*) AS count, "
                    "md5(coalesce(string_agg(to_jsonb(t)::text,',' "
                    "ORDER BY to_jsonb(t)::text),'')) "
                    "AS fingerprint FROM {} AS t"
                ).format(identifier)
            ).fetchone()
            for row, identifier in zip(tables, identifiers, strict=True)
        }


def replace_demo_data(connection, *, from_version: str, confirm_version: str) -> dict:
    """Atomically replace only the ten demo tenants, retaining identity and simulation grants.

    Stop the API and worker revisions before running. New generation-specific IDs
    and cursor secrets reject actions/cursors retained by a pre-replacement tab.
    """
    from backend.saas_data import DATASET_VERSION, TENANT_IDS, generate_tenant
    from backend.saas_store import BUSINESS_TABLES, load_dataset, tenant_context

    if confirm_version != DATASET_VERSION or not from_version or from_version == DATASET_VERSION:
        raise ReplacementError("explicit_source_and_target_versions_required")
    if connection.info.transaction_status != TransactionStatus.IDLE:
        raise ReplacementError("administrator_connection_must_be_idle")
    datasets = [generate_tenant(tenant) for tenant in TENANT_IDS]
    report = {"version": DATASET_VERSION, "tenants": [], "jobs": 0, "people": 0}
    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute("SET LOCAL lock_timeout = '10s'")
        cursor.execute("SELECT pg_advisory_xact_lock(91539965855311)")
        tables = (*BUSINESS_TABLES, "dispatch", "outbox_dispatch")
        cursor.execute(
            sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(
                sql.SQL(",").join(sql.Identifier("staff_saas", table) for table in tables)
            )
        )
        identity = _identity_snapshot(connection)
        for tenant in TENANT_IDS:
            tenant_context(connection, tenant)
            current = cursor.execute(
                "SELECT dataset_version FROM staff_saas.tenants WHERE tenant_id=%s",
                (tenant,),
            ).fetchone()
            if current is None or current["dataset_version"] != from_version:
                raise ReplacementError("source_dataset_version_mismatch")
        for dataset in datasets:
            tenant = dataset.tenant_id
            tenant_context(connection, tenant)
            for table in REPLACED_TABLES:
                cursor.execute(
                    sql.SQL("DELETE FROM staff_saas.{} WHERE tenant_id=%s").format(
                        sql.Identifier(table)
                    ),
                    (tenant,),
                )
            cursor.execute(
                "UPDATE staff_saas.tenants SET dataset_version=%s,cursor_secret=%s "
                "WHERE tenant_id=%s",
                (dataset.version, secrets.token_bytes(32), tenant),
            )
            counts = load_dataset(connection, dataset)
            if counts != {"companies": 20, "sites": 60, "jobs": 1000, "people": 1000}:
                raise ReplacementError("replacement_counts_mismatch")
            diversity = cursor.execute(
                "SELECT summary->>'occupation' AS occupation,count(*) AS people,"
                "count(DISTINCT split_part(summary->>'name',' ',1)) AS surnames "
                "FROM staff_saas.people WHERE tenant_id=%s GROUP BY summary->>'occupation'",
                (tenant,),
            ).fetchall()
            if len(diversity) != 20 or any(
                row["people"] != 50 or row["surnames"] < 20 for row in diversity
            ):
                raise ReplacementError("replacement_diversity_mismatch")
            open_jobs = cursor.execute(
                "SELECT count(*) AS count FROM staff_saas.jobs "
                "WHERE tenant_id=%s AND body->>'status'='open'",
                (tenant,),
            ).fetchone()["count"]
            report["tenants"].append(
                {
                    "tenant_id": tenant,
                    **counts,
                    "open_jobs": open_jobs,
                    "minimum_surnames_per_occupation": min(row["surnames"] for row in diversity),
                }
            )
            report["jobs"] += counts["jobs"]
            report["people"] += counts["people"]
        if _identity_snapshot(connection) != identity:
            raise ReplacementError("identity_changed_during_replacement")
    report["identity_preserved"] = True
    report["committed"] = True
    return report


def worker_conninfo(connection: psycopg.Connection, password: str) -> str:
    parameters = connection.info.get_parameters()
    parameters.update(user="staff_worker", password=password)
    return make_conninfo(**parameters)


def setup_saas(
    connection: psycopg.Connection,
    app_password: str,
    worker_password: str,
    account_seed: str,
) -> None:
    from backend.saas_data import generate_tenant
    from backend.saas_store import grant_demo_simulation, load_dataset
    from backend.saas_verification import verify_saas_runtime

    if len(worker_password) < 32:
        raise ValueError("A high-entropy worker database password is required.")
    setup_accounts(connection, account_seed, app_password)
    migration = Path(__file__).resolve().parent.parent / "db" / "saas" / "001_saas.sql"
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(91539965855311)")
        role = connection.execute(
            "SELECT rolsuper, rolbypassrls, rolreplication, rolcreatedb, rolcreaterole "
            "FROM pg_roles WHERE rolname = 'staff_worker'"
        ).fetchone()
        if role is None:
            connection.execute("CREATE ROLE staff_worker LOGIN NOINHERIT")
        elif any(role.values()):
            raise RuntimeError("Worker role has unexpected privileged attributes.")
        if connection.execute(
            "SELECT 1 FROM pg_auth_members WHERE member = 'staff_worker'::regrole LIMIT 1"
        ).fetchone():
            raise RuntimeError("Worker role must not inherit other roles.")
        verifier = connection.pgconn.encrypt_password(
            worker_password.encode(),
            b"staff_worker",
            b"scram-sha-256",
        ).decode()
        connection.execute(
            sql.SQL(
                "ALTER ROLE staff_worker LOGIN NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD {}"
            ).format(sql.Literal(verifier))
        )
        connection.execute("REVOKE ALL ON SCHEMA ag_catalog, staff_identity FROM staff_worker")
        connection.execute(migration.read_text(), prepare=False)
    totals = {"jobs": 0, "people": 0, "tenants": 0}
    for account in ACCOUNTS:
        if account.role != "coordinator":
            continue
        dataset = generate_tenant(account.tenant_id)
        if len(dataset.jobs) != 1000 or len(dataset.people) != 1000:
            raise RuntimeError("Each management tenant requires exactly 1000 jobs and people.")
        load_dataset(connection, dataset)
        with connection.transaction():
            connection.execute(
                "SELECT set_config('app.tenant_id', %s, true)",
                (account.tenant_id,),
            )
            for table in ("jobs", "people"):
                count = connection.execute(
                    sql.SQL(
                        "SELECT count(*) AS count FROM staff_saas.{} WHERE tenant_id = %s"
                    ).format(sql.Identifier(table)),
                    (account.tenant_id,),
                ).fetchone()["count"]
                if count != 1000:
                    raise RuntimeError("Stored tenant count differs from the 1000-record contract.")
            user = connection.execute(
                "SELECT id FROM staff_identity.users WHERE username = %s",
                (account.username,),
            ).fetchone()
            if user is None:
                raise RuntimeError("Expected coordinator account is absent.")
            grant_demo_simulation(connection, account.tenant_id, str(user["id"]))
        totals["jobs"] += len(dataset.jobs)
        totals["people"] += len(dataset.people)
        totals["tenants"] += 1
        print(
            json.dumps(
                {
                    "status": "PASS",
                    "check": "saas-tenant-load",
                    "tenant": account.tenant_id,
                    "jobs": len(dataset.jobs),
                    "people": len(dataset.people),
                }
            ),
            flush=True,
        )
    if totals != {"jobs": 10000, "people": 10000, "tenants": 10}:
        raise RuntimeError("The management-company dataset totals do not match the contract.")
    connection.commit()
    verify_saas_runtime(
        runtime_conninfo(connection, app_password),
        worker_conninfo(connection, worker_password),
    )
    print(json.dumps({"status": "PASS", "check": "saas-bootstrap", **totals}), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify", action="store_true")
    mode.add_argument("--replace-demo-data", action="store_true")
    parser.add_argument("--from-version")
    parser.add_argument("--confirm-version")
    args = parser.parse_args()
    if args.replace_demo_data != bool(args.from_version and args.confirm_version):
        parser.error("--replace-demo-data requires both --from-version and --confirm-version")
    if not args.replace_demo_data and (args.from_version or args.confirm_version):
        parser.error("Version confirmations are only valid with --replace-demo-data")
    try:
        with psycopg.connect(
            "",
            row_factory=dict_row,
            application_name="staff-saas-setup",
        ) as connection:
            app_password = os.environ["POC_APP_DB_PASSWORD"]
            worker_password = os.environ["POC_WORKER_DB_PASSWORD"]
            if args.replace_demo_data:
                report = replace_demo_data(
                    connection,
                    from_version=args.from_version,
                    confirm_version=args.confirm_version,
                )
                print(
                    json.dumps(
                        {
                            "status": "PASS",
                            "check": "saas-demo-replacement",
                            **report,
                        }
                    ),
                    flush=True,
                )
                from backend.saas_verification import verify_saas_runtime

                verify_saas_runtime(
                    runtime_conninfo(connection, app_password),
                    worker_conninfo(connection, worker_password),
                )
            elif args.verify:
                from backend.saas_verify import verify_saas

                verify_saas(connection, app_password, worker_password)
            else:
                setup_saas(
                    connection,
                    app_password,
                    worker_password,
                    os.environ["POC_ACCOUNT_SEED"],
                )
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "check": "saas-setup",
                    "category": type(error).__name__,
                    "sqlstate": getattr(error, "sqlstate", None),
                    "reason": str(error) if isinstance(error, ReplacementError) else None,
                }
            ),
            flush=True,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
