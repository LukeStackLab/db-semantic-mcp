import json
import os
import sys

import pytest

from db_semantic_mcp import credentials
from db_semantic_mcp.profiles import ConnectionProfile, load_profile, policy_bytes, read_profile
from db_semantic_mcp.setup import migration
from db_semantic_mcp.setup.clients import ClientTarget
from db_semantic_mcp.setup.files import SetupError, atomic_write


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    old = tmp_path / "old"
    new = tmp_path / "new"
    directory = old / "erp"
    directory.mkdir(parents=True, mode=0o700)
    profile = ConnectionProfile(
        name="erp",
        server="server",
        database="db",
        user="reader",
        max_response_bytes=2048,
        max_cell_chars=32,
        max_rows_returned=12,
        credential={"storage": "env", "key": "TASK4_MIGRATION_PASSWORD"},
    )
    data = profile.model_dump()
    data.pop("database_type")
    data.pop("oracle")
    data.pop("sources")
    data["format_version"] = 1
    data["budget"] = "tiny"
    atomic_write(directory / "connection.json", json.dumps(data).encode())
    policy = json.loads(policy_bytes(profile))
    policy["profiles"]["readonly"]["scope"].update(allowed_schemas=["APP"], denied_tables=["APP.SECRETS"])
    policy["profiles"]["readonly"]["constraints"]["max_rows_returned"] = 7
    atomic_write(directory / "policy.json", json.dumps(policy).encode())
    client = ClientTarget("cursor", "Cursor", tmp_path / "client.json", "json")
    entry = {
        "command": sys.executable,
        "args": ["-I", "-m", "sqlserver_semantic_mcp.main", "serve", "--config", str(directory / "connection.json")],
        "extra": "keep",
    }
    atomic_write(
        client.path,
        json.dumps({"theme": "dark", "mcpServers": {"erp": entry, "unrelated": {"command": "keep"}}}).encode(),
    )
    monkeypatch.setenv("TASK4_MIGRATION_PASSWORD", "SECRET")
    monkeypatch.setattr(migration, "check_mcp", lambda path: 12)
    return old, new, client, directory


def plan(legacy):
    old, new, client, _ = legacy
    return migration.preview_migration(legacy_root=old, destination_root=new, clients=[client])


def test_preview_never_reads_secret_vault_or_writes(legacy, monkeypatch):
    old, new, client, _ = legacy
    original = {p: p.read_bytes() for p in old.rglob("*") if p.is_file()}

    def forbidden(*a):
        raise AssertionError("secret access")

    monkeypatch.setattr(credentials, "read_password", forbidden)
    monkeypatch.setattr(credentials, "_backend", forbidden)
    result = plan(legacy).preview()
    assert not new.exists() and "SECRET" not in json.dumps(result)
    assert result["profiles"][0]["name"] == "erp"
    assert original == {p: p.read_bytes() for p in old.rglob("*") if p.is_file()}


def test_apply_preserves_names_caps_policy_and_other_client_entries(legacy):
    old, new, client, directory = legacy
    before = {p: p.read_bytes() for p in directory.iterdir() if p.is_file()}
    result = migration.apply_migration(plan(legacy))
    cfg = load_profile(new / "erp" / "connection.json")
    assert cfg.max_rows_returned == 7 and cfg.max_response_bytes == 2048 and cfg.max_cell_chars == 32
    assert (new / "erp" / "policy.json").read_bytes() == (directory / "policy.json").read_bytes()
    data = json.loads(client.path.read_text())
    assert data["theme"] == "dark" and data["mcpServers"]["unrelated"] == {"command": "keep"}
    assert data["mcpServers"]["erp"]["extra"] == "keep"
    assert data["mcpServers"]["erp"]["args"] == [
        "-I",
        "-m",
        "db_semantic_mcp.main",
        "serve",
        "--config",
        str(new / "erp" / "connection.json"),
    ]
    assert len(result["backups"]) == 1
    assert before == {p: p.read_bytes() for p in directory.iterdir() if p.is_file()}
    assert "SECRET" not in client.path.read_text() + (new / "erp" / "connection.json").read_text()


