import json
import stat

import pytest

from scripts import poc_credentials


@pytest.mark.parametrize("worker", [False, True])
@pytest.mark.parametrize("qualified", [False, True])
def test_bootstrap_parameters_reuse_initialized_secrets(tmp_path, monkeypatch, worker, qualified):
    monkeypatch.chdir(tmp_path)
    secret = "worker-database-password" if worker else "poc-account-bootstrap"
    reference = "pg-worker" if worker else "account-seed"
    calls = []

    def azure(_subscription, *arguments):
        calls.append(arguments)
        if arguments[0] == "rest":
            return ["vault/" + secret if qualified else secret]
        assert "--show-values" in arguments
        assert f"[?name=='{reference}'].value | [0]" in arguments
        return "synthetic-value-" * 4

    monkeypatch.setattr(poc_credentials, "azure_json", azure)
    output = poc_credentials.prepare_parameters("sub", "group", "job", "vault", worker=worker)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    parameters = json.loads(output.read_text())["parameters"]
    name = "workerDatabasePassword" if worker else "bootstrapSecret"
    assert parameters == {name: {"value": "synthetic-value-" * 4}}
    assert len(calls) == 2
    with pytest.raises(RuntimeError, match="already exist"):
        poc_credentials.prepare_parameters("sub", "group", "job", "vault", worker=worker)


def test_existing_secret_without_resolvable_reference_is_never_rotated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def azure(_subscription, *arguments):
        return ["worker-database-password"] if arguments[0] == "rest" else None

    monkeypatch.setattr(poc_credentials, "azure_json", azure)
    with pytest.raises(RuntimeError, match="initialized secret reference"):
        poc_credentials.prepare_parameters("sub", "group", "job", "vault", worker=True)
    assert not (tmp_path / ".azure/private/saas.parameters.json").exists()


def test_new_secret_gets_high_entropy_parameter_without_reading_account_seed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    queries = []

    def azure(_subscription, *arguments):
        queries.append(arguments)
        return [] if arguments[0] == "rest" else ["account-seed"]

    monkeypatch.setattr(poc_credentials, "azure_json", azure)
    output = poc_credentials.prepare_parameters("sub", "group", "job", "vault", worker=True)
    value = json.loads(output.read_text())["parameters"]["workerDatabasePassword"]["value"]
    assert len(value) >= 64
    assert all("--show-values" not in arguments for arguments in queries)


def test_malformed_secret_metadata_cannot_trigger_new_credential_generation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(poc_credentials, "azure_json", lambda *args: [None])
    with pytest.raises(RuntimeError, match="metadata"):
        poc_credentials.prepare_parameters("sub", "group", "job", "vault", worker=True)
    assert not (tmp_path / ".azure/private/saas.parameters.json").exists()
