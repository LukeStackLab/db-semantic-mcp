"""Authorization regressions use AST even when routing uses regex."""

import pytest

from db_semantic_mcp.config import Config
from db_semantic_mcp.policy.models import (
    PolicyConstraints,
    PolicyOperations,
    PolicyProfile,
    PolicyScope,
)
from db_semantic_mcp.services.policy_service import PolicyService


def service(*, scope=None, operations=None, constraints=None, analyzer="regex"):
    svc = PolicyService(
        Config(
            server="unused",
            database="app",
            intent_analyzer=analyzer,
            policy_file=None,
            policy_profile=None,
            _env_file=None,
        )
    )
    svc._policy = PolicyProfile(
        profile_name="test",
        scope=scope or PolicyScope(),
        operations=operations or PolicyOperations(),
        constraints=constraints or PolicyConstraints(),
    )
    return svc


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM dbo.Users; DELETE FROM dbo.Users WHERE Id=1",
        "SELECT * FROM dbo.Users; SELECT * FROM dbo.Secrets",
        "SELECT * FROM dbo.Users, dbo.Secrets",
        "SELECT * FROM dbo.Users WHERE EXISTS(SELECT 1 FROM dbo.Secrets)",
        "WITH c AS (SELECT * FROM dbo.Secrets) SELECT * FROM c",
        "WITH c AS (SELECT * FROM dbo.Users) SELECT * FROM c UNION ALL SELECT * FROM dbo.Secrets",
        "WITH c AS (SELECT * FROM dbo.Users) SELECT * FROM c; SELECT * FROM c",
        "SELECT * FROM other.dbo.Users",
        "SELECT * FROM linked.app.dbo.Users",
        "SELECT * FROM app..Users",
    ],
)
def test_all_statements_and_physical_sources_are_authorized(sql):
    svc = service(
        scope=PolicyScope(allowed_databases=["app"], allowed_schemas=["dbo"], allowed_tables=["Users"]),
        constraints=PolicyConstraints(allow_multi_statement=True),
    )
    assert svc.validate(sql)["allowed"] is False


@pytest.mark.parametrize(
    "sql",
    [
        "WITH c AS (SELECT * FROM dbo.Users) SELECT * FROM c",
        "WITH c AS (SELECT * FROM dbo.Users), d AS (SELECT * FROM c) SELECT * FROM d",
        "SELECT * FROM app.dbo.Users",
        "SELECT ';--not SQL' AS s FROM dbo.Users",
        'SELECT [DELETE], "UPDATE" FROM [dbo].[Users]',
        "SELECT 1; -- trailing comment",
    ],
)
def test_ctes_and_literal_punctuation_do_not_invent_objects_or_statements(sql):
    svc = service(scope=PolicyScope(allowed_databases=["app"], allowed_schemas=["dbo"], allowed_tables=["dbo.Users"]))
    result = svc.validate(sql)
    assert result["allowed"], result


def test_cte_shadowing_does_not_hide_qualified_physical_table():
    svc = service(scope=PolicyScope(denied_tables=["dbo.c"]))
    assert svc.validate("WITH c AS (SELECT 1 x) SELECT * FROM c")["allowed"]
    assert not svc.validate("WITH c AS (SELECT 1 x) SELECT * FROM dbo.c")["allowed"]


def test_nested_cte_scope_does_not_leak_into_outer_source():
    svc = service(scope=PolicyScope(denied_tables=["c"]))
    sql = "SELECT * FROM (WITH c AS (SELECT 1 x) SELECT * FROM c) a, c"
    assert not svc.validate(sql)["allowed"]


def test_quoted_identifier_components_preserve_dots_and_escapes():
    svc = service(
        scope=PolicyScope(
            allowed_schemas=["a.b"],
            allowed_tables=["[a.b].[x]]y]"],
        )
    )
    assert svc.validate("SELECT * FROM [a.b].[x]]y]")["allowed"]
    assert not svc.validate("SELECT * FROM [a].[b.x]]y]")["allowed"]


def test_update_alias_resolves_only_its_statement_sources():
    svc = service(
        scope=PolicyScope(allowed_schemas=["dbo"], allowed_tables=["dbo.Users"]),
        operations=PolicyOperations(update=True),
    )
    assert svc.validate("UPDATE u SET x=1 FROM dbo.Users u WHERE u.Id=1")["allowed"]
    assert not svc.validate("UPDATE u SET x=1 FROM dbo.Secrets u WHERE u.Id=1")["allowed"]


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE dbo.Users SET x=(SELECT 1 WHERE 1=1)",
        "UPDATE dbo.Users SET x=1 WHERE Id=1; UPDATE dbo.Users SET x=2",
    ],
)
def test_where_constraint_checks_each_write_root(sql):
    svc = service(operations=PolicyOperations(update=True), constraints=PolicyConstraints(allow_multi_statement=True))
    assert not svc.validate(sql)["allowed"]


