"""Exercise the authenticated synthetic-data PoC, or recheck saved state after restart."""

import argparse
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import httpx

from backend.account_seed import initial_password
from scripts.poc_credentials import read_seed


def azure_value(subscription: str, *arguments: str) -> str:
    result = subprocess.run(
        ["az", *arguments, "--subscription", subscription, "--output", "tsv"],
        check=True,
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip()
    if not value:
        raise RuntimeError("Azure returned an empty deployment value.")
    return value


def require(response: httpx.Response, status: int = 200) -> httpx.Response:
    if response.status_code != status:
        raise RuntimeError(
            f"{response.request.method} {response.request.url.path}: "
            f"expected HTTP {status}, received {response.status_code}"
        )
    return response


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--resource-group", required=True)
    parser.add_argument("--app", required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--username", default="coordinator")
    parser.add_argument("--state-file", type=Path, default=Path(".azure/http-acceptance.json"))
    parser.add_argument("--check-persistence", action="store_true")
    args = parser.parse_args()
    hostname = azure_value(
        args.subscription,
        "containerapp",
        "show",
        "--resource-group",
        args.resource_group,
        "--name",
        args.app,
        "--query",
        "properties.configuration.ingress.fqdn",
    )
    if not hostname.endswith(".azurecontainerapps.io") or "/" in hostname:
        raise RuntimeError("Refusing to send the PoC credential to an unexpected hostname.")
    base_url = f"https://{hostname}"
    state_file = args.state_file
    with httpx.Client(base_url=base_url, timeout=45) as anonymous:
        require(anonymous.get("/healthz"))
        session = anonymous.get("/api/auth/session")
        if session.status_code not in (200, 401):
            raise RuntimeError("Could not determine deployed authentication mode.")
        accounts = session.status_code == 200 and session.json().get("enabled") is True
        require(anonymous.get("/"), 200 if accounts else 401)
        for path in ("/api/health", "/api/staff", "/docs", "/api/extensions"):
            require(anonymous.get(path), 401)
        require(anonymous.get("/api/health", auth=("coordinator", "wrong-password")), 401)

    if accounts:
        password = initial_password(
            read_seed(args.subscription, args.resource_group, args.job), args.username,
        )
    else:
        password = azure_value(
            args.subscription, "containerapp", "secret", "list",
            "--resource-group", args.resource_group, "--name", args.app,
            "--show-values", "--query", "[?name=='poc-auth'].value | [0]",
        )
    with httpx.Client(
        base_url=base_url,
        auth=None if accounts else (args.username, password),
        timeout=45,
    ) as client:
        if accounts:
            session = require(client.post(
                "/api/auth/login", json={"username": args.username, "password": password},
                headers={"Origin": base_url, "X-Requested-With": "staff-matching"},
            )).json()
            assert session["authenticated"] is True
            client.headers["X-Tenant-ID"] = session["tenant"]["id"]
        page = require(client.get("/"))
        if "text/html" not in page.headers.get("content-type", ""):
            raise RuntimeError("The application did not serve its compiled frontend.")
        health = require(client.get("/api/health")).json()
        assert health["mode"] == "azure-poc" and health["persistence"] == "postgresql"
        extensions = require(client.get("/api/extensions")).json()
        assert extensions["synthetic"] is True
        assert set(extensions["extensions"]) == {"age", "postgis", "vector", "btree_gist"}
        assert all(extensions["extensions"].values())
        assert extensions["graph_matches_source"] is True
        assert extensions["history_source_count"] == extensions["graph_history_count"] > 0
        assert extensions["sample"]["spatial_distance_m"] > 0
        assert extensions["sample"]["vector_distance"] is not None
        require(client.post("/api/matching/optimize", json={"idempotency_key": uuid4().hex}), 403)
        client.headers["X-Requested-With"] = "staff-matching"
        client.headers["Origin"] = "https://untrusted.example"
        require(client.post("/api/matching/optimize", json={"idempotency_key": uuid4().hex}), 403)
        client.headers["Origin"] = base_url

        if args.check_persistence:
            saved = json.loads(state_file.read_text())
            assert saved["url"] == base_url
            assert saved.get("username", "coordinator") == args.username
            run = require(client.get(f"/api/matching/runs/{saved['run_id']}")).json()
            assert run["result"] == saved["result"]
            assert any(
                item["id"] == saved["proposal_id"] and item["status"] == "confirmed"
                for item in run["proposals"]
            )
            assignments = require(client.get("/api/assignments")).json()
            assert saved["assignment"] in assignments
            print(json.dumps({"persistence": "passed", "run_id": run["id"]}))
            return

        before = require(client.get("/api/assignments")).json()
        request = {"idempotency_key": uuid4().hex}
        accepted = require(client.post("/api/matching/optimize", json=request), 202).json()
        replay = require(client.post("/api/matching/optimize", json=request), 202).json()
        assert replay["run_id"] == accepted["run_id"]
        run_path = f"/api/matching/runs/{accepted['run_id']}"
        for _ in range(60):
            run = require(client.get(run_path)).json()
            if run["status"] in ("completed", "failed"):
                break
            time.sleep(1)
        assert run["status"] == "completed", run.get("error")
        assert run["result"]["optimized"]["filled_slots"] > 0
        proposal = run["proposals"][0]
        path = f"/api/proposals/{proposal['id']}"
        require(
            client.post(
                path + "/confirm",
                json={
                    "expected_version": proposal["version"],
                    "idempotency_key": uuid4().hex,
                },
            ),
            409,
        )
        consent = require(
            client.post(
                path + "/respond",
                json={
                    "decision": "consent",
                    "expected_version": proposal["version"],
                    "idempotency_key": uuid4().hex,
                },
            )
        ).json()
        confirmation = {
            "expected_version": consent["version"],
            "idempotency_key": uuid4().hex,
        }
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(
                pool.map(
                    lambda _: client.post(path + "/confirm", json=confirmation),
                    range(2),
                )
            )
        assignment = require(responses[0]).json()
        assert require(responses[1]).json() == assignment
        assert require(client.post(path + "/confirm", json=confirmation)).json() == assignment
        after = require(client.get("/api/assignments")).json()
        assert len(after) == len(before) + 1 and assignment in after
        for remaining in run["proposals"][1:]:
            require(
                client.post(
                    f"/api/proposals/{remaining['id']}/respond",
                    json={
                        "decision": "decline",
                        "expected_version": remaining["version"],
                        "idempotency_key": uuid4().hex,
                    },
                )
            )
        state_file.parent.mkdir(exist_ok=True)
        state_file.write_text(
            json.dumps(
                {
                    "url": base_url,
                    "username": args.username,
                    "run_id": run["id"],
                    "proposal_id": proposal["id"],
                    "assignment": assignment,
                    "result": run["result"],
                },
                indent=2,
            )
        )
        print(
            json.dumps(
                {
                    "url": base_url,
                    "health": health,
                    "extensions": extensions["extensions"],
                    "feature_evidence": {
                        "pairs": len(extensions["pairs"]),
                        "graph_history_count": extensions["graph_history_count"],
                        "history_source_count": extensions["history_source_count"],
                        "sample_distance_m": extensions["sample"]["spatial_distance_m"],
                        "sample_vector_distance": extensions["sample"]["vector_distance"],
                    },
                    "optimized_slots": run["result"]["optimized"]["filled_slots"],
                    "greedy_slots": run["result"]["greedy"]["filled_slots"],
                    "run_id": run["id"],
                    "confirmed_assignments": len(after),
                    "authentication_csrf_concurrency_idempotency": "passed",
                    "authentication_mode": "accounts" if accounts else "basic",
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
