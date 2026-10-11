"""Hard constraints shared by retrieval, manual decisions, and the assignment solver.

Contract end dates are exclusive. Shifts crossing midnight are clipped at the
exclusive contract boundary, never treated as if they finished on their start day.
"""

from datetime import UTC, date, datetime, time, timedelta

from backend.saas_models import (
    Candidate,
    Commute,
    Job,
    PersonRecord,
    Schedule,
    Scores,
)
from backend.store import StoreError

MODEL_VERSION = "sparse-structural-v1"
TERMINAL_INTRODUCTIONS = {"declined", "cancelled", "expired", "placed"}
SIMULATED_TRANSITIONS = {
    "company_request": {"shortlisted": "requested"},
    "person_consent": {"requested": "consented"},
    "person_decline": {"requested": "declined", "consented": "declined"},
    "withdraw_consent": {
        "consented": "cancelled",
        "interview_proposed": "cancelled",
        "interview_accepted": "cancelled",
    },
}


def shift_intervals(start: date, end: date, schedule: Schedule):
    """Yield local wall-clock intervals; Tokyo has no DST within the modeled period."""
    boundary = datetime.combine(end, time())
    day = start
    while day < end:
        if day.weekday() in schedule.weekdays:
            midnight = datetime.combine(day, time())
            yield (
                midnight + timedelta(minutes=schedule.start_minute),
                min(midnight + timedelta(minutes=schedule.end_minute), boundary),
            )
        day += timedelta(days=1)


def schedules_conflict(left: Job, right: Job) -> bool:
    if left.end_date <= right.start_date or right.end_date <= left.start_date:
        return False
    # A repeating weekly pattern needs at most one full week plus its boundaries.
    start = max(left.start_date, right.start_date)
    end = min(left.end_date, right.end_date, start + timedelta(days=9))
    a = iter(shift_intervals(max(left.start_date, start - timedelta(days=1)), end, left.schedule))
    b = iter(shift_intervals(max(right.start_date, start - timedelta(days=1)), end, right.schedule))
    x, y = next(a, None), next(b, None)
    while x and y:
        if x[0] < y[1] and y[0] < x[1]:
            return True
        if x[1] <= y[1]:
            x = next(a, None)
        else:
            y = next(b, None)
    return False


def available(person: PersonRecord, job: Job) -> bool:
    if person.available_from > job.start_date or person.available_until < job.end_date:
        return False
    seen = False
    for sample_start in {job.start_date, max(job.start_date, job.end_date - timedelta(days=9))}:
        sample_end = min(job.end_date, sample_start + timedelta(days=9))
        windows = iter(
            shift_intervals(
                max(person.available_from, sample_start - timedelta(days=1)),
                min(person.available_until, sample_end + timedelta(days=1)),
                person.availability,
            )
        )
        window = next(windows, None)
        for start, end in shift_intervals(sample_start, sample_end, job.schedule):
            seen = True
            while window and window[1] <= start:
                window = next(windows, None)
            if window is None or window[0] > start:
                return False
            while window[1] < end:
                following = next(windows, None)
                if following is None or following[0] > window[1]:
                    return False
                window = (window[0], max(window[1], following[1]))
    return seen


def experience_months(person: PersonRecord, job: Job) -> int:
    intervals = sorted(
        (entry.start_date, min(entry.end_date, job.start_date))
        for entry in person.employment_history
        if entry.occupation == job.occupation and entry.start_date < job.start_date
    )
    days, latest = 0, date.min
    for start, end in intervals:
        start = max(start, latest)
        days += max(0, (end - start).days)
        latest = max(latest, end)
    return days // 30


