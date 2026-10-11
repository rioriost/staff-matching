import random
import re
from collections import Counter, defaultdict

import pytest

from backend.saas_data import DATASET_VERSION, TENANT_IDS, dataset_provenance, generate_tenant
from backend.saas_domain import hard_rejections
from backend.saas_models import Person
from backend.saas_routing import load_network


@pytest.fixture(scope="module")
def dataset():
    return generate_tenant("demo")


def test_deterministic_versioned_generation_and_stable_prefix(dataset):
    again = generate_tenant("demo")
    assert dataset.model_dump() == again.model_dump()
    prefix = generate_tenant("demo", count=12)
    assert dataset.version == DATASET_VERSION
    assert prefix.jobs == dataset.jobs[:12]
    assert prefix.people == dataset.people[:12]
    assert dataset_provenance()["original_synthetic_text"]


def test_all_ten_tenants_have_exact_counts_and_globally_unique_identifiers():
    seen_jobs, seen_people, seen_companies, seen_sites = set(), set(), set(), set()
    totals = Counter()
    assert TENANT_IDS == (
        "demo",
        "demo02",
        "demo03",
        "demo04",
        "demo05",
        "demo06",
        "demo07",
        "demo08",
        "demo09",
        "demo10",
    )
    for tenant in TENANT_IDS:
        data = generate_tenant(tenant)
        surnames_by_occupation = defaultdict(Counter)
        weekday_surnames = defaultdict(Counter)
        jobs_by_occupation = defaultdict(list)
        for person in data.people:
            surname = person.name.split()[0]
            surnames_by_occupation[person.occupation][surname] += 1
            if set(range(5)) <= set(person.availability.weekdays):
                weekday_surnames[person.occupation][surname] += 1
        for job in data.jobs:
            jobs_by_occupation[job.occupation].append(job)
        assert len({person.name for person in data.people}) == 1000
        for occupation, counts in surnames_by_occupation.items():
            assert sum(counts.values()) == 50
            assert len(counts) >= 20, (tenant, occupation, counts)
            assert max(counts.values()) <= 9, (tenant, occupation, counts)
            weekday_counts = weekday_surnames[occupation]
            assert sum(weekday_counts.values()) >= 20
            assert len(weekday_counts) >= 12, (tenant, occupation, weekday_counts)
            jobs = jobs_by_occupation[occupation]
            assert len(jobs) == 50
            assert len({job.experience_months for job in jobs}) >= 3
            assert len({job.hourly_pay_min for job in jobs}) >= 4
        assert (
            sum(
                job.occupation == person.occupation
                for job, person in zip(data.jobs, data.people, strict=True)
            )
            < 100
        )
        for label, values, seen, expected in (
            ("jobs", data.jobs, seen_jobs, 1000),
            ("people", data.people, seen_people, 1000),
            ("companies", data.companies, seen_companies, 20),
            ("sites", data.sites, seen_sites, 60),
        ):
            identifiers = {value.id for value in values}
            assert len(values) == len(identifiers) == expected
            assert not seen.intersection(identifiers)
            assert all(value.startswith(tenant + "-") for value in identifiers)
            seen.update(identifiers)
            totals[label] += len(values)
    assert totals == {"jobs": 10000, "people": 10000, "companies": 200, "sites": 600}


def test_rich_original_jobs_and_company_owned_multiple_sites(dataset):
    assert len({job.title for job in dataset.jobs}) == 1000
    assert len({job.duties for job in dataset.jobs}) == 1000
    assert len({job.occupation for job in dataset.jobs}) == 20
    assert {job.business_type for job in dataset.jobs} == {"dispatch", "placement"}
    assert {job.status for job in dataset.jobs} == {"open", "closed"}
    for company in dataset.companies:
        assert company.contact_email.endswith(".invalid")
        owned = [site for site in dataset.sites if site.company_id == company.id]
        assert {site.kind for site in owned} == {"headquarters", "branch", "worksite"}
    regions = set()
    for site in dataset.sites:
        assert len(site.stations) >= 2
        assert len({option.station_id for option in site.stations}) == len(site.stations)
        assert site.accessibility and "架空" in site.name
        regions.add(site.area.label[:3])
    assert regions >= {"東京都", "千葉県", "埼玉県", "神奈川"}
    for job in dataset.jobs:
        assert job.start_date < job.end_date
        assert job.hourly_pay_min >= 1600
        assert job.hourly_pay_max >= job.hourly_pay_min
        assert job.required_skills
        for field in (
            "renewal",
            "duties_change_scope",
            "location_change_scope",
            "overtime",
            "holidays",
            "allowances",
            "benefits",
            "application",
        ):
            assert getattr(job, field)
        assert job.site.company_id == job.company_id
        assert job.site.id == job.site_id
    assert any(job.schedule.end_minute > 1440 for job in dataset.jobs)
    assert any(job.experience_months == 0 for job in dataset.jobs)


