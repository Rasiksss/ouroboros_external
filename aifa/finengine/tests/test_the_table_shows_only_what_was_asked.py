"""Таблица показывает то, что просили, а не весь реестр ячейки.

🔴 ЖИВОЙ ДЕФЕКТ, НАЙДЕННЫЙ ВЛАДЕЛЬЦЕМ 22.08.2026 (Северсталь, нонс e606e6ce).

Человек попросил пять коэффициентов. Модель отработала БЕЗУПРЕЧНО: открыла
справочник, выписала входы этих пяти коэффициентов и запустила извлечение с
`--metrics` из тринадцати имён. Станция честно сузила состав и прочитала девять
показателей — в её собственной сводке так и записано: `metrics_found: 9,
metrics_total: 10, scope.show: [9 имён]`.

А в рабочей области открылось ШЕСТЬДЕСЯТ строк: девять прочитанных плюс
пятьдесят одна формула ячейки. Владелец прочитал это ровно так, как оно
выглядело: «он до сих пор извлекает все метрики этой роли по реестру, вместо
тех, что просили. Я думал, что он выведет только то, что просили».

ГДЕ ЛОМАЛОСЬ (на старом пути, двухстадийный движок). Ни движок, ни навык, ни
`--metrics` ни при чём: все трое сделали своё. `station_turn._finish_run`
строил план ВЫДАЧИ заново — `build_engine_scope(role, company_type, cell)`, без
`requested`. Станция сузила — бэкенд разжал обратно. Класс дефекта — ОДНО
РЕШЕНИЕ, ПРИНЯТОЕ В ДВУХ МЕСТАХ, причём второй раз без входных данных первого.

⚠️ Срез 13.5.4: ВТОРОГО МЕСТА БОЛЬШЕ НЕТ ПО УСТРОЙСТВУ, а не по дисциплине.
`out.form` (`skills/atoms/out_form.py::make_out_form_executor`) — ЕДИНСТВЕННЫЙ
узел, который решает срез показа: он уже сужает `FormOutput.rows` до
`args.extra["requested_concepts"]` (`root_extra["requested_concepts"]`,
которое `extract.py` прокидывает от `--metrics`), внутри графа, один раз.
`station_turn.py` эту работу не повторяет — `FormOutput.rows` typed-результата
УЖЕ есть готовый срез показа; слушатель передаёт его как есть (pass-through в
`_relay_event` для живого потока, дословно в `screen`/канон в
`_finish_canonical_station_result`). Второму решателю здесь физически неоткуда
взяться: у `station_turn.py` нет доступа ни к реестру ячейки, ни к роли
отдельно от того, что уже сказал `FormOutput`.

Отсюда два теста ниже: один держит СТАРЫЙ дефект закрытым (если бы слушатель
когда-нибудь снова начал пересчитывать состав из роли/реестра — а не из
`FormOutput.rows` typed-результата, — тест обязан покраснеть), второй
документирует, ПОЧЕМУ остальные три старых сценария этого файла (нет среза,
пустой срез, своя формула теряется при пересечении с реестром) не имеют
аналога на каноническом пути: там не было ВТОРОГО пересечения с реестром,
которое их и порождало (`_narrow_to_requested` в `roles/scope.py`) — сегодня
`out.form` фильтрует ровно по `requested_concepts`, без похода в реестр ячейки
второй раз, поэтому свои формулы (`EXTRACT_AD_HOC_METRIC`) не могут выпасть
тем же классом дефекта.
"""
from __future__ import annotations

import asyncio

import pytest

from finengine.store import bus as _bus_mod

from finengine.api import internal_docs, internal_tools, station_progress_ingest, station_turn
from finengine.skills import grounding_bus, run_logger

GW_KEY = "agent:aifa-station:openresponses:fe-" + "cd" * 16
NONCE = station_turn.run_nonce_of(GW_KEY)
DOC = "doc-северсталь"
OWNER = 7
RUN = "run-северсталь"
CELL = "ifrs_nf_ru"

