"""Долгая стадия извлечения говорит с экраном (Б1, п.6).

ЖАЛОБА ВЛАДЕЛЬЦА 19.08: «история статусов не обновляется — пять минут на
экране ничего не менялось».

ПРИЧИНА, НАЙДЕННАЯ ПО КОДУ, А НЕ ПО ДОГАДКЕ. Движок шлёт про свои стадии пять
событий `status` («Сжимаем контекст отчета», «Извлекаем показатели из сжатого
контекста: N батчей», «Формируем таблицу», «Сжимаем контекст сегментных
раскрытий»). Скрипт навыка релеит их в канал хода, сток исправно доносит до
`_relay_event` — а там обработчика у `status` не было вовсе, и событие молча
выбрасывалось. Между стадиями сжатия и извлечения проходит по три-четыре
минуты; на экране в это время не менялось НИЧЕГО, и живой прогон выглядел
зависшим.

ЧТО СДЕЛАНО. Стадия цепляется заметкой к ИДУЩЕМУ шагу плана: остров прогресса
уже умеет её показывать, а `_progress_events` из той же заметки делает второе
событие `tool_progress` — им обновляется мерцающая строка состояния в чате.
Своей строки на экране стадия не заводит.

🔴 ПРАВИЛО «ШАГА НЕ ЗНАЕМ — МОЛЧИМ» ОТМЕНЕНО 30.08.2026, И ВОТ ЧЕМ.

Оно держалось на том, что идущий шаг есть ВСЕГДА: план объявлял шаг 0 порядка
работ `aifa-extract` (`_runner/plan.py`). 29.08 план уехал из тела навыка к
ядровому `update_plan`, а его модель зовёт не каждый ход — и стадии начали
молча выбрасываться снова, тем же способом, что и до 19.08.

Владелец увидел это первым же прогоном по НОВАТЭКу: в станции четыре минуты
работал `extract.py`, в журнале лежал status «Извлекаем показатели», а на
экране — «нет активного плана» и ни строки движения. Отличить работу от
поломки было нечем.

Место у подписи есть и было всё это время: строка без ключа `step` уходит в
`tool_progress`, тот ложится в `thinking` реплики, а `v2/page.tsx` рисует
`thinking` отдельным идущим шагом острова. План есть — заметка идёт ПОД шагом
(так она стоит в чек-листе, а не рядом); плана нет — идёт своей строкой.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: убрать ветку `if kind == "status"` из `_relay_event`
— красен первый тест; вернуть сырое сообщение движка вместо `_STAGE_NOTE` —
второй.
"""
from __future__ import annotations

import pytest

from finengine.api import station_turn


@pytest.fixture()
def screen(monkeypatch):
    """Всё, что уехало на экран этого хода."""
    from finengine.skills import run_orchestrator

    sent: list[dict] = []

    def _catch(run_id: str, event: dict) -> bool:
        sent.append(event)
        return True

    monkeypatch.setattr(run_orchestrator, "emit_to_run", _catch)
    return sent


def _channel() -> station_turn.TurnChannel:
    return station_turn.TurnChannel(
        nonce="a" * 32, gw_key="agent:aifa-station:openresponses:fe-" + "a" * 32,
        queue=None, owner_id=1, project_id="p", run_cell={"run_id": "r-1"},
        started_at=0.0)


def test_the_stage_becomes_a_note_on_the_running_step(screen):
    channel = _channel()
    station_turn._relay_event(channel, {"type": "plan", "steps": ["Определяю тип", "Извлекаю"]})
    station_turn._relay_event(channel, {"type": "plan_step", "index": 1, "status": "running"})
    screen.clear()

    station_turn._relay_event(channel, {
        "type": "status", "stage": "two_stage_compression",
        "message": "Сжимаем контекст отчета"})

    steps = [e for e in screen if e.get("type") == "plan_step"]
    assert steps, ("стадия движка не доехала до экрана — пять минут сжатия "
                   "выглядят как зависший прогон (жалоба 19.08)")
    assert steps[0]["index"] == 1, "заметка прицепилась не к идущему шагу"
    assert steps[0].get("note"), "шаг отметили без текста — на экране ничего не изменится"
    assert [e for e in screen if e.get("type") == "tool_progress"], (
        "строка состояния в чате не обновится: `tool_progress` из заметки не "
        "родился")


