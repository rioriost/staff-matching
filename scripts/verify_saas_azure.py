"""Opt-in SaaS acceptance; creates runs and a demo introduction, then cancels that introduction."""

import argparse
import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx

from backend.account_seed import ACCOUNTS, initial_password
from backend.saas_verify import _page
from scripts.poc_credentials import read_seed
from scripts.verify_azure import azure_value, require


def application_urls(hostname: str, *, preview: bool) -> tuple[str, str]:
    if not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+\.azurecontainerapps\.io", hostname):
        raise RuntimeError("Refusing to send a credential to an unexpected hostname.")
    origin = "https://" + hostname
    app, domain = hostname.split(".", 1)
    endpoint = f"https://{app}---saas-preview.{domain}" if preview else origin
    return origin, endpoint


def all_jobs(client: httpx.Client) -> list[dict]:
    jobs = []
    cursor = None
    while True:
        result = require(client.get("/api/saas/jobs", params={
            "limit": 100, **({"cursor": cursor} if cursor else {}),
        })).json()
        jobs.extend(result["items"])
        cursor = result["next_cursor"]
        if cursor is None:
            return jobs
        if len(jobs) > 1000:
            raise RuntimeError("Tenant job pagination exceeded the seeded dataset.")


def hundred_vacancies(jobs: list[dict]) -> list[str]:
    selected = []
    positions = 0
    for job in jobs:
        if job["status"] == "open" and positions + job["headcount"] <= 100:
            selected.append(job["id"])
            positions += job["headcount"]
        if positions == 100:
            return selected
    raise RuntimeError("Could not select exactly 100 seeded vacancy slots.")


def submit(client: httpx.Client, job_ids: list[str]) -> tuple[dict, float]:
    started = time.monotonic()
    result = require(client.post("/api/saas/matching/runs", json={
        "job_ids": job_ids, "idempotency_key": uuid4().hex,
    }), 202).json()
    return result, started


def finish(client: httpx.Client, run: dict, started: float, timeout: int) -> dict:
    deadline = started + timeout
    while time.monotonic() < deadline:
        current = require(client.get(f"/api/saas/matching/runs/{run['id']}")).json()
        if current["status"] in ("completed", "failed", "cancelled"):
            return {
                "id": current["id"], "status": current["status"],
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "recommended_count": current["recommended_count"],
                "coverage": current["coverage"], "solver_status": current["solver_status"],
                "objective_bound": current["objective_bound"],
            }
        time.sleep(0.5)
    raise TimeoutError("Matching exceeded the explicit acceptance time budget.")


def browse(client: httpx.Client, *, rounds: int = 10) -> list[float]:
    samples = []
    for _ in range(rounds):
        for endpoint in ("jobs", "people"):
            started = time.monotonic()
            response = require(client.get(f"/api/saas/{endpoint}", params={"limit": 50}))
            samples.append(time.monotonic() - started)
            value = response.json()
            if len(value["items"]) != 50 or value["total"] != 1000:
                raise AssertionError("The 50-row list contract or tenant count is incorrect.")
    return samples


def verify_whole_tenant_scope(run: dict, jobs: list[dict]) -> dict[str, int]:
    scope = {
        "registered_jobs": len(jobs),
        "open_jobs": sum(job["status"] == "open" for job in jobs),
        "excluded_non_open_jobs": sum(job["status"] != "open" for job in jobs),
    }
    if scope["registered_jobs"] != 1000 or scope["open_jobs"] == 0:
        raise AssertionError(
            "The whole-tenant fixture must contain 1,000 jobs with open vacancies."
        )
    if run["status"] != "completed" or run["coverage"]["jobs"] != scope["open_jobs"]:
        raise AssertionError("The tenant-wide run did not cover every open job.")
    return scope