# Ровно те девять, что прочитал живой прогон.
ПРОЧИТАНЫ = ["LONG_TERM_LEASE_LIABILITIES", "SHORT_TERM_BORROWINGS", "CASH_EQUIVALENTS",
             "TOTAL_CURRENT_LIABILITIES", "TOTAL_ASSETS", "SALES_REVENUE",
             "OPERATING_PROFIT", "NET_OPERATING_CASH_FLOW", "ROU_DEPRECIATION"]


def _run_passport() -> dict:
    return {
        "run_id": RUN, "goal_ir_id": "goal-1",
        "classification": {"profile_id": CELL, "standard": "ifrs",
                           "company_type": "non_financial", "language": "ru"},
        "entity": {"entity_id": "e-1", "canonical_name": "Северсталь",
                  "raw_name": "Северсталь"},
        "registry_version": "v1", "methodology_version": "unversioned",
        "model_version": "m1", "prompt_version": "p1", "sandbox_image_version": "s1",
    }


def _rows(enums: list[str]) -> list[dict]:
    return [
        {"row_id": f"r{i}", "order": i, "kind": "metric", "enum": enum, "label": enum,
         "value": 100.0, "display_value": "100.0", "status": "supported",
         "evidence_refs": []}
        for i, enum in enumerate(enums)
    ]


def _form(rows: list[dict]) -> dict:
    return {
        "schema_version": "1.0", "output_type": "FormOutput", "output_id": "fo-1",
        "run_id": RUN, "status": "complete", "role": "other", "layout_id": "legacy",
        "layout_version": "0", "preserve_empty": False, "rows": rows, "gaps": [],
        "warnings": [], "run_metadata": {"run_id": RUN, "methodology_version": "unversioned"},
    }


def _done(rows: list[dict]) -> dict:
    return {"type": "done", "result": {
        "schema_version": "1.0", "case_id": "case-1", "document_id": DOC,
        "document_version": "sha256:abc", "run_passport": _run_passport(),
        "form_output": _form(rows), "file_artifact": None,
    }}


class _Turn:
    """Ход с подменёнными внешними опорами; станционный путь — боевой."""

    def __init__(self, monkeypatch, tmp_path) -> None:
        self.events: list[dict] = []
        self.history: list[tuple[str, bool, str]] = []
        self.cell: dict = {"run_id": RUN}

        monkeypatch.setattr("finengine.skills.run_orchestrator.emit_to_run",
                            lambda run_id, event: (self.events.append(event), True)[1])

        async def _save_upload(*, user_id, project_id, filename, content, content_type):
            return type("Meta", (), {"file_id": "file-1"})()

        monkeypatch.setattr("finengine.skills.sources.save_upload", _save_upload)
        monkeypatch.setattr(run_logger, "_LOG_PATH", tmp_path / "extract_runs.jsonl")

        session = self

        class _Agent:
            def get_or_create_session(self, sid, auth_user_id=None):  # noqa: ANN001
                return session

        monkeypatch.setattr("finengine.api.main.get_agent", lambda: _Agent())
        monkeypatch.setattr("finengine.orchestrator.agent._save_session", lambda _s: None)

        internal_tools.register_session(GW_KEY, user_id=OWNER, project_id="p",
                                        run_id=RUN, session_id="sess-1")
        grounding_bus.bind_worker_doc(NONCE, DOC)
        station_turn.arm(nonce=NONCE, gw_key=GW_KEY, doc_ids=[DOC], owner_id=OWNER,
                         project_id="p", cell=self.cell)
        station_turn.bind_session(self.cell, "sess-1")
        self.channel = station_turn.channels_of(self.cell)[station_turn.KIND_EXTRACT]

    def add_tool_output(self, name: str, ok: bool, payload: str) -> None:
        self.history.append((name, ok, payload))

    async def feed(self, *events: dict) -> None:
        for event in events:
            self.channel.queue.put_nowait(event)
        for _ in range(50):
            await asyncio.sleep(0)
            if self.channel.queue.empty() and "outcome" in self.channel.delivered:
                break

    def of_type(self, kind: str) -> list[dict]:
        return [e for e in self.events if e.get("type") == kind]

    @property
    def canon(self) -> str:
        return "".join(e.get("text", "") for e in self.of_type("text_delta"))


