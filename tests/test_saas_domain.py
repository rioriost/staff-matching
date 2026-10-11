from datetime import date, timedelta

import pytest

from backend.saas_domain import (
    available,
    commute_valid,
    evaluate,
    hard_rejections,
    next_simulated_state,
    schedules_conflict,
    shift_intervals,
)
from backend.saas_models import (
    Area,
    Commute,
    Company,
    Credential,
    Dataset,
    Employment,
    Evaluation,
    Job,
    PersonRecord,
    Point,
    Schedule,
    Site,
)
from backend.store import StoreError


def fixture_dataset(tenant="unit-tenant"):
    area = Area(id="town", label="架空エリア", point=Point(lat=35.68, lon=139.76))
    company = Company(
        id="company",
        name="架空会社",
        industry="事務",
        description="合成テスト",
        contact_email="company@example.invalid",
    )
    site = Site(
        id="site",
        company_id=company.id,
        name="架空事業所",
        kind="worksite",
        address="架空住所",
        area=area,
        point=area.point,
        stations=[],
        parking=True,
        accessibility="入口段差なし",
    )
    schedule = Schedule(weekdays=[0, 1, 2, 3, 4], start_minute=540, end_minute=1020)
    job = Job(
        id="job",
        company_id=company.id,
        company_name=company.name,
        site_id=site.id,
        site=site,
        title="合成事務求人",
        occupation="office",
        duties="架空帳票の確認",
        business_type="placement",
        employment_type="有期",
        headcount=1,
        start_date=date(2027, 1, 4),
        end_date=date(2027, 4, 1),
        schedule=schedule,
        hourly_pay_min=1500,
        hourly_pay_max=1800,
        required_skills={"office": 2},
        required_qualifications=[],
        preferred_qualifications=[],
        experience_months=0,
        renewal="更新なし",
        duties_change_scope="変更なし",
        location_change_scope="変更なし",
        overtime="なし",
        holidays="土日",
        allowances="交通費",
        benefits=["社会保険"],
        application="デモ画面",
    )
    person = PersonRecord(
        id="person",
        name="架空候補",
        occupation="office",
        home_area=area,
        stations=[],
        skills={"office": 3},
        qualifications=[],
        available_from=date(2027, 1, 1),
        available_until=date(2027, 12, 31),
        availability=schedule,
        desired_hourly_pay=1500,
        preferred_occupations=["office"],
        commute_modes=["transit", "car"],
        driving_eligible=True,
        max_commute_minutes=120,
        employment_history=[],
        private_location=Point(lat=35.68123, lon=139.76234),
        private_address="非公開の架空住居",
        contact_email="private@example.invalid",
    )
    return Dataset(
        tenant_id=tenant,
        version="unit-v1",
        companies=[company],
        sites=[site],
        jobs=[job],
        people=[person],
    )


def commute(seconds=1200, **updates):
    return Commute(
        status="estimated",
        mode="transit",
        duration_seconds=seconds,
        eligible=True,
        summary="合成経路",
        network_version="unit-v1",
        assumptions_version="unit-v1",
    ).model_copy(update=updates)


def test_private_record_never_public():
    person = fixture_dataset().people[0]
    public = person.public().model_dump_json()
    assert "private" not in public
    assert "35.68123" not in public
    assert "非公開" not in public


@pytest.mark.parametrize(
    "seconds,max_minutes,expected",
    [
        (10799, 180, True),
        (10800, 240, False),
        (10801, 240, False),
        (3600, 60, True),
        (3601, 60, False),
        (0, 60, True),
    ],
)
def test_exact_commute_bounds(seconds, max_minutes, expected):
    dataset = fixture_dataset()
    person = dataset.people[0].model_copy(update={"max_commute_minutes": max_minutes})
    assert commute_valid(person, dataset.jobs[0], commute(seconds)) == expected


@pytest.mark.parametrize(
    "updates",
    [
        {"status": "unknown"},
        {"status": "unreachable"},
        {"duration_seconds": None},
        {"eligible": False},
        {"mode": None},
    ],
)
def test_unknown_routes_excluded(updates):
    dataset = fixture_dataset()
    assert not commute_valid(dataset.people[0], dataset.jobs[0], commute(**updates))


