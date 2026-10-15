"""Route the agent's request down the shortest safe path."""

from __future__ import annotations

from ..policy.analyzer import SqlIntent
from ..services.policy_service import PolicyService
from .contracts import RouteDecision


def route_query(
    query: str | None,
    *,
    policy: PolicyService,
    database: str = "",
) -> RouteDecision:
    """Decide which path a ``query`` argument belongs to.

    * ``direct_execute``   — SQL-ready and currently allowed by policy
    * ``direct_validate``  — SQL-ready but policy denies → agent should revise
    * ``discovery``        — natural-language / unparseable → agent must explore
    * ``object_analysis``  — identified procedure/view reference (future hook)
    * ``policy_only``      — empty input / explicit policy inspection
    """
    if not query or not query.strip():
        return RouteDecision(
            route="policy_only",
            reason="empty query; nothing to execute or validate",
            recommended_tools=["get_execution_policy"],
            confidence=1.0,
        )

    intent: SqlIntent = policy.analyze(query)
    if not intent.is_sql_like or intent.requires_discovery:
        return RouteDecision(
            route="discovery",
            reason="input does not look like executable SQL",
            recommended_tools=[
                "discover_relevant_tables",
                "describe_table",
                "find_join_path",
            ],
            confidence=max(intent.confidence, 0.4),
        )

    validation = policy.validate(query, database=database, intent=intent)
    if validation["allowed"]:
        return RouteDecision(
            route="direct_execute",
            reason="policy allows direct execution",
            recommended_tools=["plan_or_execute_query"],
            confidence=intent.confidence,
        )
    return RouteDecision(
        route="direct_validate",
        reason=validation["reason"],
        recommended_tools=["plan_or_execute_query"],
        confidence=intent.confidence,
    )