def test_the_stage_is_named_in_the_analysts_words(screen):
    channel = _channel()
    station_turn._relay_event(channel, {"type": "plan_step", "index": 0, "status": "running"})
    screen.clear()
    station_turn._relay_event(channel, {
        "type": "status", "stage": "two_stage_segment_compression",
        "message": "Сжимаем контекст сегментных раскрытий"})

    note = next(e for e in screen if e.get("type") == "plan_step")["note"]
    assert "контекст" not in note.lower(), (
        f"на экран уехало внутреннее слово движка: «{note}». Пользователь — "
        f"аналитик, а не оператор станции")
    assert "сегмент" in note.lower(), "подпись потеряла смысл стадии"


def test_the_segment_note_does_not_promise_finished_work(screen):
    """Подпись сегментной стадии — про настоящее, а не про законченное.

    С 20.08 сегментная стадия идёт ПАРАЛЛЕЛЬНО основной, с первой секунды
    прогона, а её статус движок отдаёт в конце. Прежнее «Ищу разбивку по
    сегментам» после этого стало бы ложью: к моменту надписи разбивка найдена и
    движок её собирает. Молчание было бы честнее вранья, но собирать — тоже
    работа, и назвать её можно правдой.
    """
    channel = _channel()
    station_turn._relay_event(channel, {"type": "plan_step", "index": 0, "status": "running"})
    screen.clear()
    station_turn._relay_event(channel, {
        "type": "status", "stage": "two_stage_segment_compression",
        "message": "Собираем разбивку по сегментам"})

    note = next(e for e in screen if e.get("type") == "plan_step")["note"].lower()
    assert not ("ищу" in note or "сжима" in note), (
        f"подпись «{note}» обещает работу, которая к этому моменту закончилась: "
        "сегментная стадия отработала параллельно основной")


def test_an_unknown_stage_is_shown_as_the_engine_said_it(screen):
    """Незнакомая стадия — сообщение движка как есть: лучше техничное, чем ничего."""
    channel = _channel()
    station_turn._relay_event(channel, {"type": "plan_step", "index": 0, "status": "running"})
    screen.clear()
    station_turn._relay_event(channel, {
        "type": "status", "stage": "two_stage_partial",
        "message": "Не удалось восстановить батчей: 2"})
    note = next(e for e in screen if e.get("type") == "plan_step")["note"]
    assert note == "Не удалось восстановить батчей: 2"


def test_without_a_plan_the_stage_still_speaks(screen):
    """Плана нет — стадия говорит СВОЕЙ строкой, а не молчит (30.08.2026).

    Прямой сторож регрессии, которую владелец поймал живым прогоном: план
    перестал объявляться телом навыка, идущего шага не стало — и четыре минуты
    работы движка выглядели на экране зависанием.

    ⚠️ ШАГ ВСЁ РАВНО НЕ ВЫДУМЫВАЕТСЯ. Проверяем это тут же: события `plan_step`
    с несуществующим индексом быть не должно, иначе чек-лист покажет шаг,
    которого никто не объявлял.
    """
    channel = _channel()
    station_turn._relay_event(channel, {
        "type": "status", "stage": "two_stage_compression", "message": "Сжимаем"})
    assert screen, (
        "стадия молча выброшена: без плана на экране не появится НИЧЕГО, и "
        "живая работа будет неотличима от зависшей"
    )
    виды = {e.get("type") for e in screen}
    assert "tool_progress" in виды, f"стадия ушла не тем событием: {виды}"
    assert "plan_step" not in виды, (
        "выдуман шаг плана, которого модель не объявляла — чек-лист покажет "
        "то, чего нет"
    )
    # Формулировку НЕ пришпиливаем: её переводит `_STAGE_NOTE` в слова
    # аналитика, и стережёт это соседний тест. Здесь предмет — что строка
    # непустая и доехала.
    текст = " ".join(str(e.get("text") or "") for e in screen).strip()
    assert текст, "событие уехало пустым — на экране будет пустая строка"


def test_a_finished_step_stops_collecting_notes(screen):
    """Шаг закрыт — стадия к нему не липнет, но и не пропадает (30.08.2026).

    Раньше здесь требовалось ПОЛНОЕ молчание. Это был тот же дефект в миниатюре:
    последний шаг помечен готовым, а движок ещё работает — и экран замирает,
    хотя работа идёт. Не липнуть к закрытому шагу правильно; молчать — нет.
    """
    channel = _channel()
    station_turn._relay_event(channel, {"type": "plan_step", "index": 1, "status": "running"})
    station_turn._relay_event(channel, {"type": "plan_step", "index": 1, "status": "done"})
    screen.clear()
    station_turn._relay_event(channel, {
        "type": "status", "stage": "table_build", "message": "Формируем таблицу"})
    assert screen, "после закрытия шага стадии снова замолчали"
    assert not [e for e in screen if e.get("type") == "plan_step"], (
        "заметка прицепилась к уже завершённому шагу — он больше не «сейчас»"
    )
