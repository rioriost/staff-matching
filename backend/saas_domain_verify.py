"""Executable private-PostgreSQL domain acceptance; no pytest/runtime dependency.

Call after migration, before starting worker replicas, with an idle administrator
connection and an empty active matching queue. The function commits its own
uniquely named fixture tenants, exercises independent restricted connections,
then removes only those fixtures. It never reads or modifies identity/legacy data.
Geographic correctness and HTTP authorization are separate acceptance checks.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

from backend.saas_domain import evaluate
from backend.saas_engine import Solution
from backend.saas_models import (
    AppointmentRequest,
    Area,
    Commute,
    Company,
    Coverage,
    Dataset,
    Job,
    MatchRequest,
    PersonRecord,
    Point,
    Schedule,
    ShortlistRequest,
    SimulationRequest,
    Site,
    Versioned,
)
from backend.saas_store import (
    SaasStore,
    grant_demo_simulation,
    load_dataset,
    tenant_context,
)
from backend.saas_worker import LeaseLost, SaasWorker
from backend.store import StoreError


def _require(condition, message):
    if not condition:
        raise RuntimeError("SaaS domain verification failed: " + message)


def _expect_error(callback, error_type, code=None):
    try:
        callback()
    except error_type as error:
        if code is not None:
            _require(getattr(error, "code", None) == code, f"expected error code {code}")
    else:
        raise RuntimeError(f"SaaS domain verification failed: expected {error_type.__name__}")


def _fixture(tenant: str) -> Dataset:
    """One controlled vacancy, not a substitute for the versioned SaaS generator."""
    point = Point(lat=35.68, lon=139.76)
    area = Area(id="acceptance-area", label="Synthetic acceptance area", point=point)
    company = Company(
        id="company",
        name="Synthetic acceptance company",
        industry="office",
        description="Disposable domain verification fixture",
        contact_email="test@example.invalid",
    )
    site = Site(
        id="site",
        company_id=company.id,
        name="Synthetic site",
        kind="worksite",
        address="Synthetic worksite",
        area=area,
        point=point,
        stations=[],
        parking=True,
        accessibility="Synthetic accessible entrance",
    )
    start = date.today() + timedelta(days=30)
    schedule = Schedule(weekdays=[0, 1, 2, 3, 4], start_minute=540, end_minute=1020)
    job = Job(
        id=tenant + "-job",
        company_id=company.id,
        company_name=company.name,
        site_id=site.id,
        site=site,
        title="Synthetic office job",
        occupation="office",
        duties="Synthetic records checking",
        business_type="placement",
        employment_type="fixed",
        headcount=1,
        start_date=start,
        end_date=start + timedelta(days=30),
        schedule=schedule,
        hourly_pay_min=1600,
        hourly_pay_max=1800,
        required_skills={"office": 2},
        required_qualifications=[],
        preferred_qualifications=[],
        experience_months=0,
        renewal="none",
        duties_change_scope="none",
        location_change_scope="none",
        overtime="none",
        holidays="weekends",
        allowances="synthetic",
        benefits=[],
        application="verification only",
    )
    person = PersonRecord(
        id="person-a",
        name="Synthetic acceptance person",
        occupation="office",
        home_area=area,
        stations=[],
        skills={"office": 3},
        qualifications=[],
        available_from=start,
        available_until=start + timedelta(days=90),
        availability=schedule,
        desired_hourly_pay=1600,
        preferred_occupations=["office"],
        commute_modes=["transit"],
        driving_eligible=False,
        max_commute_minutes=60,
        employment_history=[],
        private_location=Point(lat=35.68123, lon=139.76234),
        private_address="Never expose this synthetic private address",
        contact_email="private-test@example.invalid",
    )
    return Dataset(
        tenant_id=tenant,
        version="domain-acceptance-v1",
        companies=[company],
        sites=[site],
        jobs=[job],
        people=[person, person.model_copy(update={"id": "person-b"})],
    )


def _cleanup(admin_connection, tenants):
    with admin_connection.transaction():
        for tenant in tenants:
            tenant_context(admin_connection, tenant)
            for table in (
                "notifications",
                "outbox",
                "audit_events",
                "idempotency",
                "appointments",
                "placements",
                "introductions",
                "candidates",
                "runs",
                "simulation_grants",
                "jobs",
                "people",
                "sites",
                "companies",
                "dataset_manifests",
                "tenants",
                "dispatch",
                "outbox_dispatch",
            ):
                admin_connection.execute(
                    f"DELETE FROM staff_saas.{table} WHERE tenant_id=%s",
                    (tenant,),
                )


def _concurrent(actions):
    barrier = Barrier(len(actions))

    def invoke(action):
        barrier.wait(timeout=10)
        try:
            return action()
        except StoreError as error:
            return error

    with ThreadPoolExecutor(max_workers=len(actions)) as pool:
        return list(pool.map(invoke, actions))


def workflow_dataset(tenant):
    """Explicit positive fixtures, independent of the demo population's random distribution."""
    from backend.saas_data import generate_tenant
    from backend.saas_models import Credential, Schedule

    dataset = generate_tenant(tenant, count=30)
    for index, business in ((0, "placement"), (5, "dispatch")):
        job, person = dataset.jobs[index], dataset.people[index]
        job.business_type = business
        job.employment_type = "有期契約社員" if business == "placement" else "有期雇用派遣"
        job.status = "open"
        job.experience_months = 0
        job.schedule = Schedule(weekdays=[0, 1, 2, 3, 4], start_minute=540, end_minute=1080)
        person.occupation = job.occupation
        person.skills = {skill: 5 for skill in job.required_skills}
        person.qualifications = [
            Credential(name=name, verified=True, valid_until=None)
            for name in dict.fromkeys((*job.required_qualifications, *job.preferred_qualifications))
        ]
        person.employment_history = []
        person.private_location = job.site.point.model_copy()
        person.home_area = job.site.area.model_copy(deep=True)
        person.stations = [station.model_copy() for station in job.site.stations]
        person.available_from, person.available_until = job.start_date, job.end_date
        person.availability = job.schedule.model_copy(deep=True)
        person.desired_hourly_pay = job.hourly_pay_min
        person.preferred_occupations = [job.occupation]
        person.commute_modes = ["transit"]
        person.max_commute_minutes = 180
        person.matching_enabled = True
    dataset.version += "-workflow-fixture"
    return dataset