def workflow(client: httpx.Client, run_id: str, jobs: list[dict]) -> dict:
    by_id = {job["id"]: job for job in jobs}
    choices = [
        item for item in _page(client, f"/api/saas/matching/runs/{run_id}/candidates")
        if item["eligible"] and by_id[item["job_id"]]["business_type"] == "placement"
    ]
    if not choices:
        raise AssertionError("No eligible placement candidate was retrieved for the live workflow.")
    candidate = min(choices, key=lambda item: item["recommended"])
    intro = require(client.post("/api/saas/introductions", json={
        "run_id": run_id, "job_id": candidate["job_id"], "person_id": candidate["person_id"],
        "expected_job_version": candidate["job_version"],
        "expected_person_version": candidate["person_version"], "idempotency_key": uuid4().hex,
    }), 201).json()
    intro_id = intro["id"]
    path = f"/api/saas/introductions/{intro_id}"
    try:
        for action in ("company_request", "person_consent"):
            intro = require(client.post(path + "/simulate", json={
                "expected_version": intro["version"], "idempotency_key": uuid4().hex,
                "action": action, "simulated": True,
            })).json()
        start = (datetime.now(UTC) + timedelta(days=3)).replace(
            hour=4, minute=0, second=0, microsecond=0,
        )
        appointment = require(client.post(path + "/appointments", json={
            "expected_version": intro["version"], "idempotency_key": uuid4().hex,
            "start_at": start.isoformat(), "end_at": (start + timedelta(minutes=30)).isoformat(),
            "online": True,
        }), 201).json()
        accepted = require(client.post(f"/api/saas/appointments/{appointment['id']}/accept", json={
            "expected_version": appointment["version"], "idempotency_key": uuid4().hex,
        })).json()
        if accepted["status"] != "accepted":
            raise AssertionError("The server did not acknowledge demo interview acceptance.")
        return {
            "introduction_id": intro_id, "appointment_id": accepted["id"],
            "accepted_then_cancelled": True,
        }
    finally:
        current = next(
            item for item in _page(client, "/api/saas/introductions") if item["id"] == intro_id
        )
        cancelled = require(client.post(path + "/cancel", json={
            "expected_version": current["version"], "idempotency_key": uuid4().hex,
        })).json()
        if cancelled["status"] != "cancelled":
            raise AssertionError("The isolated live acceptance introduction was not cancelled.")
        appointments = _page(client, "/api/saas/appointments")
        if any(item["introduction_id"] == intro_id and item["status"] != "cancelled"
               for item in appointments):
            raise AssertionError("An appointment survived its introduction cancellation.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--resource-group", required=True)
    parser.add_argument("--app", required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--state-file", type=Path, default=Path(".azure/saas-http-acceptance.json"))
    args = parser.parse_args()
    report = {
        "status": "running", "preview": args.preview, "started_at": datetime.now(UTC).isoformat(),
    }
    try:
        hostname = azure_value(
            args.subscription, "containerapp", "show", "--resource-group", args.resource_group,
            "--name", args.app, "--query", "properties.configuration.ingress.fqdn",
        )
        origin, endpoint = application_urls(hostname, preview=args.preview)
        seed = read_seed(args.subscription, args.resource_group, args.job)
        report["url"] = endpoint
        with ExitStack() as stack:
            clients = []
            for account in (item for item in ACCOUNTS if item.role == "coordinator"):
                client = stack.enter_context(httpx.Client(
                    base_url=endpoint, timeout=30,
                    headers={"Origin": origin, "X-Requested-With": "staff-matching"},
                ))
                session = require(client.post("/api/auth/login", json={
                    "username": account.username,
                    "password": initial_password(seed, account.username),
                })).json()
                if session["tenant"]["id"] != account.tenant_id:
                    raise AssertionError("Unexpected authenticated management company.")
                client.headers["X-Tenant-ID"] = account.tenant_id
                if require(client.get("/api/workspace")).json() != {"kind": "saas"}:
                    raise AssertionError("SaaS mode is not active on the selected revision.")
                overview = require(client.get("/api/saas/overview")).json()
                assert overview["jobs"] == overview["people"] == 1000
                require(client.get("/api/saas/jobs", params={"limit": 50}))
                require(client.get("/api/saas/people", params={"limit": 50}))
                clients.append(client)
                stack.callback(
                    lambda current=client: require(current.post("/api/auth/logout", json={}))
                )
            jobs = [all_jobs(client) for client in clients[-2:]]
            with ThreadPoolExecutor(max_workers=10) as pool:
                warmups = [pool.submit(browse, client, rounds=1) for client in clients]
                for future in warmups:
                    future.result()
            submissions = [
                submit(client, hundred_vacancies(items))
                for client, items in zip(clients[-2:], jobs, strict=True)
            ]
            with ThreadPoolExecutor(max_workers=12) as pool:
                run_futures = [
                    pool.submit(finish, client, run, started, 180)
                    for client, (run, started) in zip(clients[-2:], submissions, strict=True)
                ]
                browsing = list(pool.map(browse, clients))
                runs = [future.result() for future in run_futures]
            samples = sorted(value for group in browsing for value in group)
            report.update(
                tenants=10, jobs=10000, people=10000, concurrent_browsers=10,
                list_samples=len(samples),
                list_p95_seconds=samples[math.ceil(len(samples) * .95) - 1],
                hundred_vacancy_runs=runs,
            )
            if report["list_p95_seconds"] >= 1:
                raise AssertionError("Warmed list p95 must be under one second.")
            for run in runs:
                if run["status"] != "completed" or run["recommended_count"] <= 0:
                    raise AssertionError("A 100-vacancy run produced no usable recommendations.")
                if run["elapsed_seconds"] > 60:
                    raise AssertionError("A 100-vacancy run exceeded the 60-second target.")
            whole, started = submit(clients[-1], [])
            full = report["whole_tenant_run"] = finish(clients[-1], whole, started, 900)
            report["whole_tenant_scope"] = verify_whole_tenant_scope(full, jobs[-1])
            report["workflow"] = workflow(clients[-1], runs[-1]["id"], jobs[-1])
            report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", error_category=type(error).__name__)
        raise SystemExit(
            f"SaaS acceptance failed ({type(error).__name__}); response and credentials omitted."
        ) from None
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        args.state_file.parent.mkdir(parents=True, exist_ok=True)
        args.state_file.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report))


if __name__ == "__main__":
    main()
