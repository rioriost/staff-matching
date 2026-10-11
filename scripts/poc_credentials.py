"""Authorized operator utility for initial PoC credentials; never log its output in automation."""

import argparse
import json
import os
import secrets
import subprocess
from pathlib import Path

from backend.account_seed import initial_password


def azure_json(subscription: str, *arguments: str) -> object:
    result = subprocess.run(
        ["az", *arguments, "--subscription", subscription, "--output", "json",
         "--only-show-errors"],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)


def read_job_secret(subscription: str, group: str, job: str, name: str) -> str:
    if name not in ("account-seed", "pg-worker"):
        raise ValueError("Unsupported setup-secret reference.")
    value = azure_json(
        subscription, "containerapp", "job", "secret", "list",
        "--resource-group", group, "--name", job, "--show-values",
        "--query", f"[?name=='{name}'].value | [0]",
    )
    if not isinstance(value, str) or len(value) < 32:
        raise RuntimeError("The setup job does not expose the initialized secret reference.")
    return value


def read_seed(subscription: str, group: str, job: str) -> str:
    return read_job_secret(subscription, group, job, "account-seed")


def prepare_parameters(
    subscription: str, group: str, job: str, vault: str, *, worker: bool = False,
) -> Path:
    filename = "saas.parameters.json" if worker else "identity.parameters.json"
    output = Path(".azure/private") / filename
    secret_name = "worker-database-password" if worker else "poc-account-bootstrap"
    job_ref = "pg-worker" if worker else "account-seed"
    if output.exists():
        raise RuntimeError("Private bootstrap parameters already exist; reuse the existing file.")
    names = azure_json(
        subscription, "rest", "--method", "get", "--url",
        f"https://management.azure.com/subscriptions/{subscription}/resourceGroups/{group}"
        f"/providers/Microsoft.KeyVault/vaults/{vault}/secrets?api-version=2024-11-01",
        "--query", "value[].name",
    )
    if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
        raise RuntimeError("Unexpected Azure secret metadata response.")
    if secret_name in {name.rsplit("/", 1)[-1] for name in names}:
        seed = read_job_secret(subscription, group, job, job_ref)
    else:
        # Also check the existing job reference; never rotate an initialized deployment implicitly.
        refs = azure_json(
            subscription, "containerapp", "job", "secret", "list",
            "--resource-group", group, "--name", job, "--query", "[].name",
        )
        if not isinstance(refs, list) or any(not isinstance(name, str) for name in refs):
            raise RuntimeError("Unexpected setup-job secret metadata response.")
        seed = (
            read_job_secret(subscription, group, job, job_ref)
            if job_ref in refs else secrets.token_urlsafe(48)
        )
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump({
            "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
            "contentVersion": "1.0.0.0",
            "parameters": {
                "workerDatabasePassword" if worker else "bootstrapSecret": {"value": seed},
            },
        }, handle)
        handle.write("\n")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--resource-group", required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--vault")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--prepare-bootstrap", action="store_true")
    action.add_argument("--prepare-worker", action="store_true")
    action.add_argument("--username")
    args = parser.parse_args()
    try:
        if args.prepare_bootstrap or args.prepare_worker:
            if not args.vault:
                parser.error("Bootstrap preparation requires --vault.")
            path = prepare_parameters(
                args.subscription, args.resource_group, args.job, args.vault,
                worker=args.prepare_worker,
            )
            print(f"Wrote {path} (0600); credential values are not displayed.")
        else:
            seed = read_seed(args.subscription, args.resource_group, args.job)
            print(initial_password(seed, args.username))
    except (subprocess.CalledProcessError, RuntimeError, ValueError) as error:
        raise SystemExit(
            f"Credential operation failed ({type(error).__name__}); no values logged."
        ) from None


if __name__ == "__main__":
    main()
