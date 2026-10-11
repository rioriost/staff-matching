import secrets

import pytest

from backend.account_seed import ACCOUNTS, initial_password


def test_seed_has_ten_companies_and_a_separate_read_only_account():
    assert len(ACCOUNTS) == 11
    assert len({account.username for account in ACCOUNTS}) == 11
    assert len({account.tenant_id for account in ACCOUNTS}) == 10
    assert ACCOUNTS[0].tenant_id == "demo"
    assert ACCOUNTS[-1].role == "viewer"


def test_passwords_are_stable_separated_and_change_with_root():
    seed = secrets.token_urlsafe(48)
    passwords = [initial_password(seed, account.username) for account in ACCOUNTS]
    assert len(set(passwords)) == len(ACCOUNTS)
    assert all(len(password) == 43 for password in passwords)
    assert all(seed not in password for password in passwords)
    assert passwords[0] == initial_password(seed, "coordinator")
    assert passwords[0] != initial_password(secrets.token_urlsafe(48), "coordinator")


def test_unknown_account_and_short_seed_are_rejected():
    with pytest.raises(ValueError):
        initial_password("short", "coordinator")
    with pytest.raises(ValueError):
        initial_password(secrets.token_urlsafe(48), "unregistered")
