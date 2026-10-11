import httpx
import pytest

from scripts.verify_saas_azure import (
    all_jobs,
    application_urls,
    browse,
    hundred_vacancies,
    verify_whole_tenant_scope,
)


@pytest.mark.parametrize("preview", [False, True])
def test_preview_label_follows_app_name_and_preserves_canonical_origin(preview):
    hostname = "staff.example.japaneast.azurecontainerapps.io"
    origin, endpoint = application_urls(hostname, preview=preview)
    assert origin == "https://" + hostname
    assert endpoint == (
        "https://staff---saas-preview.example.japaneast.azurecontainerapps.io"
        if preview else origin
    )


@pytest.mark.parametrize("hostname", [
    "staff.example.invalid", "user@staff.example.azurecontainerapps.io",
    "staff.example.azurecontainerapps.io/path",
])
def test_unexpected_hostnames_are_rejected_before_credential_use(hostname):
    with pytest.raises(RuntimeError, match="unexpected hostname"):
        application_urls(hostname, preview=True)


def test_batch_selects_exactly_one_hundred_vacancies_not_one_hundred_jobs():
    jobs = [
        {"id": f"job-{index}", "headcount": 3, "status": "open"} for index in range(40)
    ]
    jobs.extend([
        {"id": "closed-one", "headcount": 1, "status": "closed"},
        {"id": "last-one", "headcount": 1, "status": "open"},
    ])
    selected = hundred_vacancies(jobs)
    assert len(selected) == 34
    assert "closed-one" not in selected
    assert sum(job["headcount"] for job in jobs if job["id"] in selected) == 100


def test_inadequate_seed_is_explicitly_rejected():
    with pytest.raises(RuntimeError, match="exactly 100"):
        hundred_vacancies([{"id": "one", "headcount": 1, "status": "open"}])


def test_job_pagination_follows_server_cursor_and_does_not_infer_total():
    calls = []

    def response(request):
        calls.append(request)
        second = request.url.params.get("cursor") == "opaque-page-two"
        return httpx.Response(200, json={
            "items": [{"id": "two" if second else "one"}],
            "next_cursor": None if second else "opaque-page-two",
            "total": 2,
        })

    with httpx.Client(base_url="https://synthetic.invalid", transport=httpx.MockTransport(
        response,
    )) as client:
        assert all_jobs(client) == [{"id": "one"}, {"id": "two"}]
    assert len(calls) == 2
    assert all(call.url.params["limit"] == "100" for call in calls)


def test_browsing_requires_fifty_rows_and_the_real_thousand_person_scope():
    def response(request):
        assert request.url.params["limit"] == "50"
        return httpx.Response(200, json={"items": [{}] * 49, "total": 1000})

    with httpx.Client(base_url="https://synthetic.invalid", transport=httpx.MockTransport(
        response,
    )) as client:
        with pytest.raises(AssertionError, match="50-row"):
            browse(client)


def test_whole_tenant_scope_covers_open_jobs_without_counting_closed_jobs_as_omitted():
    jobs = [{"status": "open"}] * 968 + [{"status": "closed"}] * 32
    run = {"status": "completed", "coverage": {"jobs": 968}}
    assert verify_whole_tenant_scope(run, jobs) == {
        "registered_jobs": 1000, "open_jobs": 968, "excluded_non_open_jobs": 32,
    }
    with pytest.raises(AssertionError, match="every open job"):
        verify_whole_tenant_scope({**run, "coverage": {"jobs": 967}}, jobs)
    with pytest.raises(AssertionError, match="every open job"):
        verify_whole_tenant_scope({**run, "status": "failed"}, jobs)
    with pytest.raises(AssertionError, match="1,000"):
        verify_whole_tenant_scope(run, jobs[:-1])


def test_explicit_warmup_retains_the_same_list_shape_without_polluting_samples():
    calls = []

    def response(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"items": [{}] * 50, "total": 1000})

    with httpx.Client(base_url="https://synthetic.invalid", transport=httpx.MockTransport(
        response,
    )) as client:
        assert len(browse(client, rounds=1)) == 2
    assert calls == ["/api/saas/jobs", "/api/saas/people"]
