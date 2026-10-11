"""Shared domain and HTTP contracts for the local-only Phase 0 slice."""

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Period(Model):
    start: date
    end: date

    @model_validator(mode="after")
    def valid_period(self) -> "Period":
        if self.start >= self.end:
            raise ValueError("期間は開始日 < 終了日（終了日を含まない）で指定してください")
        return self


class Qualification(Model):
    verified: bool
    valid_until: date


class Staff(Model):
    id: str
    name: str
    skills: dict[str, Annotated[int, Field(ge=0, strict=True)]] | None
    qualifications: dict[str, Qualification] | None
    availability: Period | None
    min_hourly_rate: int | None = Field(ge=0)
    commute_limit_minutes: int | None = Field(gt=0)
    matching_enabled: bool | None
    eligibility_verified: bool | None
    mandatory_verified: bool | None
    version: int = Field(default=1, ge=1)


class Order(Model):
    id: str
    title: str
    period: Period
    headcount: int = Field(gt=0, le=100)
    hourly_rate: int = Field(gt=0)
    required_skills: dict[str, Annotated[int, Field(gt=0, strict=True)]]
    required_qualifications: list[str]
    eligibility_verified: bool | None
    mandatory_verified: bool | None
    scheduling_mode: Literal["full_period"] = "full_period"
    version: int = Field(default=1, ge=1)


class PairEvidence(Model):
    staff_id: str
    order_id: str
    commute_minutes: int | None = Field(ge=0)
    commute_verified: bool
    skill_fit: float = Field(ge=0, le=1)
    preference_fit: float = Field(ge=0, le=1)
    history_fit: float | None = Field(default=None, ge=0, le=1)
    rated_assignments: int = Field(default=0, ge=0)
    synthetic: Literal[True] = True
    version: int = Field(default=1, ge=1)


class Assignment(Model):
    id: str
    staff_id: str
    order_id: str
    period: Period
    source_run_id: str | None
    proposal_id: str | None
    confirmed_at: datetime


class Snapshot(Model):
    tenant_id: str
    revision: int
    staff: list[Staff]
    orders: list[Order]
    evidence: list[PairEvidence]
    assignments: list[Assignment]


class Reason(Model):
    code: str
    message: str
    kind: Literal["passed", "violation", "unknown", "information"]


class ScoreParts(Model):
    skill: float
    preference: float
    commute: float
    history: float | None
    history_missing: bool
    rated_assignments: int
    weights_version: str = "mock-rules-v1"
    synthetic: Literal[True] = True


class Candidate(Model):
    staff_id: str
    order_id: str
    eligibility: Literal["eligible", "ineligible", "pending"]
    score: float | None
    score_parts: ScoreParts | None
    commute_minutes: int | None
    reasons: list[Reason]
    selected: bool = False
    explanation: str


class Pair(Model):
    staff_id: str
    order_id: str


class Allocation(Model):
    method: str
    pairs: list[Pair]
    filled_slots: int
    total_score: float


class SolverReport(Model):
    status: Literal["OPTIMAL", "FEASIBLE", "INFEASIBLE", "UNKNOWN", "MODEL_INVALID"]
    count_status: str
    score_status: str | None
    count_optimal: bool
    score_optimal: bool
    objective_bound: float | None
    gap: float | None
    elapsed_ms: float
    time_limit_seconds: float
    engine: str
    seed: int = 0
    workers: int = 1
    score_scale: int = 10000
    fallback_used: bool = False


class Unfilled(Model):
    order_id: str
    slots: int
    reason: str
    eligible_candidates: int
    pending_candidates: int


class OptimizationResult(Model):
    candidates: list[Candidate]
    optimized: Allocation
    greedy: Allocation
    solver: SolverReport
    unfilled: list[Unfilled]
    candidate_count: int
    candidate_limit: int = 2000
    truncated: Literal[False] = False
    total_remaining_slots: int
    confirmed_slots: int
    input_revision: int


class OptimizeRequest(Model):
    time_limit_seconds: float = Field(default=3.0, ge=0.05, le=10)
    scheduling_mode: Literal["full_period"] = "full_period"
    idempotency_key: str = Field(min_length=8, max_length=128)


class Proposal(Model):
    id: str
    run_id: str
    staff_id: str
    order_id: str
    status: Literal["proposed", "consented", "declined", "expired", "confirmed"]
    version: int
    staff_version: int
    order_version: int
    evidence_version: int
    expires_at: datetime
    responded_at: datetime | None
    confirmed_assignment_id: str | None


class RespondRequest(Model):
    decision: Literal["consent", "decline"]
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=128)


class ConfirmRequest(Model):
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=128)


class RunAccepted(Model):
    run_id: str
    status: Literal["queued", "running", "completed", "failed"]


class RunView(Model):
    id: str
    status: Literal["queued", "running", "completed", "failed"]
    created_at: datetime
    input_revision: int
    config: OptimizeRequest
    error: str | None
    result: OptimizationResult | None
    proposals: list[Proposal]


class ErrorDetail(Model):
    code: str
    message: str


class ErrorResponse(Model):
    detail: ErrorDetail
