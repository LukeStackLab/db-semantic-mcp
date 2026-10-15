"""Direct-execution fast path for SQL-ready agents."""

from __future__ import annotations

from ..config import Config, get_config
from ..services.policy_service import PolicyService
from ..services.query_service import QueryService
from .contracts import ToolEnvelope
from .router import route_query


def plan_or_execute_query(
    query: str,
    *,
    policy: PolicyService,
    query_service: QueryService,
    mode: str = "auto",
    max_rows: int | None = None,
    affected_rows_policy: str | None = None,
    validate_result_schema: bool = False,
    cfg: Config | None = None,
) -> dict:
    """Single entry point for agents holding ready-to-run SQL.

    mode:
      * ``auto``           — execute if safe, otherwise return plan
      * ``validate``       — validate + risk breakdown, then stop
      * ``dry_run``        — return preview (validation + shape, no side effects)
      * ``execute_if_safe``— same as ``auto`` (kept as alias for clarity)
    """
    cfg = cfg or get_config()
    database = cfg.database

    # Explicit sub-modes short-circuit routing.
    if mode == "validate":
        payload = query_service.validate_query(query, database=database)
        intent = policy.analyze(query)
        risk_level, risks = _assess_risk(intent, policy)
        return ToolEnvelope(
            kind="plan_or_execute_query",
            confidence=payload["intent"]["confidence"],
            next_action=payload["next_action"],
            recommended_tool="plan_or_execute_query",
            data={
                "path": "direct_validate",
                "executed": False,
                "risk_level": risk_level,
                "risks": risks,
                **payload,
            },
        ).to_dict()

    if mode == "dry_run":
        preview = query_service.preview_query(
            query,
            max_rows=max_rows,
            database=database,
        )
        return ToolEnvelope(
            kind="plan_or_execute_query",
            next_action=preview["next_action"],
            recommended_tool="plan_or_execute_query",
            data={"path": "dry_run", "executed": False, **preview},
        ).to_dict()

    decision = route_query(query, policy=policy, database=database)

    if decision.route == "direct_execute" and cfg.direct_execute_enabled:
        result = query_service.execute_query(
            query,
            max_rows=max_rows,
            affected_rows_policy=affected_rows_policy,
            validate_result_schema=validate_result_schema,
            database=database,
        )
        if result.get("executed"):
            # Execution already passed the same policy; the intent remains
            # available through mode="validate" rather than repeated here.
            result = {key: value for key, value in result.items() if key != "validation"}
        data = {"path": "direct_execute", **result}
        return ToolEnvelope(
            kind="plan_or_execute_query",
            confidence=decision.confidence,
            next_action=result.get("next_action", "done"),
            recommended_tool=None,
            data=data,
        ).to_dict()

    if decision.route == "direct_validate":
        # Policy denied — don't execute even under mode=auto.
        payload = query_service.validate_query(query, database=database)
        return ToolEnvelope(
            kind="plan_or_execute_query",
            confidence=decision.confidence,
            next_action=payload["next_action"],
            recommended_tool="plan_or_execute_query",
            data={
                "path": "direct_validate",
                "executed": False,
                **payload,
                "route": decision.to_dict(),
            },
        ).to_dict()

    # discovery / policy_only
    return ToolEnvelope(
        kind="plan_or_execute_query",
        confidence=decision.confidence,
        next_action="discover",
        recommended_tool=decision.recommended_tools[0] if decision.recommended_tools else "discover_relevant_tables",
        data={
            "path": decision.route,
            "executed": False,
            "reason": decision.reason,
            "route": decision.to_dict(),
        },
    ).to_dict()


def _assess_risk(intent, policy):
    """Risk accumulation backing ``plan_or_execute_query(mode="validate")``.

    Returns a concise ``(level, risks_list)`` pair — callers that already have
    the full intent/payload should not need the intent re-serialized here.
    """
    order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    level = "low"
    risks = []

    def bump(new):
        nonlocal level
        if order[new] > order[level]:
            level = new

    if intent.risk_level.value in ("critical", "high"):
        bump(intent.risk_level.value)
        risks.append(
            {"kind": "policy_risk", "detail": f"{intent.primary_operation.value} is {intent.risk_level.value}-risk"}
        )
    if intent.is_multi_statement and not policy.current_policy().constraints.allow_multi_statement:
        bump("high")
        risks.append({"kind": "policy_risk", "detail": "multi-statement disallowed"})
    if intent.has_unqualified_tables:
        bump("medium")
        risks.append({"kind": "schema_qualification_risk", "detail": "unqualified tables"})
    if intent.contains_dynamic_sql:
        bump("high")
        risks.append({"kind": "dynamic_sql_risk", "detail": "dynamic SQL not inspectable"})
    if intent.primary_operation.value == "SELECT" and not intent.has_top_clause and not intent.has_where_clause:
        bump("medium")
        risks.append({"kind": "payload_risk", "detail": "SELECT without TOP/WHERE"})
    return level, risks
