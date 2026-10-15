"""Исход прогона обязан пережить отсутствие слушателя (#276, шаг 1).

ДЕФЕКТ, РАДИ КОТОРОГО ЭТОТ ФАЙЛ. 18.08.2026 два прогона пропали целиком —
ДОМ.РФ (нонс `060f41c1`, 10:00:44) и ГК «Самолёт» (`4ce68a11`, 16:24:14, 13
событий разом). Оба прошли одной веткой: ящик хода закрылся по молчанию
очереди, терминал приехал позже, приём отбросил его с «прогресс станции некуда
доставить … подписчика нет», комната станции истекла. В `extract_runs.jsonl`
не появилось ничего — он пишется только из `finalize_*`, то есть только тогда,
когда исход КОМУ-ТО ДОСТАВЛЕН. Час работы движка восстановить было неоткуда.

ЧТО ЗДЕСЬ ДОКАЗЫВАЕТСЯ: исход оседает в хранилище В ТОЧКЕ ПРИЁМА, до и
независимо от того, слушает ли кто-нибудь; и читается обратно по нонсу и по
документу — без единого события SSE.

ПУТЬ ТЕСТА — БОЕВОЙ. Взвод канала — настоящий `station_turn.arm`, приём — сам
роут `/internal/station-progress`, чтение — ручки `/runs/*` приложения FastAPI
с подставленным пользователем. В базу заглядываем только НАБЛЮДАТЕЛЕМ
(`rev`, число строк): тест, ходивший в обход движка, у нас уже был зелёным при
сломанном проде (#137).

ЧЕМ ЭТОТ ТЕСТ НЕ ЯВЛЯЕТСЯ. Постгреса на стенде нет, поэтому SQL-хранилище
исполняется здесь на временной файловой базе тем же кодом, что пойдёт в прод.
Это проверяет НАШУ логику (адресность, монотонность статуса, изоляцию
владельца), но НЕ поведение самого Postgres и не одновременную запись из
нескольких процессов.

ПРОВЕРКА СТОРОЖЕЙ ОТКАТОМ (каждая правка обязана покрасить свой тест):
  * убрать `_remember_terminal` из `station_progress_ingest.ingest` →
    `test_outcome_survives_when_nobody_listens`;
  * писать только на `done` → `test_a_failed_run_is_stored_as_a_failure`;
  * убрать `runs.save_best_effort` из `station_turn.arm` → оба первых
    (строки прогона нет — терминалу некуда лечь);
  * снять монотонность статуса в `runs._merged_status` →
    `test_a_late_duplicate_failure_does_not_bury_a_delivered_result`;
  * убрать `user_id` из WHERE в `runs.SqlRunStore.load` →
    `test_another_users_outcome_is_not_readable`;
  * снять последнюю сеть в `runs.save_best_effort`/`finish_best_effort` →
    `test_a_broken_store_does_not_break_the_turn`;
  * сделать дефолт непустым или унаследовать адрес от диалогов →
    `test_without_database_url_nothing_changes`;
  * убрать `runs.finish_best_effort` из `station_run_sink._abandon` →
    `test_a_run_that_died_in_silence_leaves_its_outcome`;
  * убрать `_remember_screen` из `station_turn._finish_run`, ИЛИ переставить
    запоминание шапки в `_relay_event` НИЖЕ проверки `turn_over` →
    `test_the_screen_of_a_finished_run_survives_a_closed_tab`;
  * убрать `FINENGINE_RUNS_DATABASE_URL` из базового `deploy/docker-compose.yml`
    (или вернуть его в dev-оверлей) → `test_the_stand_can_turn_the_storage_on`;
  * снять адресата `on_abandoned` в `station_run_sink.drain` ИЛИ вернуть
    `_remember_partial` к записи одних счётчиков →
    `test_a_silent_end_saves_the_rows_it_managed_to_collect`.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

# Через сколько дренаж сдаётся в тестах молчания. Боевое значение —
# `WORKER_LIFETIME_SEC` (двадцать минут), ждать его тест не может.
#
# ⚠️ НЕ УМЕНЬШАТЬ ДО ДЕСЯТКОВ МИЛЛИСЕКУНД. Здесь стояло 0.05, и это была
# ловушка: `timeout` у дренажа — ПОЛНОЕ время жизни, а не простой очереди.
# Первый же `on_event` тянет ленивый импорт `llm.openclaw` — на холодном
# процессе это дольше 50 мс, дедлайн срабатывает РАНЬШЕ, чем строки успевают
# доехать, и тест проверяет пустоту. Поймано смоуком на стенде 19.08: локально
# зелено (модуль уже импортирован фикстурой), в контейнере — ноль строк.
_ABANDON_AFTER = 0.5

OWNER = 7
STRANGER = 8
NONCE = "060f41c1deadbeef060f41c1deadbeef"
GW_KEY = f"agent:aifa-station:openresponses:fe-{NONCE}"
DOC = "doc-domrf"

# Терминал станции ровно той формы, что приезжает с пути по навыкам
# (`aifa-extract/scripts/extract.py` → `done.result`).
DONE_EVENT = {
    "type": "done",
    "result": {
        "cell": "ifrs_bank_ru", "company_type": "bank", "role": "other",
        "company_name": "ДОМ.РФ", "main_period": "2024", "prev_period": "2023",
        "metrics_found": 96, "metrics_total": 110, "segments_count": 20,
        "group_count": 0,
    },
}


@pytest.fixture
def api(tmp_path, monkeypatch):
    """Приложение с включённым хранилищем исходов + вошедший пользователь."""
    from finengine.api import internal_docs, main as m, station_progress_ingest
    from finengine.api import station_turn
    from finengine.auth.db import User
    from finengine.auth.dependencies import get_current_user
    from finengine.config import settings
    from finengine.skills import grounding_bus, run_logger
    from finengine.store import runs

    db_path = tmp_path / "runs.db"
    monkeypatch.setattr(settings, "runs_database_url", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setattr(runs, "_cached", None)
    # Сырьё хода уводим в tmp: журнал событий пишется по пути, вычисленному при
    # импорте, и без подмены тест дописывал бы рабочий station_events.jsonl.
    monkeypatch.setattr(run_logger, "_STATION_EVENTS_PATH", tmp_path / "station_events.jsonl")
    # То же для журнала прогонов: молчаливый конец и `finalize_extraction` в него
    # пишут, а по нему считается бенчмарк — засорять его тестовыми прогонами
    # значит портить измерение качества.
    monkeypatch.setattr(run_logger, "_LOG_PATH", tmp_path / "extract_runs.jsonl")
    # Токен приёма в окружении стенда сделал бы вызов 401 — приём тут не предмет.
    monkeypatch.delenv("FINENGINE_OPENCLAW_GATEWAY_TOKEN", raising=False)

    station_progress_ingest._bus.clear()
    station_turn._detached.clear()
    internal_docs.reset_scopes()
    grounding_bus._worker_docs.clear()

    who = {"id": OWNER}
    m.app.dependency_overrides[get_current_user] = lambda: User(
        id=who["id"], username=f"u{who['id']}")
    try:
        yield TestClient(m.app), who, db_path
    finally:
        m.app.dependency_overrides.pop(get_current_user, None)
        station_progress_ingest._bus.clear()
        station_turn._detached.clear()
        grounding_bus._worker_docs.clear()


def _turn_without_a_listener(events: list[dict], *, nonce: str = NONCE,
                             doc_id: str = DOC, owner: int = OWNER) -> dict:
    """Ход взвёлся, закрылся — и ТОЛЬКО ПОТОМ станция прислала терминал.

    Это буквальный сценарий 18.08: подписки на момент прихода исхода нет ни
    одной, и до появления хранилища событие в этот момент отбрасывалось.
    """
    from finengine.api import station_progress_ingest as ingest
    from finengine.api import station_turn
    from finengine.skills import grounding_bus

    async def scenario() -> dict:
        cell: dict = {}
        station_turn.arm(nonce=nonce, gw_key=f"agent:aifa-station:openresponses:fe-{nonce}",
                         doc_ids=[doc_id], owner_id=owner, project_id="p1", cell=cell)
        # Комната забрала текст — с этого момента бэкенд знает документ нонса.
        grounding_bus.bind_worker_doc(nonce, doc_id)
        station_turn.disarm(cell, stopped=True)
        await asyncio.sleep(0)
        assert not ingest._bus.get(nonce), (
            "подписчик остался — тогда сценарий проверяет не то, ради чего написан")
        return await ingest.ingest(ingest.ProgressBatch(nonce=nonce, events=events),
                                   authorization="")

    return asyncio.run(scenario())


def _rows(db_path) -> list[dict]:
    """Строки исходов глазами наблюдателя."""
    engine = create_engine(f"sqlite+pysqlite:///{db_path}")
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(
            "SELECT nonce, kind, status, user_id, doc_id, rev, finished_at, error "
            "FROM station_run ORDER BY nonce")).mappings()]


def test_outcome_survives_when_nobody_listens(api):
    """ГЛАВНЫЙ СТОРОЖ: подписчика нет вовсе, а исход всё равно записан и читается."""
    client, _who, db = api

    accepted = _turn_without_a_listener([DONE_EVENT])
    assert accepted["reason"] == "no_consumer", (
        "приём нашёл подписчика — значит проверяется не потеря 18.08")

    got = client.get(f"/runs/{NONCE}")
    assert got.status_code == 200, "исход прогона не сохранился ни в каком виде"
    body = got.json()
    assert body["status"] == "done"
    assert body["doc_id"] == DOC, "исход есть, но не привязан к документу"
    assert body["payload"]["company_name"] == "ДОМ.РФ"
    assert body["payload"]["metrics_found"] == 96, (
        "счётчики прогона потеряны — по такому исходу не понять, что он дал")
    assert body["finished_at"] > 0, "прогон записан как идущий, хотя он кончился"

    stored = _rows(db)
    assert [r["nonce"] for r in stored] == [NONCE], "исход адресуется не нонсом"
    assert stored[0]["user_id"] == OWNER, (
        "исход записан без владельца — прочитать его не сможет никто")


def test_extraction_and_analysis_of_one_turn_are_two_outcomes(api):
    """У одного хода два прогона — и два исхода, а не один затёртый.

    `run_id` у них общий (ран хода один), нонсы разные. Ключуй строку раном —
    и второй исход молча заменил бы первый.
    """
    from finengine.api import station_progress_ingest as ingest
    from finengine.api import station_turn

    client, _who, db = api
    analysis = station_turn.analysis_nonce_of(NONCE)

    async def scenario() -> None:
        cell: dict = {}
        station_turn.arm(nonce=NONCE, gw_key=GW_KEY, doc_ids=[DOC], owner_id=OWNER,
                         project_id="p1", cell=cell)
        station_turn.arm(nonce=analysis, gw_key=GW_KEY, doc_ids=[DOC], owner_id=OWNER,
                         project_id="p1", cell=cell, kind=station_turn.KIND_ANALYZE)
        station_turn.disarm(cell, stopped=True)
        await asyncio.sleep(0)
        await ingest.ingest(ingest.ProgressBatch(nonce=NONCE, events=[DONE_EVENT]),
                            authorization="")
        await ingest.ingest(ingest.ProgressBatch(
            nonce=analysis,
            events=[{"type": "done", "result": {"analysis_type": "general"}}]),
            authorization="")

    asyncio.run(scenario())

    assert {r["nonce"]: r["kind"] for r in _rows(db)} == {
        NONCE: "extract", analysis: "analyze"}
    assert client.get(f"/runs/{analysis}").json()["payload"]["analysis_type"] == "general"


def test_a_failed_run_is_stored_as_a_failure_not_as_silence(api):
    """Отказ прогона — это тоже исход. Молчание вместо него неотличимо от «идёт»."""
    client, _who, _db = api

    _turn_without_a_listener([{"type": "error", "message": "станция: MemoryError"}])

    body = client.get(f"/runs/{NONCE}").json()
    assert body["status"] == "failed"
    assert "MemoryError" in (body["error"] or ""), "текст отказа не доехал"
    assert body["finished_at"] > 0


def test_a_red_step_is_not_a_failed_run(api):
    """Красный ШАГ плана — не отказ прогона.

    `plan_step` приезжает с `status: "error"`, и приём его логирует как ошибку
    станции. Похоронить по нему весь исход значило бы записать отказом прогон,
    который через минуту отдаст таблицу.
    """
    client, _who, _db = api

    _turn_without_a_listener([{"type": "plan_step", "index": 2, "status": "error"}])

    assert client.get(f"/runs/{NONCE}").json()["status"] == "running"


def test_the_same_outcome_is_found_by_the_document(api):
    """Второй адрес исхода — документ: нонса пользователь не знает."""
    client, _who, _db = api

    _turn_without_a_listener([DONE_EVENT])

    body = client.get(f"/runs/by-doc/{DOC}", params={"kind": "extract"}).json()
    assert body["nonce"] == NONCE
    assert body["payload"]["metrics_found"] == 96
    assert client.get("/runs/by-doc/doc-другой").status_code == 404


def test_a_late_duplicate_failure_does_not_bury_a_delivered_result(api):
    """Дубль прогона (#185), добежавший позже и упавший, не смеет стереть успех.

    18.08 в комнате ДОМ.РФ шли ТРИ копии извлечения. Копия, упавшая последней,
    переписала бы строку в `failed` — при таблице, уже стоящей у пользователя
    на экране.
    """
    client, _who, db = api

    _turn_without_a_listener([DONE_EVENT])
    _turn_without_a_listener([{"type": "error", "message": "дубль упал"}])

    body = client.get(f"/runs/{NONCE}").json()
    assert body["status"] == "done", "поздний отказ дубля похоронил доставленный результат"
    assert body["payload"]["metrics_found"] == 96, "дубль стёр числа успешного прогона"
    assert [r["rev"] for r in _rows(db) if r["nonce"] == NONCE][0] >= 3, (
        "повтор не оставил следа — разбирать дубли будет нечем")


def test_another_users_outcome_is_not_readable(api):
    """Чужой исход не читается ни по нонсу, ни по документу — и это 404, не 403.

    Различимый ответ («есть, но не твой») превращает ручку в перебор чужих
    нонсов.
    """
    client, who, _db = api

    _turn_without_a_listener([DONE_EVENT])
    who["id"] = STRANGER

    assert client.get(f"/runs/{NONCE}").status_code == 404
    assert client.get(f"/runs/by-doc/{DOC}").status_code == 404


def test_a_broken_store_does_not_break_the_turn(api, caplog):
    """Упало хранилище — ход НЕ падает, но и молчания нет: причина в логе."""
    from finengine.store import runs

    client, _who, _db = api

    class _BrokenStore:
        def save(self, outcome):
            raise RuntimeError("база недоступна")

        def finish(self, nonce, **kwargs):
            raise RuntimeError("база недоступна")

        def load(self, nonce, *, user_id):
            raise RuntimeError("база недоступна")

        def latest_for_doc(self, doc_id, *, user_id, kind=""):
            raise RuntimeError("база недоступна")

    with caplog.at_level(logging.WARNING):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(runs, "get_run_store", lambda: _BrokenStore())
            accepted = _turn_without_a_listener([DONE_EVENT])

    assert accepted["reason"] == "no_consumer", "приём упал вместе с хранилищем"
    text_log = caplog.text
    assert "не записан" in text_log, "отказ хранилища проглочен молча"
    assert "RuntimeError" in text_log, "в логе нет причины — разбирать будет нечего"
    assert any(r.exc_info for r in caplog.records), "отказ записан без стека"
    # Хранилище живо — ход не оставил после себя сломанного состояния.
    assert client.get(f"/runs/{NONCE}").status_code == 404


def test_a_run_that_died_in_silence_leaves_its_outcome(api):
    """МОЛЧАНИЕ — ТОЖЕ ИСХОД (Д2 в разборе #276).

    ЧТО ЛОВИТ. Точка приёма закрывает строку прогона по событиям `done`/`error`.
    Молчаливый конец события НЕ ПРИСЫЛАЕТ — его нет ни в одной пачке, — поэтому
    строка оставалась `running` навсегда, а `latest_for_doc` отбирает по
    `finished_at > 0` и не отдавал её никогда. Хранилище было заведено ради
    потерь ровно этого класса, и мимо него же этот класс и проходил.

    Дренаж здесь БОЕВОЙ, укорочен только его собственный параметр `timeout`:
    ждать штатные восемнадцать минут тест не может, а подменять сам сток —
    значит проверять не тот код, который поедет.
    """
    from finengine.api import station_turn
    from finengine.skills import grounding_bus, station_run_sink

    client, _who, db = api

    async def scenario() -> None:
        cell: dict = {}
        station_turn.arm(nonce=NONCE, gw_key=GW_KEY, doc_ids=[DOC], owner_id=OWNER,
                         project_id="p1", cell=cell)
        grounding_bus.bind_worker_doc(NONCE, DOC)
        station_turn.disarm(cell, stopped=True)
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait({"type": "metric", "enum": "NET_PROFIT",
                          "company_name": "ДОМ.РФ", "found": True})
        await station_run_sink.drain(
            nonce=NONCE, queue=queue, owner_id=OWNER, timeout=_ABANDON_AFTER,
            on_done=lambda run: None, on_failed=lambda message: None)

    asyncio.run(scenario())

    body = client.get(f"/runs/{NONCE}").json()
    assert body["status"] == "failed", "прогон замолчал, а в базе он всё ещё «идёт»"
    assert body["finished_at"] > 0, "без отметки конца исход не найдётся по документу"
    assert "затих" in (body["error"] or ""), "по исходу не понять, что именно случилось"
    assert body["payload"]["metrics_extracted"] == 1, (
        "счётчик собранного потерян — не отличить «умер пустым» от «умер с таблицей»")

    # ГЛАВНОЕ: исход находится по ДОКУМЕНТУ. Пользователь нонса не знает.
    assert client.get(f"/runs/by-doc/{DOC}").json()["nonce"] == NONCE

    assert [r["status"] for r in _rows(db)] == ["failed"]


def test_the_screen_of_a_finished_run_survives_a_closed_tab(api):
    """Typed-результат, который видел человек, лежит в строке прогона (Д5).

    СЦЕНАРИЙ БУКВАЛЬНО ТОТ, ЧТО ПОТЕРЯЛ «САМОЛЁТ»: прогресс приехал, вкладку
    закрыли, ход кончился — и только потом станция отдала терминал. Раньше в
    этот момент собранный экран уезжал в никуда.

    ⚠️ Срез 13.5.4: канонический путь не копит СТРОКИ отдельно от терминала —
    `_finish_canonical_station_result` сохраняет typed-результат (`FormOutput`)
    ЦЕЛИКОМ, ОДНИМ прохода по готовому `done`, независимо от того, кончился ли
    ход раньше терминала. Порядок «шапка сначала» (который старый тест
    сторожил в `_relay_event`) больше не существует как отдельная забота: нет
    отдельной шапки, нет отдельных строк — есть один typed-результат.
    """
    from finengine.api import station_progress_ingest as ingest
    from finengine.api import station_turn
    from finengine.skills import grounding_bus

    client, _who, _db = api
    rows = [
        {"row_id": "r0", "order": 0, "kind": "metric", "enum": "NET_PROFIT",
         "label": "Чистая прибыль", "value": 12.5, "display_value": "12.5",
         "status": "supported", "evidence_refs": []},
        {"row_id": "r1", "order": 1, "kind": "metric", "enum": "TOTAL_EQUITY",
         "label": "Итого капитал", "value": 80.0, "display_value": "80.0",
         "status": "supported", "evidence_refs": []},
    ]
    done_event = {"type": "done", "result": {
        "schema_version": "1.0", "case_id": "case-1", "document_id": DOC,
        "document_version": "sha256:abc",
        "run_passport": {
            "run_id": "run-1", "goal_ir_id": "goal-1",
            "classification": {"profile_id": "ifrs_nf_ru", "standard": "ifrs",
                               "company_type": "non_financial", "language": "ru"},
            "entity": {"entity_id": "e-1", "canonical_name": "ГК «Самолёт»",
                      "raw_name": "ГК «Самолёт»"},
            "registry_version": "v1", "methodology_version": "unversioned",
            "model_version": "m1", "prompt_version": "p1", "sandbox_image_version": "s1",
        },
        "form_output": {
            "schema_version": "1.0", "output_type": "FormOutput", "output_id": "fo-1",
            "run_id": "run-1", "status": "complete", "role": "other",
            "layout_id": "legacy", "layout_version": "0", "preserve_empty": False,
            "rows": rows, "gaps": [], "warnings": [],
            "run_metadata": {"run_id": "run-1", "methodology_version": "unversioned"},
        },
        "file_artifact": None,
    }}

    async def scenario() -> None:
        cell: dict = {}
        # ПРОЕКТА НЕТ СОЗНАТЕЛЬНО: с ним слушатель полез бы искать хранилище
        # пользователя для кнопки Excel, а предмет теста — не книга.
        station_turn.arm(nonce=NONCE, gw_key=GW_KEY, doc_ids=[DOC], owner_id=OWNER,
                         project_id="", cell=cell)
        grounding_bus.bind_worker_doc(NONCE, DOC)
        channel = station_turn.channels_of(cell)[station_turn.KIND_EXTRACT]

        await ingest.ingest(ingest.ProgressBatch(
            nonce=NONCE, events=[{"type": "node_run", "plan_graph_hash": "h1",
                                  "node_run": {}}]), authorization="")
        for _ in range(50):
            await asyncio.sleep(0)
            if channel.saw_life:
                break
        assert channel.saw_life, "дренаж не забрал прогресс — сценарий проверяет не то"

        station_turn.disarm(cell)  # вкладку закрыли, прогон дорабатывает один
        assert channel.turn_over, "ход не отцепился — сценарий проверяет не то"

        await ingest.ingest(ingest.ProgressBatch(nonce=NONCE, events=[done_event]),
                            authorization="")
        await asyncio.wait_for(channel.task, timeout=10)

    asyncio.run(scenario())

    screen = client.get(f"/runs/{NONCE}").json()["screen"]
    assert screen, "экран не сохранён — после перезагрузки страницы таблицы не будет"
    assert screen["output_type"] == "FormOutput"
    saved_rows = screen["result"]["rows"]
    assert [r["enum"] for r in saved_rows] == ["NET_PROFIT", "TOTAL_EQUITY"], (
        "в экране не те строки")
    assert saved_rows[0]["value"] == 12.5
    assert screen["result"]["role"] == "other", "роль потеряна — раскладку не восстановить"


def test_a_silent_end_saves_the_rows_it_managed_to_collect(api):
    """Оборванный прогон отдаёт СТРОКИ, а не четыре счётчика (находка 19.08).

    ЗАМЕР, КОТОРЫЙ ЭТО ОПЛАТИЛ. Прогон по Банку «Санкт-Петербург» (нонс
    `4155b31e…`): станция за пятнадцать минут отдала 110 показателей и 23
    сегментные строки, `metrics.json` на 121 917 байт записан в комнате — и
    терминала не прислала. Модель запустила извлечение с `--no-final` и
    закончила ход, не запустив расчёт, который по эстафете ставит терминал.
    Сток прождал дедлайн, закрыл прогон отказом — и ВЫБРОСИЛ все 133 строки,
    лежавшие у него в памяти. В базу уехали счётчики.

    Здесь проверяется, что строки доезжают. Роли и раскладки у них нет и быть
    не может: их собирает `finalize_extraction` по сводке терминала, которого
    не было. `done: null` — это честно, а не недоделка.
    """
    from finengine.api import station_turn
    from finengine.skills import grounding_bus, station_run_sink

    client, _who, _db = api
    start = {"type": "start", "company_name": "БАНК «САНКТ-ПЕТЕРБУРГ",
             "company_type": "bank", "total_extractable": 80,
             "period_current": "Q2 2025", "period_prev": "Q2 2024", "unit": "млн. руб."}

    async def scenario() -> None:
        cell: dict = {}
        station_turn.arm(nonce=NONCE, gw_key=GW_KEY, doc_ids=[DOC], owner_id=OWNER,
                         project_id="", cell=cell)
        grounding_bus.bind_worker_doc(NONCE, DOC)
        channel = station_turn.channels_of(cell)[station_turn.KIND_EXTRACT]
        # Дренаж БОЕВОЙ, укорочен только его собственный `timeout`: ждать
        # штатные двадцать минут тест не может.
        queue: asyncio.Queue = asyncio.Queue()
        for ev in (start,
                   {"type": "metric", "enum": "NET_PROFIT", "name_ru": "Чистая прибыль",
                    "value_current": 12.5, "found": True},
                   {"type": "metric", "enum": "FEE_AND_COMMISSION_INCOME",
                    "dimension": "segment", "segment": "Розница", "value_current": 3615.0}):
            queue.put_nowait(ev)
        await station_run_sink.drain(
            nonce=NONCE, queue=queue, owner_id=OWNER, timeout=_ABANDON_AFTER,
            on_event=lambda ev: station_turn._relay_event(channel, ev),
            on_done=lambda run: None, on_failed=lambda message: None,
            on_abandoned=lambda run: station_turn._remember_partial(channel, run))
        station_turn.disarm(cell, stopped=True)

    asyncio.run(scenario())

    body = client.get(f"/runs/{NONCE}").json()
    assert body["status"] == "failed", "оборванный прогон не должен выглядеть удачным"
    screen = body["screen"]
    assert screen, "строки выброшены — в базе снова одни счётчики"
    assert [r["enum"] for r in screen["rows"]] == ["NET_PROFIT"], (
        "в экране не те строки: сегментные едут своей дорогой, как и в живом потоке")
    assert screen["start"]["company_name"] == "БАНК «САНКТ-ПЕТЕРБУРГ", "шапка не сохранена"
    assert screen["done"] is None, (
        "итог выдуман: сводки у оборванного прогона нет, и подделывать её нельзя")
    # Счётчики от `_abandon` при этом на месте — две записи не затирают друг друга.
    assert body["payload"]["metrics_extracted"] == 1
    assert body["payload"]["segments_count"] == 1
    assert client.get(f"/runs/by-doc/{DOC}").json()["nonce"] == NONCE


def test_the_stand_can_turn_the_storage_on():
    """ПРОВОДКА, а не только код: без переменной в `deploy/` всё выше — no-op.

    Первая редакция этого механизма пролежала сутки и не сделала ничего:
    `grep -rn FINENGINE_RUNS deploy/` не находил ничего, оба стенда работали на
    `NullRunStore`. Это болезнь «сделанное не доезжает», и сторож здесь потому,
    что кода она не касается вовсе.

    ⚠️ ФАЙЛ ИМЕННО БАЗОВЫЙ, И ЭТО ПОЛОВИНА СМЫСЛА ТЕСТА. 19.08 переменная
    сначала легла в `docker-compose.dev.yml` — и была бы так же мертва: стенд
    поднимается ОДНИМ `docker compose -f deploy/docker-compose.yml` (проверено
    `docker inspect finengine-backend-1`, метка `config_files`). Ровно этим
    объясняется, почему адрес базы диалогов, положенный туда же 09.08, включить
    было нечем. Переставь путь на dev-оверлей — тест обязан покраснеть.
    """
    base = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "deploy/docker-compose.yml").read_text())

    env = base["services"]["backend"]["environment"]
    assert env.get("FINENGINE_RUNS_DATABASE_URL") == "${FINENGINE_RUNS_DATABASE_URL:-}", (
        "адрес базы прогонов не пробрасывается из окружения — либо его нет вовсе, "
        "либо он зашит в compose и перестал быть решением владельца")
    assert env.get("FINENGINE_DIALOGS_DATABASE_URL") == "${FINENGINE_DIALOGS_DATABASE_URL:-}", (
        "адрес базы диалогов снова только в dev-оверлее — включить его на стенде нечем")
    # 🔴 ПРОФИЛЕЙ БОЛЬШЕ НЕТ, И ЭТО ПРАВКА, А НЕ ПОЛОМКА (26.08.2026, `8049e4b`).
    #
    # Здесь стояло `base["services"]["postgres"]["profiles"]` с проверкой на
    # `runs-pg` / `dialogs-pg`. Профили появились в эпоху первых тумблеров,
    # когда база была экспериментом и её прятали от обычного `up -d`. Теперь
    # бэкенд ТРЕБУЕТ её на старте — файловых запасных путей больше нет, — и
    # «необязательный» флажок стал ловушкой: пропади контейнер после `down`
    # или чистки диска, обычный `up -d` его бы не вернул.
    #
    # Предмет сторожа тот же — «базу есть чем поднять», — но проверяется теперь
    # обратное условие: она поднимается ВСЕГДА, без флага.
    pg = base["services"]["postgres"]
    assert "profiles" not in pg, (
        "postgres снова спрятан под профиль — обычный `up -d` его не поднимет, "
        "а бэкенд без базы больше не стартует вовсе")
    assert pg.get("healthcheck"), (
        "у postgres нет healthcheck — любое «дождись готовности» станет слепым: "
        "контейнер создан, а принимать соединения он ещё не начал")


def test_without_database_url_nothing_changes(api, monkeypatch):
    """ДЕФОЛТ НИЧЕГО НЕ ПЕРЕКЛЮЧАЕТ: пустая настройка = поведение как раньше.

    Момент переезда живых пользователей выбирает владелец. Этот сторож ловит и
    непустой дефолт, и наследование адреса от диалогов — то есть попытку
    включить хранилище заодно с выкаткой кода.
    """
    from finengine.config import Settings, settings
    from finengine.store import runs

    client, _who, db = api
    assert Settings.model_fields["runs_database_url"].default == "", (
        "дефолт непустой — выкатка кода включила бы хранилище живым пользователям")

    monkeypatch.setattr(settings, "runs_database_url", "")
    monkeypatch.setattr(settings, "dialogs_database_url", "postgresql+psycopg://x/y")
    monkeypatch.setattr(runs, "_cached", None)
    assert isinstance(runs.get_run_store(), runs.NullRunStore), (
        "адрес унаследован от диалогов — один тумблер включает две миграции")

    accepted = _turn_without_a_listener([DONE_EVENT])

    assert accepted == {"accepted": 0, "dropped": 1, "reason": "no_consumer"}, (
        "приём повёл себя не так, как до правки")
    assert not db.exists(), "к базе подключились при выключенном хранилище"
    assert client.get(f"/runs/{NONCE}").status_code == 404, (
        "выключенное хранилище обязано отвечать «нет данных», а не отказом")
