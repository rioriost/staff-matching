"""Stable bootstrap identities; passwords are derived only by authorized setup/operator tools."""

import base64
import hashlib
import hmac
from dataclasses import dataclass


@dataclass(frozen=True)
class BootstrapAccount:
    username: str
    tenant_id: str
    tenant_name: str
    role: str = "coordinator"


ACCOUNTS = (
    BootstrapAccount("coordinator", "demo", "管理企業01（合成）"),
    *(BootstrapAccount(
        f"coordinator{index:02}", f"demo{index:02}", f"管理企業{index:02}（合成）"
    ) for index in range(2, 11)),
    BootstrapAccount("viewer", "demo", "管理企業01（合成）", "viewer"),
)


def initial_password(seed: str, username: str) -> str:
    if len(seed) < 32:
        raise ValueError("Account bootstrap requires a high-entropy seed (32+ characters).")
    if username not in {account.username for account in ACCOUNTS}:
        raise ValueError("Unknown bootstrap account.")
    digest = hmac.digest(
        seed.encode(), f"staff-matching:initial-password:v1:{username}".encode(), hashlib.sha256
    )
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")
