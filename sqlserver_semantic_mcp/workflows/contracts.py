"""Handoff contracts for workflow tools.

All workflow tools return structured envelopes so a downstream agent
knows what to do next without re-parsing payloads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Route = Literal[
    "direct_execute",
    "direct_validate",
    "discovery",
    "object_analysis",
    "policy_only",
]


@dataclass
class RouteDecision:
    route: Route
    reason: str
    recommended_tools: list[str] = field(default_factory=list)
    confidence: float = 1.0

    def to_dict(self) -> dict:
        return {
            "route": self.route,
            "reason": self.reason,
            "recommended_tools": list(self.recommended_tools),
            "confidence": self.confidence,
        }


@dataclass
class ToolEnvelope:
    """Uniform envelope returned by workflow tools.

    ``data`` carries the tool-specific payload. Top-level fields are the
    agent-visible routing cues.
    """

    kind: str
    confidence: float | None = None
    next_action: str | None = None
    recommended_tool: str | None = None
    bundle_key: str | None = None
    data: Any = None

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"kind": self.kind}
        if self.confidence is not None:
            out["confidence"] = self.confidence
        if self.next_action is not None:
            out["next_action"] = self.next_action
        if self.recommended_tool is not None:
            out["recommended_tool"] = self.recommended_tool
        if self.bundle_key is not None:
            out["bundle_key"] = self.bundle_key
        if self.data is not None:
            out["data"] = self.data
        return out
