"""Request-local Oracle catalog resolution on the execution connection.

No snapshot can authorize a synonym. Resolve its current path each time, check
both aliases and base objects, and keep route identities in source cache paths.
"""

from dataclasses import dataclass, field

from ...config import Config
from ...policy.models import PolicyProfile
from ...policy.scope import ObjectReference, oracle_object_allowed
from . import oracle_catalog as catalog
from .oracle_read_safety import reject_policy_functions, verified_columns


@dataclass
class Resolution:
    references: list[ObjectReference] = field(default_factory=list)
    routes: dict[str, dict] = field(default_factory=dict)
    source_configs: dict[str, Config] = field(default_factory=dict)
    object_types: dict[ObjectReference, str] = field(default_factory=dict)
    targets: dict[ObjectReference, ObjectReference] = field(default_factory=dict)
    chains: dict[ObjectReference, list[ObjectReference]] = field(default_factory=dict)


class OracleResolver:
    def __init__(self, cfg: Config, conn, policy: PolicyProfile):
        # Canonical configs must carry the exact policy used by this proof.
        # Metadata consumers may construct another PolicyService from them;
        # that must never reload a newer file after authorization.
        self.policy = policy.model_copy(deep=True)
        self.cfg = cfg.model_copy(deep=True)
        self.cfg._policy_snapshot = self.policy
        self.conn = conn
        self.result = Resolution(source_configs={"local": self.cfg.for_source("local")})
        self._schema = None
        self._scope_frames = []

    def source_config(self, source: str, operation: str = "SELECT") -> Config:
        if self.policy.scope.allowed_databases and self.cfg.database not in self.policy.scope.allowed_databases:
            raise PermissionError("Database is not enabled by the global policy")
        if not getattr(self.policy.operations, operation.lower(), False):
            raise PermissionError("Operation is not enabled by the global policy")
        if source == "local":
            return self.result.source_configs["local"]
        scope = self.policy.scope.sources.get(source)
        definition = self.cfg.sources.get(source)
        if scope is None or definition is None:
            raise PermissionError("Remote source requires explicit configuration and policy scope")
        if not getattr(scope.operations, operation.lower(), False):
            raise PermissionError("Operation is not enabled for the remote source")
        if operation != "SELECT" and definition.readonly:
            raise PermissionError("Remote source is readonly")
        if source not in self.result.source_configs:
            candidate = self.cfg.for_source(source)
            route = catalog.resolve_db_link(candidate, self.conn)
            self.result.routes[source] = route
            self.result.source_configs[source] = self.cfg.for_source(
                source, route_identity={**route, "policy": self.policy.model_dump()}
            )
        return self.result.source_configs[source]

    def source_for_link(self, link: str, operation: str) -> str:
        # Check configuration/policy before touching any remote catalog. Full
        # and short forms are accepted only when the configured route proves it.
        candidates = []
        for source, definition in self.cfg.sources.items():
            configured = definition.db_link.upper()
            requested = link.upper()
            if (
                requested == configured
                or requested.startswith(configured + ".")
                or configured.startswith(requested + ".")
            ):
                if source in self.policy.scope.sources:
                    self.source_config(source, operation)
                    actual = self.result.routes[source]["db_link"].upper()
                    if requested == actual or actual.startswith(requested + "."):
                        candidates.append(source)
        if len(candidates) != 1:
            raise PermissionError("DB link is not a unique explicitly authorized source")
        return candidates[0]

    def _suffix(self, source):
        return "@" + self.result.routes[source]["db_link"] if source != "local" else ""

    def _authorize(self, ref, operation):
        self.source_config(ref.source, operation)
        scope = self.policy.scope if ref.source == "local" else self.policy.scope.sources[ref.source]
        deny_scope = scope.model_copy(update={"allowed_schemas": [], "allowed_tables": []})
        if not oracle_object_allowed(deny_scope, self.cfg.database, ref.schema, ref.name):
            raise PermissionError("Oracle alias or target is denied by source policy scope")
        self.result.references.append(ref)
        self._scope_frames[-1].append(ref)

    def resolve(
        self, ref: ObjectReference, operation="SELECT", *, path=(), depth=0, expand_views=True, default_schema=""
    ):
        frame = []
        self._scope_frames.append(frame)
        try:
            target = self._resolve(
                ref, operation, path=path, depth=depth, expand_views=expand_views, default_schema=default_schema
            )
            intrinsic = (
                operation == "SELECT"
                and target.schema == "SYS"
                and target.name == "DUAL"
                and self.result.object_types.get(target) == "TABLE"
            )
            # A view's dependencies have separate frames and were already
            # authorized, including any catalog-proven intrinsic DUAL path.
            for touched in frame:
                scope = self.policy.scope if touched.source == "local" else self.policy.scope.sources[touched.source]
                if not intrinsic and not oracle_object_allowed(scope, self.cfg.database, touched.schema, touched.name):
                    raise PermissionError("Oracle alias or target is denied by source policy scope")
            key = ObjectReference(
                "", ref.schema or default_schema, ref.name, source=ref.source, db_link=ref.db_link, dialect="oracle"
            )
            self.result.targets[key] = target
            self.result.chains[key] = frame
            return target
        finally:
            self._scope_frames.pop()

    def _resolve(
        self, ref: ObjectReference, operation="SELECT", *, path=(), depth=0, expand_views=True, default_schema=""
    ):
        if depth >= 24:
            raise PermissionError("Oracle resolution depth limit exceeded")
        source = ref.source
        if ref.db_link:
            if source != "local":
                raise PermissionError("Chained remote DB links cannot be proven from the local session")
            source = self.source_for_link(ref.db_link, operation)
        self.source_config(source, operation)
        if ref.schema:
            owner = ref.schema
        elif default_schema:
            owner = default_schema
        elif source != "local":
            raise PermissionError("Remote SQL requires an explicit object owner; remote CURRENT_SCHEMA is unproven")
        else:
            if self._schema is None:
                self._schema = catalog.current_schema(self.cfg.for_source("local"), self.conn)
            owner = self._schema
        current = ObjectReference("", owner, ref.name, source=source, dialect="oracle")
        key = (source, owner, ref.name)
        if key in path:
            raise PermissionError("Oracle synonym or dependency cycle cannot be authorized")
        suffix = self._suffix(source)
        params = {"owner": owner, "name": ref.name}
        rows = catalog._fetch(
            self.conn,
            f"SELECT object_type FROM sys.all_objects{suffix} WHERE owner=:owner AND object_name=:name "
            "AND object_type IN ('TABLE','VIEW','MATERIALIZED VIEW','SYNONYM')",
            params,
        )
        physical = {r[0] for r in rows} - {"SYNONYM"}
        if physical:
            self._authorize(current, operation)
            self.result.object_types[current] = (
                "MATERIALIZED VIEW" if "MATERIALIZED VIEW" in physical else next(iter(physical))
            )
            if physical == {"VIEW"} and expand_views:
                # Views can invoke functions or conceal sources. Parse the live
                # SELECT and resolve with the view owner as its lexical schema.
                if operation != "SELECT":
                    raise PermissionError("Oracle writes through views are not supported")
                definition = catalog.get_object_definition(
                    self.source_config(source), owner, ref.name, "VIEW", conn=self.conn
                )
                from sqlglot import exp

                from ...policy.intents.oracle_analyzer import parse_oracle

                tree, references = parse_oracle(definition or "")
                if not isinstance(tree, exp.Query):
                    raise PermissionError("Oracle view definition cannot be proven safe")
                for child in references:
                    child = ObjectReference(
                        "", child.schema, child.name, source=source, db_link=child.db_link, dialect="oracle"
                    )
                    self.resolve(child, "SELECT", path=(*path, key), depth=depth + 1, default_schema=owner)
                self.verify_columns(tree, default_schema=owner, source=source)
            return current
        synonyms = catalog._fetch(
            self.conn,
            f"SELECT table_owner, table_name, db_link FROM sys.all_synonyms{suffix} "
            "WHERE owner=:owner AND synonym_name=:name",
            params,
        )
        if not synonyms and not ref.schema:
            owner = "PUBLIC"
            current = ObjectReference("", owner, ref.name, source=source, dialect="oracle")
            key = (source, owner, ref.name)
            synonyms = catalog._fetch(
                self.conn,
                f"SELECT table_owner, table_name, db_link FROM sys.all_synonyms{suffix} "
                "WHERE owner='PUBLIC' AND synonym_name=:name",
                {"name": ref.name},
            )
        if len(synonyms) != 1:
            raise PermissionError("Oracle source is missing, inaccessible or ambiguous")
        self._authorize(current, operation)
        self.result.object_types[current] = "SYNONYM"
        target_owner, target_name, link = synonyms[0]
        if not target_owner or not target_name:
            raise PermissionError("Oracle synonym target cannot be proven")
        target = ObjectReference("", target_owner, target_name, source=source, db_link=link or "", dialect="oracle")
        return self._resolve(target, operation, path=(*path, key), depth=depth + 1, expand_views=expand_views)

    def verify_columns(self, tree, *, default_schema="", source="local"):
        """Prove identifier expressions are columns, not bare PL/SQL calls.

        SQLGlot cannot distinguish `SELECT evil FROM sys.dual` from a column read.
        Oracle can execute a zero-argument function there. Resolve columns from
        the live target catalog, then validate CTE/projection/alias scopes on an
        analysis-only tree. Never send its renamed tables to the driver.
        """
        from sqlglot import exp
        from sqlglot.optimizer.qualify import qualify
        from sqlglot.schema import MappingSchema

        analysis = tree.copy()
        schema = MappingSchema(dialect="oracle", normalize=False)
        target_columns = {}
        table_names = set()
        for index, table in enumerate(analysis.find_all(exp.Table)):
            reference = table.meta.get("oracle_reference")
            if reference is None:
                continue  # lexical CTE binding, resolved by qualify()
            reference = ObjectReference(
                "",
                reference.schema or default_schema,
                reference.name,
                source=source,
                db_link=reference.db_link,
                dialect="oracle",
            )
            target = self.result.targets.get(reference)
            if target is None:
                raise PermissionError("Unresolved Oracle column source")
            for alias in self.result.chains.get(reference, []):
                if alias != target:
                    reject_policy_functions(self.conn, alias.schema, alias.name, self._suffix(alias.source))
            columns = verified_columns(
                self.conn,
                target.schema,
                target.name,
                self._suffix(target.source),
                self.result.object_types[target],
            )
            target_columns.update(columns)
            alias = table.args.get("alias") or exp.TableAlias(this=table.this.copy())
            table_names.add(alias.name)
            table_names.add(reference.name)
            marker = f"__DBMCP_COLUMNS_{index}"
            table.set("this", exp.Identifier(this=marker, quoted=True))
            table.set("db", None)
            table.set("catalog", None)
            table.set("alias", alias)
            schema.add_table(table, columns, normalize=False)
        if isinstance(analysis, exp.Insert):
            if any(analysis.expression.find_all(exp.Column)):
                raise PermissionError("INSERT values cannot contain unresolved identifier expressions")
            target = analysis.this
            if isinstance(target, exp.Schema) and any(i.name not in target_columns for i in target.expressions):
                raise PermissionError("Unknown Oracle INSERT column")
        elif isinstance(analysis, exp.Update):
            for column in analysis.find_all(exp.Column):
                if column.name not in target_columns or column.db or column.table and column.table not in table_names:
                    raise PermissionError("Unproven Oracle UPDATE column or bare function")
        else:
            if any(column.db or column.catalog for column in analysis.find_all(exp.Column)):
                raise PermissionError("Use a table alias for Oracle qualified columns")
            try:
                qualify(analysis, dialect="oracle", schema=schema, expand_stars=False)
            except Exception:  # noqa: BLE001 (do not expose SQLGlot SQL fragments)
                raise PermissionError("Unproven Oracle column or bare function") from None


def resolve_object(
    cfg: Config, conn, policy: PolicyProfile, schema: str, name: str, *, source="local", operation="SELECT"
) -> dict:
    """Bounded-worker API; raises PermissionError on an unproven or denied path.

    Names are decoded catalog names (use oracle_identifier for caller SQL-style
    identifiers). Caller owns conn. canonical_config carries route+policy cache
    identity and must be used for final-target metadata/cache work.
    """
    resolver = OracleResolver(cfg, conn, policy)
    initial = ObjectReference("", schema, name, source=source, dialect="oracle")
    terminal = resolver.resolve(initial, operation)

    def describe(ref):
        return {
            "source": ref.source,
            "schema": ref.schema,
            "name": ref.name,
            "object_type": resolver.result.object_types.get(ref, "UNKNOWN"),
        }

    return {
        "allowed": True,
        "chain": [describe(r) for r in resolver.result.chains[initial]],
        "target": describe(terminal),
        "canonical_config": resolver.result.source_configs[terminal.source],
        "source_configs": resolver.result.source_configs,
    }
