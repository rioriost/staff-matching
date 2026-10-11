"""Version-one SaaS HTTP/data contracts; private person locations never form an HTTP DTO."""

from datetime import date, datetime
from typing import Generic, Literal, TypeVar

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Point(Model):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)


class Area(Model):
    id: str
    label: str
    point: Point


class StationAccess(Model):
    station_id: str
    name: str
    walking_minutes: int = Field(ge=0)


class Company(Model):
    id: str
    name: str
    industry: str
    description: str
    contact_email: str


class Site(Model):
    id: str
    company_id: str
    name: str
    kind: Literal["headquarters", "branch", "worksite"]
    address: str
    area: Area
    point: Point
    stations: list[StationAccess]
    parking: bool
    accessibility: str


class Schedule(Model):
    weekdays: list[int] = Field(min_length=1, max_length=7)
    start_minute: int = Field(ge=0, lt=1440)
    end_minute: int = Field(gt=0, le=2880)
    break_minutes: int = Field(default=60, ge=0)

    @model_validator(mode="after")
    def valid(self):
        if len(set(self.weekdays)) != len(self.weekdays) or any(
            day not in range(7) for day in self.weekdays
        ):
            raise ValueError("Weekdays must be unique Monday=0 through Sunday=6.")
        duration = self.end_minute - self.start_minute
        if not 0 < duration <= 1440 or self.break_minutes >= duration:
            raise ValueError("Use an increasing shift interval; overnight end may exceed 1440.")
        return self


class Job(Model):
    id: str
    company_id: str
    company_name: str
    site_id: str
    site: Site
    title: str
    occupation: str
    duties: str
    business_type: Literal["placement", "dispatch"]
    employment_type: str
    headcount: int = Field(ge=1, le=20)
    start_date: date
    end_date: date
    schedule: Schedule
    hourly_pay_min: int = Field(gt=0)
    hourly_pay_max: int = Field(gt=0)
    required_skills: dict[str, int]
    required_qualifications: list[str]
    preferred_qualifications: list[str]
    experience_months: int = Field(ge=0)
    renewal: str
    duties_change_scope: str
    location_change_scope: str
    overtime: str
    holidays: str
    allowances: str
    benefits: list[str]
    application: str
    status: Literal["open", "closed"] = "open"
    version: int = 1
    synthetic: Literal[True] = True

    @model_validator(mode="after")
    def consistent(self):
        if self.start_date >= self.end_date or self.hourly_pay_max < self.hourly_pay_min:
            raise ValueError("Invalid contract period or pay interval.")
        if self.site.id != self.site_id or self.site.company_id != self.company_id:
            raise ValueError("Job and worksite ownership differ.")
        if any(not 1 <= level <= 5 for level in self.required_skills.values()):
            raise ValueError("Skill levels range from one to five.")
        return self


class Credential(Model):
    name: str
    verified: bool
    valid_until: date | None


class Evaluation(Model):
    recorded_at: date
    observation: str
    dimension: Literal["attendance", "quality", "collaboration", "improvement"]
    score: int | None = Field(default=None, ge=1, le=5)
    source: str


class Employment(Model):
    employer: str
    occupation: str
    start_date: date
    end_date: date
    duties: str
    evaluations: list[Evaluation]


class PersonSummary(Model):
    id: str
    name: str
    occupation: str
    home_area: Area
    stations: list[StationAccess]
    skills: dict[str, int]
    qualifications: list[Credential]
    available_from: date
    available_until: date
    availability: Schedule
    desired_hourly_pay: int = Field(gt=0)
    preferred_occupations: list[str]
    commute_modes: list[Literal["transit", "car"]]
    driving_eligible: bool
    max_commute_minutes: int = Field(gt=0, le=240)
    matching_enabled: bool = True
    version: int = 1
    synthetic: Literal[True] = True


class Person(PersonSummary):
    employment_history: list[Employment]


class PersonRecord(Person):
    private_location: Point
    private_address: str
    contact_email: str

    def public(self) -> Person:
        return Person.model_validate(self.model_dump(exclude={
            "private_location", "private_address", "contact_email",
        }))


