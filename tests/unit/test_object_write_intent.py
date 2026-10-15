"""Tests for read/write split on object definitions (P3)."""

from db_semantic_mcp.services.object_service import split_read_write


def test_select_only_view_has_only_reads():
    sql = "CREATE VIEW vw_x AS SELECT * FROM dbo.Users u JOIN dbo.Org o ON u.OrgId=o.Id"
    reads, writes = split_read_write(sql)
    assert set(reads) == {"dbo.Users", "dbo.Org"}
    assert writes == []


def test_update_procedure_has_writes():
    sql = (
        "CREATE PROCEDURE usp_CloseOrder AS BEGIN "
        "UPDATE dbo.Orders SET status='closed' WHERE Id=1; "
        "INSERT INTO dbo.AuditLog (at) VALUES (GETDATE()); "
        "END"
    )
    reads, writes = split_read_write(sql)
    assert set(writes) == {"dbo.Orders", "dbo.AuditLog"}


def test_procedure_with_select_and_update():
    sql = "CREATE PROCEDURE p AS SELECT * FROM dbo.A; UPDATE dbo.B SET x=1 WHERE y=2; "
    reads, writes = split_read_write(sql)
    assert set(reads) >= {"dbo.A"}
    assert set(writes) == {"dbo.B"}


def test_update_with_join_separates_target_from_source():
    sql = "CREATE PROCEDURE p AS UPDATE dbo.Orders SET status='x' FROM dbo.Orders o JOIN dbo.Users u ON o.UserId=u.Id"
    reads, writes = split_read_write(sql)
    # dbo.Orders is the write target; Users joined is a read source.
    # (Orders may appear in both since it's self-referenced via FROM — that's fine,
    # writes take precedence semantically for the agent's reasoning.)
    assert "dbo.Orders" in writes
    assert "dbo.Users" in reads


def test_empty_or_garbage_sql_returns_empty():
    reads, writes = split_read_write("")
    assert reads == []
    assert writes == []

    reads, writes = split_read_write("   ")
    assert reads == []
    assert writes == []


def test_delete_with_where():
    sql = "CREATE PROCEDURE p AS DELETE FROM dbo.Old WHERE at < GETDATE()"
    reads, writes = split_read_write(sql)
    assert writes == ["dbo.Old"]


def test_truncate_is_a_write():
    sql = "CREATE PROCEDURE p AS TRUNCATE TABLE dbo.T"
    reads, writes = split_read_write(sql)
    assert "dbo.T" in writes