def test_car_requires_license_mode_and_parking():
    dataset = fixture_dataset()
    person, job = dataset.people[0], dataset.jobs[0]
    car = commute(mode="car")
    assert commute_valid(person, job, car)
    assert not commute_valid(person.model_copy(update={"driving_eligible": False}), job, car)
    assert not commute_valid(person.model_copy(update={"commute_modes": ["transit"]}), job, car)
    assert not commute_valid(
        person,
        job.model_copy(
            update={
                "site": job.site.model_copy(update={"parking": False}),
            }
        ),
        car,
    )


def test_dates_weekdays_and_overnight_are_actual_conflicts():
    job = fixture_dataset().jobs[0]
    overnight = job.model_copy(
        update={
            "schedule": Schedule(weekdays=[0], start_minute=1320, end_minute=1800),
        }
    )
    early = job.model_copy(
        update={
            "id": "early",
            "schedule": Schedule(weekdays=[1], start_minute=300, end_minute=600),
        }
    )
    boundary = early.model_copy(
        update={
            "schedule": Schedule(weekdays=[1], start_minute=360, end_minute=600),
        }
    )
    assert schedules_conflict(overnight, early)
    assert not schedules_conflict(overnight, boundary)
    assert not schedules_conflict(
        job,
        job.model_copy(
            update={
                "start_date": job.end_date,
                "end_date": date(2027, 7, 1),
            }
        ),
    )
    monday_only = overnight.model_copy(update={"end_date": date(2027, 1, 5)})
    assert not schedules_conflict(monday_only, early)
    intervals = list(
        shift_intervals(monday_only.start_date, monday_only.end_date, monday_only.schedule)
    )
    assert intervals[0][1].date() == monday_only.end_date
    assert intervals[0][1].hour == 0


def test_availability_contains_whole_overnight_shift():
    dataset = fixture_dataset()
    job = dataset.jobs[0].model_copy(
        update={
            "schedule": Schedule(weekdays=[0], start_minute=1380, end_minute=1560, break_minutes=0),
        }
    )
    person = dataset.people[0].model_copy(
        update={
            "availability": Schedule(
                weekdays=[0],
                start_minute=1320,
                end_minute=1680,
                break_minutes=0,
            ),
        }
    )
    assert available(person, job)
    assert not available(
        person.model_copy(
            update={
                "availability": Schedule(
                    weekdays=[0],
                    start_minute=1320,
                    end_minute=1440,
                    break_minutes=0,
                ),
            }
        ),
        job,
    )


def test_touching_full_day_availability_windows_cover_overnight_work():
    dataset = fixture_dataset()
    person, job = dataset.people[0], dataset.jobs[0]
    person.availability = Schedule(
        weekdays=list(range(7)),
        start_minute=0,
        end_minute=1440,
        break_minutes=0,
    )
    job.schedule = Schedule(weekdays=[0], start_minute=1320, end_minute=1800)
    assert available(person, job)


def test_history_missing_is_null_and_observations_are_dated():
    dataset = fixture_dataset()
    job, person = dataset.jobs[0], dataset.people[0]
    candidate = evaluate(person, job, commute())
    assert candidate.eligible
    assert candidate.scores.history is None
    assert candidate.evidence == []
    person.employment_history = [
        Employment(
            employer="架空職場",
            occupation="office",
            start_date=date(2024, 1, 1),
            end_date=date(2026, 5, 1),
            duties="帳票確認",
            evaluations=[
                Evaluation(
                    recorded_at=date(2026, 4, 1),
                    observation="確認表を完了",
                    dimension="quality",
                    score=4,
                    source="合成記録A",
                ),
                Evaluation(
                    recorded_at=date(2027, 1, 1),
                    observation="未来の観測",
                    dimension="quality",
                    score=1,
                    source="未来",
                ),
            ],
        )
    ]
    candidate = evaluate(person, job, commute())
    assert candidate.scores.history == 80
    assert len(candidate.evidence) == 1
    assert "2026-04-01 | 合成記録A | quality" in candidate.evidence[0]