def test_top_constraint_checks_outer_query_and_each_union_branch():
    svc = service(constraints=PolicyConstraints(require_top_for_select=True))
    assert not svc.validate("SELECT (SELECT TOP 1 Id FROM dbo.Users) FROM dbo.Users")["allowed"]
    assert not svc.validate("SELECT TOP 1 Id FROM dbo.Users UNION ALL SELECT Id FROM dbo.Users")["allowed"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * INTO dbo.Copy FROM dbo.Users",
        "BEGIN TRAN; SELECT 1; COMMIT",
        "EXEC dbo.Run",
        "EXEC sp_executesql N'SELECT * FROM dbo.Secrets'",
        "IF 1=1 SELECT 1",
        "SET NOCOUNT ON; SELECT 1",
        "USE other; SELECT 1",
        "DBCC CHECKDB",
        "SELECT NEXT VALUE FOR dbo.Seq",
        "SELECT * FROM OPENQUERY(linked, 'SELECT * FROM dbo.Secrets')",
        "SELECT * FROM dbo.TableFunction(1)",
        "SELECT dbo.ScalarFunction(1)",
        "SELECT * FROM #Users",
        "SELECT * FROM @Users",
        "THIS IS NOT VALID SQL",
    ],
)
def test_unsupported_effects_fail_closed_even_for_write_profiles(sql):
    svc = service(
        operations=PolicyOperations(**{name: True for name in PolicyOperations.model_fields}),
        constraints=PolicyConstraints(allow_multi_statement=True),
    )
    assert not svc.validate(sql)["allowed"]


def test_shared_object_scope_matches_sql_and_raises_on_denial():
    svc = service(
        scope=PolicyScope(
            allowed_databases=["app"],
            allowed_schemas=["dbo"],
            allowed_tables=["app.dbo.Users"],
            denied_tables=["secrets"],
        )
    )
    assert svc.object_allowed("dbo", "Users")
    assert svc.authorize_object("dbo", "Users") is None
    assert not svc.object_allowed("dbo", "Users", "other")
    with pytest.raises(PermissionError):
        svc.authorize_object("dbo", "Secrets")


def test_scope_allows_exact_case_but_denies_case_variants():
    svc = service(scope=PolicyScope(allowed_tables=["dbo.Users"], denied_tables=["SECRET"]))
    assert svc.object_allowed("dbo", "Users")
    assert not svc.object_allowed("dbo", "users")
    assert not svc.object_allowed("dbo", "secret")


def test_qualified_denial_cannot_be_bypassed_with_unqualified_name():
    svc = service(scope=PolicyScope(denied_tables=["dbo.Secrets"]))
    assert not svc.validate("SELECT * FROM Secrets")["allowed"]


def test_database_scope_rejects_missing_database():
    from db_semantic_mcp.policy.scope import object_allowed

    assert not object_allowed(PolicyScope(allowed_databases=["app"]), "", "dbo", "Users")


def test_malformed_scope_entry_fails_closed():
    svc = service(scope=PolicyScope(denied_tables=["dbo.Secret; SELECT 1"]))
    assert not svc.object_allowed("dbo", "Users")


@pytest.mark.parametrize("value", ["dbo.T; SELECT 1", "dbo.F(1)", "dbo.T t", "dbo.T TABLESAMPLE(5)"])
def test_object_name_split_rejects_extra_sql(value):
    from db_semantic_mcp.policy.scope import split_object_name

    with pytest.raises(ValueError):
        split_object_name(value)


def test_object_name_split_preserves_decoded_identifiers():
    from db_semantic_mcp.policy.scope import split_object_name

    assert split_object_name("[app.db].[a.b].[x]]y]") == ("app.db", "a.b", "x]y")


