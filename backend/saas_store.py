"""Tenant-scoped SaaS persistence. Runtime initialization is strictly read-only.

Connections use the shared process-wide guard without pooling; every query uses
transaction-local tenant context. Mutations serialize on the tenant row.
Identity membership authorization remains the authenticated server's responsibility.
"""

import base64
import hashlib
import hmac
import json
import secrets
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from backend.database import connect
from backend.saas_domain import (
    MODEL_VERSION,
    TERMINAL_INTRODUCTIONS,
    evaluate,
    next_simulated_state,
    schedules_conflict,
    shift_intervals,
)
from backend.saas_engine import skill_vector
from backend.saas_models import (
    Appointment,
    AppointmentRequest,
    Candidate,
    Company,
    Dataset,
    Introduction,
    Job,
    MatchRequest,
    MatchRun,
    Notification,
    Overview,
    Page,
    Person,
    PersonRecord,
    PersonSummary,
    Point,
    ShortlistRequest,
    SimulationRequest,
    Site,
    Versioned,
)
from backend.store import StoreError

BUSINESS_TABLES = (
    "tenants",
    "dataset_manifests",
    "companies",
    "sites",
    "jobs",
    "people",
    "simulation_grants",
    "runs",
    "candidates",
    "introductions",
    "placements",
    "appointments",
    "idempotency",
    "audit_events",
    "outbox",
    "notifications",
)
INTRO_COLUMNS = (
    "id, job_id, person_id, company_id, candidate_alias, status, version, "
    "created_at, expires_at, simulated"
)
APPOINTMENT_COLUMNS = (
    "id, introduction_id, job_id, person_id, company_id, start_at, end_at, "
    "site_id, online, status, version"
)
RUN_COLUMNS = (
    "id, status, created_at, progress, attempt, error, coverage, recommended_count, "
    "solver_status, objective_bound, model_version"
)
TOKYO = ZoneInfo("Asia/Tokyo")


