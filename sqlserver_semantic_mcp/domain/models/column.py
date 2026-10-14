from typing import Optional
from pydantic import BaseModel


class Column(BaseModel):
    schema_name: str
    table_name: str
    column_name: str
    data_type: str
    max_length: Optional[int] = None
    numeric_precision: Optional[int] = None
    numeric_scale: Optional[int] = None
    datetime_precision: Optional[int] = None
    collation_name: Optional[str] = None
    is_nullable: bool
    column_default: Optional[str] = None
    ordinal_position: int
    description: Optional[str] = None
