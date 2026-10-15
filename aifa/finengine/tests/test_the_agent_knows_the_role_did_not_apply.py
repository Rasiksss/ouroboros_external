"""Агент обязан ЗНАТЬ, что роль не применилась, — а не только фронт (20.08.2026).

ДЕФЕКТ. `roles/scope.role_not_applied` и `role_notice` считались на живом пути
(`api/station_turn._finish_run`) и уходили в событие `extract_done` — то есть
ТОЛЬКО фронту, который рисует по ним значок на карточке. Ни stdout станционного
скрипта, ни словарь итога (`_result_for_screen`) этих ключей не содержали, и в
теле навыка `aifa-extract` про неприменённую роль не было ни слова.

ЧТО ИМЕННО ДЕРЖИТ ЭТОТ ФАЙЛ — названо точно, потому что первая редакция
говорила «словарь, который читает модель», а это ВЕРНО НЕ ВЕЗДЕ. Модель читает
`_result_for_screen` только когда ход кончился раньше прогона: тогда итог
уходит в историю сессии целиком. На обычном пути словарь идёт в канон, а
`render_extract_result` собирает шапку из своего перечня ключей — этих двух там
нет. На обычном пути модель узнаёт о неприменённой роли из stdout станции, и
это держит третий тест файла.

ЦЕНА. На парах «ДГР × нефинансовая» и «Риски × банк» раскладки роли нет, состав
молча проваливается в ПОЛНЫЙ набор ячейки. Человек выбирал узкую роль, получал
119 показателей — и агент, который объясняет ему результат, про несработавший
выбор не знал и сказать не мог. Пользователю оставалось гадать, проигнорирован
его выбор или в отчёте действительно столько показателей. Числа при этом
верные, поэтому отказывать не за что: молчание — единственный дефект, и он же
самый дорогой, потому что незаметен.

ПОЧЕМУ ЭТОТ ФАЙЛ, А НЕ СТРОЧКА В `test_role_scope.py`. Тот сторож проверяет
условие и проводку ПО ТЕКСТУ модуля, и этого мало: имена остаются в файле после
снятия вызова (так был потерян #137). Здесь дёргается живой путь станционного
хода целиком — взвод канала, приём терминала, `_finish_run`, — а подменён
только последний адресат (`_report_to_history`), чтобы прочитать, что ему
отдали.

ПРОВЕРКА СТОРОЖЕЙ ОТКАТОМ:
  * убрать `role_not_applied`/`role_notice` из словаря `_result_for_screen`
    (или подставить в вызов константы `False`/`""`) →
    `test_an_unapplied_role_reaches_the_dictionary_the_model_reads`;
  * заставить `_result_for_screen` объявлять неприменённой ЛЮБУЮ роль →
    `test_a_working_role_stays_silent`;
  * убрать фразу про `role_not_applied` из `station-skills/aifa-extract/SKILL.md`
    или ключи из итогового JSON станционного скрипта →
    `test_the_skill_body_tells_the_model_what_to_do`;
  * дописать `role_notice` в шапку `render_extract_result` →
    `test_the_canon_header_stays_silent_and_the_comment_says_so`.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATION_EXTRACT = ROOT / "station-skills" / "aifa-extract" / "scripts" / "extract.py"

OWNER = 7
NONCE = "b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0"
GW_KEY = f"agent:aifa-station:openresponses:fe-{NONCE}"
DOC = "doc-role"
RUN = "run-role"

_START = {"type": "start", "company_name": "ПАО Тест", "total_extractable": 119,
          "period_current": "2024", "period_prev": "2023", "unit": "млн руб."}
_ROW = {"type": "metric", "enum": "TOTAL_ASSETS", "name_ru": "Итого активы",
        "value_current": 1000.0, "value_prev": 900.0, "found": True,
        "period_values": [{"period": "2024", "value": 1000.0},
                          {"period": "2023", "value": 900.0}]}


def _canonical_done(*, role: str, company_type: str, cell: str) -> dict:
    """Ровно та `done` форма, что реально шлёт `extract.py`."""
    form = {
        "schema_version": "1.0", "output_type": "FormOutput", "output_id": "fo-1",
        "run_id": RUN, "status": "complete", "role": role, "layout_id": "legacy",
        "layout_version": "0", "preserve_empty": False,
        "rows": [{"row_id": "r0", "order": 0, "kind": "metric", "enum": "TOTAL_ASSETS",
                  "label": "Итого активы", "value": 1000.0, "display_value": "1000.0",
                  "status": "supported", "evidence_refs": []}],
        "gaps": [], "warnings": [],
        "run_metadata": {"run_id": RUN, "methodology_version": "unversioned"},
    }
    passport = {
        "run_id": RUN, "goal_ir_id": "goal-1",
        "classification": {"profile_id": cell, "standard": "ifrs",
                           "company_type": company_type, "language": "ru"},
        "entity": {"entity_id": "e-1", "canonical_name": "ПАО Тест", "raw_name": "ПАО Тест"},
        "registry_version": "v1", "methodology_version": "unversioned",
        "model_version": "m1", "prompt_version": "p1", "sandbox_image_version": "s1",
    }
    return {"type": "done", "result": {
        "schema_version": "1.0", "case_id": "case-1", "document_id": DOC,
        "document_version": "sha256:abc", "run_passport": passport,
        "form_output": form, "file_artifact": None,
    }}


@pytest.fixture
def turn(tmp_path, monkeypatch):
    """Чистый ход станции: журналы в tmp, приём без токена, шины пусты."""
    from finengine.api import internal_docs, station_progress_ingest, station_turn
    from finengine.skills import grounding_bus, run_logger

    monkeypatch.setattr(run_logger, "_STATION_EVENTS_PATH", tmp_path / "station_events.jsonl")
    monkeypatch.setattr(run_logger, "_LOG_PATH", tmp_path / "extract_runs.jsonl")
    monkeypatch.delenv("FINENGINE_OPENCLAW_GATEWAY_TOKEN", raising=False)

    station_progress_ingest._bus.clear()
    station_turn._detached.clear()
    internal_docs.reset_scopes()
    grounding_bus._worker_docs.clear()
    try:
        yield
    finally:
        station_progress_ingest._bus.clear()
        station_turn._detached.clear()
        grounding_bus._worker_docs.clear()


def _model_sees(monkeypatch, *, role: str, company_type: str, cell: str) -> dict:
    """Прогнать живой ход и вернуть словарь, который прочитает модель.

    Ход закрывается ДО терминала — тогда итог уходит в историю сессии, то есть
    ровно в тот текст, который следующим ходом читает модель-оркестратор.
    """
    from finengine.api import station_progress_ingest as ingest
    from finengine.api import station_turn
    from finengine.skills import grounding_bus

    seen: list[dict] = []
    monkeypatch.setattr(station_turn, "_report_to_history",
                        lambda _channel, payload: seen.append(payload))

    done = _canonical_done(role=role, company_type=company_type, cell=cell)

    async def scenario() -> None:
        turn_cell: dict = {}
        # Проекта нет сознательно: с ним `finalize_extraction` полезла бы
        # складывать книгу Excel в хранилище пользователя, а предмет теста —
        # не книга.
        station_turn.arm(nonce=NONCE, gw_key=GW_KEY, doc_ids=[DOC], owner_id=OWNER,
                         project_id="", cell=turn_cell)
        grounding_bus.bind_worker_doc(NONCE, DOC)
        channel = station_turn.channels_of(turn_cell)[station_turn.KIND_EXTRACT]

        await ingest.ingest(ingest.ProgressBatch(nonce=NONCE, events=[_START]),
                            authorization="")
        for _ in range(50):
            await asyncio.sleep(0)
            if channel.saw_life:
                break
        assert channel.saw_life, "дренаж не забрал старт — сценарий проверяет не то"

        station_turn.disarm(turn_cell)  # ход кончился, прогон дорабатывает один
        await ingest.ingest(ingest.ProgressBatch(nonce=NONCE, events=[_ROW, done]),
                            authorization="")
        await asyncio.wait_for(channel.task, timeout=10)

    asyncio.run(scenario())
    assert seen, "итог не доехал до истории — сценарий проверяет не то"
    return seen[0]


@pytest.mark.parametrize(("role", "company_type", "cell"), [
    ("dgr", "non_financial", "ifrs_nf_ru"),   # ДГР раскладки для нефинансовой нет
    ("risks", "bank", "ifrs_bank_ru"),        # «Риски» — для банка
])
def test_an_unapplied_role_reaches_the_dictionary_the_model_reads(
        turn, monkeypatch, role, company_type, cell):
    """Роль не сработала — и об этом сказано ТАМ, ГДЕ ЧИТАЕТ АГЕНТ."""
    payload = _model_sees(monkeypatch, role=role, company_type=company_type, cell=cell)

    assert payload.get("role_not_applied") is True, (
        f"{role}×{company_type}: агент получил итог без признака неприменённой "
        "роли — объясняя человеку 119 показателей вместо узкого среза, он не "
        "знает, что его выбор не сработал"
    )
    assert "не заведена" in (payload.get("role_notice") or ""), (
        "признак есть, а фразы нет: агенту нечего сказать человеку словами"
    )


@pytest.mark.parametrize(("role", "company_type", "cell"), [
    ("dgr", "bank", "ifrs_bank_ru"),      # роль СРАБОТАЛА
    ("other", "bank", "ifrs_bank_ru"),    # «Общая» = полный набор по замыслу
])
def test_a_working_role_stays_silent(turn, monkeypatch, role, company_type, cell):
    """Ложная тревога научит человека не читать предупреждения вовсе."""
    payload = _model_sees(monkeypatch, role=role, company_type=company_type, cell=cell)

    assert payload.get("role_not_applied") is False, (
        f"{role}×{company_type}: сработавшая роль объявлена неприменённой")
    assert not payload.get("role_notice")


def test_the_skill_body_tells_the_model_what_to_do():
    """Признак без указания бесполезен: модель обязана знать, что с ним делать.

    ⚠️ Срез 13.5.4: канонический stdout не несёт булевого `role_not_applied` —
    он несёт ГОТОВУЮ ФРАЗУ (`_role_notice_line`), словами, той же функцией
    `roles.scope.role_notice`, которой пользуется и слушатель станции
    (`station_turn._finish_canonical_station_result`) для канона/истории хода.
    Одна функция, два читателя, вместо признака-без-текста — формулировку
    самой фразы проверять здесь не нужно (и нельзя, `test_guards_do_not_pin_
    prompt_wording.py`): модель увидит готовый текст в stdout всегда, когда он
    непуст, независимо от того, что́ про это сказано в SKILL.md словами.
    """
    script = STATION_EXTRACT.read_text(encoding="utf-8")
    assert "_role_notice_line(form, context.passport)" in script, (
        "станционный скрипт не спрашивает переиспользуемую roles.scope-функцию "
        "— признак «роль не применилась» больше некому посчитать")

    turn_src = (ROOT / "src" / "finengine" / "api" / "station_turn.py").read_text(encoding="utf-8")
    assert "_scope.role_not_applied(plan)" in turn_src and "_scope.role_notice(" in turn_src, (
        "слушатель станции больше не зовёт roles.scope — канон/история хода "
        "перестанут говорить о неприменённой роли")


def test_the_canon_header_stays_silent_and_the_comment_says_so():
    """ГРАНИЦА ПРИЗНАНА ВСЛУХ, А НЕ ЗАМОЛЧАНА: канон фразу НЕ несёт.

    Первая редакция комментария в `_result_for_screen` утверждала, что словарь
    читает модель, потому что он идёт в канон. В канон он идёт, но
    `render_extract_result` собирает шапку из своего перечня ключей, и этих
    двух в нём нет — то есть на ОБЫЧНОМ пути здешняя половина до модели не
    доходит, а доходит станционная (stdout скрипта).

    Тест держит эту границу ровно затем, чтобы её нельзя было перейти молча:
    добавить фразу в шапку — решение владельца о том, что читает ЧЕЛОВЕК
    (значок на карточке у него уже есть), и вместе с ним обязан поменяться
    комментарий, который сейчас говорит обратное.
    """
    from finengine.orchestrator.agent import render_extract_result

    text, _events = render_extract_result({
        "company_name": "ПАО Тест", "cell": "ifrs_nf_ru", "role": "dgr",
        "main_period": "2024", "prev_period": "2023", "unit": "млн руб.",
        "metrics_extracted": 119, "metrics_total_for_role_cell": 119,
        "role_not_applied": True,
        "role_notice": "роль «ДГР» для нефинансовой компании не заведена",
        "table_markdown": "| Показатель | 2024 |\n| --- | --- |\n",
    })

    assert "не заведена" not in text, (
        "шапка канона понесла фразу про неприменённую роль. Само по себе это "
        "не дефект — но комментарий в `api/station_turn._result_for_screen` "
        "утверждает, что канон её выбрасывает, и теперь врёт: поправь его"
    )
