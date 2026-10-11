"""Durable worker: ``python -m backend.saas_worker`` using staff_worker PG* only.

SAAS_WORKER_POLL_SECONDS defaults to 2; --once performs one matching/outbox pass.
Runs renew a 90-second lease every 20 seconds, retry at most three attempts, and
publish under a transactional token/attempt fence. Restart reclaims expired work;
partial candidate rows never become visible. No identity/admin secret is read.
"""

import argparse
import logging
import os
import signal
import threading
import time
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from backend.saas_domain import evaluate, hard_rejections, schedules_conflict
from backend.saas_engine import Solution, run_matching
from backend.saas_models import Coverage, Job, MatchRequest, Notification
from backend.saas_store import (
    PERSON_COLUMNS,
    SaasStore,
    _person,
    encoded_vector,
    model_body,
    payload_hash,
    tenant_context,
)
from backend.store import StoreError

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 3


class LeaseLost(Exception):
    pass


class SaasWorker:
    def __init__(self, conninfo: str = ""):
        self.store = SaasStore(conninfo)

    def initialize(self):
        from backend.saas_routing import load_network

        self.store.initialize()
        with self.store.connect() as connection:
            role = connection.execute("SELECT current_user AS name").fetchone()["name"]
            if role != "staff_worker":
                raise RuntimeError("The durable worker must use the separate staff_worker role.")
            permissions = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_namespace n "
                "WHERE n.nspname='staff_identity' "
                "AND has_schema_privilege(current_user,n.oid,'USAGE')) AS identity_access, "
                "has_table_privilege(current_user,'staff_saas.dispatch','SELECT') AS raw_dispatch"
            ).fetchone()
            if permissions["identity_access"] or permissions["raw_dispatch"]:
                raise RuntimeError("Worker must not have identity or raw dispatch access.")
        load_network()

    def claim(self):
        token = uuid4().hex
        with self.store.connect() as connection:
            row = connection.execute("SELECT * FROM staff_saas.claim(%s)", (token,)).fetchone()
        return {**row, "token": token} if row else None

    @staticmethod
    def _fence(connection, claim, function="lock_lease"):
        if function not in ("lock_lease", "heartbeat"):
            raise ValueError("Unknown lease function")
        ok = connection.execute(
            f"SELECT staff_saas.{function}(%s,%s,%s,%s) AS ok",
            (claim["tenant_id"], claim["run_id"], claim["token"], claim["attempt"]),
        ).fetchone()["ok"]
        if not ok:
            raise LeaseLost

    def heartbeat(self, claim, *, progress=None, coverage=None):
        with self.store.connect() as connection:
            tenant_context(connection, claim["tenant_id"])
            self._fence(connection, claim, "heartbeat")
            if progress is not None:
                connection.execute(
                    "UPDATE runs SET progress=%s,coverage=%s "
                    "WHERE tenant_id=%s AND id=%s AND status='running' AND attempt=%s",
                    (
                        progress,
                        model_body(coverage),
                        claim["tenant_id"],
                        claim["run_id"],
                        claim["attempt"],
                    ),
                )

    def _snapshot(self, claim):
        with self.store.transaction(claim["tenant_id"], maintenance=False) as (connection, tenant):
            self._fence(connection, claim)
            row = connection.execute(
                "UPDATE runs SET status='running',attempt=%s,progress=1,error=NULL "
                "WHERE tenant_id=%s AND id=%s AND status IN ('queued','running') RETURNING request",
                (claim["attempt"], claim["tenant_id"], claim["run_id"]),
            ).fetchone()
            if row is None:
                raise LeaseLost
            request = MatchRequest.model_validate(row["request"])
            jobs = [
                Job.model_validate(item["body"])
                for item in connection.execute(
                    "SELECT body FROM jobs WHERE tenant_id=%s AND id=ANY(%s) ORDER BY id",
                    (claim["tenant_id"], request.job_ids),
                ).fetchall()
            ]
            if len(jobs) != len(request.job_ids) or any(job.status != "open" for job in jobs):
                raise StoreError(409, "job_changed", "A requested job is now missing or closed.")
            placements = [
                (item["person_id"], Job.model_validate(item["job_snapshot"]))
                for item in connection.execute(
                    "SELECT person_id,job_snapshot FROM placements WHERE tenant_id=%s",
                    (claim["tenant_id"],),
                ).fetchall()
            ]
            people = connection.execute(
                "SELECT count(*) AS n FROM people WHERE tenant_id=%s",
                (claim["tenant_id"],),
            ).fetchone()["n"]
            if people > 2000:
                raise StoreError(
                    422, "people_budget", "This worker supports up to 2,000 people/tenant."
                )
            return (
                request,
                jobs,
                placements,
                people,
                self._input_stamp(connection, claim["tenant_id"]),
                {"dataset": tenant["dataset_version"], "network": tenant["network_version"]},
            )

    @staticmethod
    def _input_stamp(connection, tenant):
        state = {}
        for table in ("jobs", "people"):
            state[table] = connection.execute(
                f"SELECT id,version FROM {table} WHERE tenant_id=%s ORDER BY id",
                (tenant,),
            ).fetchall()
        state["placements"] = connection.execute(
            "SELECT introduction_id FROM placements WHERE tenant_id=%s ORDER BY introduction_id",
            (tenant,),
        ).fetchall()
        return payload_hash(state)

    def retrieve(self, tenant, job, limit):
        # Exact structural filters precede graph routing. Geographic distance is
        # only a tie-breaker, never a fabricated commute or exclusion radius.
        with self.store.transaction(tenant, maintenance=False) as (connection, _):
            rows = connection.execute(
                f"SELECT {PERSON_COLUMNS} FROM people "
                "WHERE tenant_id=%s AND (summary->>'matching_enabled')::boolean "
                "AND (summary->>'desired_hourly_pay')::integer<=%s "
                "AND (summary->>'available_from')::date<=%s "
                "AND (summary->>'available_until')::date>=%s "
                "AND NOT EXISTS(SELECT 1 FROM jsonb_each_text(%s::jsonb) required "
                "WHERE coalesce((summary->'skills'->>required.key)::integer,0)"
                "<required.value::integer) "
                "AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements_text(%s::jsonb) required(name) "
                "WHERE NOT EXISTS(SELECT 1 FROM jsonb_array_elements(summary->'qualifications') c "
                "WHERE c->>'name'=required.name AND (c->>'verified')::boolean "
                "AND (c->>'valid_until' IS NULL OR (c->>'valid_until')::date>=%s::date-1))) "
                "ORDER BY features <-> %s::public.vector, "
                "public.ST_Distance(private_location,public.ST_SetSRID("
                "public.ST_MakePoint(%s,%s),4326)::public.geography),id LIMIT 2000",
                (
                    tenant,
                    job.hourly_pay_max,
                    job.start_date,
                    job.end_date,
                    Jsonb(job.required_skills),
                    Jsonb(job.required_qualifications),
                    job.end_date,
                    encoded_vector(job.required_skills),
                    job.site.point.lon,
                    job.site.point.lat,
                ),
            )
            result = []
            for row in rows:
                person = _person(row)
                if not hard_rejections(person, job):
                    result.append(person)
                    if len(result) >= limit:
                        break
            return result

    def _get_person(self, tenant, person_id):
        with self.store.transaction(tenant, maintenance=False) as (connection, _):
            return self.store._private_person(connection, tenant, person_id)

    def publish(self, claim, solution: Solution, input_stamp=None):
        with self.store.transaction(
            claim["tenant_id"],
            mutate=True,
            maintenance=False,
        ) as (connection, _):
            self._fence(connection, claim)
            row = connection.execute(
                "SELECT status,attempt FROM runs WHERE tenant_id=%s AND id=%s FOR UPDATE",
                (claim["tenant_id"], claim["run_id"]),
            ).fetchone()
            if row is None or row["status"] != "running" or row["attempt"] != claim["attempt"]:
                raise LeaseLost
            if (
                input_stamp is not None
                and self._input_stamp(
                    connection,
                    claim["tenant_id"],
                )
                != input_stamp
            ):
                raise StoreError(
                    409, "inputs_changed", "Matching inputs changed during calculation."
                )
            connection.execute(
                "DELETE FROM candidates WHERE tenant_id=%s AND run_id=%s",
                (claim["tenant_id"], claim["run_id"]),
            )
            ranked = sorted(
                solution.candidates,
                key=lambda c: (
                    c.job_id,
                    not c.eligible,
                    -(c.score or 0),
                    c.person_id,
                ),
            )
            with connection.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO candidates(tenant_id,run_id,job_id,person_id,rank,body) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    [
                        (
                            claim["tenant_id"],
                            claim["run_id"],
                            c.job_id,
                            c.person_id,
                            rank,
                            model_body(c),
                        )
                        for rank, c in enumerate(ranked, 1)
                    ],
                )
            connection.execute(
                "UPDATE runs SET status='completed',progress=100,coverage=%s,"
                "recommended_count=%s,solver_status=%s,objective_bound=%s,error=NULL "
                "WHERE tenant_id=%s AND id=%s",
                (
                    model_body(solution.coverage),
                    solution.recommended_count,
                    solution.solver_status,
                    solution.objective_bound,
                    claim["tenant_id"],
                    claim["run_id"],
                ),
            )
            self._finish(connection, claim, False)

    @staticmethod
    def _finish(connection, claim, retry):
        ok = connection.execute(
            "SELECT staff_saas.finish(%s,%s,%s,%s,%s) AS ok",
            (claim["tenant_id"], claim["run_id"], claim["token"], claim["attempt"], retry),
        ).fetchone()["ok"]
        if not ok:
            raise LeaseLost

    def fail(self, claim, code, *, retry=False):
        retry = retry and claim["attempt"] < MAX_ATTEMPTS
        with self.store.transaction(
            claim["tenant_id"],
            mutate=True,
            maintenance=False,
        ) as (connection, _):
            self._fence(connection, claim)
            connection.execute(
                "UPDATE runs SET status=%s,error=%s,attempt=%s WHERE tenant_id=%s AND id=%s",
                (
                    "queued" if retry else "failed",
                    code,
                    claim["attempt"],
                    claim["tenant_id"],
                    claim["run_id"],
                ),
            )
            self._finish(connection, claim, retry)

    def process(self, claim):
        if claim["attempt"] > MAX_ATTEMPTS:
            self.fail(claim, "retry_limit_exceeded")
            return
        stop, lost = threading.Event(), threading.Event()

        def renew():
            while not stop.wait(20):
                try:
                    self.heartbeat(claim)
                except (LeaseLost, psycopg.Error, StoreError):
                    lost.set()
                    return

        heartbeat_thread = threading.Thread(target=renew, name="saas-lease", daemon=True)
        heartbeat_thread.start()
        last_progress = 0.0
        try:
            request, jobs, placements, people_count, input_stamp, versions = self._snapshot(claim)
            from backend.saas_routing import estimate_commute

            def progress(value: int, coverage: Coverage):
                nonlocal last_progress
                if lost.is_set():
                    raise LeaseLost
                if time.monotonic() - last_progress >= 1:
                    self.heartbeat(claim, progress=value, coverage=coverage)
                    last_progress = time.monotonic()

            def evaluate_pair(person, job):
                if lost.is_set():
                    raise LeaseLost
                candidate = evaluate(person, job, estimate_commute(person, job))
                if any(
                    person_id == person.id
                    and (existing.id == job.id or schedules_conflict(job, existing))
                    for person_id, existing in placements
                ):
                    candidate.eligible = False
                    candidate.reasons.append("existing_placement_conflict")
                return candidate

            solution = run_matching(
                jobs,
                people_count,
                lambda job, limit: self.retrieve(claim["tenant_id"], job, limit),
                evaluate_pair,
                placements,
                request.pins,
                lambda person_id: self._get_person(claim["tenant_id"], person_id),
                progress,
            )
            solution.coverage.notes.append(
                f"Input dataset={versions['dataset']}; manifest network={versions['network']}."
            )
            if lost.is_set():
                raise LeaseLost
            self.publish(claim, solution, input_stamp)
            logger.info(
                "run_completed attempt=%s candidates=%s",
                claim["attempt"],
                solution.coverage.evaluated_pairs,
            )
        except LeaseLost:
            logger.info("run_lease_lost attempt=%s", claim["attempt"])
        except StoreError as error:
            try:
                self.fail(claim, error.code, retry=error.status == 503)
            except LeaseLost:
                logger.info("failure_fenced")
        except (psycopg.Error, ValueError, RuntimeError) as error:
            # Persist a bounded, non-profile error class; never success-shaped fallback.
            logger.error("run_failed kind=%s", type(error).__name__)
            try:
                self.fail(claim, type(error).__name__, retry=isinstance(error, psycopg.Error))
            except LeaseLost:
                logger.info("failure_fenced")
        finally:
            stop.set()
            heartbeat_thread.join(timeout=12)

    def deliver_outbox(self) -> int:
        with self.store.connect() as connection:
            tenant = connection.execute("SELECT staff_saas.next_outbox() AS tenant").fetchone()[
                "tenant"
            ]
        if not tenant:
            return 0
        with self.store.transaction(tenant, maintenance=False) as (connection, _):
            rows = connection.execute(
                "SELECT id,body FROM outbox WHERE tenant_id=%s AND delivered_at IS NULL "
                "AND attempts<3 ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 100",
                (tenant,),
            ).fetchall()
            for row in rows:
                try:
                    notice = Notification.model_validate(row["body"])
                except ValueError:
                    connection.execute(
                        "UPDATE outbox SET attempts=3,error='invalid_notification' "
                        "WHERE tenant_id=%s AND id=%s",
                        (tenant, row["id"]),
                    )
                    continue
                if notice.id != row["id"]:
                    connection.execute(
                        "UPDATE outbox SET attempts=3,error='invalid_notification_id' "
                        "WHERE tenant_id=%s AND id=%s",
                        (tenant, row["id"]),
                    )
                    continue
                try:
                    with connection.transaction():
                        connection.execute(
                            "INSERT INTO notifications(tenant_id,id,audience,recipient_id,"
                            "introduction_id,body,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                            "ON CONFLICT DO NOTHING",
                            (
                                tenant,
                                notice.id,
                                notice.audience,
                                notice.recipient_id,
                                notice.introduction_id,
                                model_body(notice),
                                notice.created_at,
                            ),
                        )
                        connection.execute(
                            "UPDATE outbox SET delivered_at=%s,attempts=attempts+1 "
                            "WHERE tenant_id=%s AND id=%s",
                            (datetime.now(UTC), tenant, notice.id),
                        )
                except psycopg.IntegrityError as error:
                    connection.execute(
                        "UPDATE outbox SET attempts=attempts+1,error=%s "
                        "WHERE tenant_id=%s AND id=%s",
                        (type(error).__name__, tenant, row["id"]),
                    )
            return len(rows)

    def tick(self) -> bool:
        delivered = self.deliver_outbox()
        claim = self.claim()
        if claim:
            self.process(claim)
        return bool(delivered or claim)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    worker = SaasWorker()
    worker.initialize()
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda _signum, _frame: stop.set())
    interval = max(0.2, min(30, float(os.environ.get("SAAS_WORKER_POLL_SECONDS", "2"))))

    def notifications():
        while not stop.is_set():
            try:
                delivered = worker.deliver_outbox()
            except (psycopg.Error, StoreError) as error:
                logger.error("outbox_database_error kind=%s", type(error).__name__)
                delivered = 0
            if not delivered:
                stop.wait(interval)

    outbox_thread = None
    if not args.once:
        outbox_thread = threading.Thread(target=notifications, name="saas-outbox", daemon=True)
        outbox_thread.start()
    while not stop.is_set():
        try:
            worked = worker.tick()
        except (psycopg.Error, StoreError) as error:
            logger.error("worker_database_error kind=%s", type(error).__name__)
            if args.once:
                raise
            worked = False
        if args.once:
            break
        if not worked:
            stop.wait(interval)
    stop.set()
    if outbox_thread:
        outbox_thread.join(timeout=12)


if __name__ == "__main__":
    main()
