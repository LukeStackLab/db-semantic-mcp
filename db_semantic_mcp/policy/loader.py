import json
import logging
from pathlib import Path

from ..config import Config, get_config
from .models import (
    PolicyConstraints,
    PolicyFile,
    PolicyOperations,
    PolicyProfile,
)

logger = logging.getLogger(__name__)


def builtin_readonly() -> PolicyProfile:
    return PolicyProfile(
        profile_name="readonly",
        operations=PolicyOperations(select=True),
        constraints=PolicyConstraints(
            max_rows_returned=1000,
            allow_multi_statement=False,
        ),
    )


def select_profile(pf: PolicyFile, override: str | None) -> PolicyProfile:
    name = override or pf.active_profile
    if name not in pf.profiles:
        raise ValueError(f"Profile '{name}' not found in policy file")
    profile = pf.profiles[name]
    profile.profile_name = name
    return profile


def apply_env_overrides(profile: PolicyProfile, cfg: Config) -> PolicyProfile:
    data = profile.model_dump()
    for key, value in {
        "max_rows_returned": cfg.max_rows_returned,
        "max_rows_affected": cfg.max_rows_affected,
        "query_timeout_seconds": cfg.query_timeout,
    }.items():
        data["constraints"][key] = min(data["constraints"][key], value) if cfg.connection_name else value
    return PolicyProfile.model_validate(data)


def load_policy_from_file(
    path: str | None,
    profile_override: str | None,
) -> PolicyProfile:
    if not path:
        logger.warning("No policy file specified; using built-in readonly profile")
        return builtin_readonly()

    p = Path(path)
    if not p.exists():
        raise ValueError("Explicit policy file is missing; refusing to load")

    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise ValueError("Explicit policy file is unreadable; refusing to load") from None

    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        raise ValueError("Explicit policy file contains invalid JSON; refusing to load") from None

    try:
        pf = PolicyFile.model_validate(raw)
    except Exception:  # noqa: BLE001 (controlled error replacement)
        raise ValueError("Explicit policy file failed schema validation; refusing to load") from None

    # Profile-override errors still raise (caller misconfiguration)
    return select_profile(pf, profile_override)


def load_active_policy(cfg: Config | None = None) -> PolicyProfile:
    cfg = cfg or get_config()
    base = load_policy_from_file(cfg.policy_file, cfg.policy_profile)
    return apply_env_overrides(base, cfg)
