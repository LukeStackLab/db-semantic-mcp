from . import (
    cache,
    metadata,
    metrics,
    object_tool,
    policy,
    query,
    relationship,
    semantic,
    workflow,
)  # noqa: F401

_GROUP_REGISTRATIONS = {
    "metadata": metadata.register,
    "policy": policy.register,
    "query": query.register,
    "cache": cache.register,
    "relationship": relationship.register,
    "object": object_tool.register,
    "semantic": semantic.register,
    "metrics": metrics.register,
    "workflow": workflow.register,
}

_NAMED_PROFILES = {
    "all": list(_GROUP_REGISTRATIONS),
    "query": ["policy", "query"],
    "explore": ["metadata", "policy", "query", "relationship", "workflow"],
    "maintenance": ["policy", "cache", "metrics"],
}


def _resolve_profile_groups(profile: str) -> list[str]:
    if not profile:
        return list(_GROUP_REGISTRATIONS.keys())
    if profile in _NAMED_PROFILES:
        return list(_NAMED_PROFILES[profile])
    requested = [g.strip() for g in profile.split(",") if g.strip()]
    if not requested:
        return list(_GROUP_REGISTRATIONS.keys())
    unknown = [g for g in requested if g not in _GROUP_REGISTRATIONS]
    if unknown:
        raise ValueError(
            f"Unknown tool profile group(s): {', '.join(unknown)}. "
            f"Valid groups: {', '.join(sorted(_GROUP_REGISTRATIONS.keys()))}"
        )
    return list(dict.fromkeys(requested))


def register_all() -> None:
    from ...config import get_config

    cfg = get_config()
    groups = _resolve_profile_groups(cfg.tool_profile)
    if not cfg.workflow_tools_enabled and "workflow" in groups:
        groups = [g for g in groups if g != "workflow"]
    for group in groups:
        _GROUP_REGISTRATIONS[group]()