@pytest.fixture(autouse=True)
def _clean():
    internal_docs.reset_scopes()
    station_progress_ingest._bus.clear()
    station_turn._detached.clear()
    internal_tools.reset_sessions()
    _bus_mod.reset_bus()
    yield
    internal_docs.reset_scopes()
    station_progress_ingest._bus.clear()
    station_turn._detached.clear()
    internal_tools.reset_sessions()
    _bus_mod.reset_bus()


@pytest.fixture()
async def turn(monkeypatch, tmp_path):
    t = _Turn(monkeypatch, tmp_path)
    yield t
    station_turn.disarm(t.cell, stopped=True)


async def test_the_screen_shows_exactly_what_out_form_produced(turn):
    """Главный случай Северстали, на каноническом пути.

    `out.form` уже сузил до девяти — ни живой поток (`typed_result`
    pass-through), ни сохранённый экран, ни сводка канона не имеют права
    досчитать эту девятку обратно до полного реестра ячейки.
    """
    rows = _rows(ПРОЧИТАНЫ)
    await turn.feed({"type": "typed_result", "status": "complete",
                     "result": _form(rows)})
    typed = turn.of_type("typed_result")
    assert typed, "typed_result не дошёл до рабочей области — второй решатель вернулся"
    shown = [r["enum"] for r in typed[-1]["result"]["rows"]]
    assert shown == ПРОЧИТАНЫ, (
        f"на экран уехало {len(shown)} строк вместо девяти запрошенных: "
        f"лишние {sorted(set(shown) - set(ПРОЧИТАНЫ))[:5]} — слушатель "
        "пересчитал срез вместо того, чтобы передать typed_result как есть "
        "(живой дефект 22.08 по Северстали, теперь на новом пути)")

    await turn.feed(_done(rows))
    assert f"Извлечено ЗНАЧЕНИЙ: {len(rows)} из {len(rows)}" in turn.canon, (
        f"сводка канона считает не тот набор: {turn.canon[:200]!r}")


def test_out_form_is_the_only_place_that_narrows_by_requested_concepts():
    """Пломба: срез показа считает РОВНО ОДНО место — `out.form`.

    Старый дефект родился из ВТОРОГО пересечения с реестром ячейки
    (`roles/scope._narrow_to_requested`, звалась и `build_engine_scope`
    станцией, и `station_turn._finish_run` бэкендом заново). Раз слушатель
    сегодня физически не читает ни `roles/scope.build_engine_scope`, ни
    реестр ячейки для решения «что показать» — вернуть тот класс дефекта
    можно только добавив в `station_turn.py` новый вызов одной из них.
    """
    import inspect

    from finengine.api import station_turn as st

    source = inspect.getsource(st._finish_canonical_station_result)
    assert "build_engine_scope" not in source, (
        "в канонический finish вернулся второй решатель среза показа — "
        "ровно тот класс дефекта, что стоил Северстали 22.08.2026")


# ⚠️ ТРИ СТАРЫХ СЦЕНАРИЯ ЭТОГО ФАЙЛА БЕЗ АНАЛОГА НА КАНОНИЧЕСКОМ ПУТИ —
# названо, а не тихо удалено:
#
#   · «станция среза не посчитала — показываем всё» (`show=None`);
#   · «пустой срез — тоже "среза нет", не сужение до нуля» (`show=[]`);
#   · «своя формула теряется при пересечении с реестром» (`_narrow_to_requested`).
#
# Все три были про то, как ВТОРОЕ пересечение с реестром ячейки
# (`roles/scope._narrow_to_requested`, которое вызывал бэкенд ПОВТОРНО поверх
# уже посчитанного станцией среза) отличает «среза нет» от «срез пуст» и что
# оно делает со своими формулами человека. Сегодня этого второго пересечения
# нет: `out.form` фильтрует `FormOutput.rows` по `requested_concepts` ОДИН
# РАЗ, внутри графа, и станция/слушатель эту работу не повторяют. Пустой
# `FormOutput.rows` (аналог старого «срез пуст») — уже не два разных случая, а
# один: то, что вернул `out.form`, и есть окончательный список, каким бы он
# ни был. Дальнейшее направление (например, порог, при котором пустой
# `FormOutput.rows` должен читаться как отказ, а не «в отчёте правда пусто»)
# — предмет `extract_result.form_refusal_reason`, а не этого файла.
