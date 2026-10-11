from datetime import UTC, datetime
from itertools import combinations
from random import Random

import pytest
from ortools.sat.python import cp_model
from pydantic import ValidationError

from backend.engine import (
    MAX_PAIRS,
    evaluate_candidate,
    generate_candidates,
    optimize,
    overlaps,
    score_units,
)
from backend.models import (
    Assignment,
    OptimizeRequest,
    Order,
    PairEvidence,
    Period,
    Qualification,
    Snapshot,
    Staff,
)
from backend.seed import demo_snapshot


def worker(identifier="s1", **changes):
    return Staff(
        **{
            "id": identifier,
            "name": "合成スタッフ",
            "skills": {"office": 3},
            "qualifications": {},
            "availability": Period(start="2026-10-01", end="2027-02-01"),
            "min_hourly_rate": 1200,
            "commute_limit_minutes": 60,
            "matching_enabled": True,
            "eligibility_verified": True,
            "mandatory_verified": True,
            **changes,
        }
    )


def order(identifier="o1", **changes):
    return Order(
        **{
            "id": identifier,
            "title": "架空事務",
            "period": Period(start="2026-11-01", end="2026-12-01"),
            "headcount": 1,
            "hourly_rate": 1500,
            "required_skills": {"office": 2},
            "required_qualifications": [],
            "eligibility_verified": True,
            "mandatory_verified": True,
            **changes,
        }
    )


def evidence(staff_id="s1", order_id="o1", score=0.8, **changes):
    return PairEvidence(
        **{
            "staff_id": staff_id,
            "order_id": order_id,
            "commute_minutes": 20,
            "commute_verified": True,
            "skill_fit": score,
            "preference_fit": score,
            **changes,
        }
    )


def snapshot(staff=None, orders=None, pairs=None, assignments=None):
    return Snapshot(
        tenant_id="test",
        revision=1,
        staff=[worker()] if staff is None else staff,
        orders=[order()] if orders is None else orders,
        evidence=[evidence()] if pairs is None else pairs,
        assignments=assignments or [],
    )


def test_two_lower_scores_beat_one_high_score():
    data = snapshot(
        staff=[worker("s1"), worker("s2")],
        orders=[order("o1"), order("o2")],
        pairs=[evidence("s1", "o1", 1), evidence("s2", "o1", 0.5), evidence("s1", "o2", 0.5)],
    )
    result = optimize(data)
    assert result.optimized.filled_slots == 2
    assert result.greedy.filled_slots == 1
    assert {(pair.staff_id, pair.order_id) for pair in result.optimized.pairs} == {
        ("s2", "o1"),
        ("s1", "o2"),
    }
    assert result.solver.status == "OPTIMAL"
    assert result.solver.count_optimal and result.solver.score_optimal
    assert result.solver.objective_bound == 2
    assert result.solver.gap == 0


def test_score_is_second_lexicographic_objective():
    result = optimize(
        snapshot(
            staff=[worker("s1"), worker("s2")],
            pairs=[evidence("s1", score=0.3), evidence("s2", score=0.9)],
        )
    )
    assert result.optimized.pairs[0].staff_id == "s2"


def test_multiple_headcount_and_confirmed_capacity():
    job = order(headcount=3)
    data = snapshot(
        staff=[worker(f"s{i}") for i in range(4)],
        orders=[job],
        pairs=[evidence(f"s{i}") for i in range(4)],
        assignments=[
            Assignment(
                id="a1",
                staff_id="s0",
                order_id=job.id,
                period=job.period,
                source_run_id=None,
                proposal_id=None,
                confirmed_at=datetime.now(UTC),
            )
        ],
    )
    result = optimize(data)
    assert result.optimized.filled_slots == 2
    assert result.greedy.filled_slots == 2
    assert result.confirmed_slots == 1
    assert result.total_remaining_slots == 2
    assert all(pair.staff_id != "s0" for pair in result.optimized.pairs)


@pytest.mark.parametrize(("second_start", "expected"), [("2026-11-30", 1), ("2026-12-01", 2)])
def test_half_open_periods_overlap_or_allow_consecutive_work(second_start, expected):
    data = snapshot(
        orders=[
            order("o1"),
            order("o2", period=Period(start=second_start, end="2027-01-01")),
        ],
        pairs=[evidence("s1", "o1"), evidence("s1", "o2")],
    )
    result = optimize(data)
    assert result.optimized.filled_slots == expected
    assert result.greedy.filled_slots == expected


