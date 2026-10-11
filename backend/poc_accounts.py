"""Admin-only identity bootstrap/recovery; never run DDL from the Web application."""

import argparse
import getpass
import json
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from backend.account_seed import ACCOUNTS, initial_password
from backend.postgres_setup import bootstrap, runtime_conninfo, seed_tenant
from backend.seed import demo_snapshot


def setup_accounts(connection: psycopg.Connection, seed: str, app_password: str) -> None:
    from backend.identity import IdentityStore

    bootstrap(connection, app_password)
    migration = Path(__file__).resolve().parent.parent / "db" / "identity" / "001_identity.sql"
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(91539965855310)")
        connection.execute(migration.read_text(), prepare=False)
    identities = IdentityStore(connection.info.dsn)
    tenants: set[str] = set()
    for account in ACCOUNTS:
        if account.tenant_id not in tenants:
            seed_tenant(
                connection,
                demo_snapshot().model_copy(update={"tenant_id": account.tenant_id}),
            )
            tenants.add(account.tenant_id)
        identities.provision_user(
            username=account.username,
            display_name=account.username,
            password=initial_password(seed, account.username),
            tenant_id=account.tenant_id,
            tenant_name=account.tenant_name,
            role=account.role,
        )
    IdentityStore(runtime_conninfo(connection, app_password)).initialize()
    print(json.dumps({
        "status": "PASS",
        "check": "identity-bootstrap",
        "accounts": len(ACCOUNTS),
        "management_companies": len(tenants),
        "existing_credentials_preserved": True,
        "dataset": "legacy-small-fixtures; not the planned 10000-record SaaS dataset",
    }), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--verify", action="store_true")
    action.add_argument("--recover", metavar="USERNAME")
    parser.add_argument("--revoke-passkeys", action="store_true")
    args = parser.parse_args()
    if args.revoke_passkeys and not args.recover:
        parser.error("--revoke-passkeys requires --recover.")
    try:
        with psycopg.connect(
            "", row_factory=dict_row, application_name="staff-identity-setup"
        ) as conn:
            if args.recover:
                from backend.identity import IdentityStore

                password = os.getenv("POC_RECOVERY_PASSWORD") or getpass.getpass("New password: ")
                IdentityStore(conn.info.dsn).recover_password(
                    args.recover, password, revoke_passkeys=args.revoke_passkeys,
                    actor="authorized-admin-cli",
                )
                print(json.dumps({"status": "PASS", "check": "account-recovery"}))
            else:
                app_password = os.environ["POC_APP_DB_PASSWORD"]
                seed = os.environ["POC_ACCOUNT_SEED"]
                if args.verify:
                    from backend.poc_accounts_verify import verify_accounts

                    verify_accounts(conn, app_password)
                else:
                    setup_accounts(conn, seed, app_password)
    except Exception as error:
        # Credential processing must never include exception text or SQL parameters in logs.
        print(json.dumps({
            "status": "FAIL", "check": "identity-setup", "category": type(error).__name__,
            "sqlstate": getattr(error, "sqlstate", None),
        }), flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