def _workflow_fixture(tenant):
    from backend.saas_routing import estimate_commute

    dataset = workflow_dataset(tenant)
    placement = next(job for job in dataset.jobs if job.id.endswith("0001"))
    dispatch = next(job for job in dataset.jobs if job.id.endswith("0006"))
    person = next(person for person in dataset.people if person.id.endswith("0001"))
    dispatch_person = next(person for person in dataset.people if person.id.endswith("0006"))
    _require(placement.business_type == "placement", "generated placement fixture")
    _require(dispatch.business_type == "dispatch", "generated dispatch fixture")
    placement.headcount = 1
    alternative = person.model_copy(
        update={
            "id": person.id + "-capacity",
            "name": "Synthetic capacity verification person",
            "contact_email": "capacity-fixture@example.invalid",
        }
    )
    secondary = placement.model_copy(update={"id": placement.id + "-overlap"})
    dataset.people.append(alternative)
    dataset.jobs.append(secondary)
    dataset.version += "-domain-acceptance"
    for job, candidate in (
        (placement, person),
        (placement, alternative),
        (secondary, person),
        (secondary, alternative),
        (dispatch, dispatch_person),
    ):
        _require(
            evaluate(candidate, job, estimate_commute(candidate, job)).eligible,
            "generated workflow fixture must pass real hard constraints and routing",
        )
    return dataset, placement, secondary, dispatch, person, alternative, dispatch_person