def test_preexisting_work_blocks_other_order_not_just_same_order():
    busy = order("busy")
    assignment = Assignment(
        id="a1",
        staff_id="s1",
        order_id="busy",
        period=busy.period,
        source_run_id=None,
        proposal_id=None,
        confirmed_at=datetime.now(UTC),
    )
    result = optimize(snapshot(orders=[order(), busy], assignments=[assignment]))
    assert result.optimized.filled_slots == result.greedy.filled_slots == 0
    assert "confirmed_conflict" in [reason.code for reason in result.candidates[0].reasons]


@pytest.mark.parametrize(
    ("field", "value", "expected", "code"),
    [
        ("availability", None, "pending", "availability"),
        (
            "availability",
            Period(start="2026-11-02", end="2026-12-01"),
            "ineligible",
            "availability",
        ),
        (
            "availability",
            Period(start="2026-11-01", end="2026-11-30"),
            "ineligible",
            "availability",
        ),
        ("matching_enabled", False, "ineligible", "matching_opt_in"),
        ("matching_enabled", None, "pending", "matching_opt_in"),
        ("min_hourly_rate", 2000, "ineligible", "hourly_rate"),
        ("min_hourly_rate", None, "pending", "hourly_rate"),
        ("skills", {"office": 1}, "ineligible", "skills"),
        ("skills", None, "pending", "skills"),
        ("commute_limit_minutes", None, "pending", "commute_limit"),
        ("commute_limit_minutes", 10, "ineligible", "commute"),
        ("eligibility_verified", False, "ineligible", "staff_eligibility"),
        ("eligibility_verified", None, "pending", "staff_eligibility"),
        ("mandatory_verified", False, "ineligible", "staff_information"),
        ("mandatory_verified", None, "pending", "staff_information"),
    ],
)
def test_hard_conditions_cannot_be_offset_by_high_scores(field, value, expected, code):
    candidate = evaluate_candidate(worker(**{field: value}), order(), evidence(score=1), [])
    assert candidate.eligibility == expected
    assert candidate.score is None
    assert candidate.score_parts is None
    assert any(
        item.code == code and item.kind in ("violation", "unknown") for item in candidate.reasons
    )


@pytest.mark.parametrize("field", ["mandatory_verified", "eligibility_verified"])
def test_order_required_information_unknown_is_not_eligible(field):
    assert (
        evaluate_candidate(worker(), order(**{field: None}), evidence(), []).eligibility
        == "pending"
    )


@pytest.mark.parametrize(
    "pair",
    [None, evidence(commute_minutes=None), evidence(commute_verified=False)],
)
def test_unverified_commute_is_not_eligible(pair):
    candidate = evaluate_candidate(worker(), order(), pair, [])
    assert candidate.eligibility == "pending"
    assert candidate.score is None


@pytest.mark.parametrize(
    ("qualifications", "expected"),
    [
        (None, "pending"),
        ({}, "ineligible"),
        ({"cert": Qualification(verified=False, valid_until="2027-01-01")}, "pending"),
        ({"cert": Qualification(verified=True, valid_until="2026-11-29")}, "ineligible"),
        ({"cert": Qualification(verified=True, valid_until="2026-11-30")}, "eligible"),
    ],
)
def test_qualification_valid_for_entire_half_open_period(qualifications, expected):
    candidate = evaluate_candidate(
        worker(qualifications=qualifications),
        order(required_qualifications=["cert"]),
        evidence(),
        [],
    )
    assert candidate.eligibility == expected


def test_unknown_history_renormalizes_instead_of_penalizing():
    pair = evidence(score=0.8, commute_minutes=12)
    no_history = evaluate_candidate(worker(), order(), pair, [])
    with_history = evaluate_candidate(
        worker(), order(), pair.model_copy(update={"history_fit": 0.8, "rated_assignments": 3}), []
    )
    assert no_history.score == with_history.score == 0.8
    assert no_history.score_parts.history_missing
    assert not with_history.score_parts.history_missing
    assert no_history.score_parts.synthetic


def test_no_candidates_is_valid_and_explicit():
    result = optimize(snapshot(pairs=[]))
    assert result.optimized.filled_slots == 0
    assert result.solver.status == "OPTIMAL"
    assert result.unfilled[0].slots == 1
    assert "候補ペアがありません" in result.unfilled[0].reason
    assert result.candidate_count == 0 and not result.truncated


def test_no_staff_or_orders_is_empty_optimum():
    result = optimize(snapshot(staff=[], orders=[], pairs=[]))
    assert result.total_remaining_slots == 0 and result.unfilled == []


def test_sparse_limit_rejects_not_silently_truncates():
    with pytest.raises(ValueError, match=str(MAX_PAIRS)):
        optimize(snapshot(pairs=[evidence()] * (MAX_PAIRS + 1)))


