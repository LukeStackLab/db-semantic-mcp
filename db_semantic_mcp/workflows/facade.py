"""Single workflow entry point exposed to ``server.app.Context``."""

from __future__ import annotations

from ..config import Config
from ..services.policy_service import PolicyService
from ..services.query_service import QueryService
from .bundle import bundle_context_for_next_step
from .discovery_flow import discover_relevant_tables
from .query_flow import plan_or_execute_query


class WorkflowFacade:
    """Thin wrapper so tool modules reach workflow helpers via one object."""

    def __init__(
        self,
        cfg: Config,
        policy: PolicyService,
        query: QueryService,
    ) -> None:
        self.cfg = cfg
        self.policy = policy
        self.query = query

    # ---- synchronous helpers ------------------------------------------------

    def plan_or_execute_query(
        self,
        query: str,
        *,
        mode: str = "auto",
        max_rows: int | None = None,
        affected_rows_policy: str | None = None,
        validate_result_schema: bool = False,
    ) -> dict:
        return plan_or_execute_query(
            query,
            policy=self.policy,
            query_service=self.query,
            mode=mode,
            max_rows=max_rows,
            affected_rows_policy=affected_rows_policy,
            validate_result_schema=validate_result_schema,
            cfg=self.cfg,
        )

    # ---- async helpers ------------------------------------------------------

    async def discover_relevant_tables(
        self,
        goal: str,
        *,
        schemas: list[str] | None = None,
        keyword: str | None = None,
        limit: int = 10,
        classify: bool = False,
    ) -> dict:
        return await discover_relevant_tables(
            goal,
            schemas=schemas,
            keyword=keyword,
            limit=limit,
            classify=classify,
            cfg=self.cfg,
            policy=self.policy,
        )

    async def bundle_context_for_next_step(
        self,
        items: list[dict],
        *,
        goal: str = "joining",
    ) -> dict:
        return await bundle_context_for_next_step(
            items,
            goal=goal,
            cfg=self.cfg,
            policy=self.policy,
        )
