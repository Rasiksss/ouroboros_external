"""Слияние диалогов не имеет права обеднить сообщение (#143).

РЕАЛЬНЫЙ СЛУЧАЙ. Пользователь провёл извлечение, увидел таблицу на 75 строк,
ушёл на другую вкладку, вернулся — в рабочей области пусто, в ленте чата
таблица на месте. Причина: карточка извлечения уезжает на сервер В МОМЕНТ
СТАРТА прогона, пустой (`metrics: []`, `status: running`), а союз сообщений
брал ПЕРВОЕ вхождение id — то есть хранимую, стартовую версию. Дозаполненная
карточка на сервер уже не попадала никогда, и подтяжка приносила клиенту ноль
строк поверх живых семидесяти пяти.

Инвариант, который здесь закреплён: слияние может ДОПОЛНИТЬ сообщение, но не
обеднить — ни по числу извлечённых строк, ни по тексту ответа, ни по статусу
(running → done необратим).
"""
from __future__ import annotations


def _merge(a: list, b: list) -> list:
    from finengine.api.main import _merge_messages  # noqa: PLC0415

    return _merge_messages(a, b)


def _card(n_metrics: int, status: str = "done") -> dict:
    return {
        "id": "extract-1",
        "role": "extraction",
        "created_at": 1000,
        "status": status,
        "metrics": [{"enum": f"M_{i}"} for i in range(n_metrics)],
    }


def test_rich_extraction_card_beats_stored_empty_one():
    """Хранимая пустая карточка не должна пережить пришедшую полную."""
    merged = _merge([_card(0, "running")], [_card(75)])
    assert len(merged) == 1
    assert len(merged[0]["metrics"]) == 75
    assert merged[0]["status"] == "done"


def test_order_of_arguments_does_not_decide():
    """Порядок аргументов — деталь вызова, а не политика сохранности."""
    assert len(_merge([_card(75)], [_card(0, "running")])[0]["metrics"]) == 75


def test_streamed_answer_is_not_truncated_by_its_stub():
    """Ответ агента пушится по ходу стрима: обрезок не должен победить целое."""
    stub = {"id": "a1", "role": "assistant", "created_at": 5, "content": "Ана"}
    full = {"id": "a1", "role": "assistant", "created_at": 5, "content": "Анализ готов."}
    assert _merge([stub], [full])[0]["content"] == "Анализ готов."
    assert _merge([full], [stub])[0]["content"] == "Анализ готов."


def test_union_keeps_both_sides_and_chronology():
    """Слияние остаётся union'ом: чужое сообщение не теряется."""
    mine = {"id": "m1", "role": "user", "created_at": 2, "content": "привет"}
    theirs = {"id": "m2", "role": "assistant", "created_at": 1, "content": "ответ"}
    assert [m["id"] for m in _merge([mine], [theirs])] == ["m2", "m1"]


def test_legacy_message_without_id_survives():
    """У легаси-строк id нет — суррогатный ключ не должен их склеивать."""
    a = {"role": "user", "created_at": 1, "content": "раз"}
    b = {"role": "user", "created_at": 2, "content": "два"}
    assert len(_merge([a], [b])) == 2
