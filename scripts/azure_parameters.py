"""Create a private ARM parameter file without exposing secrets in CLI arguments."""

import argparse
import json
import os
import secrets
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--location", required=True)
    parser.add_argument("--resource-group", required=True)
    parser.add_argument("--operator-object-id", required=True)
    parser.add_argument(
        "--from-vault", help="Reuse existing credentials; never rotate on redeploy."
    )
    args = parser.parse_args()
    output = Path(".azure/private/infra.parameters.json")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output.exists():
        raise SystemExit(f"{output} already exists; reuse it or remove it explicitly.")
    if not args.from_vault:
        exists = (
            subprocess.run(
                [
                    "az",
                    "group",
                    "exists",
                    "--subscription",
                    args.subscription,
                    "--name",
                    args.resource_group,
                    "--output",
                    "tsv",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            .stdout.strip()
            .lower()
        )
        if exists not in {"true", "false"}:
            raise RuntimeError("Azure returned an unexpected resource-group existence result.")
        if exists == "true":
            raise SystemExit(
                "Resource group already exists; use --from-vault to reuse credentials."
            )

    values = {
        "location": args.location,
        "resourceGroupName": args.resource_group,
        "operatorObjectId": args.operator_object_id,
    }
    secret_names = {
        "postgresAdminPassword": "postgres-admin-password",
        "appDatabasePassword": "app-database-password",
        "pocAccessPassword": "poc-access-password",
    }
    for parameter, secret_name in secret_names.items():
        if args.from_vault:
            result = subprocess.run(
                [
                    "az",
                    "keyvault",
                    "secret",
                    "show",
                    "--subscription",
                    args.subscription,
                    "--vault-name",
                    args.from_vault,
                    "--name",
                    secret_name,
                    "--query",
                    "value",
                    "--output",
                    "tsv",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            value = result.stdout.strip()
            if not value:
                raise SystemExit(f"Key Vault returned an empty {secret_name}.")
        else:
            value = "Aa1!" + secrets.token_urlsafe(36)
        values[parameter] = value

    document = {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
        "contentVersion": "1.0.0.0",
        "parameters": {key: {"value": value} for key, value in values.items()},
    }
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(document, handle, indent=2)
        handle.write("\n")
    print(f"Wrote {output} (0600). Use only with subscription {args.subscription}.")


if __name__ == "__main__":
    main()