@pytest.mark.parametrize(
    "failure",
    ["invalid_deny", "unsafe_write", "unknown_field", "v2", "name_mismatch", "destination", "client_collision"],
)
def test_invalid_inputs_fail_before_secret_reads(legacy, monkeypatch, failure):
    old, new, client, directory = legacy
    raw = json.loads((directory / "connection.json").read_text())
    pol = json.loads((directory / "policy.json").read_text())
    if failure == "invalid_deny":
        pol["profiles"]["readonly"]["scope"]["denied_tables"] = ["a; DROP TABLE secret"]
    elif failure == "unsafe_write":
        pol["profiles"]["readonly"]["operations"]["delete"] = True
    elif failure == "unknown_field":
        raw["secret"] = "DO_NOT_ECHO"
    elif failure == "v2":
        raw["format_version"] = 2
    elif failure == "name_mismatch":
        raw["name"] = "other"
    elif failure == "destination":
        (new / "erp").mkdir(parents=True)
    elif failure == "client_collision":
        client.path.write_text('{"mcpServers":{"erp":{"command":"other","args":[]}}}')
    (directory / "connection.json").write_text(json.dumps(raw))
    (directory / "policy.json").write_text(json.dumps(pol))
    monkeypatch.setattr(credentials, "_backend", lambda: pytest.fail("vault called"))
    with pytest.raises(SetupError) as error:
        plan(legacy)
    assert "DO_NOT_ECHO" not in str(error.value)


def test_keyring_copies_to_new_service_and_keeps_old(legacy, monkeypatch):
    old, new, client, directory = legacy
    raw = json.loads((directory / "connection.json").read_text())
    raw["credential"] = {"storage": "keyring", "key": "old-key"}
    (directory / "connection.json").write_text(json.dumps(raw))
    values = {(migration.LEGACY_SERVICE, "old-key"): "SECRET"}

    class Vault:
        def get_password(self, service, key):
            return values.get((service, key))

        def set_password(self, service, key, value):
            values[service, key] = value

        def delete_password(self, service, key):
            values.pop((service, key), None)

    monkeypatch.setattr(credentials, "_backend", Vault)
    migration.apply_migration(plan(legacy))
    ref = read_profile(new / "erp" / "connection.json").credential
    assert ref.key != "old-key" and values[credentials.SERVICE, ref.key] == "SECRET"
    assert values[migration.LEGACY_SERVICE, "old-key"] == "SECRET"


@pytest.mark.parametrize("stage", ["vault", "handshake", "client_conflict", "source_conflict"])
def test_failures_roll_back_only_new_credentials_and_files(legacy, monkeypatch, stage):
    old, new, client, directory = legacy
    raw = json.loads((directory / "connection.json").read_text())
    raw["credential"] = {"storage": "keyring", "key": "old-key"}
    (directory / "connection.json").write_text(json.dumps(raw))
    values = {(migration.LEGACY_SERVICE, "old-key"): "SECRET"}

    class Vault:
        def get_password(self, service, key):
            return values.get((service, key))

        def set_password(self, service, key, value):
            if stage == "vault":
                raise RuntimeError("SECRET")
            values[service, key] = value

        def delete_password(self, service, key):
            values.pop((service, key), None)

    monkeypatch.setattr(credentials, "_backend", Vault)

    def handshake(path):
        if stage == "handshake":
            raise SetupError("handshake failed")
        if stage == "client_conflict":
            client.path.write_text('{"newer":"client"}')
        if stage == "source_conflict":
            (directory / "policy.json").write_text('{"newer":"policy"}')
        return 12

    monkeypatch.setattr(migration, "check_mcp", handshake)
    with pytest.raises(SetupError) as error:
        migration.apply_migration(plan(legacy))
    assert "SECRET" not in str(error.value)
    assert not (new / "erp" / "connection.json").exists()
    assert values == {(migration.LEGACY_SERVICE, "old-key"): "SECRET"}
    if stage == "client_conflict":
        assert json.loads(client.path.read_text()) == {"newer": "client"}
    assert (directory / "connection.json").exists()


def test_concurrent_new_profile_is_preserved_with_credential(legacy, monkeypatch):
    old, new, client, directory = legacy

    def handshake(path):
        data = json.loads(path.read_text())
        data["server"] = "newer"
        path.write_text(json.dumps(data))
        return 12

    monkeypatch.setattr(migration, "check_mcp", handshake)
    with pytest.raises(SetupError, match="保留"):
        migration.apply_migration(plan(legacy))
    assert json.loads((new / "erp" / "connection.json").read_text())["server"] == "newer"


