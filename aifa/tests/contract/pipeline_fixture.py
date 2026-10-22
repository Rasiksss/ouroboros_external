"""Фикстурный документ и подставные границы конвейера службы для сквозного теста шага 10.

Настоящие: приложение службы, `run_orchestrator`, `tool_api`, привязка, `StandardPath`,
`GraphPlanner`,
`GraphExecutor`, `CaseRuntime` (временный SQLite). Подставляется только внешнее и
недетерминированное
(PRINCIPLES.md §Б.4): модель разбора цели, пара `locate`/`read.value` и хранилище документов.
Тот же набор подмен, что у сквозного теста шага 09
(`finengine/tests/assistant/test_tool_api_to_journal.py`).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

AIFA = Path(__file__).resolve().parents[2]
CLASSIFICATION = AIFA / "finengine" / "tests" / "golden" / "classification" / "cases.json"

OWNER = 42
PROJECT = "p-contract"
DOC = "doc-contract-1"
REVENUE = 987_654.0
QUESTION = "какая выручка за 2024 год?"
INTENT = {
    "intent_kind": "extract_registered_metric",
    "targets": [{"concept": "SALES_REVENUE"}],
    "entity_ref": None,
    "period_ref": "p-2024-fy",
    "role": None,
    "unresolved": [],
    "ambiguities": [],
}


def document_text() -> str:
    """Выдержка МСФО-отчёта Северстали из эталонов службы: профиль `ifrs_nf_ru` определяется без
    модели."""
    cases = json.loads(CLASSIFICATION.read_text(encoding="utf-8"))
    return str(next(c for c in cases if c["id"] == "cls-023")["excerpt"])


@dataclass(frozen=True)
class FixtureStore:
    """Хранилище документов: документ хода принадлежит пользователю входа."""

    def get_document(self, doc_id: str) -> Any:
        if doc_id != DOC:
            return None
        return SimpleNamespace(
            owner_id=OWNER, filename="severstal-2024.pdf", company_name="Северсталь", meta_card=None
        )


class NoOldChat:
    """Агент прежнего чата: на ходе через ouroboros его не зовут, документы сессии — пусты."""

    def session_doc_ids(self, session_id: str, *, auth_user_id: int) -> list[str]:
        return []


def fixture_doc() -> str:
    return DOC


def _executors(doc: Callable[[], str] = fixture_doc) -> tuple[Any, Any]:
    from finengine.contracts import CandidateLocator, ScalarFact, SourceLocator

    def locate(items: Any, ctx: Any) -> dict[str, tuple[Any, ...]]:
        return {
            args.concept: (
                CandidateLocator(
                    produced_by_atom="locate",
                    produced_by_atom_version="1.0.0",
                    document_id=doc(),
                    document_version=ctx.run_id,
                    concept=args.concept or "",
                    locator=SourceLocator(
                        page=1, text_fragment=f"fixture evidence for {args.concept}"
                    ),
                    confidence=0.9,
                    origin="model",
                ),
            )
            for args in items
        }

    def read_value(items: Any, ctx: Any) -> dict[str, tuple[Any, ...]]:
        return {
            args.concept: (
                ScalarFact(
                    produced_by_atom="read.value",
                    produced_by_atom_version="1.0.0",
                    source_ref=f"fixture-read-value:{args.concept}",
                    concept=args.concept,
                    period_ref=args.period_ref or "unknown",
                    value=REVENUE,
                    unit="RUB_MLN",
                ),
            )
            for args in items
        }

    return locate, read_value


def install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    doc: Callable[[], str] = fixture_doc,
    real_documents: bool = False,
) -> None:
    """Подменить границы конвейера и хранилища; `tmp_path` — каталог данных теста (Case,
    журналы).

    `real_documents` — документы и их текст настоящие (реестр на временном SQLite, текст в
    хранилище службы): документ, который называет `doc()`, приходит в тест через ручку загрузки,
    а не из фикстуры (шаг 11)."""
    from finengine import config
    from finengine.api import main as app_main
    from finengine.assistant import read_tools
    from finengine.skills.atoms.graph_atoms import build_registry

    locate, read_value = _executors(doc)

    async def parsed(system: str, prompt: str, *, model_version: Any = None) -> str:
        return json.dumps(INTENT, ensure_ascii=False)

    def registry(*, profile_id: str, registry_data: Any = None, **kwargs: Any) -> Any:
        kwargs.setdefault("locate_executor", locate)
        kwargs.setdefault("read_value_executor", read_value)
        return build_registry(profile_id=profile_id, registry_data=registry_data, **kwargs)

    text = document_text()
    store = FixtureStore()
    if real_documents:
        import finengine.ingestion.document_store as documents

        monkeypatch.setattr(
            config.settings, "documents_database_url", f"sqlite:///{tmp_path / 'documents.db'}"
        )
        documents.reset_store()
    monkeypatch.setattr(config.settings, "state_database_url", f"sqlite:///{tmp_path / 'case.db'}")
    monkeypatch.setattr(config.settings, "docs_dir", tmp_path)
    monkeypatch.setattr(config.settings, "data_dir", tmp_path)
    monkeypatch.setattr(config.settings, "redis_url", "")
    if not real_documents:
        monkeypatch.setattr(read_tools, "get_store", lambda: store)
        monkeypatch.setattr(app_main, "get_store", lambda: store)
        monkeypatch.setattr(
            "finengine.store.blobs.document_text",
            lambda doc_id, **_: text if doc_id == DOC else None,
        )
        monkeypatch.setattr(
            read_tools, "document_text", lambda doc_id, **_: text if doc_id == DOC else None
        )
    monkeypatch.setattr(app_main, "get_agent", lambda: NoOldChat())
    monkeypatch.setattr(app_main, "_upsert_dialog_stub", lambda *args, **kwargs: None)
    monkeypatch.setattr("finengine.skills.atoms.intent._default_complete", parsed)
    monkeypatch.setattr("finengine.skills.production_runtime.build_registry", registry)
