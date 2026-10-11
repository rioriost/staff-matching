"""Read-only, non-profile acceptance checks suitable for the in-VNet setup job."""

from backend.saas_store import BUSINESS_TABLES, SaasStore, tenant_context
from backend.saas_worker import SaasWorker


def verify_saas_runtime(
    app_conninfo: str,
    worker_conninfo: str,
    tenant_ids: list[str] | None = None,
    expected_per_tenant: int = 1000,
) -> dict:
    """Verify readiness, forced isolation and worker ACLs without submitting work."""
    tenants = tenant_ids or ["demo", *(f"demo{i:02}" for i in range(2, 11))]
    app = SaasStore(app_conninfo)
    worker = SaasWorker(worker_conninfo)
    app.initialize()
    worker.initialize()
    result = {"schema_version": 1, "tenants": [], "jobs": 0, "people": 0}
    with app.connect() as connection:
        for tenant in tenants:
            with connection.transaction():
                tenant_context(connection, tenant)
                counts = {}
                for table in ("jobs", "people"):
                    counts[table] = connection.execute(
                        f"SELECT count(*) AS n FROM staff_saas.{table}",
                    ).fetchone()["n"]
                    if counts[table] != expected_per_tenant:
                        raise RuntimeError(f"Unexpected {table} count for tenant {tenant}.")
                for table in BUSINESS_TABLES:
                    if table in {"audit_events", "outbox"}:
                        # The web role intentionally has INSERT-only access.
                        continue
                    if connection.execute(
                        f"SELECT 1 FROM staff_saas.{table} WHERE tenant_id != %s LIMIT 1",
                        (tenant,),
                    ).fetchone():
                        raise RuntimeError(f"Cross-tenant visibility in {table}.")
                result["tenants"].append({"tenant_id": tenant, **counts})
                result["jobs"] += counts["jobs"]
                result["people"] += counts["people"]
        # Local settings are rolled back at this explicit outer boundary before
        # checking the same connection's unset context.
        connection.commit()
        with connection.transaction():
            if connection.execute("SELECT count(*) AS n FROM staff_saas.jobs").fetchone()["n"]:
                raise RuntimeError("Tenant context leaked across transactions.")
    with worker.store.connect() as connection:
        grants = connection.execute(
            "SELECT has_table_privilege(current_user,'staff_saas.people','UPDATE') AS profiles, "
            "has_table_privilege(current_user,'staff_saas.simulation_grants','INSERT') "
            "AS simulation, "
            "has_table_privilege(current_user,'staff_saas.dispatch','SELECT') AS dispatch"
        ).fetchone()
        if any(grants.values()):
            raise RuntimeError("Worker has overbroad runtime privileges.")
    result["rls_isolation"] = True
    result["worker_acl"] = True
    result["scope"] = (
        "Readiness/isolation/counts only; workflow/lease concurrency require live tests."
    )
    return result