def identifier(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def encoded_vector(skills: dict[str, int]) -> str:
    return "[" + ",".join(str(v) for v in skill_vector(skills)) + "]"


def payload_hash(payload) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def model_body(model) -> Jsonb:
    return Jsonb(model.model_dump(mode="json"))


def tenant_context(connection, tenant_id: str):
    if not tenant_id or len(tenant_id) > 128:
        raise StoreError(403, "invalid_tenant", "An authorized tenant is required.")
    connection.execute(
        "SELECT set_config('app.tenant_id', %s, true), "
        "set_config('search_path', 'staff_saas,public,pg_catalog', true), "
        "set_config('lock_timeout', '5s', true), "
        "set_config('statement_timeout', '25s', true)",
        (tenant_id,),
    )


def _person(row) -> PersonRecord:
    return PersonRecord(
        **row["summary"],
        employment_history=row["history"],
        private_location=Point(lat=row["lat"], lon=row["lon"]),
        private_address=row["private_address"],
        contact_email=row["contact_email"],
    )


PERSON_COLUMNS = (
    "summary, history, private_address, contact_email, "
    "public.ST_Y(private_location::public.geometry) AS lat, "
    "public.ST_X(private_location::public.geometry) AS lon"
)


def load_dataset(admin_connection, dataset: Dataset) -> dict[str, int]:
    """Add missing stable IDs only; never overwrite operator edits or legacy data.

    Caller owns commit/rollback. Batches of 100 rows bound each SQL round trip.
    The manifest is recorded only after all normalized foreign keys are satisfied.
    """
    from backend.saas_routing import NETWORK_VERSION

    connection = admin_connection
    tenant_context(connection, dataset.tenant_id)
    connection.execute(
        "INSERT INTO staff_saas.tenants(tenant_id,dataset_version,network_version,cursor_secret) "
        "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
        (dataset.tenant_id, dataset.version, NETWORK_VERSION, secrets.token_bytes(32)),
    )
    with connection.cursor(row_factory=dict_row) as cursor:
        current = cursor.execute(
            "SELECT dataset_version FROM staff_saas.tenants WHERE tenant_id=%s FOR UPDATE",
            (dataset.tenant_id,),
        ).fetchone()
    if current["dataset_version"] != dataset.version:
        raise RuntimeError(
            "Dataset version differs; explicit administrator replacement is required."
        )
    tenant = dataset.tenant_id
    batches = [
        (
            "INSERT INTO staff_saas.companies(tenant_id,id,body) VALUES (%s,%s,%s) "
            "ON CONFLICT DO NOTHING",
            [(tenant, item.id, model_body(item)) for item in dataset.companies],
        ),
        (
            "INSERT INTO staff_saas.sites(tenant_id,id,company_id,body,location) "
            "VALUES (%s,%s,%s,%s,public.ST_SetSRID(public.ST_MakePoint(%s,%s),4326)"
            "::public.geography) ON CONFLICT DO NOTHING",
            [
                (tenant, item.id, item.company_id, model_body(item), item.point.lon, item.point.lat)
                for item in dataset.sites
            ],
        ),
        (
            "INSERT INTO staff_saas.jobs(tenant_id,id,company_id,site_id,version,body,features) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s::public.vector) ON CONFLICT DO NOTHING",
            [
                (
                    tenant,
                    item.id,
                    item.company_id,
                    item.site_id,
                    item.version,
                    model_body(item),
                    encoded_vector(item.required_skills),
                )
                for item in dataset.jobs
            ],
        ),
        (
            "INSERT INTO staff_saas.people(tenant_id,id,version,summary,history,private_location,"
            "private_address,contact_email,features) VALUES "
            "(%s,%s,%s,%s,%s,public.ST_SetSRID(public.ST_MakePoint(%s,%s),4326)"
            "::public.geography,%s,%s,%s::public.vector) ON CONFLICT DO NOTHING",
            [
                (
                    tenant,
                    item.id,
                    item.version,
                    Jsonb(item.public().model_dump(mode="json", exclude={"employment_history"})),
                    Jsonb([entry.model_dump(mode="json") for entry in item.employment_history]),
                    item.private_location.lon,
                    item.private_location.lat,
                    item.private_address,
                    item.contact_email,
                    encoded_vector(item.skills),
                )
                for item in dataset.people
            ],
        ),
    ]
    for statement, rows in batches:
        for start in range(0, len(rows), 100):
            with connection.cursor() as cursor:
                cursor.executemany(statement, rows[start : start + 100])
    counts = {}
    for table in ("companies", "sites", "jobs", "people"):
        with connection.cursor(row_factory=dict_row) as cursor:
            counts[table] = cursor.execute(
                f"SELECT count(*) AS n FROM staff_saas.{table} WHERE tenant_id=%s",
                (tenant,),
            ).fetchone()["n"]
    connection.execute(
        "INSERT INTO staff_saas.dataset_manifests(tenant_id,version,counts) VALUES (%s,%s,%s) "
        "ON CONFLICT DO NOTHING",
        (tenant, dataset.version, Jsonb(counts)),
    )
    return counts


def grant_demo_simulation(admin_connection, tenant_id: str, user_id: str) -> None:
    """Setup-only explicit actor grant; staff_app cannot grant itself this permission."""
    tenant_context(admin_connection, tenant_id)
    admin_connection.execute(
        "INSERT INTO staff_saas.simulation_grants(tenant_id,user_id) VALUES (%s,%s) "
        "ON CONFLICT DO NOTHING",
        (tenant_id, str(user_id)),
    )


class SaasStore:
    def __init__(self, conninfo: str = ""):
        self.conninfo = conninfo

    @contextmanager
    def connect(self):
        with connect(
            self.conninfo,
            row_factory=dict_row,
            connect_timeout=10,
            application_name="staff-saas",
        ) as connection:
            yield connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            role = connection.execute(
                "SELECT rolsuper,rolbypassrls FROM pg_catalog.pg_roles WHERE rolname=current_user"
            ).fetchone()
            if role["rolsuper"] or role["rolbypassrls"]:
                raise RuntimeError("SaaS runtime must be a non-superuser without BYPASSRLS.")
            tables = connection.execute(
                "SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity,"
                "pg_catalog.pg_has_role(current_user,c.relowner,'MEMBER') AS owns "
                "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='staff_saas' AND c.relname=ANY(%s)",
                (list(BUSINESS_TABLES),),
            ).fetchall()
            if len(tables) != len(BUSINESS_TABLES) or any(
                not t["relrowsecurity"] or not t["relforcerowsecurity"] or t["owns"] for t in tables
            ):
                raise RuntimeError("SaaS requires non-owner access and FORCE RLS on all tables.")
            if (
                connection.execute(
                    "SELECT max(version) AS n FROM staff_saas.schema_migrations"
                ).fetchone()["n"]
                != 1
            ):
                raise RuntimeError("SaaS schema version one is required.")
            extensions = connection.execute(
                "SELECT extname FROM pg_catalog.pg_extension "
                "WHERE extname IN ('postgis','vector','btree_gist')"
            ).fetchall()
            if len(extensions) != 3:
                raise RuntimeError("SaaS requires PostGIS, pgvector and btree_gist.")
            if connection.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname='ag_catalog' "
                "AND has_schema_privilege(current_user,oid,'USAGE')) AS allowed"
            ).fetchone()["allowed"]:
                raise RuntimeError("SaaS runtime must not have raw AGE access.")

    @contextmanager
    def transaction(self, tenant_id: str, *, mutate=False, maintenance=True):
        rejected = None
        try:
            with self.connect() as connection:
                tenant_context(connection, tenant_id)
                tenant = connection.execute(
                    "SELECT * FROM tenants WHERE tenant_id=%s"
                    + (" FOR UPDATE" if mutate or maintenance else ""),
                    (tenant_id,),
                ).fetchone()
                if tenant is None:
                    raise StoreError(404, "tenant_not_found", "Business tenant is not provisioned.")
                if maintenance:
                    self._expire(connection, tenant_id)
                    try:
                        with connection.transaction():
                            yield connection, tenant
                    except StoreError as error:
                        rejected = error
                else:
                    # No expiry work needs preserving on rejection: the enclosing
                    # connection transaction supplies rollback without a savepoint.
                    yield connection, tenant
            if rejected:
                raise rejected
        except (psycopg.errors.UniqueViolation, psycopg.errors.ExclusionViolation) as error:
            raise StoreError(
                409,
                "concurrent_conflict",
                "A duplicate action or overlapping reservation already exists.",
            ) from error
        except (psycopg.OperationalError, psycopg.errors.QueryCanceled) as error:
            raise StoreError(503, "database_unavailable", "Please retry this operation.") from error

    def _expire(self, connection, tenant):
        rows = connection.execute(
            "UPDATE introductions i SET status='expired',version=i.version+1 "
            "WHERE i.tenant_id=%s AND i.status NOT IN ('placed','expired','cancelled','declined') "
            "AND (i.expires_at<=clock_timestamp() OR EXISTS(SELECT 1 FROM jobs j "
            "WHERE j.tenant_id=i.tenant_id AND j.id=i.job_id AND j.body->>'status'='closed')) "
            "RETURNING i.*",
            (tenant,),
        ).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE appointments SET status='cancelled',version=version+1 "
                "WHERE tenant_id=%s AND introduction_id=%s AND status!='cancelled'",
                (tenant, row["id"]),
            )
            self._event(connection, tenant, "system", "expired", row)

    @staticmethod
    def _simulation(connection, tenant, user_id, allow_simulation):
        if (
            not allow_simulation
            or connection.execute(
                "SELECT 1 FROM simulation_grants WHERE tenant_id=%s AND user_id=%s",
                (tenant, str(user_id)),
            ).fetchone()
            is None
        ):
            raise StoreError(
                403,
                "simulation_forbidden",
                "A server-enabled, explicitly granted demo actor is required.",
            )

    @staticmethod
    def _one(connection, table, tenant, key):
        # table comes exclusively from constants in this module, never HTTP input.
        row = connection.execute(
            f"SELECT * FROM {table} WHERE tenant_id=%s AND id=%s",
            (tenant, key),
        ).fetchone()
        if row is None:
            raise StoreError(
                404, "not_found", "The requested object does not exist in this tenant."
            )
        return row

    @staticmethod
    def _version(row, version):
        if row["version"] != version:
            raise StoreError(409, "stale_version", "This object changed; refresh before acting.")

    @staticmethod
    def _intro(row):
        return Introduction.model_validate({k: row[k] for k in Introduction.model_fields})

    @staticmethod
    def _appointment(row):
        body = {k: row[k] for k in Appointment.model_fields}
        body["start_at"] = body["start_at"].astimezone(TOKYO)
        body["end_at"] = body["end_at"].astimezone(TOKYO)
        return Appointment.model_validate(body)

    @staticmethod
    def _run(row):
        return MatchRun.model_validate({k: row[k] for k in MatchRun.model_fields})

    def _mutate(self, tenant, user, operation, request, model, callback, preflight=None):
        payload = request.model_dump(mode="json") if hasattr(request, "model_dump") else request
        key = payload["idempotency_key"]
        digest = payload_hash(payload)
        with self.transaction(tenant, mutate=True) as (connection, _):
            if preflight:
                preflight(connection)
            previous = connection.execute(
                "SELECT * FROM idempotency WHERE tenant_id=%s AND user_id=%s AND key=%s",
                (tenant, str(user), key),
            ).fetchone()
            if previous:
                if previous["operation"] != operation or previous["fingerprint"] != digest:
                    raise StoreError(
                        409,
                        "idempotency_conflict",
                        "This idempotency key was used for a different request.",
                    )
                return model.model_validate(previous["response"])
            recent = connection.execute(
                "SELECT count(*) AS n FROM idempotency WHERE tenant_id=%s AND user_id=%s "
                "AND created_at>clock_timestamp()-interval '1 minute'",
                (tenant, str(user)),
            ).fetchone()["n"]
            if recent >= 120:
                raise StoreError(
                    429,
                    "mutation_rate_limit",
                    "At most 120 mutations per actor and tenant per minute.",
                )
            response = callback(connection)
            connection.execute(
                "INSERT INTO idempotency(tenant_id,user_id,key,operation,fingerprint,response) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (tenant, str(user), key, operation, digest, model_body(response)),
            )
            return response

    @staticmethod
    def _event(connection, tenant, user, event, intro):
        connection.execute(
            "INSERT INTO audit_events(tenant_id,id,actor,event,entity_id) VALUES (%s,%s,%s,%s,%s)",
            (tenant, identifier("audit"), str(user), event, intro["id"]),
        )
        for audience, recipient in (
            ("management", tenant),
            ("company", intro["company_id"]),
            ("person", intro["person_id"]),
        ):
            notice = Notification(
                id=identifier("notice"),
                audience=audience,
                recipient_id=recipient,
                kind=event,
                title=f"[SIMULATED / デモ] {event}",
                message=f"架空データの操作です。{intro['candidate_alias']} / "
                f"{event}。実在する企業・本人の依頼や同意を示すものではありません。",
                introduction_id=intro["id"],
                created_at=datetime.now(UTC),
                read=False,
            )
            connection.execute(
                "INSERT INTO outbox(tenant_id,id,body) VALUES (%s,%s,%s)",
                (tenant, notice.id, model_body(notice)),
            )
        connection.execute("SELECT staff_saas.wake_outbox(%s)", (tenant,))

    @staticmethod
    def _cursor(secret, tenant, scope, after=None, cursor=None):
        binding = payload_hash({"tenant": tenant, "scope": scope})
        if cursor:
            try:
                raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
                payload, signature = raw[:-32], raw[-32:]
                if not hmac.compare_digest(
                    signature, hmac.digest(bytes(secret), payload, "sha256")
                ):
                    raise ValueError
                decoded = json.loads(payload)
                if decoded["binding"] != binding or not isinstance(decoded["after"], str):
                    raise ValueError
                return decoded["after"]
            except (ValueError, KeyError, TypeError, UnicodeError) as error:
                raise StoreError(
                    400, "invalid_cursor", "Cursor is invalid for this tenant or these filters."
                ) from error
        payload = json.dumps({"binding": binding, "after": after}, separators=(",", ":")).encode()
        return (
            base64.urlsafe_b64encode(
                payload + hmac.digest(bytes(secret), payload, "sha256"),
            )
            .decode()
            .rstrip("=")
        )

    def _page(
        self,
        connection,
        tenant_row,
        table,
        model,
        *,
        where="",
        args=(),
        scope=None,
        cursor=None,
        limit=50,
        columns="id,body",
        mapper=None,
        key="id",
    ):
        if not 1 <= limit <= 100:
            raise StoreError(422, "invalid_limit", "Page size must be between one and 100.")
        tenant = tenant_row["tenant_id"]
        condition = "tenant_id=%s" + (" AND " + where if where else "")
        parameters = [tenant, *args]
        count_condition = condition
        count_parameters = tuple(parameters)
        scope = {"table": table, "filters": scope or {}}
        if cursor:
            after = self._cursor(tenant_row["cursor_secret"], tenant, scope, cursor=cursor)
            condition += f" AND {key} > %s"
            parameters.append(after)
        # The independent count/page statements share one network flush. Only
        # limit+1 bodies are fetched; COUNT never transfers profile bodies.
        with connection.pipeline():
            count_cursor = connection.execute(
                f"SELECT count(*) AS n FROM {table} WHERE {count_condition}",
                count_parameters,
            )
            page_cursor = connection.execute(
                f"SELECT {columns} FROM {table} WHERE {condition} ORDER BY {key} LIMIT %s",
                (*parameters, limit + 1),
            )
        total = count_cursor.fetchone()["n"]
        rows = page_cursor.fetchall()
        next_cursor = None
        if len(rows) > limit:
            next_cursor = self._cursor(
                tenant_row["cursor_secret"],
                tenant,
                scope,
                after=rows[limit - 1]["id"],
            )
        items = [
            (mapper(row) if mapper else model.model_validate(row["body"])) for row in rows[:limit]
        ]
        return Page[model](items=items, next_cursor=next_cursor, total=total)

    def overview(self, tenant, user_id, allow_simulation=False):
        with self.transaction(tenant) as (connection, row):
            counts = {}
            for table in ("jobs", "people", "companies", "introductions", "appointments"):
                counts[table] = connection.execute(
                    f"SELECT count(*) AS n FROM {table} WHERE tenant_id=%s",
                    (tenant,),
                ).fetchone()["n"]
            unread = connection.execute(
                "SELECT count(*) AS n FROM notifications WHERE tenant_id=%s "
                "AND audience='management' AND NOT read",
                (tenant,),
            ).fetchone()["n"]
            can_simulate = (
                allow_simulation
                and connection.execute(
                    "SELECT 1 FROM simulation_grants WHERE tenant_id=%s AND user_id=%s",
                    (tenant, str(user_id)),
                ).fetchone()
                is not None
            )
            return Overview(
                tenant_id=tenant,
                dataset_version=row["dataset_version"],
                network_version=row["network_version"],
                unread_notifications=unread,
                can_simulate=can_simulate,
                **counts,
            )

    def list_jobs(self, tenant, *, q="", occupation="", business_type="", cursor=None, limit=50):
        clauses, values = [], []
        for field, value in (("occupation", occupation), ("business_type", business_type)):
            if value:
                clauses.append(f"body->>'{field}'=%s")
                values.append(value)
        if q:
            clauses.append("(strpos(body->>'title',%s)>0 OR strpos(body->>'duties',%s)>0)")
            values.extend((q, q))
        with self.transaction(tenant, maintenance=False) as (connection, row):
            return self._page(
                connection,
                row,
                "jobs",
                Job,
                where=" AND ".join(clauses),
                args=values,
                scope={"q": q, "occupation": occupation, "business_type": business_type},
                cursor=cursor,
                limit=limit,
            )

    def job(self, tenant, job_id):
        with self.transaction(tenant, maintenance=False) as (connection, _):
            return Job.model_validate(self._one(connection, "jobs", tenant, job_id)["body"])

    def list_people(self, tenant, *, q="", cursor=None, limit=50):
        with self.transaction(tenant, maintenance=False) as (connection, row):
            return self._page(
                connection,
                row,
                "people",
                PersonSummary,
                where=(
                    "(strpos(summary->>'name',%s)>0 OR strpos(summary->>'occupation',%s)>0)"
                    if q
                    else ""
                ),
                args=(q, q) if q else (),
                scope={"q": q},
                cursor=cursor,
                limit=limit,
                columns="id, summary AS body",
            )

    def person(self, tenant, person_id):
        with self.transaction(tenant, maintenance=False) as (connection, _):
            row = connection.execute(
                "SELECT summary,history FROM people WHERE tenant_id=%s AND id=%s",
                (tenant, person_id),
            ).fetchone()
            if row is None:
                raise StoreError(404, "not_found", "Person not found in this tenant.")
            return Person(**row["summary"], employment_history=row["history"])

    def list_companies(self, tenant, *, cursor=None, limit=50):
        with self.transaction(tenant, maintenance=False) as (connection, row):
            return self._page(connection, row, "companies", Company, cursor=cursor, limit=limit)

    def list_sites(self, tenant, *, company_id="", cursor=None, limit=50):
        with self.transaction(tenant, maintenance=False) as (connection, row):
            return self._page(
                connection,
                row,
                "sites",
                Site,
                where="company_id=%s" if company_id else "",
                args=(company_id,) if company_id else (),
                scope={"company_id": company_id},
                cursor=cursor,
                limit=limit,
            )

    def create_run(self, tenant, user, request: MatchRequest):
        def action(connection):
            if connection.execute(
                "SELECT 1 FROM runs WHERE tenant_id=%s AND status IN ('queued','running')",
                (tenant,),
            ).fetchone():
                raise StoreError(429, "tenant_run_limit", "This tenant already has an active run.")
            if (
                connection.execute(
                    "SELECT count(*) AS n FROM runs WHERE tenant_id=%s "
                    "AND created_at>clock_timestamp()-interval '1 minute'",
                    (tenant,),
                ).fetchone()["n"]
                >= 3
            ):
                raise StoreError(429, "run_rate_limit", "At most three runs per tenant per minute.")
            if len(set(request.job_ids)) != len(request.job_ids):
                raise StoreError(422, "duplicate_jobs", "Job IDs must be unique.")
            jobs = connection.execute(
                "SELECT id FROM jobs WHERE tenant_id=%s AND body->>'status'='open' "
                + ("AND id=ANY(%s) " if request.job_ids else "")
                + "ORDER BY id LIMIT 1001",
                (tenant, request.job_ids) if request.job_ids else (tenant,),
            ).fetchall()
            ids = {row["id"] for row in jobs}
            if request.job_ids and ids != set(request.job_ids):
                raise StoreError(
                    409, "job_unavailable", "Some requested jobs are missing or closed."
                )
            if not jobs or len(jobs) > 1000:
                raise StoreError(422, "job_budget", "Select between one and 1,000 open jobs.")
            if len(set(request.pins)) != len(request.pins):
                raise StoreError(422, "duplicate_pins", "Pins must be unique.")
            for job_id, person_id in request.pins:
                if job_id not in ids:
                    raise StoreError(409, "pin_outside_run", "Pinned job must be included.")
                self._eligible(connection, tenant, job_id, person_id)
            run_id = identifier("run")
            body = request.model_copy(update={"job_ids": sorted(ids)})
            if not connection.execute(
                "SELECT staff_saas.enqueue(%s,%s) AS accepted",
                (tenant, run_id),
            ).fetchone()["accepted"]:
                raise StoreError(429, "queue_full", "The bounded worker queue is full.")
            row = connection.execute(
                f"INSERT INTO runs(tenant_id,id,status,request,model_version) "
                f"VALUES (%s,%s,'queued',%s,%s) RETURNING {RUN_COLUMNS}",
                (tenant, run_id, model_body(body), MODEL_VERSION),
            ).fetchone()
            connection.execute(
                "INSERT INTO audit_events(tenant_id,id,actor,event,entity_id) "
                "VALUES (%s,%s,%s,'matching_queued',%s)",
                (tenant, identifier("audit"), str(user), run_id),
            )
            return self._run(row)

        return self._mutate(tenant, user, "matching:create", request, MatchRun, action)

    def list_runs(self, tenant, *, cursor=None, limit=50):
        with self.transaction(tenant, maintenance=False) as (connection, row):
            return self._page(
                connection,
                row,
                "runs",
                MatchRun,
                columns=RUN_COLUMNS,
                mapper=self._run,
                cursor=cursor,
                limit=limit,
            )

    def run(self, tenant, run_id):
        with self.transaction(tenant, maintenance=False) as (connection, _):
            return self._run(self._one(connection, "runs", tenant, run_id))

    def cancel_run(self, tenant, user, run_id, request):
        def action(connection):
            row = self._one(connection, "runs", tenant, run_id)
            if row["status"] not in ("queued", "running"):
                raise StoreError(409, "run_terminal", "Only an active run can be cancelled.")
            connection.execute("SELECT staff_saas.cancel_dispatch(%s,%s)", (tenant, run_id))
            updated = connection.execute(
                f"UPDATE runs SET status='cancelled',error='Cancelled by coordinator' "
                f"WHERE tenant_id=%s AND id=%s RETURNING {RUN_COLUMNS}",
                (tenant, run_id),
            ).fetchone()
            connection.execute(
                "INSERT INTO audit_events(tenant_id,id,actor,event,entity_id) "
                "VALUES (%s,%s,%s,'matching_cancelled',%s)",
                (tenant, identifier("audit"), str(user), run_id),
            )
            return self._run(updated)

        return self._mutate(tenant, user, f"matching:cancel:{run_id}", request, MatchRun, action)

    def list_candidates(self, tenant, run_id, *, job_id="", cursor=None, limit=50):
        with self.transaction(tenant, maintenance=False) as (connection, row):
            run = self._one(connection, "runs", tenant, run_id)
            if run["status"] != "completed":
                raise StoreError(
                    409, "run_not_completed", "Candidates publish only after completion."
                )
            return self._page(
                connection,
                row,
                "candidates",
                Candidate,
                where="run_id=%s" + (" AND job_id=%s" if job_id else ""),
                args=(run_id, job_id) if job_id else (run_id,),
                scope={"run_id": run_id, "job_id": job_id},
                cursor=cursor,
                limit=limit,
                columns=(
                    "lpad(rank::text,10,'0') AS id,body,"
                    "(SELECT j.version FROM jobs j WHERE j.tenant_id=candidates.tenant_id "
                    "AND j.id=candidates.job_id) AS current_job_version,"
                    "(SELECT j.body->>'status' FROM jobs j WHERE j.tenant_id=candidates.tenant_id "
                    "AND j.id=candidates.job_id) AS job_status,"
                    "(SELECT p.version FROM people p WHERE p.tenant_id=candidates.tenant_id "
                    "AND p.id=candidates.person_id) AS current_person_version"
                ),
                key="lpad(rank::text,10,'0')",
                mapper=self._candidate,
            )

    @staticmethod
    def _candidate(row):
        candidate = Candidate.model_validate(row["body"])
        if (
            row["job_status"] != "open"
            or candidate.job_version != row["current_job_version"]
            or candidate.person_version != row["current_person_version"]
        ):
            candidate.eligible = False
            candidate.recommended = False
            candidate.reasons.append("stale_candidate: job/person changed; run matching again")
        return candidate

    @staticmethod
    def _private_person(connection, tenant, person_id):
        row = connection.execute(
            f"SELECT {PERSON_COLUMNS} FROM people WHERE tenant_id=%s AND id=%s",
            (tenant, person_id),
        ).fetchone()
        if row is None:
            raise StoreError(404, "not_found", "Person not found in this tenant.")
        return _person(row)

    def _eligible(self, connection, tenant, job_id, person_id, *, versions=None, capacity=False):
        from backend.saas_routing import estimate_commute

        job = Job.model_validate(self._one(connection, "jobs", tenant, job_id)["body"])
        person = self._private_person(connection, tenant, person_id)
        if versions and (job.version, person.version) != versions:
            raise StoreError(409, "stale_candidate", "Job or person changed; run matching again.")
        candidate = evaluate(person, job, estimate_commute(person, job))
        if not candidate.eligible:
            raise StoreError(
                409,
                "candidate_ineligible",
                "Hard constraints no longer hold: " + ", ".join(candidate.reasons),
            )
        placements = connection.execute(
            "SELECT person_id,job_snapshot FROM placements WHERE tenant_id=%s "
            "AND (person_id=%s OR job_id=%s)",
            (tenant, person_id, job_id),
        ).fetchall()
        if any(
            row["person_id"] == person_id
            and (
                row["job_snapshot"]["id"] == job_id
                or schedules_conflict(job, Job.model_validate(row["job_snapshot"]))
            )
            for row in placements
        ):
            raise StoreError(
                409, "placement_conflict", "Person already has overlapping employment."
            )
        if (
            capacity
            and sum(row["job_snapshot"]["id"] == job_id for row in placements) >= job.headcount
        ):
            raise StoreError(409, "job_full", "The job has no remaining headcount.")
        return job, person

    def shortlist(self, tenant, user, request: ShortlistRequest):
        def action(connection):
            run = self._one(connection, "runs", tenant, request.run_id)
            if run["status"] != "completed":
                raise StoreError(409, "run_not_completed", "A completed run is required.")
            row = connection.execute(
                "SELECT body FROM candidates WHERE tenant_id=%s AND run_id=%s "
                "AND job_id=%s AND person_id=%s",
                (tenant, request.run_id, request.job_id, request.person_id),
            ).fetchone()
            if row is None or not row["body"]["eligible"]:
                raise StoreError(
                    409,
                    "candidate_not_in_run",
                    "Choose an eligible retrieved candidate from this run.",
                )
            versions = (request.expected_job_version, request.expected_person_version)
            if (row["body"]["job_version"], row["body"]["person_version"]) != versions:
                raise StoreError(
                    409, "stale_candidate", "Candidate versions do not match this run."
                )
            job, _ = self._eligible(
                connection,
                tenant,
                request.job_id,
                request.person_id,
                versions=versions,
            )
            intro_id = identifier("intro")
            # Dispatch begins with the person's request, never employer candidate selection.
            state = "requested" if job.business_type == "dispatch" else "shortlisted"
            row = connection.execute(
                "INSERT INTO introductions(tenant_id,id,run_id,job_id,person_id,company_id,"
                "candidate_alias,status,job_version,person_version,expires_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    tenant,
                    intro_id,
                    request.run_id,
                    job.id,
                    request.person_id,
                    job.company_id,
                    "候補-" + secrets.token_hex(4),
                    state,
                    *versions,
                    datetime.now(UTC) + timedelta(days=7),
                ),
            ).fetchone()
            self._event(connection, tenant, user, "shortlisted", row)
            return self._intro(row)

        return self._mutate(tenant, user, "shortlist", request, Introduction, action)

    def list_introductions(self, tenant, *, cursor=None, limit=50):
        with self.transaction(tenant) as (connection, row):
            return self._page(
                connection,
                row,
                "introductions",
                Introduction,
                columns=INTRO_COLUMNS,
                mapper=self._intro,
                cursor=cursor,
                limit=limit,
            )

    def simulate(self, tenant, user, intro_id, request: SimulationRequest, allow_simulation=False):
        def action(connection):
            self._simulation(connection, tenant, user, allow_simulation)
            row = self._one(connection, "introductions", tenant, intro_id)
            self._version(row, request.expected_version)
            job = Job.model_validate(self._one(connection, "jobs", tenant, row["job_id"])["body"])
            state = next_simulated_state(row["status"], request.action, job.business_type)
            if state in ("requested", "consented"):
                self._eligible(
                    connection,
                    tenant,
                    row["job_id"],
                    row["person_id"],
                    versions=(row["job_version"], row["person_version"]),
                )
            row = connection.execute(
                "UPDATE introductions SET status=%s,version=version+1 "
                "WHERE tenant_id=%s AND id=%s RETURNING *",
                (state, tenant, intro_id),
            ).fetchone()
            if state in TERMINAL_INTRODUCTIONS:
                connection.execute(
                    "UPDATE appointments SET status='cancelled',version=version+1 "
                    "WHERE tenant_id=%s AND introduction_id=%s AND status!='cancelled'",
                    (tenant, intro_id),
                )
            self._event(connection, tenant, user, "simulated_" + request.action, row)
            return self._intro(row)

        return self._mutate(
            tenant,
            user,
            f"simulate:{intro_id}",
            request,
            Introduction,
            action,
            preflight=lambda connection: self._simulation(
                connection, tenant, user, allow_simulation
            ),
        )

    def cancel_introduction(self, tenant, user, intro_id, request: Versioned):
        def action(connection):
            row = self._one(connection, "introductions", tenant, intro_id)
            self._version(row, request.expected_version)
            if row["status"] in TERMINAL_INTRODUCTIONS:
                raise StoreError(409, "invalid_transition", "Introduction is already terminal.")
            row = connection.execute(
                "UPDATE introductions SET status='cancelled',version=version+1 "
                "WHERE tenant_id=%s AND id=%s RETURNING *",
                (tenant, intro_id),
            ).fetchone()
            connection.execute(
                "UPDATE appointments SET status='cancelled',version=version+1 "
                "WHERE tenant_id=%s AND introduction_id=%s AND status!='cancelled'",
                (tenant, intro_id),
            )
            self._event(connection, tenant, user, "cancelled", row)
            return self._intro(row)

        return self._mutate(tenant, user, f"cancel:{intro_id}", request, Introduction, action)

    def place(self, tenant, user, intro_id, request: Versioned):
        def action(connection):
            row = self._one(connection, "introductions", tenant, intro_id)
            self._version(row, request.expected_version)
            job, _ = self._eligible(
                connection,
                tenant,
                row["job_id"],
                row["person_id"],
                versions=(row["job_version"], row["person_version"]),
                capacity=True,
            )
            expected = "consented" if job.business_type == "dispatch" else "interview_accepted"
            if row["status"] != expected:
                raise StoreError(
                    409,
                    "consent_required",
                    "Complete the permitted consent/interview flow before placement.",
                )
            connection.execute(
                "INSERT INTO placements(tenant_id,introduction_id,job_id,person_id,job_snapshot) "
                "VALUES (%s,%s,%s,%s,%s)",
                (tenant, intro_id, job.id, row["person_id"], model_body(job)),
            )
            row = connection.execute(
                "UPDATE introductions SET status='placed',version=version+1 "
                "WHERE tenant_id=%s AND id=%s RETURNING *",
                (tenant, intro_id),
            ).fetchone()
            self._event(connection, tenant, user, "placed", row)
            return self._intro(row)

        return self._mutate(tenant, user, f"place:{intro_id}", request, Introduction, action)

    def list_appointments(self, tenant, *, cursor=None, limit=50):
        with self.transaction(tenant) as (connection, row):
            return self._page(
                connection,
                row,
                "appointments",
                Appointment,
                columns=APPOINTMENT_COLUMNS,
                mapper=self._appointment,
                cursor=cursor,
                limit=limit,
            )

    def _meeting_guard(self, connection, tenant, intro, request):
        job, _ = self._eligible(
            connection,
            tenant,
            intro["job_id"],
            intro["person_id"],
            versions=(intro["job_version"], intro["person_version"]),
        )
        if job.business_type != "placement":
            raise StoreError(
                409,
                "dispatch_interview_forbidden",
                "Ordinary dispatch does not permit employer selection interviews.",
            )
        if request.start_at <= datetime.now(UTC):
            raise StoreError(422, "appointment_in_past", "Propose a future interview.")
        if request.end_at > intro["expires_at"]:
            raise StoreError(
                409,
                "appointment_after_expiry",
                "The interview must end before this introduction expires.",
            )
        if request.site_id:
            site = self._one(connection, "sites", tenant, request.site_id)
            if site["company_id"] != intro["company_id"]:
                raise StoreError(
                    403,
                    "site_not_authorized",
                    "Interview site must belong to the recruiting company.",
                )
        start = request.start_at.astimezone(TOKYO).replace(tzinfo=None)
        end = request.end_at.astimezone(TOKYO).replace(tzinfo=None)
        for row in connection.execute(
            "SELECT job_snapshot FROM placements WHERE tenant_id=%s AND person_id=%s",
            (tenant, intro["person_id"]),
        ).fetchall():
            placed = Job.model_validate(row["job_snapshot"])
            for work_start, work_end in shift_intervals(
                max(placed.start_date, start.date() - timedelta(days=1)),
                min(placed.end_date, end.date() + timedelta(days=1)),
                placed.schedule,
            ):
                if start < work_end and work_start < end:
                    raise StoreError(
                        409,
                        "interview_work_conflict",
                        "Interview overlaps this person's existing employment.",
                    )

    def propose_appointment(self, tenant, user, intro_id, request: AppointmentRequest):
        def action(connection):
            intro = self._one(connection, "introductions", tenant, intro_id)
            self._version(intro, request.expected_version)
            self._meeting_guard(connection, tenant, intro, request)
            if intro["status"] != "consented":
                raise StoreError(409, "consent_required", "Current person consent is required.")
            row = connection.execute(
                "INSERT INTO appointments(tenant_id,id,introduction_id,job_id,person_id,company_id,"
                "start_at,end_at,site_id,online,status) VALUES "
                f"(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'proposed') RETURNING {APPOINTMENT_COLUMNS}",
                (
                    tenant,
                    identifier("appointment"),
                    intro_id,
                    intro["job_id"],
                    intro["person_id"],
                    intro["company_id"],
                    request.start_at,
                    request.end_at,
                    request.site_id,
                    request.online,
                ),
            ).fetchone()
            connection.execute(
                "UPDATE introductions SET status='interview_proposed',version=version+1 "
                "WHERE tenant_id=%s AND id=%s",
                (tenant, intro_id),
            )
            self._event(connection, tenant, user, "interview_proposed", intro)
            return self._appointment(row)

        return self._mutate(
            tenant, user, f"appointment:create:{intro_id}", request, Appointment, action
        )

    def change_appointment(
        self, tenant, user, appointment_id, request, action_name, allow_simulation=False
    ):
        def action(connection):
            appointment = self._one(connection, "appointments", tenant, appointment_id)
            self._version(appointment, request.expected_version)
            intro = self._one(connection, "introductions", tenant, appointment["introduction_id"])
            if appointment["status"] == "cancelled" or intro["status"] not in (
                "interview_proposed",
                "interview_accepted",
            ):
                raise StoreError(409, "invalid_transition", "This interview is no longer active.")
            if action_name == "accept":
                self._simulation(connection, tenant, user, allow_simulation)
                if appointment["status"] != "proposed":
                    raise StoreError(
                        409, "invalid_transition", "Only a proposed meeting can be accepted."
                    )
                self._meeting_guard(
                    connection,
                    tenant,
                    intro,
                    AppointmentRequest(
                        **request.model_dump(),
                        start_at=appointment["start_at"],
                        end_at=appointment["end_at"],
                        site_id=appointment["site_id"],
                        online=appointment["online"],
                    ),
                )
                state, intro_state = "accepted", "interview_accepted"
            elif action_name == "cancel":
                state, intro_state = "cancelled", "consented"
            elif action_name == "reschedule":
                self._meeting_guard(connection, tenant, intro, request)
                state, intro_state = "proposed", "interview_proposed"
                connection.execute(
                    "UPDATE appointments SET start_at=%s,end_at=%s,site_id=%s,online=%s "
                    "WHERE tenant_id=%s AND id=%s",
                    (
                        request.start_at,
                        request.end_at,
                        request.site_id,
                        request.online,
                        tenant,
                        appointment_id,
                    ),
                )
            else:
                raise StoreError(400, "unknown_action", "Unknown appointment action.")
            row = connection.execute(
                "UPDATE appointments SET status=%s,version=version+1 WHERE tenant_id=%s AND id=%s "
                f"RETURNING {APPOINTMENT_COLUMNS}",
                (state, tenant, appointment_id),
            ).fetchone()
            connection.execute(
                "UPDATE introductions SET status=%s,version=version+1 WHERE tenant_id=%s AND id=%s",
                (intro_state, tenant, intro["id"]),
            )
            event = (
                "simulated_interview_accept"
                if action_name == "accept"
                else ("interview_" + action_name)
            )
            self._event(connection, tenant, user, event, intro)
            return self._appointment(row)

        return self._mutate(
            tenant,
            user,
            f"appointment:{action_name}:{appointment_id}",
            request,
            Appointment,
            action,
            preflight=(
                lambda connection: self._simulation(
                    connection,
                    tenant,
                    user,
                    allow_simulation,
                )
            )
            if action_name == "accept"
            else None,
        )

    def list_notifications(
        self, tenant, user, *, audience="management", cursor=None, limit=50, allow_simulation=False
    ):
        with self.transaction(tenant) as (connection, row):
            if audience != "management":
                self._simulation(connection, tenant, user, allow_simulation)
            return self._page(
                connection,
                row,
                "notifications",
                Notification,
                where="audience=%s",
                args=(audience,),
                scope={"audience": audience},
                cursor=cursor,
                limit=limit,
                columns="id,body,read",
                mapper=lambda item: self._notification(connection, tenant, item),
            )

    @staticmethod
    def _notification(connection, tenant, row):
        notice = Notification.model_validate({**row["body"], "read": row["read"]})
        if notice.audience != "company" or notice.kind not in {
            "simulated_person_consent",
            "interview_proposed",
            "interview_reschedule",
            "simulated_interview_accept",
            "placed",
        }:
            return notice
        consent = connection.execute(
            "SELECT p.summary FROM introductions i JOIN people p "
            "ON p.tenant_id=i.tenant_id AND p.id=i.person_id "
            "WHERE i.tenant_id=%s AND i.id=%s AND i.company_id=%s "
            "AND i.status IN ('consented','interview_proposed','interview_accepted','placed')",
            (tenant, notice.introduction_id, notice.recipient_id),
        ).fetchone()
        if consent:
            summary = PersonSummary.model_validate(consent["summary"])
            # Resolve at read time, never persist the shared name in the outbox:
            # withdrawal/expiry immediately removes it from historical inbox reads.
            notice.message += (
                f"\n[SIMULATED] 同意（デモ）に基づく公開プロフィール: "
                f"{summary.name} / {summary.occupation} / {summary.home_area.label}"
            )
        return notice

    def read_notification(self, tenant, user, notice_id, request, allow_simulation=False):
        def authorize(connection):
            row = self._one(connection, "notifications", tenant, notice_id)
            if row["audience"] != "management":
                self._simulation(connection, tenant, user, allow_simulation)

        def action(connection):
            row = self._one(connection, "notifications", tenant, notice_id)
            if row["audience"] != "management":
                self._simulation(connection, tenant, user, allow_simulation)
            connection.execute(
                "UPDATE notifications SET read=true WHERE tenant_id=%s AND id=%s",
                (tenant, notice_id),
            )
            return Notification.model_validate({**row["body"], "read": True})

        return self._mutate(
            tenant,
            user,
            f"notification:read:{notice_id}",
            request,
            Notification,
            action,
            preflight=authorize,
        )