def test_expired_and_unverified_qualification_hard_filter():
    dataset = fixture_dataset()
    person, job = dataset.people[0], dataset.jobs[0]
    job.required_qualifications = ["資格"]
    person.qualifications = [
        Credential(
            name="資格",
            verified=True,
            valid_until=job.end_date - timedelta(days=1),
        )
    ]
    assert not hard_rejections(person, job)
    person.qualifications[0].valid_until -= timedelta(days=1)
    assert "required_qualifications" in hard_rejections(person, job)
    person.qualifications[0].valid_until = None
    person.qualifications[0].verified = False
    assert "required_qualifications" in hard_rejections(person, job)


def test_state_machine_and_dispatch_are_enforced():
    assert next_simulated_state("shortlisted", "company_request", "placement") == "requested"
    assert next_simulated_state("requested", "person_consent", "placement") == "consented"
    assert (
        next_simulated_state("interview_accepted", "withdraw_consent", "placement") == "cancelled"
    )
    with pytest.raises(StoreError, match="dispatch"):
        next_simulated_state("shortlisted", "company_request", "dispatch")
    with pytest.raises(StoreError, match="current state"):
        next_simulated_state("shortlisted", "person_consent", "placement")


def test_schedule_repeating_optimization_agrees_with_full_enumeration():
    import random

    randomizer = random.Random(82)
    job = fixture_dataset().jobs[0]
    for index in range(200):
        shifts = []
        for which in range(2):
            start = randomizer.randint(0, 1300)
            schedule = Schedule(
                weekdays=randomizer.sample(range(7), randomizer.randint(1, 7)),
                start_minute=start,
                end_minute=start + randomizer.randint(61, 1000),
            )
            first = date(2027, 1, 1) + timedelta(days=randomizer.randint(0, 20))
            last = first + timedelta(days=randomizer.randint(1, 90))
            shifts.append(
                job.model_copy(
                    update={
                        "id": f"{index}-{which}",
                        "start_date": first,
                        "end_date": last,
                        "schedule": schedule,
                    }
                )
            )
        left, right = shifts
        exhaustive = any(
            a < d and c < b
            for a, b in shift_intervals(left.start_date, left.end_date, left.schedule)
            for c, d in shift_intervals(right.start_date, right.end_date, right.schedule)
        )
        assert schedules_conflict(left, right) == exhaustive


def test_availability_matches_full_contract_interval_union():
    import random

    randomizer = random.Random(31)
    dataset = fixture_dataset()
    for _ in range(120):
        shifts = []
        for _which in range(2):
            minute = randomizer.randrange(1200)
            shifts.append(
                Schedule(
                    weekdays=randomizer.sample(range(7), randomizer.randint(1, 7)),
                    start_minute=minute,
                    end_minute=minute + randomizer.randint(61, 1440),
                )
            )
        first = date(2027, 1, 1) + timedelta(days=randomizer.randrange(20))
        last = first + timedelta(days=randomizer.randint(1, 80))
        job = dataset.jobs[0].model_copy(
            update={
                "start_date": first,
                "end_date": last,
                "schedule": shifts[0],
            }
        )
        person = dataset.people[0].model_copy(
            update={
                "available_from": first - timedelta(days=randomizer.randrange(3)),
                "available_until": last + timedelta(days=randomizer.randrange(3)),
                "availability": shifts[1],
            }
        )
        windows = []
        for start, end in shift_intervals(
            person.available_from,
            person.available_until,
            person.availability,
        ):
            if windows and windows[-1][1] >= start:
                windows[-1] = (windows[-1][0], max(windows[-1][1], end))
            else:
                windows.append((start, end))
        job_intervals = list(shift_intervals(first, last, job.schedule))
        expected = bool(job_intervals) and all(
            any(a <= start and end <= b for a, b in windows) for start, end in job_intervals
        )
        assert available(person, job) == expected
