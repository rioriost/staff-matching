import pytest
from test_saas_domain import commute, fixture_dataset

from backend.saas_domain import evaluate
from backend.saas_engine import run_matching, skill_vector, solve
from backend.saas_models import Schedule
from backend.store import StoreError


def test_structural_vectors_are_repeatable_and_explicit():
    assert len(skill_vector({"office": 3})) == 32
    assert sum(skill_vector({"office": 3, "data": 2})) == 5
    assert skill_vector({"office": 3, "data": 2}) == skill_vector({"data": 2, "office": 3})


def test_multiple_nonoverlapping_jobs_share_person():
    dataset = fixture_dataset()
    person, job = dataset.people[0], dataset.jobs[0]
    monday = job.model_copy(
        update={
            "id": "mon",
            "schedule": Schedule(weekdays=[0], start_minute=540, end_minute=1020),
        }
    )
    tuesday = job.model_copy(
        update={
            "id": "tue",
            "schedule": Schedule(weekdays=[1], start_minute=540, end_minute=1020),
        }
    )
    candidates = [evaluate(person, j, commute()) for j in (monday, tuesday)]
    status, bound, count = solve([monday, tuesday], candidates, [], [])
    assert status == "OPTIMAL"
    assert bound > 0
    assert count == 2
    assert all(c.recommended for c in candidates)


def test_conflicting_pins_fail_and_existing_placements_consume_capacity():
    dataset = fixture_dataset()
    person, job = dataset.people[0], dataset.jobs[0]
    other = job.model_copy(update={"id": "other"})
    second = person.model_copy(update={"id": "second"})
    candidates = [evaluate(person, j, commute()) for j in (job, other)]
    with pytest.raises(StoreError, match="Pins conflict"):
        solve([job, other], candidates, [], [(job.id, person.id), (other.id, person.id)])
    candidates.append(evaluate(second, job, commute()))
    _, _, count = solve([job, other], candidates, [(person.id, job)], [])
    assert count == 0


def test_sparse_retrieval_expands_only_unfilled_vacancies():
    dataset = fixture_dataset()
    person, job = dataset.people[0], dataset.jobs[0]
    calls = []

    def retrieve(current_job, limit):
        calls.append((current_job.id, limit))
        return [] if limit == 40 else [person]

    solution = run_matching(
        [job],
        1000,
        retrieve,
        lambda p, j: evaluate(p, j, commute()),
        [],
        [],
        lambda _: person,
        lambda *_: None,
    )
    assert calls == [(job.id, 40), (job.id, 80)]
    assert solution.recommended_count == 1
    assert solution.coverage.evaluated_pairs == 1
    assert solution.coverage.truncated
    assert "sparse" in " ".join(solution.coverage.notes)


def test_ineligible_pin_never_bypasses_hard_constraints():
    dataset = fixture_dataset()
    person, job = dataset.people[0], dataset.jobs[0]
    person.skills = {}
    with pytest.raises(StoreError, match="eligible"):
        run_matching(
            [job],
            1,
            lambda *_: [person],
            lambda p, j: evaluate(p, j, commute()),
            [],
            [(job.id, person.id)],
            lambda _: person,
            lambda *_: None,
        )


def test_every_job_keeps_multiple_ranked_options_and_recommendation_is_separate():
    dataset = fixture_dataset()
    people = [dataset.people[0].model_copy(update={"id": f"p{i}"}) for i in range(4)]
    job = dataset.jobs[0]
    solution = run_matching(
        [job],
        4,
        lambda *_: people,
        lambda p, j: evaluate(p, j, commute()),
        [],
        [],
        lambda key: next(p for p in people if p.id == key),
        lambda *_: None,
    )
    assert len(solution.candidates) == 4
    assert solution.recommended_count == 1
    assert all(c.scores.history is None for c in solution.candidates)


def test_sweep_cliques_reject_overnight_and_allow_touching_boundaries():
    dataset = fixture_dataset()
    person, base = dataset.people[0], dataset.jobs[0]
    person.availability = Schedule(
        weekdays=list(range(7)),
        start_minute=0,
        end_minute=1440,
        break_minutes=0,
    )
    jobs = [
        base.model_copy(
            update={
                "id": "night",
                "schedule": Schedule(
                    weekdays=[0],
                    start_minute=1320,
                    end_minute=1800,
                ),
            }
        ),
        base.model_copy(
            update={
                "id": "overlap",
                "schedule": Schedule(
                    weekdays=[1],
                    start_minute=300,
                    end_minute=600,
                ),
            }
        ),
        base.model_copy(
            update={
                "id": "touch",
                "schedule": Schedule(
                    weekdays=[1],
                    start_minute=360,
                    end_minute=600,
                ),
            }
        ),
    ]
    candidates = [evaluate(person, job, commute()) for job in jobs]
    _, _, count = solve(jobs, candidates, [], [("night", person.id)])
    assert count == 2
    assert {c.job_id for c in candidates if c.recommended} == {"night", "touch"}


def test_scope_notes_distinguish_open_job_records_from_headcount_slots():
    base = fixture_dataset().jobs[0]
    jobs = [
        base.model_copy(update={"id": "one", "headcount": 2}),
        base.model_copy(update={"id": "two", "headcount": 3}),
    ]
    solution = run_matching(
        jobs,
        0,
        lambda *_: [],
        lambda *_: None,
        [],
        [],
        lambda *_: None,
        lambda *_: None,
    )
    assert solution.coverage.jobs == 2
    assert "2 admitted open job records, 5 headcount slots" in solution.coverage.notes[0]
    assert "Closed jobs are excluded by admission" in solution.coverage.notes[0]
