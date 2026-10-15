"""Ш11: в гейтвей-режиме за модель не решают — решателей больше нет вовсе.

ДЕФЕКТ, ради которого это написано. `gateway_session_key` встречался в
`agent.py` ровно дважды — параметром `query_stream` и пробросом в
`llm.stream_events`. То есть на гейтвей-пути исполнялись И механический
диспетчер `_start_direct_extract_run`, И отдельный вызов модели
`detect_turn_intent`, И взвод почтовых ящиков воркеров — байт-в-байт как на
инлайне. Любой замер «двух путей» при этом сравнивал не два пути, а ОДИН
диспетчер под двумя петлями, и приёмки следующих шагов давали ложный зелёный.

ЧЕМ ХОДИМ. Тем же путём, что пользователь: боевым `FinengineAgent.query_stream`.
Подменены только внешние опоры хода — текст документа и вызов модели.

🔴 ШАГ Ш11 ЗАКРЫТ СНОСОМ, а не гейтом (aa13cb1). Обоих решателей больше нет:
`orchestrator.intent` снесён модулем целиком, `agent._start_direct_extract_run`
— вместе с монолитными тулами (проверено грепом по всему `src/`: ноль
вхождений обоих). Поэтому здесь не осталось ни шпионов на них, ни инлайн-
половины «а тут решают по-прежнему»: сравнивать не с чем, инлайн-путь такого
диспетчера тоже не имеет.

ЧТО СТОРОЖИТСЯ СЕГОДНЯ — то, что осталось живым и держится ТОЛЬКО этим файлом:
нонс хода доезжает до модели одним источником (`api/station_turn.run_nonce_of`),
пул воркеров в гейтвей-петле пуст, и промпт не обещает `sessions_spawn`.
Снятие любого из трёх даёт НЕМОЙ отказ станции на первом шаге доставки
документа: 403 по скоупу, FETCH-DENIED, код возврата 3.
"""
from __future__ import annotations

import asyncio

import pytest

from finengine.config import settings
from finengine.orchestrator import agent as ag

USER = 11
DOC = "doc-gateway-mode"
# Ключ РЕАЛЬНОЙ формы (`main.py`): из него выводится нонс хода — хвост после
# `fe-`. С условной строкой вроде «gw-session-key» проверка нонса прошла бы
# и при неверном выводе: там весь ключ равен «нонсу».
GATEWAY_KEY = "agent:aifa-station:openresponses:fe-a1b2c3d4e5f60718"


@pytest.fixture(autouse=True)
def _tmp_data(tmp_path, monkeypatch):
    """Журналы и текст документа — в tmp, а не в рабочие данные владельца."""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "docs_dir", tmp_path)
    (tmp_path / f"{DOC}.txt").write_text("текст отчёта", encoding="utf-8")
    yield


@pytest.fixture(autouse=True)
def _the_router_hands_the_turn_to_the_dispatcher(monkeypatch):
    """Срез 31: каждое сообщение сначала разбирает модель-маршрутизатор. Эти тесты
    про то, что ДОЕЗЖАЕТ до диспетчера в гейтвей-режиме (нонс, карта, пул), — маршрут
    «ответить самой» подменён детерминированно, чтобы без живой модели ход дошёл
    до диспетчера (раньше его останавливал fail-closed гейт: реестр документов в
    тесте не настроен)."""
    async def _answer(system, prompt, *, model_version=None):
        return '{"route": "answer", "intent_kind": null, "targets": []}'

    monkeypatch.setattr("finengine.skills.atoms.intent._default_complete", _answer)


class _Turn:
    """Внешние опоры хода. Шпионов на решатели больше нет — нет и решателей."""

    def __init__(self) -> None:
        self.prompt = ""

    def arm(self, monkeypatch) -> None:
        async def _model(**kw):
            self.prompt = kw.get("prompt") or ""
            yield {"type": "done", "usage": {}}

        monkeypatch.setattr(ag, "_build_context", lambda *_a, **_kw: ("текст отчёта", []))
        monkeypatch.setattr(ag.llm, "stream_events", _model)

    def run(self, *, gateway: bool) -> list[dict]:
        async def _go():
            agent = ag.FinengineAgent()
            return [ev async for ev in agent.query_stream(
                "извлеки показатели и дай краткий анализ",
                doc_ids=[DOC], auth_user_id=USER, project_id="p",
                gateway_session_key=(GATEWAY_KEY if gateway else ""),
            )]

        return asyncio.run(_go())


