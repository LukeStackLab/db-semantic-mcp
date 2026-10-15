from sqlserver_semantic_mcp.domain.enums import RiskLevel, SqlOperation
from sqlserver_semantic_mcp.policy.analyzer import analyze_sql


def test_select_simple():
    intent = analyze_sql("SELECT * FROM Users WHERE Id = 1")
    assert intent.primary_operation == SqlOperation.SELECT
    assert intent.has_where_clause is True
    assert "Users" in [t.strip("[]") for t in intent.affected_tables]
    assert intent.risk_level == RiskLevel.LOW
    assert intent.is_multi_statement is False


def test_select_with_top():
    intent = analyze_sql("SELECT TOP 10 * FROM Users")
    assert intent.has_top_clause is True
    assert intent.has_where_clause is False


def test_update_without_where_is_high_risk():
    intent = analyze_sql("UPDATE Users SET Active = 0")
    assert intent.primary_operation == SqlOperation.UPDATE
    assert intent.has_where_clause is False
    assert intent.risk_level == RiskLevel.HIGH


def test_delete_with_where():
    intent = analyze_sql("DELETE FROM Users WHERE Id = 1")
    assert intent.primary_operation == SqlOperation.DELETE
    assert intent.has_where_clause is True
    assert intent.risk_level == RiskLevel.MEDIUM


def test_drop_is_critical():
    intent = analyze_sql("DROP TABLE Users")
    assert intent.risk_level == RiskLevel.CRITICAL


def test_multi_statement():
    intent = analyze_sql("SELECT 1; SELECT 2;")
    assert intent.is_multi_statement is True


def test_comments_stripped():
    sql = "-- comment\n/* another */ SELECT * FROM T"
    intent = analyze_sql(sql)
    assert intent.primary_operation == SqlOperation.SELECT


def test_exec_procedure():
    intent = analyze_sql("EXEC dbo.SpSomething @p = 1")
    assert intent.primary_operation in (
        SqlOperation.EXEC,
        SqlOperation.EXECUTE,
    )


def test_insert_detects_target():
    intent = analyze_sql("INSERT INTO dbo.Users (Name) VALUES ('x')")
    targets = [t.strip("[]").split(".")[-1] for t in intent.affected_tables]
    assert "Users" in targets


def test_join_tables_extracted():
    sql = "SELECT * FROM A INNER JOIN B ON A.Id = B.AId"
    intent = analyze_sql(sql)
    names = [t.strip("[]") for t in intent.affected_tables]
    assert "A" in names
    assert "B" in names