def test_duplicate_and_unscoped_pairs_rejected():
    with pytest.raises(ValueError, match="Duplicate"):
        generate_candidates(snapshot(pairs=[evidence(), evidence()]))
    with pytest.raises(ValueError, match="unscoped"):
        generate_candidates(snapshot(pairs=[evidence(staff_id="another-tenant")]))
    with pytest.raises(ValueError, match="different candidate"):
        evaluate_candidate(worker(), order(), evidence(staff_id="other"), [])


def test_shift_and_invalid_inputs_rejected_explicitly():
    with pytest.raises(ValidationError):
        order(scheduling_mode="night_shift")
    with pytest.raises(ValidationError):
        OptimizeRequest(idempotency_key="request-key", scheduling_mode="hourly")
    with pytest.raises(ValidationError):
        Period(start="2026-11-01", end="2026-11-01")
    with pytest.raises(ValidationError):
        worker(skills={"office": -1})
    with pytest.raises(ValidationError):
        order(required_skills={"office": 0})
    with pytest.raises(ValidationError):
        evidence(score=1.1)
    with pytest.raises(ValueError, match="budget"):
        optimize(snapshot(), time_limit_seconds=0)


def test_unknown_solver_uses_explicit_feasible_fallback(monkeypatch):
    monkeypatch.setattr(cp_model.CpSolver, "solve", lambda self, model: cp_model.UNKNOWN)
    result = optimize(snapshot())
    assert result.optimized.filled_slots == result.greedy.filled_slots == 1
    assert result.optimized.method == "greedy_fallback"
    assert result.solver.status == "UNKNOWN"
    assert result.solver.fallback_used
    assert not result.solver.count_optimal and not result.solver.score_optimal
    assert result.solver.objective_bound is None


def test_score_timeout_does_not_claim_optimum(monkeypatch):
    original_solve = cp_model.CpSolver.solve
    calls = 0

    def solve(self, model):
        nonlocal calls
        calls += 1
        return original_solve(self, model) if calls == 1 else cp_model.UNKNOWN

    monkeypatch.setattr(cp_model.CpSolver, "solve", solve)
    result = optimize(snapshot())
    assert result.solver.status == "FEASIBLE"
    assert result.solver.count_optimal and not result.solver.score_optimal
    assert result.solver.score_status == "UNKNOWN"
    assert result.optimized.filled_slots == 1


@pytest.mark.parametrize("status", [cp_model.INFEASIBLE, cp_model.MODEL_INVALID])
def test_solver_failure_not_reported_as_empty_success(monkeypatch, status):
    monkeypatch.setattr(cp_model.CpSolver, "solve", lambda self, model: status)
    with pytest.raises(ValueError, match="no valid solution"):
        optimize(snapshot())


def test_seed_quality_and_reproducibility():
    first = optimize(demo_snapshot())
    second = optimize(demo_snapshot())
    assert first.optimized == second.optimized
    assert first.optimized.filled_slots == 4
    assert first.greedy.filled_slots == 3
    assert first.total_remaining_slots == 5
    assert first.unfilled[0].order_id == "o05"
    assert any(candidate.eligibility == "pending" for candidate in first.candidates)
    assert any(candidate.eligibility == "ineligible" for candidate in first.candidates)


def test_small_random_cases_match_exhaustive_enumeration():
    rng = Random(42)
    for _ in range(24):
        jobs = [
            order(
                f"o{i}",
                headcount=rng.randint(1, 2),
                period=Period(start=f"2026-11-0{1 + i}", end=f"2026-11-0{3 + i}"),
            )
            for i in range(3)
        ]
        staff = [worker(f"s{i}") for i in range(3)]
        pairs = [
            evidence(person.id, job.id, score=rng.randint(1, 10) / 10)
            for person in staff
            for job in jobs
            if rng.random() > 0.3
        ]
        data = snapshot(staff=staff, orders=jobs, pairs=pairs)
        candidates = generate_candidates(data)
        order_map = {job.id: job for job in jobs}
        best = (0, 0)
        for size in range(len(candidates) + 1):
            for chosen in combinations(candidates, size):
                if any(
                    sum(pair.order_id == job.id for pair in chosen) > job.headcount for job in jobs
                ):
                    continue
                if any(
                    left.staff_id == right.staff_id
                    and overlaps(order_map[left.order_id].period, order_map[right.order_id].period)
                    for left, right in combinations(chosen, 2)
                ):
                    continue
                best = max(best, (size, sum(score_units(pair.score) for pair in chosen)))
        result = optimize(data)
        assert (result.optimized.filled_slots, score_units(result.optimized.total_score)) == best
        assert result.optimized.filled_slots >= result.greedy.filled_slots