def test_unsupported_sql_does_not_log_caller_literals(caplog):
    svc = service()
    assert not svc.validate("BEGIN PRINT 'caller-private-literal' END")["allowed"]
    assert "caller-private-literal" not in caplog.text


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE dbo.Users(Id INT)",
        "CREATE VIEW dbo.v AS SELECT * FROM dbo.Users",
        "ALTER TABLE dbo.Users ADD x INT",
        "DROP TABLE dbo.Users",
        "TRUNCATE TABLE dbo.Users",
        "DELETE u FROM dbo.Users u WHERE u.Id=1",
        "WITH c AS (SELECT * FROM dbo.Users) UPDATE c SET x=1 WHERE Id=1",
        "WITH c AS (SELECT * FROM dbo.Users) INSERT INTO dbo.v SELECT * FROM c",
    ],
)
def test_supported_dml_and_local_table_view_ddl_keep_all_object_evidence(sql):
    svc = service(
        operations=PolicyOperations(**{name: True for name in PolicyOperations.model_fields}),
        scope=PolicyScope(allowed_schemas=["dbo"], allowed_tables=["Users", "v"]),
    )
    result = svc.validate(sql)
    assert result["allowed"], result
    assert result["intent"]["affected_tables"]


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE DATABASE other",
        "CREATE VIEW dbo.v AS SELECT * FROM dbo.Secrets",
        "CREATE TABLE dbo.Users(Id INT REFERENCES dbo.Secrets(Id))",
        "CREATE TABLE dbo.Secrets(Id INT)",
        "DELETE u FROM dbo.Secrets u WHERE u.Id=1",
        "WITH c AS (SELECT * FROM dbo.Secrets) UPDATE c SET x=1 WHERE Id=1",
    ],
)
def test_ddl_and_cte_write_targets_cannot_hide_denied_objects(sql):
    svc = service(
        operations=PolicyOperations(**{name: True for name in PolicyOperations.model_fields}),
        scope=PolicyScope(allowed_schemas=["dbo"], allowed_tables=["Users", "v"]),
    )
    assert not svc.validate(sql)["allowed"]


def test_merge_requires_its_action_permission_and_checks_sources():
    sql = "MERGE dbo.Users t USING dbo.Secrets s ON t.Id=s.Id WHEN MATCHED THEN UPDATE SET x=s.x;"
    svc = service(operations=PolicyOperations(merge=True, update=False))
    assert not svc.validate(sql)["allowed"]
    svc = service(operations=PolicyOperations(merge=True, update=True), scope=PolicyScope(denied_tables=["Secrets"]))
    assert not svc.validate(sql)["allowed"]
    svc = service(operations=PolicyOperations(merge=True, update=True))
    assert svc.validate(sql)["allowed"]


@pytest.mark.parametrize("keyword", ["DELETE", "UPDATE", "EXECUTE", "COMMIT", "DENY", "PRINT", "USE"])
def test_write_control_tokens_cannot_be_consumed_as_unquoted_aliases(keyword):
    svc = service(operations=PolicyOperations(update=True))
    assert not svc.validate(f"SELECT 1 {keyword} FROM dbo.Users WHERE Id=1")["allowed"]


def test_quoted_keywords_and_literals_still_work_in_write_profiles():
    svc = service(operations=PolicyOperations(update=True))
    assert svc.validate("SELECT N'DELETE FROM dbo.Secret' AS [COMMIT] FROM [dbo].[UPDATE]")["allowed"]
    assert svc.validate('SELECT 1 AS "DELETE" FROM dbo.Users')["allowed"]
    assert svc.validate("UPDATE [dbo].[UPDATE] SET [DELETE]=N'COMMIT' WHERE Id=1")["allowed"]


def test_create_or_alter_requires_both_permissions():
    sql = "CREATE OR ALTER VIEW dbo.v AS SELECT * FROM dbo.Users"
    assert not service(operations=PolicyOperations(create=True)).validate(sql)["allowed"]
    assert not service(operations=PolicyOperations(alter=True)).validate(sql)["allowed"]
    assert service(operations=PolicyOperations(create=True, alter=True)).validate(sql)["allowed"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT OBJECT_DEFINITION(OBJECT_ID(N'dbo.SecretView'))",
        "SELECT COL_LENGTH(N'other.dbo.Secrets',N'PrivateColumn')",
        "SELECT COL_NAME(1, 2)",
        "SELECT OBJECT_NAME(1)",
        "SELECT OBJECT_SCHEMA_NAME(1)",
        "SELECT SCHEMA_NAME(1)",
        "SELECT DB_NAME(1)",
        "SELECT DB_ID(N'other')",
        "SELECT SESSION_CONTEXT(N'private')",
        "SELECT TYPE_NAME(1)",
        "SELECT OBJECTPROPERTY(Id,N'IsTable') FROM dbo.Users",
        "SELECT IDENT_CURRENT(N'dbo.Secrets')",
        "SELECT IDENT_SEED(N'dbo.Secrets')",
        "SELECT IDENT_INCR(N'dbo.Secrets')",
        "SELECT HAS_PERMS_BY_NAME(N'dbo.Secrets', N'OBJECT', N'SELECT')",
    ],
)
def test_metadata_functions_cannot_bypass_restricted_object_scope(sql):
    svc = service(scope=PolicyScope(allowed_databases=["app"], allowed_schemas=["dbo"], allowed_tables=["Users"]))
    assert not svc.validate(sql)["allowed"]


