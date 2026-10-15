import os
from unittest.mock import Mock

import pytest

from db_semantic_mcp import credentials
from db_semantic_mcp.credentials import CredentialError, CredentialReference
from db_semantic_mcp.setup.files import SetupError, atomic_write


def test_keyring_round_trip_without_password_in_reference(monkeypatch, tmp_path):
    backend = Mock()
    backend.get_password.return_value = "secret-value"
    monkeypatch.setattr(credentials, "_backend", lambda: backend)
    reference = credentials.store_password("secret-value")
    assert "secret-value" not in reference.model_dump_json()
    assert credentials.read_password(reference, tmp_path) == "secret-value"
    credentials.delete_password(reference)
    backend.delete_password.assert_called_once()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file permissions")
def test_private_file_permissions_and_symlinks(tmp_path):
    directory = tmp_path / "private"
    reference = credentials.store_private_password(directory, "secret-value")
    path = directory / "credentials.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700
    assert credentials.read_password(reference, directory) == "secret-value"
    path.chmod(0o644)
    with pytest.raises(CredentialError, match="0600"):
        credentials.read_password(reference, directory)
    path.unlink()
    path.symlink_to(tmp_path / "outside.json")
    with pytest.raises(CredentialError):
        credentials.read_password(reference, directory)


@pytest.mark.parametrize("key", ["../secret", "/etc/passwd", "other.json"])
def test_file_credential_reference_cannot_escape(key):
    with pytest.raises(ValueError):
        CredentialReference(storage="file", key=key)


def test_atomic_write_preserves_original_on_conflict_and_symlink(tmp_path):
    path = tmp_path / "config.json"
    atomic_write(path, b"old")
    path.write_bytes(b"external-change")
    with pytest.raises(SetupError, match="其他程序"):
        atomic_write(path, b"new", expected=b"old", backup=True)
    assert path.read_bytes() == b"external-change"
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(SetupError, match="符號連結"):
        atomic_write(link, b"wrong")
    assert path.read_bytes() == b"external-change"


def test_unavailable_backend_never_echoes_secret(monkeypatch):
    def unavailable():
        raise RuntimeError("DO_NOT_ECHO")

    monkeypatch.setattr(credentials, "_backend", unavailable)
    with pytest.raises(CredentialError) as error:
        credentials.store_password("DO_NOT_ECHO")
    assert "DO_NOT_ECHO" not in str(error.value)


def test_ancestor_symlink_is_rejected(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "alias"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(SetupError, match="符號連結"):
        atomic_write(link / "nested" / "config.json", b"must-not-write")
    assert not (real / "nested").exists()


def test_cooperating_writers_cannot_overwrite_same_snapshot(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    path = tmp_path / "config.json"
    atomic_write(path, b"original")
    barrier = Barrier(2)

    def writer(data):
        barrier.wait()
        try:
            atomic_write(path, data, expected=b"original")
            return True
        except SetupError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(writer, [b"first", b"second"]))
    assert sum(results) == 1
    assert path.read_bytes() in {b"first", b"second"}


def test_rollback_never_deletes_replacement_with_identical_bytes(tmp_path):
    from db_semantic_mcp.setup.files import restore_owned

    path = tmp_path / "policy.json"
    receipt = {}
    atomic_write(path, b"same", expected=None, receipt=receipt)
    atomic_write(path, b"same", expected=b"same")
    assert not restore_owned(path, None, b"same", receipt["identity"])
    assert path.read_bytes() == b"same"
