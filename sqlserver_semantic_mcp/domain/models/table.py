from typing import Optional
from pydantic import BaseModel, Field
from ..enums import TableType
from .column import Column
from .relationship import RelationshipColumnPair


class ForeignKey(BaseModel):
    column_name: Optional[str] = None
    ref_schema: str
    ref_table: str
    ref_column: Optional[str] = None
    constraint_id: Optional[int] = None
    constraint_name: Optional[str] = None
    constraint_column_id: Optional[int] = None
    column_pairs: list[RelationshipColumnPair] = Field(default_factory=list)
    metadata_complete: bool = False
    is_disabled: Optional[bool] = None
    is_not_trusted: Optional[bool] = None


class Index(BaseModel):
    index_name: str
    is_unique: bool
    is_primary_key: bool
    columns: list[str] = Field(default_factory=list)
    has_filter: Optional[bool] = None
    is_disabled: Optional[bool] = None


class Table(BaseModel):
    schema_name: str
    table_name: str
    columns: list[Column] = Field(default_factory=list)
    primary_key: list[str] = Field(default_factory=list)
    foreign_keys: list[ForeignKey] = Field(default_factory=list)
    indexes: list[Index] = Field(default_factory=list)
    description: Optional[str] = None
    classification: Optional[TableType] = None
