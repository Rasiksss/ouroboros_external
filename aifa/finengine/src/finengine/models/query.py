from __future__ import annotations
from typing import Any
from pydantic import BaseModel, Field


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
    # Project ID this chat belongs to. Required for per-user / per-project
    # data isolation (Пакет B, УЭК-АУА-24/25): tools dispatched from this
    # chat receive ctx.user_id + ctx.project_id and refuse to touch data
    # outside that namespace. Empty string means "no project scope" — only
    # global tools are usable.
    project_id: str = ""
    # User-selected analyst role for metric extraction (other/risks/baifp/
    # die/cib/dgr). Threaded into ToolContext.role so fe_extract_metrics
    # scopes to that role's layout. Empty = «Общая»/other (full cell set).
    role: str = ""
    # МОДЕЛЬ ВХОДОВ (ARCHITECTURE_DECISIONS): shortcut buttons send a normal
    # chat turn with the decision PINNED — the client tool the gateway MUST
    # call on the first turn (OpenResponses tool_choice function pinning).
    # Empty = free-form turn (the agent routes by intent).
    pinned_tool: str = ""
    # Deterministic pinning: when a shortcut button fixes BOTH the tool AND its
    # arguments (e.g. «Быстрый/Полный анализ» → fe_analyze depth=quick|full), the
    # backend calls the tool DIRECTLY with these args and streams its result,
    # skipping the LLM turn (tool_choice pins only the tool NAME, not depth).
    # Only honoured for a small allowlist of pinnable tools. Empty = name-only pin.
    pinned_tool_args: dict = Field(default_factory=dict)
    # Пользовательский тумблер «Поиск в интернете» (меню «+» композера).
    # False = веб-инструменты (tavily_search / fetch_url) не отдаются модели на
    # этот ход. Действует на инлайн-петле; гейтвей-петля (C3-2) несёт свой
    # набор тулов и этим флагом пока не управляется.
    web_search: bool = True
    # Документ, ОТКРЫТЫЙ у пользователя в панели исходника. В чате с несколькими
    # документами запрос «проведи извлечение» без уточнения должен бить в него,
    # а не в первый попавшийся (реальный случай: извлекли МегаФон, когда открыт
    # был Deutsche Bank). Пусто = панель ничего не выделяла.
    active_doc_id: str = ""


class QueryResponse(BaseModel):
    session_id: str
    answer: str
    sources: list[dict[str, Any]] = Field(default_factory=list)
    thinking: str = ""