def hard_rejections(person: PersonRecord, job: Job) -> list[str]:
    reasons = []
    if job.status != "open":
        reasons.append("job_closed")
    if not person.matching_enabled:
        reasons.append("matching_disabled")
    if person.desired_hourly_pay > job.hourly_pay_max:
        reasons.append("pay_below_preference")
    if any(person.skills.get(skill, 0) < level for skill, level in job.required_skills.items()):
        reasons.append("required_skills")
    credentials = {
        c.name
        for c in person.qualifications
        if c.verified
        and (c.valid_until is None or c.valid_until >= job.end_date - timedelta(days=1))
    }
    if not set(job.required_qualifications) <= credentials:
        reasons.append("required_qualifications")
    if experience_months(person, job) < job.experience_months:
        reasons.append("required_experience")
    if not available(person, job):
        reasons.append("availability")
    return reasons


def commute_valid(person: PersonRecord, job: Job, commute: Commute) -> bool:
    seconds = commute.duration_seconds
    return (
        commute.status == "estimated"
        and commute.eligible
        and seconds is not None
        and seconds < 10800
        and seconds <= person.max_commute_minutes * 60
        and commute.mode in person.commute_modes
        and (commute.mode != "car" or (person.driving_eligible and job.site.parking))
    )


def evaluate(person: PersonRecord, job: Job, commute: Commute) -> Candidate:
    reasons = hard_rejections(person, job)
    if not commute_valid(person, job, commute):
        reasons.append("commute_unknown_or_exceeds_limit")
    credentials = {
        c.name
        for c in person.qualifications
        if c.verified
        and (c.valid_until is None or c.valid_until >= job.end_date - timedelta(days=1))
    }
    skills = (
        sum(min(100, person.skills.get(k, 0) / v * 80) for k, v in job.required_skills.items())
        / len(job.required_skills)
        if job.required_skills
        else 100
    )
    preferred = set(job.preferred_qualifications)
    qualification_score = 80 + 20 * len(preferred & credentials) / max(1, len(preferred))
    if not preferred:
        qualification_score = 100
    evidence = []
    observations = []
    # Only dated structured observations for this occupation are scored. Text is not analyzed.
    reference = min(job.start_date, datetime.now(UTC).date())
    for entry in person.employment_history:
        if entry.occupation != job.occupation:
            continue
        for evaluation in entry.evaluations:
            if not entry.start_date <= evaluation.recorded_at <= entry.end_date:
                continue
            if evaluation.recorded_at > reference:
                continue
            scored = (
                evaluation.score is not None and (reference - evaluation.recorded_at).days <= 1095
            )
            marker = f"score={evaluation.score}/5" if scored else "not scored (missing or outdated)"
            evidence.append(
                f"{evaluation.recorded_at.isoformat()} | {evaluation.source} | "
                f"{evaluation.dimension} | {marker} | {evaluation.observation}"
            )
            if scored:
                observations.append(evaluation.score * 20)
    history = sum(observations) / len(observations) if observations else None
    seconds = commute.duration_seconds if commute.duration_seconds is not None else 10800
    scores = Scores(
        skills=skills,
        qualifications=qualification_score,
        preferences=100 if job.occupation in person.preferred_occupations else 50,
        commute=max(0, 100 * (1 - seconds / 10800)),
        history=history,
    )
    present = [v for v in scores.model_dump().values() if v is not None]
    eligible = not reasons
    if eligible:
        reasons = [
            "資格・スキル・給与・契約期間・勤務時間の必須条件を確認済み",
            "経路は時刻表・渋滞を含まない概算（実際の通勤を保証しません）",
        ]
    return Candidate(
        job_id=job.id,
        person_id=person.id,
        person_name=person.name,
        home_area=person.home_area,
        eligible=eligible,
        score=round(sum(present) / len(present), 2),
        scores=scores,
        reasons=reasons,
        evidence=evidence,
        commute=commute,
        job_version=job.version,
        person_version=person.version,
    )


def next_simulated_state(status: str, action: str, business_type: str) -> str:
    if business_type == "dispatch" and action == "company_request":
        raise StoreError(
            409,
            "dispatch_selection_forbidden",
            "Ordinary dispatch does not permit company candidate selection.",
        )
    next_status = SIMULATED_TRANSITIONS.get(action, {}).get(status)
    if next_status is None:
        raise StoreError(
            409, "invalid_transition", "This action is not valid in the current state."
        )
    return next_status
