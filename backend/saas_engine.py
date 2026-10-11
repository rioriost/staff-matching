"""Bounded sparse assignment, not an exhaustive all-pairs optimum.

Retrieval starts with 40 structural candidates/job, expands unfilled jobs to 80,
and never evaluates more than 80,000 routes/run. CP-SAT has a ten-second budget.
The objective/bound refer exclusively to this retrieved graph.
"""

import hashlib
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from ortools.sat.python import cp_model

from backend.saas_domain import hard_rejections, schedules_conflict, shift_intervals
from backend.saas_models import Candidate, Coverage, Job, PersonRecord
from backend.store import StoreError

INITIAL_CANDIDATES = 40
MAX_CANDIDATES = 80
MAX_PAIRS = 80000
SOLVER_SECONDS = 10


def skill_vector(skills: dict[str, int]) -> list[float]:
    """Explicit hashed skill levels; this is not a learned or language embedding."""
    vector = [0.0] * 32
    for name, level in sorted(skills.items()):
        slot = int.from_bytes(hashlib.sha256(name.encode()).digest()[:2], "big") % 32
        vector[slot] += level
    return vector


@dataclass
class Solution:
    candidates: list[Candidate]
    coverage: Coverage
    solver_status: str
    objective_bound: float | None
    recommended_count: int


def solve(
    jobs: list[Job],
    candidates: list[Candidate],
    placements: list[tuple[str, Job]],
    pins: list[tuple[str, str]],
    *,
    seconds: float = SOLVER_SECONDS,
) -> tuple[str, float | None, int]:
    job_by_id = {job.id: job for job in jobs}
    eligible = {(c.job_id, c.person_id): c for c in candidates if c.eligible}
    if len(set(pins)) != len(pins) or any(pin not in eligible for pin in pins):
        raise StoreError(409, "pin_ineligible", "Every pin must be a distinct eligible candidate.")
    model = cp_model.CpModel()
    variables = {key: model.new_bool_var(f"x{index}") for index, key in enumerate(eligible)}
    by_job, by_person = defaultdict(list), defaultdict(list)
    conflict_cache = {}

    def conflict(left, right):
        # A historical placement can have the same ID as a newer job version.
        key = tuple(sorted((id(left), id(right))))
        if key not in conflict_cache:
            conflict_cache[key] = schedules_conflict(left, right)
        return conflict_cache[key]

    for key, variable in variables.items():
        by_job[key[0]].append(variable)
        by_person[key[1]].append((key[0], variable))
    for job in jobs:
        occupied = sum(existing.id == job.id for _, existing in placements)
        model.add(sum(by_job[job.id]) <= max(0, job.headcount - occupied))
    intervals = {
        job.id: list(shift_intervals(job.start_date, job.end_date, job.schedule)) for job in jobs
    }
    for person_id, options in by_person.items():
        for job_id, variable in options:
            if any(
                p == person_id and (existing.id == job_id or conflict(job_by_id[job_id], existing))
                for p, existing in placements
            ):
                model.add(variable == 0)
        # Sweep real dated shifts into overlap cliques instead of generating a
        # quadratic pair constraint graph for popular candidates. Repeated weeks
        # produce identical cliques and are collapsed before building CP-SAT.
        events = []
        option_by_job = dict(options)
        for job_id, _ in options:
            for start, end in intervals[job_id]:
                events.extend(((start, 1, job_id), (end, -1, job_id)))
        active, cliques = set(), set()
        added = False
        for _, kind, job_id in sorted(events):
            if kind == 1:
                active.add(job_id)
                added = True
            else:
                if added and len(active) > 1:
                    cliques.add(frozenset(active))
                active.remove(job_id)
                added = False
        for clique in cliques:
            model.add_at_most_one(option_by_job[job_id] for job_id in sorted(clique))
    for pin in pins:
        model.add(variables[pin] == 1)
    occupied = defaultdict(int)
    assigned = defaultdict(list)
    for person_id, job in placements:
        occupied[job.id] += 1
        assigned[person_id].append(job)
    hint = set()

    def hint_pair(key, required=False):
        job_id, person_id = key
        job = job_by_id[job_id]
        allowed = occupied[job_id] < job.headcount and not any(
            previous.id == job_id or conflict(job, previous) for previous in assigned[person_id]
        )
        if not allowed:
            if required:
                raise StoreError(
                    409, "pins_conflict", "Pins conflict with capacity or work schedules."
                )
            return
        occupied[job_id] += 1
        assigned[person_id].append(job)
        hint.add(key)

    for pin in pins:
        hint_pair(pin, required=True)
    for key in sorted(eligible, key=lambda key: (-(eligible[key].score or 0), *key)):
        if key not in hint:
            hint_pair(key)
    for key, variable in variables.items():
        model.add_hint(variable, int(key in hint))
    # Cardinality dominates score, without any protected-attribute inputs.
    cardinality_weight = 10000 * max(1, len(variables)) + 1
    model.maximize(
        sum(
            (cardinality_weight + round((eligible[key].score or 0) * 100)) * variable
            for key, variable in variables.items()
        )
    )
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = seconds
    solver.parameters.num_search_workers = 2
    solver.parameters.random_seed = 7
    status = solver.solve(model)
    if status == cp_model.INFEASIBLE and pins:
        raise StoreError(409, "pins_conflict", "Pins conflict with capacity or work schedules.")
    feasible = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    for candidate in candidates:
        key = (candidate.job_id, candidate.person_id)
        candidate.recommended = feasible and key in variables and bool(solver.value(variables[key]))
        candidate.pinned = key in pins
    return (
        solver.status_name(status),
        solver.best_objective_bound if feasible else None,
        sum(c.recommended for c in candidates),
    )