def test_gateway_turn_does_not_decide_for_the_model(monkeypatch):
    """Гейтвей-режим: ход не заявляет своего прогона и не запрещает модели считать.

    ⚠️ ДВА АССЕРТА СНЯТЫ 20.08.2026, и снятие важнее их самих. Они требовали
    `intent_calls == []` и `dispatch_calls == []` от шпионов, которым после
    сноса `orchestrator.intent` и `agent._start_direct_extract_run` (aa13cb1)
    нечего было наполнять НИКОГДА. Пустой список против пустого списка — это
    зелёный сторож над пустым местом: он не мог покраснеть ни при какой правке
    кода. Остались две проверки, у которых предмет живой.
    """
    turn = _Turn()
    turn.arm(monkeypatch)
    events = turn.run(gateway=True)

    assert not any(e.get("type") == "bg_runs" for e in events), (
        "диспетчерский прогон всё же заявлен фронту")
    assert "Извлечение УЖЕ ЗАПУЩЕНО системой" not in turn.prompt, (
        "модели запретили извлекать, хотя система ничего не запускала")


def test_gateway_turn_hands_out_no_worker_nonces(monkeypatch):
    """Нонсы режем в точке РОЖДЕНИЯ пула, а не на взводе ящиков.

    Гейт только на взводе оставлял бы худший исход, чем сегодня: нонс уехал
    модели, скоуп выдачи текста открыт, воркер отработал бы минуты — а ловить
    его итог уже некому. Немой отказ вместо работы.
    """
    turn = _Turn()
    turn.arm(monkeypatch)
    turn.run(gateway=True)

    assert "НОНСЫ запусков" not in turn.prompt, "нонс уехал модели без почтового ящика"
    assert "sessions_spawn` по шаблону выше" not in turn.prompt, (
        "модель послали за шаблоном воркера, которого в промпте нет")
    # Третья проверка была «`fe_extract_metrics(doc_id)` назван в промпте»:
    # раз воркера нет, пусть хоть тул поручат. #237 снял эту связку — чем
    # выполнять работу, решает модель по описаниям, и назвать тул значит
    # вернуть карту. Осталось то, что промпт обязан сказать про МЕХАНИКУ хода:
    # независимые работы идут параллельно, зависимые — по порядку навыка.
    assert "не делай их подряд" in turn.prompt, (
        "правило параллели пропало вместе с картой — вернулась работа по очереди")


def test_gateway_turn_hands_the_model_its_nonce(monkeypatch):
    """Нонс ХОДА обязан доехать до модели — иначе тело навыка неисполнимо.

    ДЫРА, ОТКРЫВШАЯСЯ ВМЕСТЕ СО СНЯТИЕМ КАРТЫ (#237 → #244). Тело навыка
    извлечения говорит: «идентификатор документа и нонс прогона выдаёт система в
    этом ходе — используй РОВНО их». В инлайн-ветке система их выдавала (пул
    нонсов воркеров). В гейтвей-петле пул пуст по Ш11 — и нонс модели не называл
    НИКТО, хотя канал хода на него уже подписан.

    Пока маршрут был прибит картой, это не всплывало: модель шла в тул, а тул
    заводит нонс сам. Сняли карту — модель вправе пойти по телу навыка, и там
    команда без нонса означает записку доставки с пустым полем: бэкенд отвечает
    403 по скоупу, скрипт печатает FETCH-DENIED и выходит с кодом 3. Прогон
    падает на первом шаге, и выглядит это как поломка станции.

    ИСТОЧНИК ОДИН: нонс ВЫВОДИТСЯ из ключа сессии той же функцией, которой
    пользуется раннер хода. Два источника разъехались бы молча.
    """
    from finengine.api.station_turn import run_nonce_of

    turn = _Turn()
    turn.arm(monkeypatch)
    turn.run(gateway=True)

    nonce = run_nonce_of(GATEWAY_KEY)
    assert nonce, "тест бессмыслен: из ключа не выводится нонс"
    assert nonce in turn.prompt, (
        "нонс хода не доехал до модели — команда станции по телу навыка "
        "получит пустой нонс и упадёт на доставке документа")


def test_inline_turn_does_not_get_the_gateway_nonce(monkeypatch):
    """Контроль: в инлайне нонсы выдаёт пул воркеров, ключа сессии там нет."""
    turn = _Turn()
    turn.arm(monkeypatch)
    turn.run(gateway=False)

    assert "НОНС ЭТОГО ХОДА" not in turn.prompt, (
        "инлайн-ход получил нонс гейтвей-петли — их два разных механизма")


def test_gateway_turn_promises_no_background_workers(monkeypatch):
    """В гейтвей-режиме нельзя обещать фоновых воркеров: их там нет.

    Саб-агент физически не может позвать ни один fe_*-тул — бэкенд отдаёт 403
    на незарегистрированный ключ сессии, а ключ ребёнка не регистрируется
    нигде (открыто шагом Ш10). Отказ НЕМОЙ: плагин вернёт модели строку с
    ошибкой, ход продолжится текстом, а пользователь будет ждать результата,
    которого никто не считает.
    """
    turn = _Turn()
    turn.arm(monkeypatch)
    turn.run(gateway=True)

    for promise in ("sessions_spawn", "фоновым прогоном", "воркер"):
        assert promise not in turn.prompt, (
            f"промпт обещает {promise!r}, а саб-агент получит 403 на первом же "
            f"вызове тула — это немой отказ вместо работы"
        )
