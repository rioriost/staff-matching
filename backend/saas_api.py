"""HTTP adapter for authenticated management-company identities only.

The outer ASGI middleware must cap SaaS bodies at 256 KiB before JSON parsing,
including chunked bodies. This router bounds identifier/search strings and lists.
Handlers intentionally use synchronous def: FastAPI offloads blocking store I/O.
"""

from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import Field

from backend.saas_models import (
    Appointment,
    AppointmentRequest,
    Candidate,
    Company,
    Introduction,
    Job,
    MatchRequest,
    MatchRun,
    Model,
    Notification,
    Overview,
    Page,
    Person,
    PersonSummary,
    ShortlistRequest,
    SimulationRequest,
    Site,
    Versioned,
)
from backend.saas_store import SaasStore
from backend.store import StoreError


class Idempotent(Model):
    idempotency_key: str = Field(min_length=8, max_length=128)


def _identifiers(*values: str):
    if any(not 1 <= len(value) <= 128 for value in values):
        raise StoreError(422, "invalid_identifier", "Identifiers must contain 1 to 128 characters.")


def create_saas_router(
    store: Callable[[], SaasStore],
    allow_simulation: bool = False,
) -> APIRouter:
    router = APIRouter(prefix="/api/saas", tags=["saas"])

    def identity(request: Request):
        actor = getattr(request.state, "identity", None)
        if actor is None:
            raise StoreError(401, "authentication_required", "Account login is required.")
        if actor.role not in ("admin", "coordinator", "viewer"):
            raise StoreError(403, "role_forbidden", "Management-company membership is required.")
        if request.method not in ("GET", "HEAD") and actor.role == "viewer":
            raise StoreError(403, "read_only", "This account is read-only.")
        return actor

    Actor = Annotated[object, Depends(identity)]
    Limit = Annotated[int, Query(ge=1, le=100)]
    Cursor = Annotated[str | None, Query(max_length=2048)]
    Search = Annotated[str, Query(max_length=200)]
    Identifier = Annotated[str, Path(min_length=1, max_length=128)]
    IdFilter = Annotated[str, Query(max_length=128)]

    @router.get("/overview", response_model=Overview)
    def overview(actor: Actor):
        return store().overview(
            actor.tenant_id,
            actor.user_id,
            allow_simulation and actor.role != "viewer",
        )

    @router.get("/jobs", response_model=Page[Job])
    def jobs(
        actor: Actor,
        q: Search = "",
        occupation: Search = "",
        business_type: Literal["", "placement", "dispatch"] = "",
        cursor: Cursor = None,
        limit: Limit = 50,
    ):
        return store().list_jobs(
            actor.tenant_id,
            q=q,
            occupation=occupation,
            business_type=business_type,
            cursor=cursor,
            limit=limit,
        )

    @router.get("/jobs/{job_id}", response_model=Job)
    def job(job_id: Identifier, actor: Actor):
        return store().job(actor.tenant_id, job_id)

    @router.get("/people", response_model=Page[PersonSummary])
    def people(actor: Actor, q: Search = "", cursor: Cursor = None, limit: Limit = 50):
        return store().list_people(actor.tenant_id, q=q, cursor=cursor, limit=limit)

    @router.get("/people/{person_id}", response_model=Person)
    def person(person_id: Identifier, actor: Actor):
        return store().person(actor.tenant_id, person_id)

    @router.get("/companies", response_model=Page[Company])
    def companies(actor: Actor, cursor: Cursor = None, limit: Limit = 50):
        return store().list_companies(actor.tenant_id, cursor=cursor, limit=limit)

    @router.get("/sites", response_model=Page[Site])
    def sites(actor: Actor, company_id: IdFilter = "", cursor: Cursor = None, limit: Limit = 50):
        return store().list_sites(
            actor.tenant_id, company_id=company_id, cursor=cursor, limit=limit
        )

    @router.post("/matching/runs", status_code=202, response_model=MatchRun)
    def submit(body: MatchRequest, actor: Actor):
        _identifiers(*body.job_ids, *(item for pair in body.pins for item in pair))
        return store().create_run(actor.tenant_id, actor.user_id, body)

    @router.get("/matching/runs", response_model=Page[MatchRun])
    def runs(actor: Actor, cursor: Cursor = None, limit: Limit = 50):
        return store().list_runs(actor.tenant_id, cursor=cursor, limit=limit)

    @router.get("/matching/runs/{run_id}", response_model=MatchRun)
    def run(run_id: Identifier, actor: Actor):
        return store().run(actor.tenant_id, run_id)

    @router.get("/matching/runs/{run_id}/candidates", response_model=Page[Candidate])
    def candidates(
        run_id: Identifier,
        actor: Actor,
        job_id: IdFilter = "",
        cursor: Cursor = None,
        limit: Limit = 50,
    ):
        return store().list_candidates(
            actor.tenant_id,
            run_id,
            job_id=job_id,
            cursor=cursor,
            limit=limit,
        )

    @router.post("/matching/runs/{run_id}/cancel", response_model=MatchRun)
    def cancel_run(run_id: Identifier, body: Idempotent, actor: Actor):
        return store().cancel_run(actor.tenant_id, actor.user_id, run_id, body)

    @router.get("/introductions", response_model=Page[Introduction])
    def introductions(actor: Actor, cursor: Cursor = None, limit: Limit = 50):
        return store().list_introductions(actor.tenant_id, cursor=cursor, limit=limit)

    @router.post("/introductions", status_code=201, response_model=Introduction)
    def shortlist(body: ShortlistRequest, actor: Actor):
        _identifiers(body.run_id, body.job_id, body.person_id)
        return store().shortlist(actor.tenant_id, actor.user_id, body)

    @router.post("/introductions/{intro_id}/simulate", response_model=Introduction)
    def simulate(intro_id: Identifier, body: SimulationRequest, actor: Actor):
        return store().simulate(
            actor.tenant_id,
            actor.user_id,
            intro_id,
            body,
            allow_simulation,
        )

    @router.post("/introductions/{intro_id}/cancel", response_model=Introduction)
    def cancel_intro(intro_id: Identifier, body: Versioned, actor: Actor):
        return store().cancel_introduction(actor.tenant_id, actor.user_id, intro_id, body)

    @router.post("/introductions/{intro_id}/place", response_model=Introduction)
    def place(intro_id: Identifier, body: Versioned, actor: Actor):
        return store().place(actor.tenant_id, actor.user_id, intro_id, body)

    @router.get("/appointments", response_model=Page[Appointment])
    def appointments(actor: Actor, cursor: Cursor = None, limit: Limit = 50):
        return store().list_appointments(actor.tenant_id, cursor=cursor, limit=limit)

    @router.post(
        "/introductions/{intro_id}/appointments", status_code=201, response_model=Appointment
    )
    def propose(intro_id: Identifier, body: AppointmentRequest, actor: Actor):
        if body.site_id is not None:
            _identifiers(body.site_id)
        return store().propose_appointment(actor.tenant_id, actor.user_id, intro_id, body)

    @router.post("/appointments/{appointment_id}/accept", response_model=Appointment)
    def accept(appointment_id: Identifier, body: Versioned, actor: Actor):
        return store().change_appointment(
            actor.tenant_id,
            actor.user_id,
            appointment_id,
            body,
            "accept",
            allow_simulation,
        )

    @router.post("/appointments/{appointment_id}/cancel", response_model=Appointment)
    def cancel_meeting(appointment_id: Identifier, body: Versioned, actor: Actor):
        return store().change_appointment(
            actor.tenant_id,
            actor.user_id,
            appointment_id,
            body,
            "cancel",
        )

    @router.post("/appointments/{appointment_id}/reschedule", response_model=Appointment)
    def reschedule(appointment_id: Identifier, body: AppointmentRequest, actor: Actor):
        if body.site_id is not None:
            _identifiers(body.site_id)
        return store().change_appointment(
            actor.tenant_id,
            actor.user_id,
            appointment_id,
            body,
            "reschedule",
        )

    @router.get("/notifications", response_model=Page[Notification])
    def notifications(
        actor: Actor,
        audience: Literal["management", "company", "person"] = "management",
        cursor: Cursor = None,
        limit: Limit = 50,
    ):
        return store().list_notifications(
            actor.tenant_id,
            actor.user_id,
            audience=audience,
            cursor=cursor,
            limit=limit,
            allow_simulation=allow_simulation and actor.role != "viewer",
        )

    @router.post("/notifications/{notice_id}/read", response_model=Notification)
    def read(notice_id: Identifier, body: Idempotent, actor: Actor):
        return store().read_notification(
            actor.tenant_id,
            actor.user_id,
            notice_id,
            body,
            allow_simulation,
        )

    return router
