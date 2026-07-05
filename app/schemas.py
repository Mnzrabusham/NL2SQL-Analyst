"""Request/response models — the API contract, and the OpenAPI docs for free."""
from __future__ import annotations
from typing import List, Optional

from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    question: str = Field(min_length=1)


class AskResponse(BaseModel):
    sql: str
    columns: List[str]
    rows: List[list]
    row_count: int
    truncated: bool
    # True when the first attempt failed and the model's one-shot
    # self-correction produced the SQL that ran.
    repaired: bool = False


class RejectedResponse(BaseModel):
    """Body of the 400 returned when generated SQL fails validation or
    execution — includes the SQL so the UI can show what was rejected."""
    detail: str
    sql: Optional[str] = None


class SchemaResponse(BaseModel):
    schema_text: str