def test_people_include_beginners_missing_mixed_and_old_evidence(dataset):
    assert len({person.name for person in dataset.people}) == 1000
    assert any(not person.employment_history for person in dataset.people)
    histories = [history for person in dataset.people for history in person.employment_history]
    assert any(not history.evaluations for history in histories)
    evaluations = [evaluation for history in histories for evaluation in history.evaluations]
    assert any(evaluation.score is None for evaluation in evaluations)
    assert {evaluation.score for evaluation in evaluations if evaluation.score} >= {2, 3, 4, 5}
    assert any(evaluation.recorded_at.year <= 2023 for evaluation in evaluations)
    assert any(evaluation.recorded_at.year == 2026 for evaluation in evaluations)
    credentials = [credential for person in dataset.people for credential in person.qualifications]
    assert any(not credential.verified for credential in credentials)
    assert any(
        credential.valid_until and credential.valid_until.year == 2026 for credential in credentials
    )
    assert any(credential.valid_until is None and credential.verified for credential in credentials)
    for person in dataset.people:
        assert person.contact_email.endswith(".invalid")
        assert person.available_from < person.available_until
        assert all(history.start_date < history.end_date for history in person.employment_history)
        assert all(
            history.start_date <= evaluation.recorded_at < history.end_date
            for history in person.employment_history
            for evaluation in history.evaluations
        )
        assert all(1 <= level <= 5 for level in person.skills.values())


def test_public_person_dto_has_only_constant_area_representatives(dataset):
    representatives = {}
    for person in dataset.people:
        public = person.public()
        assert isinstance(public, Person)
        payload = public.model_dump()
        assert not {"private_location", "private_address", "contact_email"}.intersection(payload)
        assert "contact_email" not in public.model_dump_json()
        point = public.home_area.point.model_dump_json()
        assert representatives.setdefault(public.home_area.id, point) == point
        assert public.home_area.point != person.private_location
    assert len(representatives) >= 8
    assert dataset_provenance()["source_personal_data_or_job_ads"] is False


def test_generated_points_attach_to_public_connected_network(dataset):
    network = load_network()
    for site in dataset.sites:
        node = network.road_node(site.point, "walk")
        assert node is not None
        assert network.road_node(site.point, "car") is not None
        for access in site.stations:
            assert node in network.station_walks(access.station_id)
            assert node in network.station_walks(access.station_id, towards_station=True)
    for person in dataset.people:
        assert network.road_node(person.private_location, "walk") is not None


@pytest.mark.parametrize(
    "tenant",
    [
        "validation-saas-b240a01c-5c7c-4e98-bc46-252fed272731-a",
        "validation-saas-b240a01c-5c7c-4e98-bc46-252fed272731-b",
        "demo01",
        "demo11",
        " 顧客/検証@example ",
        "tenant-" + "x" * 100,
    ],
)
def test_arbitrary_tenants_are_deterministic_without_forced_same_index_matches(tenant):
    data = generate_tenant(tenant, count=30)
    assert data == generate_tenant(tenant, count=30)
    assert data.tenant_id == tenant
    assert len(data.people) == len(data.jobs) == 30
    assert all(record.id.startswith(tenant + "-") for record in (*data.jobs, *data.people))
    assert (
        sum(
            person.occupation == job.occupation
            for person, job in zip(data.people, data.jobs, strict=True)
        )
        < 10
    )
    for record in (*data.companies, *data.people):
        assert re.fullmatch(r"[a-z0-9.]+@[a-z0-9-]+\.example\.invalid", record.contact_email)


def test_names_have_no_effect_on_profiles_or_jobs(monkeypatch):
    from backend import saas_data

    before = generate_tenant("demo08", count=50)
    monkeypatch.setattr(saas_data, "SURNAMES", tuple(reversed(saas_data.SURNAMES)))
    after = generate_tenant("demo08", count=50)
    assert before.jobs == after.jobs
    assert before.sites == after.sites
    assert all(a.name != b.name for a, b in zip(before.people, after.people, strict=True))
    assert [person.model_dump(exclude={"name"}) for person in before.people] == [
        person.model_dump(exclude={"name"}) for person in after.people
    ]


def test_generation_does_not_consume_global_random_state():
    state = random.getstate()
    generate_tenant("demo", count=12)
    assert random.getstate() == state


def test_structurally_eligible_candidates_keep_surname_diversity(dataset):
    groups = defaultdict(list)
    for person in dataset.people:
        groups[person.occupation].append(person)
    checked = 0
    for job in dataset.jobs:
        if job.status != "open":
            continue
        candidates = [
            person for person in groups[job.occupation] if not hard_rejections(person, job)
        ]
        if len(candidates) < 8:
            continue
        surnames = Counter(person.name.split()[0] for person in candidates)
        assert len(surnames) >= 5
        assert max(surnames.values()) <= len(candidates) / 2
        checked += 1
        if checked == 20:
            break
    assert checked == 20


def test_validation_tenants_have_distinct_identifiers():
    first = generate_tenant("validation-saas-fixture-a", count=30)
    second = generate_tenant("validation-saas-fixture-b", count=30)
    assert not {job.id for job in first.jobs}.intersection(job.id for job in second.jobs)
    assert not {person.id for person in first.people}.intersection(
        person.id for person in second.people
    )


def test_full_tenant_supports_exactly_one_hundred_vacancy_performance_fixture(dataset):
    remaining = 100
    selected = []
    for job in dataset.jobs:
        if job.status == "open" and job.headcount <= remaining:
            selected.append(job)
            remaining -= job.headcount
        if remaining == 0:
            break
    assert remaining == 0
    assert sum(job.headcount for job in selected) == 100
    assert {job.business_type for job in selected} == {"placement", "dispatch"}
    assert len(dataset.jobs) == 1000


@pytest.mark.parametrize(
    "tenant,count",
    [
        ("", 30),
        (" \t\n", 30),
        (None, 30),
        (123, 30),
        ("demo", 0),
        ("demo", -1),
        ("demo", 1001),
        ("demo", True),
        ("demo", 1.5),
    ],
)
def test_invalid_generation_parameters_fail_closed(tenant, count):
    with pytest.raises(ValueError):
        generate_tenant(tenant, count=count)