def test_file_credential_copies_private_and_never_migrates_cache(legacy):
    old, new, client, directory = legacy
    raw = json.loads((directory / "connection.json").read_text())
    raw["credential"] = {"storage": "file", "key": "credentials.json"}
    (directory / "connection.json").write_text(json.dumps(raw))
    credentials.store_private_password(directory, "SECRET")
    (directory / "cache").mkdir()
    (directory / "cache" / "old.db").write_text("do not copy")
    migration.apply_migration(plan(legacy))
    assert load_profile(new / "erp" / "connection.json").password == "SECRET"
    assert not (new / "erp" / "cache").exists()
    assert (directory / "credentials.json").exists()
    if os.name != "nt":
        assert (new / "erp" / "credentials.json").stat().st_mode & 0o777 == 0o600


def test_rollback_retains_unrelated_client_changes_after_our_write(legacy, monkeypatch):
    _, new, client, _ = legacy
    original = migration.atomic_write

    def write(path, data, **kwargs):
        result = original(path, data, **kwargs)
        if path == client.path:
            current = json.loads(path.read_text())
            current["newer"] = "retain"
            path.write_text(json.dumps(current))
            raise SetupError("post-write failure")
        return result

    monkeypatch.setattr(migration, "atomic_write", write)
    # Restore helper also writes the Client; allow that subsequent operation.
    count = []

    def once(path, data, **kwargs):
        if path == client.path and not count:
            count.append(True)
            return write(path, data, **kwargs)
        return original(path, data, **kwargs)

    monkeypatch.setattr(migration, "atomic_write", once)
    with pytest.raises(SetupError):
        migration.apply_migration(plan(legacy))
    current = json.loads(client.path.read_text())
    assert current["newer"] == "retain" and current["mcpServers"]["erp"]["args"][2] == "sqlserver_semantic_mcp.main"
    assert not (new / "erp" / "connection.json").exists()


def test_newer_profile_preserves_its_owned_policy_and_file_credential(legacy, monkeypatch):
    _, new, _, directory = legacy
    raw = json.loads((directory / "connection.json").read_text())
    raw["credential"] = {"storage": "file", "key": "credentials.json"}
    (directory / "connection.json").write_text(json.dumps(raw))
    credentials.store_private_password(directory, "SECRET")

    def handshake(path):
        current = json.loads(path.read_text())
        current["server"] = "newer"
        path.write_text(json.dumps(current))
        return 12

    monkeypatch.setattr(migration, "check_mcp", handshake)
    with pytest.raises(SetupError, match="保留"):
        migration.apply_migration(plan(legacy))
    assert load_profile(new / "erp" / "connection.json").password == "SECRET"


def test_migration_preserves_toml_comments_and_unrelated_tables(legacy):
    _, new, _, directory = legacy
    target = ClientTarget("codex", "Codex", directory.parent.parent / "config.toml", "toml")
    import tomlkit

    doc = tomlkit.document()
    doc.add(tomlkit.comment("keep this comment"))
    doc["model"] = "keep"
    doc["mcp_servers"] = {
        "erp": {
            "command": sys.executable,
            "args": [
                "-I",
                "-m",
                "sqlserver_semantic_mcp.main",
                "serve",
                "--config",
                str(directory / "connection.json"),
            ],
        },
        "other": {"command": "untouched"},
    }
    target.path.write_text(tomlkit.dumps(doc))
    migrated = migration.preview_migration(legacy_root=directory.parent, destination_root=new, clients=[target])
    migration.apply_migration(migrated)
    result = target.path.read_text()
    assert "# keep this comment" in result and "db_semantic_mcp.main" in result
    assert tomlkit.parse(result)["mcp_servers"]["other"]["command"] == "untouched"


def test_vault_acknowledgement_failure_removes_only_new_uuid(legacy, monkeypatch):
    _, new, _, directory = legacy
    raw = json.loads((directory / "connection.json").read_text())
    raw["credential"] = {"storage": "keyring", "key": "old-key"}
    (directory / "connection.json").write_text(json.dumps(raw))
    values = {(migration.LEGACY_SERVICE, "old-key"): "SECRET"}

    class Vault:
        def get_password(self, service, key):
            return values.get((service, key))

        def set_password(self, service, key, value):
            values[service, key] = value
            raise RuntimeError("lost acknowledgement SECRET")

        def delete_password(self, service, key):
            values.pop((service, key), None)

    monkeypatch.setattr(credentials, "_backend", Vault)
    with pytest.raises(SetupError):
        migration.apply_migration(plan(legacy))
    assert values == {(migration.LEGACY_SERVICE, "old-key"): "SECRET"}
    assert not (new / "erp" / "connection.json").exists()
