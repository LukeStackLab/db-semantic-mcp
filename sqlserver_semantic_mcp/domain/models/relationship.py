from typing import Optional

from pydantic import BaseModel, Field


class RelationshipColumnPair(BaseModel):
    from_column: str
    to_column: str
    ordinal: Optional[int] = None


class Relationship(BaseModel):
    from_schema: str
    from_table: str
    to_schema: str
    to_table: str
    fk_column: Optional[str] = None
    ref_column: Optional[str] = None
    type: str  # "many_to_one" | "one_to_one" | "one_to_many"
    constraint_id: Optional[int] = None
    constraint_name: Optional[str] = None
    column_pairs: list[RelationshipColumnPair] = Field(default_factory=list)
    metadata_complete: bool = False
    nullable: Optional[bool] = None
    is_disabled: Optional[bool] = None
    is_not_trusted: Optional[bool] = None
    cardinality: dict = Field(default_factory=dict)
