"""Run one manual bootstrap/verification job and wait for a terminal result."""

import argparse
import json
import re
import subprocess
import time


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--resource-group", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument(
        "--execution", help="Wait for an existing execution instead of starting one."
    )
    parser.add_argument("--timeout", type=int, default=660)
    args = parser.parse_args()
    common = [
        "--subscription",
        args.subscription,
        "--resource-group",
        args.resource_group,
        "--name",
        args.name,
        "--only-show-errors",
    ]

    def value(*command: str) -> str:
        result = subprocess.run(
            ["az", "containerapp", "job", *command, *common, "--output", "tsv"],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    execution = args.execution or value("start", "--query", "name")
    if not execution:
        raise RuntimeError("Azure did not return a job execution name.")
    if not re.fullmatch(r"[a-z0-9-]+", execution):
        raise RuntimeError("Azure returned an invalid execution name.")
    print(f"Execution: {execution}", flush=True)
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        status = value(
            "execution",
            "show",
            "--job-execution-name",
            execution,
            "--query",
            "properties.status",
        )
        if status in ("Succeeded", "Failed", "Stopped"):
            break
        time.sleep(5)
    else:
        raise RuntimeError(f"Job {execution} did not finish within {args.timeout} seconds.")

    print(f"Job {execution}: {status}; retrieving persisted logs.", flush=True)
    environment_id = value("show", "--query", "properties.environmentId")
    workspace = subprocess.run(
        [
            "az",
            "resource",
            "show",
            "--ids",
            environment_id,
            "--subscription",
            args.subscription,
            "--query",
            "properties.appLogsConfiguration.logAnalyticsConfiguration.customerId",
            "--output",
            "tsv",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not workspace:
        raise RuntimeError("The job environment has no Log Analytics workspace.")
    query = (
        "ContainerAppConsoleLogs_CL | where TimeGenerated > ago(24h) "
        f"| where ContainerGroupName_s startswith '{execution}-' "
        "| order by TimeGenerated asc | project TimeGenerated, Log_s | take 100"
    )
    ingestion_deadline = time.monotonic() + 180
    while time.monotonic() < ingestion_deadline:
        response = subprocess.run(
            [
                "az",
                "rest",
                "--method",
                "post",
                "--subscription",
                args.subscription,
                "--resource",
                "https://api.loganalytics.io",
                "--url",
                f"https://api.loganalytics.io/v1/workspaces/{workspace}/query",
                "--body",
                json.dumps({"query": query}),
                "--output",
                "json",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        rows = json.loads(response.stdout)["tables"][0]["rows"]
        if rows:
            for timestamp, message in rows:
                print(f"{timestamp} {message}")
            break
        time.sleep(10)
    else:
        raise RuntimeError(
            "Job finished, but persisted logs were not available within 180 seconds."
        )
    if status != "Succeeded":
        raise SystemExit(f"Job {execution}: {status}")
    print(f"Job {execution}: Succeeded")


if __name__ == "__main__":
    main()
