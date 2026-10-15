from __future__ import annotations
from typing import Any
from pydantic import BaseModel, Field
from .document import ExtractedItem


class ExtractionQuery(BaseModel):
    queries: list[str]
    document_filters: dict[str, Any] = Field(default_factory=dict)
    top_k: int = 8


class ExtractionResult(BaseModel):
    status: str  # "success" | "not_found" | "error" | "timeout"
    items: list[ExtractedItem] = Field(default_factory=list)
    error_message: str = ""


class ChatMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class QueryRequest(BaseModel):
    session_id: str = ""
    message: str
    doc_ids: list[str] = Field(default_factory=list)  # filter by specific docs
    # Selected agent arch in the UI ("aifa_baseline" / "finengine_baseline" /
    # …). The chat backend is shared (Finengine query_stream), but a few
    # arch-specific touches piggy-back on it — currently the AIFA Baseline
    # "пилотный режим" disclaimer prefix on open answers. Defaults to empty
    # string for backward compatibility.
    arch: str = ""


class QueryResponse(BaseModel):
    session_id: str
    answer: str
    sources: list[dict[str, Any]] = Field(default_factory=list)
    thinking: str = ""
