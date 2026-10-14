"""Общие опоры сквозных тестов входа чата (Срез 31, Ingress v2).

Ход идёт через настоящий `FinengineAgent.query_stream`. Подменяются только
внешние границы: модельный вызов `intent.parse` (`intent._default_complete`),
пара `locate`/`read.value`, диспетчерская модель (`agent.llm.stream_events`),
хранилище документов и текст документа. Всё остальное — настоящий код:
планировщик, исполнитель графа, `CaseRuntime` (временный SQLite).

Модуль не тест (нет префикса `test_`): его импортируют тестовые файлы каталога
`tests/integration/` по имени, как и `conftest.py` (каталог — не пакет).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from finengine.orchestrator import agent as ag

OWNER = 42
DOC = "doc-lukoil-1p2026"
RAW_TEXT_MARKER = "текст отчёта ЛУКОЙЛ"


def fake_llm_response(payload: dict):
    async def _complete(system: str, prompt: str, *, model_version=None) -> str:
        return json.dumps(payload, ensure_ascii=False)
    return _complete


def patched_build_registry(monkeypatch, locate_exec=None, read_value_exec=None, **defaults):
    from finengine.skills.atoms.graph_atoms import build_registry as real_build_registry

    def _wrapped(*, profile_id, registry_data=None, **kwargs):
        if locate_exec is not None:
            kwargs.setdefault("locate_executor", locate_exec)
        if read_value_exec is not None:
            kwargs.setdefault("read_value_executor", read_value_exec)
        for key, value in defaults.items():
            kwargs.setdefault(key, value)
        return real_build_registry(profile_id=profile_id, registry_data=registry_data, **kwargs)

    monkeypatch.setattr("finengine.skills.production_runtime.build_registry", _wrapped)


class FakeMeta:
    def __init__(self, owner_id):
        self.owner_id = owner_id
        self.filename = "lukoil-1p2026.pdf"
        self.company_name = "ЛУКОЙЛ"
        self.meta_card = None


class FakeStore:
    def __init__(self, docs: dict[str, FakeMeta]):
        self._docs = docs

    def get_document(self, doc_id):
        return self._docs.get(doc_id)


@pytest.fixture(autouse=True)
def gate_env(monkeypatch, tmp_path, severstal_text):
    """Владелец, документ и Case — детерминированные дубли, не хранилище."""
    from finengine import config

    monkeypatch.setattr(config.settings, "state_database_url", f"sqlite:///{tmp_path}/gate-case.db")
    monkeypatch.setattr(ag, "get_store", lambda: FakeStore({DOC: FakeMeta(OWNER)}))
    monkeypatch.setattr("finengine.store.blobs.document_text", lambda doc_id, **_kw: (
        severstal_text if doc_id == DOC else None
    ))
    yield


class Turn:
    """Внешние опоры хода: диспетчерская модель и текст документа, который видит
    `_build_context` (свободный ход) — НЕ то же, что текст канонического
    конвейера (тот идёт через `finengine.store.blobs.document_text`, см. `gate_env`)."""

    def __init__(self) -> None:
        self.prompt = ""
        self.events: list[dict] = []
        self.dispatcher_calls = 0

    def arm(self, monkeypatch) -> None:
        async def _model(**kw):
            self.dispatcher_calls += 1
            self.prompt = kw.get("prompt") or ""
            yield {"type": "done", "usage": {}}

        monkeypatch.setattr(ag, "_build_context", lambda *_a, **_kw: (RAW_TEXT_MARKER, []))
        monkeypatch.setattr(ag.llm, "stream_events", _model)

    def run(
        self, message: str, *, doc_ids=(DOC,), active_doc_id: str = "",
        session_id: str = "", owner_id: int = OWNER, role: str = "",
    ) -> list[dict]:
        async def _go():
            agent = ag.FinengineAgent()
            events = [ev async for ev in agent.query_stream(
                message, session_id=session_id, doc_ids=list(doc_ids),
                auth_user_id=owner_id, owner_id=owner_id,
                project_id="p-lukoil", active_doc_id=active_doc_id, role=role,
            )]
            self.events = events
            return events

        return asyncio.run(_go())


def set_intent(monkeypatch, payload: dict) -> None:
    monkeypatch.setattr("finengine.skills.atoms.intent._default_complete", fake_llm_response(payload))


class IntentRecorder:
    """Фейковая граница `_default_complete`: отдаёт ПО ОЧЕРЕДИ по одному payload на
    вызов (последний — повторно) и запоминает каждый полученный prompt/system."""

    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = list(payloads)
        self.calls: list[dict] = []

    async def __call__(self, system: str, prompt: str, *, model_version=None) -> str:
        idx = min(len(self.calls), len(self.payloads) - 1)
        self.calls.append({"system": system, "prompt": prompt})
        return json.dumps(self.payloads[idx], ensure_ascii=False)


def set_intent_sequence(monkeypatch, payloads: list[dict]) -> IntentRecorder:
    recorder = IntentRecorder(payloads)
    monkeypatch.setattr("finengine.skills.atoms.intent._default_complete", recorder)
    return recorder
