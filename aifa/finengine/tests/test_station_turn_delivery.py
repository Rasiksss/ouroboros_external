"""Результат прогона станции доезжает до ХОДА (#190, коммит К8b).

ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ И ПОЧЕМУ ИМЕННО ЭТО. Комнату станции будет поднимать
гейтвей, и тула — который сегодня рисует таблицу, кладёт Excel, публикует
заземление и пишет журнал — в цепочке не станет. Всё это должен сделать
слушатель нонса. Каждый кусок теряется ОТДЕЛЬНО и МОЛЧА, поэтому и проверяется
отдельно:

  1. живая таблица. Станция шлёт `{"type": "metric"}`, а фронт ждёт
     `metric_row`. Нет перевода — рабочая область пустая при полностью успешном
     прогоне, и ни одной ошибки в логе;
  2. шапка отчёта. Станция присылает `metrics_found`/`metrics_total`, а
     `_verbatim_render` читает `metrics_extracted`/`metrics_total_for_role_cell`.
     Отдай как есть — шапка скажет «Извлечено 0/0»;
  3. таблица и кнопка Excel в чате — через ТОТ ЖЕ пояс дословности, что у тула;
  4. заземление анализа. Его публикует рецепт; не позови рецепт — ждущий анализ
     запустит ВТОРОЙ полный прогон движка по тому же тексту (#121, замер 253 с);
  5. журнал прогонов обязан называть ПУТЬ (`aifa_station`), иначе бенчмарк
     припишет прогон тулу;
  6. ход кончился раньше прогона — итог уходит в историю сессии, а не в никуда;
  7. упавший адресат не смеет унести с собой очередь нонса.

ЧЕМ ХОДИМ. Настоящим `station_turn.arm` и настоящим дренажом; подменены только
внешние опоры — эмит в ран, хранилище «Источников» и сессия. Промежуточные
функции не зовём: тест, идущий не тем путём, что боевой код, уже давал здесь
зелёный экран при сломанном проде (#137).

⚠️ Срез 13.5.4. Тесты 1–3 выше (живая таблица `metric`→`metric_row`, дренаж,
изоляция упавшего адресата) по-прежнему кормят СТАРЫЙ построчный словарь
станции — тот путь `_relay_event` ещё умеет транслировать, и он остаётся
живым кодом, пока прямые потребители `two_stage_extractor` (карта Среза 13.5.2)
не переведены. Тесты 2–7 ниже (кнопка Excel, канон в чат, журнал, история хода)
кормят КАНОНИЧЕСКИЙ `done` (`schema_version: "1.0"`, `form_output`) — тот же
словарь, что реально шлёт `station-skills/aifa-extract/scripts/extract.py`
(тонкая прослойка над `CaseRuntime`/`PlanGraph`) — и проверяют единственный
слушатель typed-результата, `station_turn._finish_canonical_station_result`.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from finengine.store import bus as _bus_mod

from finengine.api import internal_docs, internal_tools, station_progress_ingest, station_turn
from finengine.skills import grounding_bus, run_logger

GW_KEY = "agent:aifa-station:openresponses:fe-" + "ab" * 16
NONCE = station_turn.run_nonce_of(GW_KEY)
DOC = "doc-station"
OWNER = 7
RUN = "run-station"

# Ячейка нефинансовой компании: у роли «Общая» раскладки нет по устройству
# (`formatted` = None), и это штатный ответ — фронт рисует плоскую таблицу.
CELL = "ifrs_nf_ru"


def _metric(enum: str, name: str, value: float) -> dict:
    return {"type": "metric", "enum": enum, "name_ru": name, "found": True,
            "unit": "млн руб.",
            "period_values": [{"period": "2024", "value": value},
                              {"period": "2023", "value": value * 0.9}]}


DONE = {"type": "done", "result": {
    "cell": CELL, "company_type": "non_financial", "role": "other",
    "role_label": "Общая", "company_name": "ПАО «Пример»",
    "main_period": "2024", "prev_period": "2023",
    "metrics_found": 3, "metrics_total": 77,
    "segments_count": 0, "group_count": 0,
    # Станция шлёт и своё, и это НЕ должно доехать: таблицу рисует бэкенд по
    # раскладке роли, а путь лежит в ФС контейнера.
    "table_markdown": "| станционная | таблица |",
    "xlsx_path": "/work/result.xlsx",
}}


def _run_passport(*, methodology_version: str = "unversioned") -> dict:
    return {
        "run_id": RUN, "goal_ir_id": "goal-1",
        "classification": {"profile_id": CELL, "standard": "ifrs",
                           "company_type": "non_financial", "language": "ru"},
        "entity": {"entity_id": "e-1", "canonical_name": "ПАО «Пример»",
                  "raw_name": "ПАО «Пример»"},
        "registry_version": "v1", "methodology_version": methodology_version,
        "model_version": "m1", "prompt_version": "p1", "sandbox_image_version": "s1",
    }


def _rows(*specs: tuple[str, str, float]) -> list[dict]:
    return [
        {"row_id": f"r{i}", "order": i, "kind": "metric", "enum": enum, "label": label,
         "value": value, "display_value": str(value), "status": "supported",
         "evidence_refs": []}
        for i, (enum, label, value) in enumerate(specs)
    ]


def _file_artifact() -> dict:
    return {
        "schema_version": "1.0", "output_type": "FileArtifact", "artifact_id": "fa-1",
        "run_id": RUN, "file_kind": "xlsx",
        "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "filename": "result.xlsx", "template_id": "FormOutput.xlsx", "template_version": "1.0",
        "source_result": {"result_type": "FormOutput", "result_id": "fo-1",
                          "schema_version": "1.0", "run_id": RUN,
                          "evidence_version": "1.0", "result_hash": "0" * 64},
        "content_hash": "1" * 64, "object_store_location": "obj://file-1", "size_bytes": 512,
    }


def _canonical_done(rows: list[dict], *, role: str = "other",
                    file_artifact: dict | None = None,
                    methodology_version: str = "unversioned") -> dict:
    """Ровно та `done` форма, что реально шлёт `extract.py` — thin shell над
    `CaseRuntime`/`PlanGraph`, не старый плоский словарь `two_stage_extractor`."""
    form = {
        "schema_version": "1.0", "output_type": "FormOutput", "output_id": "fo-1",
        "run_id": RUN, "status": "complete", "role": role, "layout_id": "legacy",
        "layout_version": "0", "preserve_empty": False, "rows": rows, "gaps": [],
        "warnings": [], "run_metadata": {"run_id": RUN,
                                         "methodology_version": methodology_version},
    }
    return {"type": "done", "result": {
        "schema_version": "1.0", "case_id": "case-1", "document_id": DOC,
        "document_version": "sha256:abc",
        "run_passport": _run_passport(methodology_version=methodology_version),
        "form_output": form, "file_artifact": file_artifact,
    }}


def _canonical_root_done(key: str, output: dict) -> dict:
    return {"type": "done", "result": {
        "schema_version": "1.0", "case_id": "case-1", "document_id": DOC,
        "document_version": "sha256:abc", "run_passport": _run_passport(),
        key: output,
    }}


class _Turn:
    """Ход с подменёнными внешними опорами. Всё остальное — боевое."""

    def __init__(self, monkeypatch, tmp_path, *, session_id: str = "sess-1") -> None:
        self.events: list[dict] = []
        self.history: list[tuple[str, bool, str]] = []
        self.saved: dict = {}
        self.cell: dict = {"run_id": RUN}

        monkeypatch.setattr("finengine.skills.run_orchestrator.emit_to_run",
                            lambda run_id, event: (self.events.append(event), True)[1])

        async def _save_upload(*, user_id, project_id, filename, content, content_type):
            self.saved = {"filename": filename, "bytes": len(content)}
            return type("Meta", (), {"file_id": "file-1"})()

        monkeypatch.setattr("finengine.skills.sources.save_upload", _save_upload)
        # ⚠️ Журнал прогонов переехал в общее хранилище (26.08): подменяем
        # каталог данных, а не путь файла. Где лежит запись — дело хранилища.
        from finengine.store import journals as _journals
        monkeypatch.setattr(run_logger.settings, "data_dir", tmp_path)
        monkeypatch.setattr(run_logger.settings, "journals_database_url", "")
        _journals._cached = None

        session = self
        class _Agent:
            def get_or_create_session(self, sid, auth_user_id=None):  # noqa: ANN001
                return session
        monkeypatch.setattr("finengine.api.main.get_agent", lambda: _Agent())
        monkeypatch.setattr("finengine.orchestrator.agent._save_session", lambda _s: None)

        internal_tools.register_session(GW_KEY, user_id=OWNER, project_id="p",
                                        run_id=RUN, session_id=session_id)
        grounding_bus.bind_worker_doc(NONCE, DOC)
        station_turn.arm(nonce=NONCE, gw_key=GW_KEY, doc_ids=[DOC], owner_id=OWNER,
                         project_id="p", cell=self.cell)
        station_turn.bind_session(self.cell, session_id)
        self.channel = station_turn.channels_of(self.cell)[station_turn.KIND_EXTRACT]

    # Сессия подменяется этим же объектом — истории нужны два метода.
    def add_tool_output(self, name: str, ok: bool, payload: str) -> None:
        self.history.append((name, ok, payload))

    async def feed(self, *events: dict) -> None:
        for event in events:
            self.channel.queue.put_nowait(event)
        # Дренаж — отдельная задача; отдаём ей управление, пока очередь не пуста
        # и терминальное событие не обработано.
        for _ in range(50):
            await asyncio.sleep(0)
            if self.channel.queue.empty() and "outcome" in self.channel.delivered:
                break

    def of_type(self, kind: str) -> list[dict]:
        return [e for e in self.events if e.get("type") == kind]

    @property
    def journal(self) -> list[dict]:
        return run_logger.прочитать(run_logger.ЖУРНАЛ_ИЗВЛЕЧЕНИЙ)

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


# ── 1–3. экран пользователя ──────────────────────────────────────────────


async def test_every_metric_becomes_a_row_on_screen(turn):
    """Перевод `metric` → `metric_row`: N событий станции = N строк таблицы.

    Без перевода фронт не покажет НИ ОДНОЙ строки, а в логе не будет ни слова:
    события приходят, просто не под тем именем.
    """
    await turn.feed(
        {"type": "start", "company_name": "ПАО «Пример»", "total_extractable": 77},
        _metric("SALES_REVENUE", "Выручка", 100.0),
        _metric("NET_INCOME", "Чистая прибыль", 20.0),
        _metric("TOTAL_ASSETS", "Итого активы", 500.0),
        DONE,
    )
    rows = turn.of_type("metric_row")
    assert len(rows) == 3, f"на экран уехало {len(rows)} строк вместо трёх"
    assert [r["metric"]["enum"] for r in rows] == [
        "SALES_REVENUE", "NET_INCOME", "TOTAL_ASSETS"]
    starts = turn.of_type("extract_start")
    assert starts, "шапки живой таблицы нет — рисовать не во что"
    assert starts[0]["doc_id"] == DOC, (
        "документ в шапке не назван: станция своего doc_id не видит, и при "
        "нескольких документах таблица уедет к чужому")


async def test_the_header_counts_are_translated_not_zeroed(turn):
    """Канон считает НАЙДЕННЫЕ значения (`FormRow.status == "supported"`), не
    строки таблицы — «итог читается одним способом» (НОВАТЭК 31.08.2026)."""
    await turn.feed(_canonical_done(_rows(("SALES_REVENUE", "Выручка", 100.0))))
    assert "Извлечено ЗНАЧЕНИЙ: 1 из 1" in turn.canon, (
        f"канон посчитал не то: {turn.canon[:200]!r}")


async def test_the_table_in_chat_is_ours_not_the_stations(turn):
    """Канон — только слова, без markdown-таблицы: рабочую область уже
    нарисовало пройденное сквозным потоком `typed_result`-событие раньше
    (пасс-through в `_relay_event`). Вторую копию таблицы в чате не строим —
    она бы однажды разошлась с первой."""
    await turn.feed(_canonical_done(_rows(("SALES_REVENUE", "Выручка", 100.0))))
    assert "|" not in turn.canon, (
        f"в чат уехала вторая копия таблицы: {turn.canon[:200]!r}")
    assert "Извлечено" in turn.canon


async def test_excel_button_and_verbatim_flag(turn):
    """Кнопка Excel — событием `file`, построенным из готового `FileArtifact`
    typed-результата; факт выдачи канона — глушилке пересказа.

    Артефакт СТАНЦИЯ уже собрала внутри графа (`out.file`) — слушатель его не
    строит второй раз, только сурфейсит, если он есть в конверте прогона.
    """
    await turn.feed(_canonical_done(
        _rows(("SALES_REVENUE", "Выручка", 100.0)), file_artifact=_file_artifact()))
    files = turn.of_type("file")
    assert files and files[0]["kind"] == "excel", "кнопки Excel в чате нет"
    assert files[0]["file_id"] == "obj://file-1"
    assert files[0]["filename"] == "result.xlsx"
    assert internal_tools.verbatim_text_of(GW_KEY), (
        "глушилка не знает про канон — модель допишет пересказ поверх таблицы")


async def test_the_registration_of_the_turn_does_not_wipe_the_verbatim_flag(turn):
    """Мина Р6: `register_session` пересобирала запись ЦЕЛИКОМ.

    Слушатель пишет признак дословности МИНУТАМИ позже регистраций, то есть в
    произвольном порядке относительно них. Замена вместо слияния стирала бы его
    молча.
    """
    await turn.feed(_canonical_done(_rows(("SALES_REVENUE", "Выручка", 100.0))))
    internal_tools.register_session(GW_KEY, user_id=OWNER, project_id="p",
                                    run_id=RUN, session_id="sess-1")
    assert internal_tools.verbatim_text_of(GW_KEY), (
        "перерегистрация хода стёрла признак дословности")


# ── 4–5. заземление и журнал ─────────────────────────────────────────────
#
# ЗАКРЫТО Срезом 13.5.4: заземление ждущего анализа через `grounding_bus`
# удалено вместе с публикующей половиной шины (`bus.announce`/`publish`/
# `take` больше не существуют). Каждый typed-прогон (extract/analyze) строит
# СВОЮ `EvidenceBundleV2` внутри своего `PlanGraph`; второй прогон движка,
# ради экономии которого шина заводилась, не запускается вовсе — ждать через
# шину больше нечего. Тест `test_waiting_analysis_gets_the_numbers` защищал
# ровно этот удалённый механизм и удалён вместе с ним.


async def test_the_journal_names_the_station_path(turn):
    """Журнал обязан называть ПУТЬ прогона: по нему считается бенчмарк."""
    await turn.feed(_canonical_done(_rows(("SALES_REVENUE", "Выручка", 100.0))))
    rows = turn.journal
    assert len(rows) == 1, f"прогон оставил {len(rows)} записей вместо одной"
    assert rows[0]["arch"] == "aifa_station", (
        f"прогон записан как {rows[0]['arch']!r} — бенчмарк припишет его тулу")
    assert rows[0]["doc_id"] == DOC


async def test_the_duplicate_guard_learns_the_run_finished(turn):
    """Сторож дубля (#160) обязан узнать о завершении — иначе второй движок."""
    await turn.feed(_metric("SALES_REVENUE", "Выручка", 100.0), DONE)
    status = grounding_bus.run_status(OWNER, DOC)
    assert status and status["state"] == "done", "сторож дубля не знает, что прогон кончился"
    assert "table_markdown" not in (status["summary"] or {}), (
        "в сводку сторожа уехала таблица — она попадёт в промпт следующего хода")


# ── 6–7. ход кончился раньше прогона; упавший адресат ─────────────────────


async def test_a_run_outliving_the_turn_reports_to_history(turn):
    """Экрана уже нет — итог уходит в историю сессии, а не в никуда.

    Прогон длиннее хода — нормальный исход, а не сбой: время и деньги
    потрачены, и молча выбросить результат было бы худшим из вариантов.
    """
    await turn.feed({"type": "start", "company_name": "ПАО «Пример»"})
    station_turn.disarm(turn.cell)  # ход кончился, прогон живой → отцепление
    await turn.feed(_canonical_done(_rows(("SALES_REVENUE", "Выручка", 100.0))))

    assert not turn.of_type("text_delta"), "рисуем в закрытый экран"
    assert turn.history, "итог не попал никуда: ни на экран, ни в историю"
    name, ok, payload = turn.history[0]
    assert name == "background_extract" and ok
    body = json.loads(payload)
    assert body["status"] == "завершено"
    assert body["output_type"] == "FormOutput"


@pytest.mark.parametrize(("key", "output", "output_type", "terminal_id"), [
    ("extracted_metric", {
        "schema_version": "1.0", "output_type": "ExtractedMetric", "status": "supported",
        "metric_id": "metric-1", "concept": "SALES_REVENUE", "period_ref": "2025",
        "definition_origin": "registry", "scalar_fact": {
            "evidence_id": "ev-1", "produced_by_atom": "read.value",
            "produced_by_atom_version": "1", "source_ref": "page:1",
            "concept": "SALES_REVENUE", "period_ref": "2025", "value": 100, "unit": "RUB",
        },
    }, "ExtractedMetric", "metric-1"),
    ("monitor_result", {
        "schema_version": "1.0", "output_type": "MonitorResult", "status": "complete",
        "result_id": "monitor-1", "subject_key": {
            "value": "issuer-1", "entity_id": "e-1", "identifiers": {"inn": "123"},
        },
        "diff": {
            "subject_key": {"value": "issuer-1", "entity_id": "e-1", "identifiers": {"inn": "123"}},
            "previous_snapshot_id": "s1", "current_snapshot_id": "s2",
            "previous_plan_run_id": "p1", "current_plan_run_id": "p2", "comparability": "comparable",
        },
        "approaches": {"policy_id": "default", "policy_version": "1", "signals": []},
        "trace": {"monitor_plan_run_id": "pm", "source_snapshot_ids": ["s1", "s2"],
                  "source_plan_run_ids": ["p1", "p2"]},
    }, "MonitorResult", "monitor-1"),
])
async def test_metric_and_monitor_terminal_results_survive_a_closed_tab(
    turn, key, output, output_type, terminal_id,
):
    await turn.feed({"type": "node_run", "plan_graph_hash": "h1", "node_run": {}})
    assert turn.channel.saw_life
    station_turn.disarm(turn.cell)
    await turn.feed(_canonical_root_done(key, output))
    assert turn.history
    _name, ok, payload = turn.history[0]
    body = json.loads(payload)
    assert ok
    assert body["output_type"] == output_type
    assert body["output_id"] == terminal_id


async def test_a_failing_destination_does_not_leak_the_queue(monkeypatch, tmp_path):
    """Упавший адресат не смеет унести с собой очередь нонса.

    Иначе очередь осталась бы зарегистрированной на всё время жизни прогона, а
    следующая ошибка того же адресата выглядела бы новым дефектом.
    """
    turn = _Turn(monkeypatch, tmp_path)
    monkeypatch.setattr(station_turn, "_finish_run",
                        lambda *_a: (_ for _ in ()).throw(RuntimeError("адресат упал")))
    turn.channel.queue.put_nowait(_metric("SALES_REVENUE", "Выручка", 100.0))
    turn.channel.queue.put_nowait(DONE)
    for _ in range(50):
        await asyncio.sleep(0)
        if turn.channel.task.done():
            break

    assert turn.channel.task.done() and not turn.channel.task.cancelled()
    assert turn.channel.queue not in (station_progress_ingest._bus.get(NONCE) or []), (
        "очередь осталась зарегистрированной после падения адресата")
    status = grounding_bus.run_status(OWNER, DOC)
    assert status and status["state"] == "done", (
        "статус прогона не выставлен — сторож дубля пустит второй движок из-за "
        "ошибки, к прогону отношения не имеющей")
