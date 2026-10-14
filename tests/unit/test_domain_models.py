from sqlserver_semantic_mcp.domain.enums import (
    TableType, ObjectType, CacheStatus, RiskLevel, SqlOperation,
)
from sqlserver_semantic_mcp.domain.models.column import Column
from sqlserver_semantic_mcp.domain.models.table import (
    Table, ForeignKey, Index,
)
from sqlserver_semantic_mcp.domain.models.relationship import Relationship
from sqlserver_semantic_mcp.domain.models.object import DbObject


def test_enums_values():
    assert TableType.FACT.value == "fact"
    assert ObjectType.VIEW.value == "VIEW"
    assert CacheStatus.PENDING.value == "pending"
    assert RiskLevel.CRITICAL.value == "critical"
    assert SqlOperation.SELECT.value == "SELECT"


def test_column_model():
    c = Column(
        schema_name="dbo", table_name="Users",
        column_name="Id", data_type="int",
        is_nullable=False, ordinal_position=1,
    )
    assert c.max_length is None
    assert c.description is None


def test_table_model_defaults():
    t = Table(schema_name="dbo", table_name="Users")
    assert t.primary_key == []
    assert t.foreign_keys == []
    assert t.indexes == []
    assert t.classification is None


def test_foreign_key_model():
    fk = ForeignKey(
        column_name="UserId", ref_schema="dbo",
        ref_table="Users", ref_column="Id",
    )
    assert fk.column_name == "UserId"


def test_relationship_model():
    r = Relationship(
        from_schema="dbo", from_table="Orders",
        to_schema="dbo", to_table="Users",
        fk_column="UserId", ref_column="Id",
        type="many_to_one",
    )
    assert r.type == "many_to_one"


def test_compound_relationship_model_does_not_require_singular_aliases():
    r = Relationship(
        from_schema="dbo", from_table="Lines", to_schema="dbo", to_table="Orders",
        type="many_to_one", constraint_id=123, constraint_name="FK_Lines_Orders",
        metadata_complete=True,
        column_pairs=[{"from_column": "TenantId", "to_column": "TenantId", "ordinal": 1},
                      {"from_column": "OrderId", "to_column": "Id", "ordinal": 2}],
    )
    assert r.fk_column is None
    assert len(r.column_pairs) == 2


def test_db_object_model():
    o = DbObject(
        schema_name="dbo", object_name="vw_Users",
        object_type=ObjectType.VIEW,
    )
    assert o.dependencies == []