def _workflow_checks(admin_connection, store, worker, tenant):
    from backend.saas_routing import estimate_commute

    dataset, job, secondary, dispatch, person, alternative, dispatch_person = _workflow_fixture(
        tenant
    )
    actor = "verifier"
    with admin_connection.transaction():
        counts = load_dataset(admin_connection, dataset)
        _require(counts["jobs"] == 31 and counts["people"] == 31, "generated workflow counts")
        grant_demo_simulation(admin_connection, tenant, actor)
    run = store.create_run(
        tenant,
        actor,
        MatchRequest(
            idempotency_key=uuid4().hex,
            job_ids=[job.id, secondary.id, dispatch.id],
        ),
    )
    claim = worker.claim()
    _require(
        claim is not None and claim["tenant_id"] == tenant and claim["run_id"] == run.id,
        "workflow worker must claim its disposable run",
    )
    worker.process(claim)
    completed = store.run(tenant, run.id)
    _require(
        completed.status == "completed",
        f"real route/solver workflow run must complete ({completed.error})",
    )
    _require(
        completed.solver_status in {"OPTIMAL", "FEASIBLE"},
        "real workflow solver must produce a feasible result",
    )

    def current_intro(intro_id):
        return next(
            item
            for item in store.list_introductions(tenant, limit=100).items
            if item.id == intro_id
        )

    def current_appointment(appointment_id):
        return next(
            item
            for item in store.list_appointments(tenant, limit=100).items
            if item.id == appointment_id
        )

    def shortlist(current_job, candidate):
        options = store.list_candidates(tenant, run.id, job_id=current_job.id, limit=100).items
        _require(
            any(item.person_id == candidate.id and item.eligible for item in options),
            "manual workflow choice must belong to the real eligible candidate graph",
        )
        return store.shortlist(
            tenant,
            actor,
            ShortlistRequest(
                run_id=run.id,
                job_id=current_job.id,
                person_id=candidate.id,
                expected_job_version=current_job.version,
                expected_person_version=candidate.version,
                idempotency_key=uuid4().hex,
            ),
        )

    def simulate(intro, action):
        return store.simulate(
            tenant,
            actor,
            intro.id,
            SimulationRequest(
                expected_version=intro.version,
                idempotency_key=uuid4().hex,
                action=action,
                simulated=True,
            ),
            allow_simulation=True,
        )

    def consented(current_job, candidate):
        intro = shortlist(current_job, candidate)
        if current_job.business_type == "placement":
            intro = simulate(intro, "company_request")
        return simulate(intro, "person_consent")

    def proposal(intro, start, *, online=False):
        return AppointmentRequest(
            expected_version=intro.version,
            idempotency_key=uuid4().hex,
            start_at=start,
            end_at=start + timedelta(hours=1),
            site_id=None if online else job.site_id,
            online=online,
        )

    def accept(appointment):
        return store.change_appointment(
            tenant,
            actor,
            appointment.id,
            Versioned(
                expected_version=appointment.version,
                idempotency_key=uuid4().hex,
            ),
            "accept",
            allow_simulation=True,
        )

    checks = ["generated_fixture_real_routing", "real_worker_solver"]
    intros = [consented(job, candidate) for candidate in (person, alternative)]
    _require(
        intros[0].person_id != intros[1].person_id and job.headcount == 1,
        "capacity test requires two distinct eligible people and one vacancy",
    )
    start = (datetime.now(UTC) + timedelta(days=1)).replace(microsecond=0)
    requests = [proposal(intro, start) for intro in intros]
    reservations = _concurrent(
        [
            lambda index=index: store.propose_appointment(
                tenant,
                actor,
                intros[index].id,
                requests[index],
            )
            for index in range(2)
        ]
    )
    successes = [
        index for index, value in enumerate(reservations) if not isinstance(value, StoreError)
    ]
    failures = [value for value in reservations if isinstance(value, StoreError)]
    _require(
        len(successes) == 1 and len(failures) == 1 and failures[0].code == "concurrent_conflict",
        "simultaneous overlapping site reservations must have exactly one winner",
    )
    winner_index = successes[0]
    loser_index = 1 - winner_index
    winner = reservations[winner_index]
    _require(
        current_intro(intros[loser_index].id).status == "consented",
        "failed reservation must not advance introduction state",
    )
    checks.append("concurrent_appointment_site_overlap")

    winner_person = person if winner_index == 0 else alternative
    loser_person = alternative if winner_index == 0 else person
    withdrawal = consented(secondary, winner_person)
    _expect_error(
        lambda: store.propose_appointment(
            tenant,
            actor,
            withdrawal.id,
            proposal(withdrawal, start, online=True),
        ),
        StoreError,
        "concurrent_conflict",
    )
    _require(
        current_intro(withdrawal.id).status == "consented",
        "attendee conflict must roll back all state changes",
    )
    checks.append("appointment_attendee_overlap")
    loser_appointment = store.propose_appointment(
        tenant,
        actor,
        intros[loser_index].id,
        proposal(intros[loser_index], start + timedelta(hours=2)),
    )
    moved = store.change_appointment(
        tenant,
        actor,
        winner.id,
        AppointmentRequest(
            expected_version=winner.version,
            idempotency_key=uuid4().hex,
            start_at=start + timedelta(hours=4),
            end_at=start + timedelta(hours=5),
            site_id=job.site_id,
        ),
        "reschedule",
    )
    _expect_error(
        lambda: store.change_appointment(
            tenant,
            actor,
            winner.id,
            Versioned(
                expected_version=winner.version,
                idempotency_key=uuid4().hex,
            ),
            "accept",
            allow_simulation=True,
        ),
        StoreError,
        "stale_version",
    )
    accept(moved)
    accept(loser_appointment)
    refreshed = current_intro(intros[winner_index].id)
    _expect_error(
        lambda: store.place(
            tenant,
            actor,
            refreshed.id,
            Versioned(
                expected_version=refreshed.version - 1,
                idempotency_key=uuid4().hex,
            ),
        ),
        StoreError,
        "stale_version",
    )
    checks.extend(
        ["appointment_reschedule", "stale_appointment_version", "stale_introduction_version"]
    )

    withdrawal_appointment = store.propose_appointment(
        tenant,
        actor,
        withdrawal.id,
        proposal(withdrawal, start + timedelta(hours=6), online=True),
    )
    accept(withdrawal_appointment)
    withdrawn = simulate(current_intro(withdrawal.id), "withdraw_consent")
    _require(withdrawn.status == "cancelled", "withdrawn consent must cancel the introduction")
    _require(
        current_appointment(withdrawal_appointment.id).status == "cancelled",
        "withdrawn consent must cancel its accepted interview",
    )
    _expect_error(
        lambda: store.place(
            tenant,
            actor,
            withdrawn.id,
            Versioned(
                expected_version=withdrawn.version,
                idempotency_key=uuid4().hex,
            ),
        ),
        StoreError,
        "consent_required",
    )
    checks.append("consent_withdrawal_blocks_placement_and_cancels_interview")

    expiring = consented(secondary, loser_person)
    expiring_appointment = store.propose_appointment(
        tenant,
        actor,
        expiring.id,
        proposal(expiring, start + timedelta(hours=6), online=True),
    )
    with admin_connection.transaction():
        tenant_context(admin_connection, tenant)
        admin_connection.execute(
            "UPDATE staff_saas.introductions SET expires_at=clock_timestamp()-interval '1 second' "
            "WHERE tenant_id=%s AND id=%s",
            (tenant, expiring.id),
        )
    expired = current_intro(expiring.id)
    _require(
        expired.status == "expired", "expired introduction must materialize its terminal state"
    )
    _require(
        current_appointment(expiring_appointment.id).status == "cancelled",
        "expiry must cancel a reserved interview",
    )
    _expect_error(lambda: simulate(expired, "person_consent"), StoreError, "invalid_transition")
    checks.append("expiry_cancels_interview_and_rejects_transition")

    dispatched = shortlist(dispatch, dispatch_person)
    _require(dispatched.status == "requested", "dispatch must skip employer candidate selection")
    _expect_error(
        lambda: simulate(dispatched, "company_request"), StoreError, "dispatch_selection_forbidden"
    )
    dispatched = simulate(dispatched, "person_consent")
    _expect_error(
        lambda: store.propose_appointment(
            tenant,
            actor,
            dispatched.id,
            proposal(dispatched, start + timedelta(hours=8), online=True),
        ),
        StoreError,
        "dispatch_interview_forbidden",
    )
    checks.extend(["dispatch_company_selection_rejected", "dispatch_interview_rejected"])

    introductions = [current_intro(intro.id) for intro in intros]
    for candidate in (person, alternative):
        _require(
            evaluate(candidate, job, estimate_commute(candidate, job)).eligible,
            "both capacity contenders must still pass real route/hard constraints",
        )
    placement_requests = [
        Versioned(
            expected_version=intro.version,
            idempotency_key=uuid4().hex,
        )
        for intro in introductions
    ]
    placements = _concurrent(
        [
            lambda index=index: store.place(
                tenant,
                actor,
                introductions[index].id,
                placement_requests[index],
            )
            for index in range(2)
        ]
    )
    placed_indices = [
        index for index, value in enumerate(placements) if not isinstance(value, StoreError)
    ]
    rejected = [value for value in placements if isinstance(value, StoreError)]
    _require(
        len(placed_indices) == 1 and len(rejected) == 1 and rejected[0].code == "job_full",
        "concurrent headcount=1 placement must have one winner and one job_full rejection",
    )
    index = placed_indices[0]
    placed = placements[index]
    _require(placed.status == "placed", "placement race winner must be durably placed")
    replay = _concurrent(
        [
            lambda: store.place(
                tenant,
                actor,
                introductions[index].id,
                placement_requests[index],
            )
            for _ in range(2)
        ]
    )
    _require(
        all(
            not isinstance(item, StoreError) and item.model_dump() == placed.model_dump()
            for item in replay
        ),
        "simultaneous idempotent placement replays must return the original response",
    )
    with admin_connection.transaction(), admin_connection.cursor(row_factory=dict_row) as cursor:
        tenant_context(admin_connection, tenant)
        count = cursor.execute(
            "SELECT count(*) AS n FROM staff_saas.placements WHERE tenant_id=%s AND job_id=%s",
            (tenant, job.id),
        ).fetchone()["n"]
        audits = cursor.execute(
            "SELECT count(*) AS n FROM staff_saas.audit_events "
            "WHERE tenant_id=%s AND entity_id=%s AND event='placed'",
            (tenant, placed.id),
        ).fetchone()["n"]
        notices = cursor.execute(
            "SELECT count(*) AS n FROM staff_saas.outbox WHERE tenant_id=%s "
            "AND body->>'introduction_id'=%s AND body->>'kind'='placed'",
            (tenant, placed.id),
        ).fetchone()["n"]
    _require(
        count == 1 and audits == 1 and notices == 3,
        "placement/replays must produce one slot, one audit, and one notice per audience",
    )
    checks.extend(
        [
            "concurrent_placement_headcount",
            "simultaneous_placement_idempotent_replay",
            "placement_audit_outbox_atomicity",
        ]
    )

    with admin_connection.transaction():
        tenant_context(admin_connection, tenant)
        admin_connection.execute(
            "UPDATE staff_saas.people SET version=version+1,"
            "summary=jsonb_set(summary,'{version}',to_jsonb(version+1)) "
            "WHERE tenant_id=%s AND id=%s",
            (tenant, dispatch_person.id),
        )
    _expect_error(
        lambda: store.place(
            tenant,
            actor,
            dispatched.id,
            Versioned(
                expected_version=dispatched.version,
                idempotency_key=uuid4().hex,
            ),
        ),
        StoreError,
        "stale_candidate",
    )
    stale = next(
        item
        for item in store.list_candidates(
            tenant,
            run.id,
            job_id=dispatch.id,
            limit=100,
        ).items
        if item.person_id == dispatch_person.id
    )
    _require(
        not stale.eligible and any("stale_candidate" in reason for reason in stale.reasons),
        "changed candidate versions must be visible and rejected at binding transitions",
    )
    checks.append("stale_person_version_blocks_placement")
    return checks