class Dataset(Model):
    tenant_id: str
    version: str
    companies: list[Company]
    sites: list[Site]
    jobs: list[Job]
    people: list[PersonRecord]


class Commute(Model):
    status: Literal["estimated", "unknown", "unreachable"]
    mode: Literal["transit", "car"] | None
    duration_seconds: int | None = Field(default=None, ge=0)
    eligible: bool
    summary: str
    station_names: list[str] = Field(default_factory=list)
    public_path: list[Point] = Field(default_factory=list)
    network_version: str
    assumptions_version: str
    uncertainty: str = "概算。時刻表・運行状況・渋滞は反映していません。"


class Scores(Model):
    skills: float = Field(ge=0, le=100)
    qualifications: float = Field(ge=0, le=100)
    preferences: float = Field(ge=0, le=100)
    commute: float = Field(ge=0, le=100)
    history: float | None = Field(default=None, ge=0, le=100)


class Candidate(Model):
    job_id: str
    person_id: str
    person_name: str
    home_area: Area
    eligible: bool
    score: float | None
    scores: Scores | None
    reasons: list[str]
    evidence: list[str]
    commute: Commute
    recommended: bool = False
    pinned: bool = False
    job_version: int
    person_version: int


T = TypeVar("T")


class Page(Model, Generic[T]):
    items: list[T]
    next_cursor: str | None
    total: int


class MatchRequest(Model):
    idempotency_key: str = Field(min_length=8, max_length=128)
    job_ids: list[str] = Field(default_factory=list, max_length=1000)
    pins: list[tuple[str, str]] = Field(default_factory=list, max_length=100)


class Coverage(Model):
    jobs: int = 0
    people: int = 0
    retrieved_pairs: int = 0
    evaluated_pairs: int = 0
    eligible_pairs: int = 0
    route_unknown: int = 0
    truncated: bool = False
    notes: list[str] = Field(default_factory=list)


class MatchRun(Model):
    id: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    created_at: datetime
    progress: int = Field(ge=0, le=100)
    attempt: int
    error: str | None
    coverage: Coverage
    recommended_count: int
    solver_status: str | None
    objective_bound: float | None
    model_version: str


class Versioned(Model):
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=128)


class ShortlistRequest(Model):
    run_id: str
    job_id: str
    person_id: str
    expected_job_version: int = Field(ge=1)
    expected_person_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=128)


class Introduction(Model):
    id: str
    job_id: str
    person_id: str
    company_id: str
    candidate_alias: str
    status: Literal[
        "shortlisted", "requested", "consented", "declined", "interview_proposed",
        "interview_accepted", "placed", "cancelled", "expired",
    ]
    version: int
    created_at: datetime
    expires_at: datetime
    simulated: bool


class SimulationRequest(Versioned):
    action: Literal["company_request", "person_consent", "person_decline", "withdraw_consent"]
    simulated: Literal[True]


class AppointmentRequest(Versioned):
    start_at: AwareDatetime
    end_at: AwareDatetime
    site_id: str | None = None
    online: bool = False

    @model_validator(mode="after")
    def valid(self):
        duration = self.end_at.timestamp() - self.start_at.timestamp()
        if duration <= 0 or duration > 14400:
            raise ValueError("Interview duration must be positive and at most four hours.")
        if self.online == (self.site_id is not None):
            raise ValueError("Choose one authorized site or an online interview.")
        return self


class Appointment(Model):
    id: str
    introduction_id: str
    job_id: str
    person_id: str
    company_id: str
    start_at: datetime
    end_at: datetime
    site_id: str | None
    online: bool
    status: Literal["proposed", "accepted", "cancelled"]
    version: int


class Notification(Model):
    id: str
    audience: Literal["management", "company", "person"]
    recipient_id: str
    kind: str
    title: str
    message: str
    introduction_id: str | None
    created_at: datetime
    read: bool
    simulated: Literal[True] = True


class Overview(Model):
    tenant_id: str
    dataset_version: str
    jobs: int
    people: int
    companies: int
    introductions: int
    appointments: int
    unread_notifications: int
    can_simulate: bool
    network_version: str
