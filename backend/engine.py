"""Sparse, full-period matching; hard eligibility never trades off against score."""

from collections import Counter, defaultdict
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from time import monotonic

import ortools
from ortools.sat.python import cp_model

from backend.models import (
    Allocation,
    Assignment,
    Candidate,
    OptimizationResult,
    Order,
    Pair,
    PairEvidence,
    Period,
    Reason,
    ScoreParts,
    Snapshot,
    SolverReport,
    Staff,
    Unfilled,
)

MAX_PAIRS = 2000
SCORE_SCALE = 10000


def overlaps(left: Period, right: Period) -> bool:
    return left.start < right.end and right.start < left.end


def score_units(score: float) -> int:
    return int((Decimal(str(score)) * SCORE_SCALE).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def evaluate_candidate(
    staff: Staff,
    order: Order,
    evidence: PairEvidence | None,
    assignments: list[Assignment],
) -> Candidate:
    reasons: list[Reason] = []

    def reason(code: str, message: str, kind: str) -> None:
        reasons.append(Reason(code=code, message=message, kind=kind))

    def known_flag(value: bool | None, code: str, label: str) -> None:
        if value is None:
            reason(code, f"{label}が未確認です", "unknown")
        elif not value:
            reason(code, f"{label}を満たしていません", "violation")
        else:
            reason(code, f"{label}を確認済み（合成データ）", "passed")

    known_flag(staff.matching_enabled, "matching_opt_in", "本人の紹介希望")
    known_flag(staff.eligibility_verified, "staff_eligibility", "スタッフの適格性")
    known_flag(order.eligibility_verified, "order_eligibility", "案件の適格性")
    known_flag(staff.mandatory_verified, "staff_information", "スタッフの必須情報")
    known_flag(order.mandatory_verified, "order_information", "案件の必須情報")
    if staff.availability is None:
        reason("availability", "勤務可能期間が未確認です", "unknown")
    elif staff.availability.start > order.period.start or staff.availability.end < order.period.end:
        reason("availability", "勤務可能期間が案件の全期間を含みません", "violation")
    else:
        reason("availability", "勤務可能期間が案件の全期間を含みます", "passed")
    if staff.skills is None:
        reason("skills", "構造化スキルが未確認です", "unknown")
    else:
        missing = [
            code
            for code, level in order.required_skills.items()
            if staff.skills.get(code, 0) < level
        ]
        reason(
            "skills",
            f"必須スキル不足: {', '.join(missing)}" if missing else "必須の構造化スキルを充足",
            "violation" if missing else "passed",
        )
    for code in order.required_qualifications:
        if staff.qualifications is None:
            reason("qualification", f"資格 {code} の保有状況が未確認です", "unknown")
            continue
        qualification = staff.qualifications.get(code)
        if qualification is None:
            reason("qualification", f"必須資格 {code} がありません", "violation")
        elif not qualification.verified:
            reason("qualification", f"資格 {code} の確認記録がありません", "unknown")
        elif qualification.valid_until < order.period.end - timedelta(days=1):
            reason("qualification", f"資格 {code} が就業期間中に失効します", "violation")
        else:
            reason("qualification", f"資格 {code} は全就業期間で有効です", "passed")
    if staff.min_hourly_rate is None:
        reason("hourly_rate", "最低希望時給が未確認です", "unknown")
    elif order.hourly_rate < staff.min_hourly_rate:
        reason("hourly_rate", "案件の時給が最低希望時給を下回ります", "violation")
    else:
        reason("hourly_rate", "最低希望時給を満たします", "passed")
    if evidence is not None and (evidence.staff_id, evidence.order_id) != (staff.id, order.id):
        raise ValueError("Evidence references a different candidate pair.")
    commute = evidence.commute_minutes if evidence else None
    if staff.commute_limit_minutes is None:
        reason("commute_limit", "本人の通勤上限が未確認です", "unknown")
    if evidence is None or commute is None or not evidence.commute_verified:
        reason("commute", "通勤時間が未確認です。自動割当はできません", "unknown")
    elif staff.commute_limit_minutes is not None:
        if commute > staff.commute_limit_minutes:
            reason("commute", "通勤時間（合成値）が本人の上限を超えます", "violation")
        else:
            reason("commute", f"通勤 {commute} 分（合成値）は希望上限内です", "passed")
    if sum(item.order_id == order.id for item in assignments) >= order.headcount:
        reason("capacity", "この案件の確定済み人数が募集人数に達しています", "violation")
    if any(
        item.staff_id == staff.id and overlaps(item.period, order.period) for item in assignments
    ):
        reason("confirmed_conflict", "既存の確定割当と期間が重なります", "violation")

    kinds = {item.kind for item in reasons}
    eligibility = (
        "ineligible" if "violation" in kinds else "pending" if "unknown" in kinds else "eligible"
    )
    parts = None
    score = None
    if eligibility == "eligible":
        if evidence is None or commute is None or staff.commute_limit_minutes is None:
            raise ValueError("Eligible pair must have known commute evidence.")
        history = evidence.history_fit if evidence.rated_assignments else None
        commute_fit = 1 - commute / staff.commute_limit_minutes
        parts = ScoreParts(
            skill=evidence.skill_fit,
            preference=evidence.preference_fit,
            commute=commute_fit,
            history=history,
            history_missing=history is None,
            rated_assignments=evidence.rated_assignments,
        )
        score = (
            0.4 * parts.skill
            + 0.2 * parts.preference
            + 0.2 * parts.commute
            + (0.2 * history if history is not None else 0)
        ) / (0.8 if history is None else 1.0)
        score = score_units(score) / SCORE_SCALE
        if history is None:
            reason("history_missing", "履歴なし。履歴の重みを外して再正規化します", "information")
    explanation = (
        "必須条件を満たす候補です。適合度は合成値による参考スコアで、決定確率ではありません。"
        if eligibility == "eligible"
        else "必須条件の違反または未確認があるため、自動割当の対象外です。"
    )
    return Candidate(
        staff_id=staff.id,
        order_id=order.id,
        eligibility=eligibility,
        score=score,
        score_parts=parts,
        commute_minutes=commute,
        reasons=reasons,
        explanation=explanation,
    )


def validate_snapshot(snapshot: Snapshot) -> None:
    if len(snapshot.evidence) > MAX_PAIRS:
        raise ValueError(f"Local mock accepts at most {MAX_PAIRS} supplied candidate pairs.")
    if len(snapshot.staff) > 500 or len(snapshot.orders) > 100:
        raise ValueError("Local mock is limited to 500 staff and 100 orders.")
    staff = {item.id: item for item in snapshot.staff}
    orders = {item.id: item for item in snapshot.orders}
    if len(staff) != len(snapshot.staff) or len(orders) != len(snapshot.orders):
        raise ValueError("Duplicate staff or order identifiers.")
    pairs: set[tuple[str, str]] = set()
    for evidence in snapshot.evidence:
        pair = (evidence.staff_id, evidence.order_id)
        if pair in pairs or pair[0] not in staff or pair[1] not in orders:
            raise ValueError("Duplicate or unscoped evidence reference.")
        pairs.add(pair)
    counts: Counter[str] = Counter()
    busy: dict[str, list[Period]] = defaultdict(list)
    assignment_ids: set[str] = set()
    for assignment in snapshot.assignments:
        if assignment.staff_id not in staff or assignment.order_id not in orders:
            raise ValueError("Assignment references an unknown staff or order.")
        if assignment.id in assignment_ids:
            raise ValueError("Duplicate assignment identifiers.")
        assignment_ids.add(assignment.id)
        order = orders[assignment.order_id]
        if assignment.period != order.period:
            raise ValueError("Confirmed assignment must occupy the full order period.")
        counts[order.id] += 1
        if counts[order.id] > order.headcount:
            raise ValueError("Preexisting assignments exceed order capacity.")
        if any(overlaps(assignment.period, period) for period in busy[assignment.staff_id]):
            raise ValueError("Preexisting assignments double-book staff.")
        busy[assignment.staff_id].append(assignment.period)


def generate_candidates(snapshot: Snapshot) -> list[Candidate]:
    validate_snapshot(snapshot)
    staff = {item.id: item for item in snapshot.staff}
    orders = {item.id: item for item in snapshot.orders}
    return [
        evaluate_candidate(staff[item.staff_id], orders[item.order_id], item, snapshot.assignments)
        for item in sorted(snapshot.evidence, key=lambda item: (item.order_id, item.staff_id))
    ]


def _allocation(method: str, chosen: list[Candidate]) -> Allocation:
    return Allocation(
        method=method,
        pairs=[Pair(staff_id=item.staff_id, order_id=item.order_id) for item in chosen],
        filled_slots=len(chosen),
        total_score=sum(score_units(item.score or 0) for item in chosen) / SCORE_SCALE,
    )


def _quality(chosen: list[Candidate]) -> tuple[int, int]:
    return len(chosen), sum(score_units(item.score or 0) for item in chosen)


def greedy_feasible(snapshot: Snapshot, candidates: list[Candidate]) -> list[Candidate]:
    remaining = {order.id: order.headcount for order in snapshot.orders}
    busy: dict[str, list[Period]] = defaultdict(list)
    for assignment in snapshot.assignments:
        remaining[assignment.order_id] -= 1
        busy[assignment.staff_id].append(assignment.period)
    chosen = []
    for order in sorted(snapshot.orders, key=lambda item: item.id):
        available = sorted(
            [
                item
                for item in candidates
                if item.order_id == order.id and item.eligibility == "eligible"
            ],
            key=lambda item: (-score_units(item.score or 0), item.staff_id),
        )
        for candidate in available:
            if remaining[order.id] <= 0:
                break
            if any(overlaps(order.period, period) for period in busy[candidate.staff_id]):
                continue
            chosen.append(candidate)
            busy[candidate.staff_id].append(order.period)
            remaining[order.id] -= 1
    return chosen


def optimize(snapshot: Snapshot, time_limit_seconds: float = 3.0) -> OptimizationResult:
    if not 0.05 <= time_limit_seconds <= 10:
        raise ValueError("Solver budget must be between 0.05 and 10 seconds.")
    started = monotonic()
    candidates = generate_candidates(snapshot)
    eligible = [candidate for candidate in candidates if candidate.eligibility == "eligible"]
    baseline = greedy_feasible(snapshot, candidates)
    orders = {order.id: order for order in snapshot.orders}
    confirmed = Counter(item.order_id for item in snapshot.assignments)
    remaining = {order.id: order.headcount - confirmed[order.id] for order in snapshot.orders}
    model = cp_model.CpModel()
    variables = [model.new_bool_var(f"pair_{index}") for index in range(len(eligible))]
    by_order: dict[str, list[int]] = defaultdict(list)
    by_staff: dict[str, list[int]] = defaultdict(list)
    for index, candidate in enumerate(eligible):
        by_order[candidate.order_id].append(index)
        by_staff[candidate.staff_id].append(index)
    for order_id, indices in by_order.items():
        model.add(sum(variables[index] for index in indices) <= remaining[order_id])
    for indices in by_staff.values():
        # Every interval-overlap clique is active at an interval start; no daily expansion.
        starts = sorted({orders[eligible[index].order_id].period.start for index in indices})
        for start in starts:
            active = [
                variables[index]
                for index in indices
                if orders[eligible[index].order_id].period.start
                <= start
                < orders[eligible[index].order_id].period.end
            ]
            if len(active) > 1:
                model.add(sum(active) <= 1)
    count = sum(variables)
    score = sum(
        variable * score_units(candidate.score or 0)
        for variable, candidate in zip(variables, eligible, strict=True)
    )
    baseline_pairs = {(item.staff_id, item.order_id) for item in baseline}
    for variable, candidate in zip(variables, eligible, strict=True):
        model.add_hint(variable, int((candidate.staff_id, candidate.order_id) in baseline_pairs))

    def solve() -> tuple[cp_model.CpSolver, cp_model.CpSolverStatus]:
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = max(
            0.000001, time_limit_seconds - (monotonic() - started)
        )
        solver.parameters.num_search_workers = 1
        solver.parameters.random_seed = 0
        return solver, solver.solve(model)

    def selected(solver: cp_model.CpSolver) -> list[Candidate]:
        return [
            candidate
            for variable, candidate in zip(variables, eligible, strict=True)
            if solver.value(variable)
        ]

    model.maximize(count)
    first, first_status = solve()
    count_status = first.status_name(first_status)
    if first_status in (cp_model.INFEASIBLE, cp_model.MODEL_INVALID):
        raise ValueError(f"Constraint solver has no valid solution: {count_status}.")
    count_optimal = first_status == cp_model.OPTIMAL
    chosen = selected(first) if first_status in (cp_model.OPTIMAL, cp_model.FEASIBLE) else []
    bound = (
        first.best_objective_bound
        if first_status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
        else None
    )
    score_status = None
    score_optimal = False
    status = count_status
    if count_optimal and monotonic() - started < time_limit_seconds:
        model.add(count == len(chosen))
        model.maximize(score)
        model.clear_hints()
        chosen_pairs = {(item.staff_id, item.order_id) for item in chosen}
        for variable, candidate in zip(variables, eligible, strict=True):
            model.add_hint(variable, int((candidate.staff_id, candidate.order_id) in chosen_pairs))
        second, second_status = solve()
        score_status = second.status_name(second_status)
        if second_status in (cp_model.INFEASIBLE, cp_model.MODEL_INVALID):
            raise ValueError(f"Second solver phase failed: {score_status}.")
        if second_status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            chosen = selected(second)
        score_optimal = second_status == cp_model.OPTIMAL
        status = "OPTIMAL" if score_optimal else "FEASIBLE"
    elif count_optimal:
        status = "FEASIBLE"
    fallback = _quality(baseline) > _quality(chosen) or first_status == cp_model.UNKNOWN
    if fallback:
        chosen = baseline
        score_optimal = False
    selected_pairs = {(candidate.staff_id, candidate.order_id) for candidate in chosen}
    for candidate in candidates:
        candidate.selected = (candidate.staff_id, candidate.order_id) in selected_pairs
        if candidate.selected:
            candidate.explanation += (
                " この入力候補集合で、追加充足枠数を優先する割当提案に選ばれました。"
                "本人承諾・担当者確定はまだ完了していません。"
            )
        elif candidate.eligibility == "eligible":
            candidate.explanation += " 残枠・他案件との期間競合・適合度を比較し、今回は未選択です。"
    chosen_counts = Counter(candidate.order_id for candidate in chosen)
    unfilled = []
    for order in sorted(snapshot.orders, key=lambda item: item.id):
        slots = remaining[order.id] - chosen_counts[order.id]
        if slots <= 0:
            continue
        pool = [candidate for candidate in candidates if candidate.order_id == order.id]
        eligible_count = sum(candidate.eligibility == "eligible" for candidate in pool)
        pending_count = sum(candidate.eligibility == "pending" for candidate in pool)
        if not pool:
            message = "登録された疎な候補ペアがありません（全スタッフ探索ではありません）"
        elif not eligible_count:
            message = "必須条件が未確認の候補のみ、または必須条件を満たす候補がありません"
        elif not count_optimal:
            message = "時間上限内の暫定割当です。未充足が不可避とは証明されていません"
        else:
            message = "候補人数不足または他案件との期間競合により、追加充足できません"
        unfilled.append(
            Unfilled(
                order_id=order.id,
                slots=slots,
                reason=message,
                eligible_candidates=eligible_count,
                pending_candidates=pending_count,
            )
        )
    return OptimizationResult(
        candidates=candidates,
        optimized=_allocation("greedy_fallback" if fallback else "cp_sat_lexicographic", chosen),
        greedy=_allocation("greedy_feasible_order_id", baseline),
        solver=SolverReport(
            status=status,
            count_status=count_status,
            score_status=score_status,
            count_optimal=count_optimal,
            score_optimal=score_optimal,
            objective_bound=bound,
            gap=max(0, bound - len(chosen)) if bound is not None else None,
            elapsed_ms=round((monotonic() - started) * 1000, 3),
            time_limit_seconds=time_limit_seconds,
            engine=f"OR-Tools CP-SAT {ortools.__version__}",
            fallback_used=fallback,
        ),
        unfilled=unfilled,
        candidate_count=len(candidates),
        total_remaining_slots=sum(remaining.values()),
        confirmed_slots=len(snapshot.assignments),
        input_revision=snapshot.revision,
    )