def test_metadata_function_policy_respects_deny_only_scope_and_quoted_columns():
    svc = service(scope=PolicyScope(denied_tables=["Secrets"]))
    assert not svc.validate("SELECT OBJECT_ID(N'dbo.Secrets')")["allowed"]
    assert svc.validate("SELECT [OBJECT_ID], N'OBJECT_DEFINITION' AS [COL_NAME] FROM dbo.Users")["allowed"]
    assert service().validate("SELECT DB_NAME()")["allowed"]


@pytest.mark.parametrize("analyzer", ["regex", "ast"])
@pytest.mark.parametrize("version", ["ALL", "AS OF '2026-01-01'"])
def test_temporal_history_scope_is_unresolved_and_never_executed(analyzer, version):
    from unittest.mock import patch

    from db_semantic_mcp.services.query_service import QueryService

    svc = service(
        analyzer=analyzer,
        scope=PolicyScope(
            allowed_tables=["dbo.Users"],
            denied_tables=["dbo.UsersHistory"],
        ),
    )
    assert svc.validate("SELECT * FROM dbo.Users")["allowed"]
    sql = f"SELECT * FROM dbo.Users FOR SYSTEM_TIME {version}"
    with patch("db_semantic_mcp.services.query_service.open_connection") as connect:
        result = QueryService(svc, svc._cfg).execute_query(sql)
    assert result["validation"]["allowed"] is False
    assert result["executed"] is False
    connect.assert_not_called()


@pytest.mark.parametrize("analyzer", ["regex", "ast"])
@pytest.mark.parametrize(
    "sql",
    [
        "CREATE EXTERNAL TABLE dbo.Users(Id INT) WITH (LOCATION = '/users', DATA_SOURCE = remote, FILE_FORMAT = csv)",
        "CREATE EXTERNAL VIEW dbo.Users AS SELECT 1",
    ],
)
def test_external_ddl_cannot_be_authorized_as_local_target(analyzer, sql):
    from unittest.mock import patch

    from db_semantic_mcp.services.query_service import QueryService

    svc = service(
        analyzer=analyzer, operations=PolicyOperations(create=True), scope=PolicyScope(allowed_tables=["dbo.Users"])
    )
    assert svc.validate("CREATE TABLE dbo.Users(Id INT)")["allowed"]
    with patch("db_semantic_mcp.services.query_service.open_connection") as connect:
        result = QueryService(svc, svc._cfg).execute_query(sql)
    assert result["validation"]["allowed"] is False
    assert result["executed"] is False
    connect.assert_not_called()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT FileTableRootPath(N'dbo.Secrets')",
        "SELECT STATS_DATE(1, 2)",
    ],
)
@pytest.mark.parametrize(
    "scope",
    [
        PolicyScope(allowed_tables=["dbo.Users"]),
        PolicyScope(denied_tables=["dbo.Secrets"]),
    ],
)
def test_additional_metadata_functions_require_unrestricted_scope(sql, scope):
    svc = service(scope=scope)
    assert svc.validate(sql)["allowed"] is False
    assert service().validate(sql)["allowed"] is True
    assert svc.validate("SELECT ABS(-1), LOWER('ABC'), LEN('abc') FROM dbo.Users")["allowed"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM dbo.Users FOR SYSTEM_TIME BETWEEN '2026-01-01' AND '2026-02-01'",
        "WITH c AS (SELECT * FROM dbo.Users FOR SYSTEM_TIME ALL) SELECT * FROM c",
        "SELECT 1 WHERE EXISTS(SELECT 1 FROM dbo.Users FOR SYSTEM_TIME AS OF '2026-01-01')",
        "CREATE EXTERNAL TABLE dbo.Users(Id INT) WITH "
        "(DATA_SOURCE = remote, SCHEMA_NAME = N'private', OBJECT_NAME = N'UsersHistory')",
        "CREATE EXTERNAL VIEW dbo.Users AS SELECT 1",
    ],
)
def test_opaque_sources_fail_closed_even_without_scope_restrictions(sql):
    svc = service(operations=PolicyOperations(create=True))
    assert svc.validate(sql)["allowed"] is False


def test_opaque_source_names_in_identifiers_and_literals_are_safe():
    svc = service(scope=PolicyScope(allowed_tables=["dbo.Users"]))
    assert (
        svc.validate(
            "SELECT [FileTableRootPath], [STATS_DATE], N'FOR SYSTEM_TIME ALL', N'CREATE EXTERNAL TABLE' FROM dbo.Users"
        )["allowed"]
        is True
    )