def run_matching(
    jobs: list[Job],
    people_count: int,
    retrieve: Callable[[Job, int], list[PersonRecord]],
    evaluate_pair: Callable[[PersonRecord, Job], Candidate],
    placements: list[tuple[str, Job]],
    pins: list[tuple[str, str]],
    get_person: Callable[[str], PersonRecord],
    progress: Callable[[int, Coverage], None],
) -> Solution:
    candidates: dict[tuple[str, str], Candidate] = {}
    evaluated_per_job = defaultdict(int)
    coverage = Coverage(
        jobs=len(jobs),
        people=people_count,
        notes=[
            f"Scope: {len(jobs)} admitted open job records, "
            f"{sum(job.headcount for job in jobs)} headcount slots. "
            "Closed jobs are excluded by admission; coverage.jobs counts records, not slots.",
            "Structural skill-vector retrieval: 40/job, then up to 80/job for unfilled vacancies.",
            "At most 80,000 evaluated pairs; optimum/bound apply only to this sparse graph.",
            "The weighted objective maximizes filled slots first, then hundredth-point scores.",
            "Contract end dates are exclusive; overnight intervals stop at the contract boundary.",
            "History excludes future/unstructured observations and scores older than three years.",
        ],
    )
    job_by_id = {job.id: job for job in jobs}

    def add(person, job, budget=MAX_CANDIDATES):
        key = (job.id, person.id)
        if key in candidates:
            return
        if coverage.evaluated_pairs >= MAX_PAIRS or evaluated_per_job[job.id] >= budget:
            coverage.truncated = True
            return
        coverage.retrieved_pairs += 1
        if hard_rejections(person, job):
            return
        candidate = evaluate_pair(person, job)
        candidates[key] = candidate
        evaluated_per_job[job.id] += 1
        coverage.evaluated_pairs += 1
        coverage.eligible_pairs += int(candidate.eligible)
        coverage.route_unknown += int(candidate.commute.status == "unknown")

    for job_id, person_id in pins:
        if job_id not in job_by_id:
            raise StoreError(409, "pin_outside_run", "Pinned job must be included in this run.")
        add(get_person(person_id), job_by_id[job_id])
    for index, job in enumerate(jobs):
        for person in retrieve(job, INITIAL_CANDIDATES):
            add(person, job, INITIAL_CANDIDATES)
        progress(5 + int(55 * (index + 1) / max(1, len(jobs))), coverage)
    status, bound, count = solve(jobs, list(candidates.values()), placements, pins)
    fill = defaultdict(int)
    for candidate in candidates.values():
        fill[candidate.job_id] += int(candidate.recommended)
    for _, job in placements:
        fill[job.id] += 1
    unfilled = [job for job in jobs if fill[job.id] < job.headcount]
    for index, job in enumerate(unfilled):
        for person in retrieve(job, MAX_CANDIDATES):
            add(person, job)
        progress(65 + int(25 * (index + 1) / max(1, len(unfilled))), coverage)
    if unfilled:
        status, bound, count = solve(jobs, list(candidates.values()), placements, pins)
    coverage.truncated = coverage.truncated or coverage.retrieved_pairs < len(jobs) * people_count
    progress(95, coverage)
    return Solution(list(candidates.values()), coverage, status, bound, count)
