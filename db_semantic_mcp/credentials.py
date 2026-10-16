"""Credentials remain outside connection and MCP client configuration."""

import json
import os
import re
import stat
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, model_validator

from .setup.files import SetupError, atomic_write, read_snapshot

SERVICE = "db-semantic-mcp"
_SECURE_MODULES = (
    "keyring.backends.macOS",
    "keyring.backends.Windows",
    "keyring.backends.SecretService",
    "keyring.backends.kwallet",
)


class CredentialError(SetupError):
    pass


class CredentialReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    storage: Literal["keyring", "file", "env"]
    key: str

    @model_validator(mode="after")
    def validate_key(self):
        if self.storage == "file" and self.key != "credentials.json":
            raise ValueError("File credentials must use credentials.json")
        if self.storage == "env" and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.key):
            raise ValueError("Invalid environment variable name")
        if not self.key:
            raise ValueError("Empty credential key")
        return self


def _backend():
    import keyring

    backend = keyring.get_keyring()
    candidates = getattr(backend, "backends", [backend])
    for candidate in candidates:
        if candidate.__class__.__module__.startswith(_SECURE_MODULES):
            return candidate
    raise CredentialError("無法使用系統憑證庫。請啟用 Keychain、Credential Manager 或 Secret Service。")


def store_password(password: str) -> CredentialReference:
    key = uuid4().hex
    backend = None
    try:
        backend = _backend()
        backend.set_password(SERVICE, key, password)
    except Exception:  # noqa: BLE001 (keyring access is best-effort)
        # A backend may store successfully and then fail acknowledgement. This
        # UUID belongs only to this attempt, so clean it without touching old refs.
        if backend is not None:
            try:
                backend.delete_password(SERVICE, key)
            except Exception:  # noqa: BLE001 (best-effort cleanup of our new UUID)
                pass
        raise CredentialError("無法將密碼存入系統憑證庫。") from None
    return CredentialReference(storage="keyring", key=key)


def private_password_bytes(password: str) -> bytes:
    return json.dumps({"password": password}, ensure_ascii=False).encode()


def store_private_password(
    directory: Path,
    password: str,
    *,
    expected=...,
    receipt: dict | None = None,
) -> CredentialReference:
    if os.name == "nt":
        raise CredentialError("Windows 請使用系統憑證庫或密碼環境變數。")
    if directory.is_symlink():
        raise CredentialError("憑證目錄不得為符號連結。")
    read_snapshot(directory / "credentials.json")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    atomic_write(
        directory / "credentials.json",
        private_password_bytes(password),
        receipt=receipt,
        **({"expected": expected} if expected is not ... else {}),
    )
    return CredentialReference(storage="file", key="credentials.json")


def read_password(reference: CredentialReference, directory: Path) -> str:
    try:
        if reference.storage == "env":
            password = os.environ.get(reference.key)
        elif reference.storage == "keyring":
            password = _backend().get_password(SERVICE, reference.key)
        else:
            path = directory / reference.key
            data = read_snapshot(path)
            if data is None:
                raise CredentialError("找不到本機憑證檔。")
            if os.name == "nt":
                raise CredentialError("Windows 不支援檔案憑證模式。")
            if stat.S_IMODE(path.stat().st_mode) & 0o077:
                raise CredentialError("憑證檔權限過寬，應設為 0600。")
            if stat.S_IMODE(directory.stat().st_mode) & 0o077:
                raise CredentialError("憑證目錄權限過寬，應設為 0700。")
            password = json.loads(data)["password"]
    except CredentialError:
        raise
    except Exception:  # noqa: BLE001 (keyring access is best-effort)
        raise CredentialError("無法讀取配置指定的密碼。請重新執行 setup。") from None
    if not isinstance(password, str) or not password:
        raise CredentialError("找不到配置指定的密碼。請重新執行 setup 或設定指定的環境變數。")
    return password


def delete_password(reference: CredentialReference | None) -> None:
    if reference is not None and reference.storage == "keyring":
        try:
            _backend().delete_password(SERVICE, reference.key)
        except Exception:  # noqa: BLE001 (keyring access is best-effort)
            pass
