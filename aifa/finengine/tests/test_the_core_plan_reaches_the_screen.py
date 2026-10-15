"""План ядра (`update_plan`) доезжает до экрана — или честно говорит, что не понял.

🔴 ЗАЧЕМ. План хода — состояние РАЗГОВОРА, а не работы в комнате. Свой
`station-skills/_runner/plan.py` держит его в станции: скрипт едет на полке,
исполняется внутри запечатанной комнаты и шлёт событие по каналу нонса. У ядра
для того же есть штатный тул `update_plan`, и его системный промпт вдобавок
требует держать ровно ОДИН шаг «в работе» и не повторять план целиком после
каждой правки — то самое перепланирование с живым статусом, которого от своего
скрипта добиться нечем.

⚠️ ЧЕГО ЭТОТ СТОРОЖ НЕ ДОКАЗЫВАЕТ. Он не доказывает, что ядро вообще шлёт
зрителю `function_call`-итемы своих собственных тулов. В коде рядом стоит
комментарий «если гейтвей когда-то начнёт их слать», написанный без проверки, и
я эту проверку тоже не добыл: тул экспериментальный, схемы в бандле нет.
Поэтому здесь проверяется ПЕРЕВОД, а не доставка, и `plan.py` не снят.

Порядок: увидели в логе живой вызов → узнали форму → свели разбор к одной
ветке → только тогда сняли свой скрипт.
"""
from __future__ import annotations

import json

import finengine.llm.openclaw as o


def _вызов(имя: str, args: dict) -> dict:
    return {"type": "function_call", "name": имя,
            "arguments": json.dumps(args, ensure_ascii=False)}


def test_a_foreign_tool_is_not_mistaken_for_a_plan():
    """`None` значит «это не план» — ветка зрителя по нему и различает."""
    assert o._spectator_plan_event(_вызов("exec", {"command": "ls"})) is None
    assert o._spectator_plan_event(_вызов("read", {"path": "a.txt"})) is None


def test_plain_step_names_reach_the_screen():
    """Простейшая форма: список строк."""
    events = o._spectator_plan_event(_вызов("update_plan", {"plan": ["Раз", "Два"]}))
    assert events == [{"type": "plan", "name": "update_plan", "steps": ["Раз", "Два"]}]


def test_statuses_become_step_marks():
    """Состояния ядра переводятся в наши три: running / done / error."""
    events = o._spectator_plan_event(_вызов("update_plan", {"plan": [
        {"step": "Классифицирую", "status": "completed"},
        {"step": "Извлекаю", "status": "in_progress"},
        {"step": "Считаю", "status": "pending"},
    ]}))
    assert events[0]["steps"] == ["Классифицирую", "Извлекаю", "Считаю"]
    отметки = [(e["index"], e["status"]) for e in events[1:]]
    assert отметки == [(0, "done"), (1, "running"), (2, "running")]


def test_a_step_without_a_status_is_not_marked():
    """Пустое состояние — это «ничего не говорю про шаг», а не «не начат».

    Ядро требует держать ровно один шаг в работе, но полагаться на чужое
    требование нельзя: отметить шаг, про который ядро смолчало, значит
    показать человеку состояние, которого никто не утверждал.
    """
    events = o._spectator_plan_event(_вызов("update_plan", {"plan": [
        {"step": "Без состояния"},
        {"step": "С состоянием", "status": "completed"},
    ]}))
    отметки = [e["index"] for e in events[1:]]
    assert отметки == [1], f"отмечены шаги {отметки}, а состояние названо только у второго"


def test_an_unknown_shape_is_reported_not_swallowed(caplog):
    """Незнакомая форма обязана попасть в лог ЦЕЛИКОМ.

    Схемы тула у нас нет. Молча вернуть пустоту значит получить «план не
    пришёл» и искать причину глазами; напечатать аргументы значит узнать форму
    с первого живого вызова.
    """
    with caplog.at_level("INFO", logger=o.log.name):
        events = o._spectator_plan_event(_вызов("update_plan", {"неведомое": [1, 2]}))
    assert events == []
    напечатано = "\n".join(r.getMessage() for r in caplog.records)
    assert "незнакомая форма" in напечатано and "неведомое" in напечатано, (
        "форма не опознана и НЕ напечатана — узнать её будет нечем")


def test_broken_json_does_not_crash_the_turn(caplog):
    """Кривые аргументы — это отказ разбора, а не падение хода.

    Зритель смотрит чужую петлю; уронить ход из-за неразобранного аргумента
    значит потерять весь ответ пользователю ради строчки плана.
    """
    with caplog.at_level("INFO", logger=o.log.name):
        events = o._spectator_plan_event(
            {"type": "function_call", "name": "update_plan", "arguments": "{не json"})
    assert events == []
    assert "не разобрались" in "\n".join(r.getMessage() for r in caplog.records)