def verify_domain(admin_connection, app_conninfo: str, worker_conninfo: str) -> dict:
    """Return non-profile evidence, or raise on any failed acceptance condition.

    Preconditions: idle administrator connection, empty active queue, no other
    worker processes. This is an operator/setup-only verifier, never an HTTP API.
    """
    _require(
        admin_connection.info.transaction_status == TransactionStatus.IDLE,
        "commit setup work before invoking verify_domain; administrator connection must be idle",
    )
    with admin_connection.transaction(), admin_connection.cursor(row_factory=dict_row) as cursor:
        pending = cursor.execute(
            "SELECT count(*) AS n FROM staff_saas.dispatch WHERE state IN ('pending','leased')",
        ).fetchone()["n"]
        _require(pending == 0, "active queue must be empty and independent workers stopped")
    store, worker = SaasStore(app_conninfo), SaasWorker(worker_conninfo)
    store.initialize()
    worker.initialize()
    suffix = uuid4().hex
    tenants = [f"verify-saas-{suffix}-{index}" for index in range(3)]
    datasets = [_fixture(tenant) for tenant in tenants[:2]]
    first, second, workflow_tenant = tenants
    checks = []
    try:
        with admin_connection.transaction():
            for dataset in datasets:
                counts = load_dataset(admin_connection, dataset)
                _require(counts["jobs"] == 1 and counts["people"] == 2, "fixture counts")
                _require(load_dataset(admin_connection, dataset) == counts, "additive import")
        checks.append("idempotent_additive_import")

        # Force-RLS filtering is tested without a WHERE tenant_id predicate.
        with store.connect() as connection:
            for tenant in tenants[:2]:
                with connection.transaction():
                    tenant_context(connection, tenant)
                    visible = connection.execute(
                        "SELECT DISTINCT tenant_id FROM staff_saas.people",
                    ).fetchall()
                    _require(visible == [{"tenant_id": tenant}], "cross-tenant read isolation")
            with connection.transaction():
                _require(
                    connection.execute(
                        "SELECT count(*) AS n FROM staff_saas.people",
                    ).fetchone()["n"]
                    == 0,
                    "transaction-local context must clear on reuse",
                )
        checks.extend(["force_rls_isolation", "reused_connection_context_reset"])

        def cross_tenant_write():
            with store.connect() as connection:
                tenant_context(connection, first)
                connection.execute(
                    "INSERT INTO staff_saas.runs(tenant_id,id,status,request,model_version) "
                    "VALUES (%s,%s,'queued','{}','verification')",
                    (second, uuid4().hex),
                )

        _expect_error(cross_tenant_write, psycopg.errors.InsufficientPrivilege)
        checks.append("force_rls_with_check")
        page = store.list_people(first, limit=1)
        _require(page.next_cursor is not None, "opaque cursor exists")
        _expect_error(
            lambda: store.list_people(second, cursor=page.next_cursor),
            StoreError,
            "invalid_cursor",
        )
        public = store.list_people(first).model_dump_json()
        _require(
            all(
                field not in public
                for field in (
                    "private_address",
                    "private_location",
                    "contact_email",
                    "employment_history",
                )
            ),
            "private fields/history absent from list",
        )
        checks.extend(["tenant_bound_cursor", "public_list_privacy"])

        request = MatchRequest(idempotency_key=uuid4().hex, job_ids=[datasets[0].jobs[0].id])
        with ThreadPoolExecutor(max_workers=2) as pool:
            runs = list(pool.map(lambda _: store.create_run(first, "verifier", request), range(2)))
        _require(runs[0].id == runs[1].id, "concurrent idempotent admission must deduplicate")
        first_run = runs[0]
        _expect_error(
            lambda: store.create_run(
                first,
                "verifier",
                request.model_copy(
                    update={"idempotency_key": uuid4().hex},
                ),
            ),
            StoreError,
            "tenant_run_limit",
        )
        _expect_error(lambda: store.run(second, first_run.id), StoreError, "not_found")
        checks.extend(["concurrent_idempotency", "one_active_run_per_tenant", "run_ownership"])

        def cross_tenant_foreign_key():
            with store.connect() as connection:
                tenant_context(connection, first)
                connection.execute(
                    "INSERT INTO introductions(tenant_id,id,run_id,job_id,person_id,company_id,"
                    "candidate_alias,status,job_version,person_version,expires_at) "
                    "VALUES (%s,%s,%s,%s,'person-a','company','test','shortlisted',1,1,%s)",
                    (
                        first,
                        uuid4().hex,
                        first_run.id,
                        datasets[1].jobs[0].id,
                        datetime.now(UTC) + timedelta(days=1),
                    ),
                )

        _expect_error(cross_tenant_foreign_key, psycopg.errors.ForeignKeyViolation)
        checks.append("tenant_composite_foreign_keys")
        second_run = store.create_run(
            second,
            "verifier",
            MatchRequest(
                idempotency_key=uuid4().hex,
                job_ids=[datasets[1].jobs[0].id],
            ),
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: worker.claim(), range(2)))
        _require(all(claim is not None for claim in claims), "both queued runs must be claimed")
        _require(
            {claim["tenant_id"] for claim in claims} == {first, second},
            "concurrent claims must be distinct",
        )
        by_tenant = {claim["tenant_id"]: claim for claim in claims}
        old, cancelled = by_tenant[first], by_tenant[second]
        worker._snapshot(old)
        worker._snapshot(cancelled)
        worker.heartbeat(old)
        checks.extend(["concurrent_skip_locked_claim", "lease_heartbeat"])
        store.cancel_run(second, "verifier", second_run.id, {"idempotency_key": uuid4().hex})
        _expect_error(lambda: worker.heartbeat(cancelled), LeaseLost)
        _require(store.run(second, second_run.id).status == "cancelled", "durable cancellation")
        checks.append("cancellation_fencing")

        with admin_connection.transaction():
            admin_connection.execute(
                "UPDATE staff_saas.dispatch SET lease_until=clock_timestamp()-interval '1 second' "
                "WHERE tenant_id=%s AND run_id=%s",
                (first, first_run.id),
            )
        _expect_error(lambda: worker.heartbeat(old), LeaseLost)
        replacement = SaasWorker(worker_conninfo)
        fresh = replacement.claim()
        _require(
            fresh is not None and fresh["tenant_id"] == first and fresh["attempt"] == 2,
            "replacement worker must reclaim expired work with an incremented attempt",
        )
        snapshot = replacement._snapshot(fresh)
        candidate = evaluate(
            datasets[0].people[0],
            datasets[0].jobs[0],
            Commute(
                status="estimated",
                mode="transit",
                duration_seconds=1200,
                eligible=True,
                summary="Verification-only controlled route; not geographic evidence",
                network_version="verification",
                assumptions_version="verification",
            ),
        )
        _require(candidate.eligible, "controlled verification candidate")
        candidate.recommended = True
        solution = Solution(
            candidates=[candidate],
            coverage=Coverage(
                jobs=1,
                people=2,
                retrieved_pairs=1,
                evaluated_pairs=1,
                eligible_pairs=1,
                notes=["Fencing test only, not a routing/solver test"],
            ),
            solver_status="VERIFICATION_FENCE_ONLY",
            objective_bound=None,
            recommended_count=1,
        )
        _expect_error(lambda: worker.publish(old, solution), LeaseLost)
        _expect_error(lambda: worker.publish(cancelled, solution), LeaseLost)
        _require(
            store.run(first, first_run.id).status == "running",
            "stale publication must not change the current run",
        )
        replacement.publish(fresh, solution, snapshot[4])
        reconnected = SaasStore(app_conninfo)
        _require(
            reconnected.run(first, first_run.id).status == "completed",
            "completed work must survive a fresh application instance",
        )
        _require(
            reconnected.list_candidates(first, first_run.id).total == 1,
            "only the current attempt may publish results",
        )
        checks.extend(
            [
                "expired_lease_recovery",
                "stale_attempt_publication_fencing",
                "transactional_publication",
                "restart_persistence",
            ]
        )
        checks.extend(_workflow_checks(admin_connection, store, replacement, workflow_tenant))
        return {
            "status": "passed",
            "checks": checks,
            "fixture_tenants": len(tenants),
            "routing_solver_scope": (
                "Generated 30-record workflow fixture plus controlled distinct-person/job copies; "
                "Real route evaluation and worker solver; "
                "HTTP/subprocess checks remain parent-owned."
            ),
        }
    finally:
        _cleanup(admin_connection, tenants)
