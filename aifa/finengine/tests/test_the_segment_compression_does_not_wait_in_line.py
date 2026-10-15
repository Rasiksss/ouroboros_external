"""Сегментное сжатие идёт ОДНОВРЕМЕННО с основным, а не после него (П2).

ЗАМЕР, КОТОРЫЙ ЭТО КУПИЛ. ПСБ, `ifrs_bank_ru`, 110 показателей, двухстадийная
схема, стенд 20.08 — посекундный разрез прогона (`extract_result.run_stages`):

    main_compression            229 с
    main_extract_1              178 с
    segment_compression          83 с
    segment_extract_batch_001    12 с
    consolidated_group            3 с
    ---------------------------------
    сумма стадий                505 с   =   время прогона 505 с

Равенство и есть диагноз: стадии стояли в очередь. Сегментное сжатие ждать не
обязано — свой промпт оно строит само, по полному тексту отчёта
(`_build_segment_compression_prompt` ← `bulk._build_segment_prompt_context`), а
ручку `compression_override` ему никто не передаёт. Восемьдесят три секунды
прогона уходили на ожидание того, что можно было считать параллельно.

ЧТО СТОРОЖИТСЯ ЗДЕСЬ. Восемь разных вещей, и каждая ломается по-своему:

  1. ПАРАЛЛЕЛЬНОСТЬ — по перекрытию окон вызовов и по тому, что сумма секунд
     стадий стала БОЛЬШЕ времени прогона (в очереди они равны).
  2. НЕЗАВИСИМОСТЬ — сегментной стадии не передают результат основного сжатия.
     Передадут — параллельный запуск станет неверным молча: стадия получит
     чужую подсказку и всё равно не дождётся её.
  3. ПОРЯДОК СОБЫТИЙ — сегментные строки по-прежнему идут ПОСЛЕ плоских.
     Разбор ответа стадии стоит там же, где стоял; на экран порядок событий
     едет как есть, и всплывшие раньше времени сегменты сдвинули бы таблицу.
  4. ОТКАЗ РАНЬШЕ СРОКА — сегментная стадия теперь может упасть до того, как
     закончится основное извлечение. Прогон обязан дожить до `done`.
  5. ЖИЗНЬ ЗАДАЧИ НЕ ДЛИННЕЕ ЖИЗНИ ГЕНЕРАТОРА — генератор бросили недочитанным,
     значит стадия гаснет. Плата за параллельность: пока стадию крутил сам
     генератор, `aclose()` останавливал её на первом же `await`; задача же
     переживает и генератор, и прогон, и уходит в модель уже без потребителя.
  6. ОТКАЗ ОСНОВНОГО СЖАТИЯ гасит сегментное СРАЗУ, до отдачи ошибки наружу:
     результат стадии в таком прогоне не нужен никому.
  7. ВЫКЛЮЧЕННЫЕ СЕГМЕНТЫ — задачи нет вовсе. Ошибка в условии запуска стоит
     теперь не лишнего разбора в конце, а вызова модели по полному тексту
     отчёта на первой секунде.
  8. ПРАВДА НА ЭКРАНЕ И В РАЗРЕЗЕ — надпись про сегменты описывает настоящее,
     а не законченное; запись вызова называет время старта, иначе перекрытие
     стадий нечем разложить обратно на отрезки.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: вернуть `await` сегментной стадии на место разбора
(убрать `asyncio.create_task(_collect_segment_events())`) — краснеют
`test_the_two_compressions_overlap` и `test_the_stage_seconds_exceed_the_run`.
Убрать `try/finally` вокруг тела движка — краснеет
`test_an_abandoned_generator_takes_the_segment_stage_with_it`.

⚠️ ШАРДИРОВАНИЕ ВТОРОЙ СТАДИИ ЗДЕСЬ НЕ ТРОГАЕТСЯ. Батчи, семафор на 4
одновременных вызова и счёт потерь при срыве батча сторожит
`test_the_batch_pace_has_something_to_pace.py`. Подставная модель этого файла
идёт по ветке БЕЗ `extraction_batch_size` (один батч `main_extract_1`) — чтобы
сторож параллельности не зависел от размера батча и наоборот.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
import textwrap
import time
from types import SimpleNamespace

from finengine.skills import two_stage_extractor as ts

# Секунды подобраны так, чтобы разница «в очередь / одновременно» была видна
# сквозь округление `elapsed_s` до десятых: 0.6 + 0.6 + 0.3 против ~0.9.
STAGE_SLEEP = {"main_compression": 0.6, "segment_compression": 0.6}
DEFAULT_SLEEP = 0.3

PDF_TEXT = """===PAGE 5===
Отчёт о прибылях и убытках

| Показатель | 2024 | 2023 |
| Чистая прибыль | 100 | 90 |

===PAGE 7===
Информация по сегментам

| Сегмент | Чистая прибыль |
| Розница | 40 |
"""

STAGE_PAYLOAD = {
    "segment_compression": {"metrics": {"NET_INCOME": {"tables": [{"table_id": "t1"}],
                                                       "quotes": []}}},
    # Имена стадий несут номер попытки с 21.08 — подставная модель ищет
    # выдачу по ТОЧНОМУ имени, и без суффикса батч отвечал бы пустотой.
    "segment_extract_batch_001_attempt_01": {"metrics": {"NET_INCOME": {"rows": [
        {"segment": "Розница", "dimension": "operating_segment",
         "values": {"FY 2024": 40.0}, "unit_text": "млн руб.", "page": 7}]}}},
    "main_compression": {"metrics": {"NET_INCOME": {"tables": [{"table_id": "t1"}],
                                                    "quotes": []}}},
    "main_extract_1_attempt_01": {"NET_INCOME": {"period_values": [
        {"period": "FY 2024", "value": 100.0, "unit": "млн руб.", "page": 5}]}},
}


def _prepared() -> dict:
    metric = {"enum": "NET_INCOME", "name_ru": "Чистая прибыль", "name_en": "Net income"}
    return {
        "doc": SimpleNamespace(company_name="ПСБ", filename="psb.pdf", period="FY 2024",
                               ocr_engine="idp"),
        "pdf_text": PDF_TEXT,
        "pdf_text_for_prompt": PDF_TEXT,
        # Сырого ответа OCR у этого документа нет — раньше это выражалось
        # несуществующим путём, теперь просто `None`: сайдкар приезжает
        # байтами из хранилища, а не путём на томе.
        "raw_idp": None,
        "has_pages": True,
        "effective_type": "bank",
        "extractable": [metric],
        "calculated_defs": [],
        "dual_enums": set(),
        "segment_extractable": [metric],
        "top_borrowers_metric": None,
        "periods_detected": ["FY 2024", "FY 2023"],
        "period_current": "FY 2024",
        "period_prev": "FY 2023",
        "period_calendar": "calendar",
        "max_report_year": 2024,
        "reporting_standard": "ifrs",
        "language": "ru",
        "default_unit": "млн руб.",
        "company_display": "ПСБ",
        "source_form_by_page": {},
    }


def _patch_engine(
    monkeypatch,
    *,
    segment_blows_up: bool = False,
    main_blows_up: bool = False,
    sleeps: dict[str, float] | None = None,
) -> list[dict]:
    """Подменить модель и подготовку; вернуть список окон вызовов."""
    calls: list[dict] = []
    stage_sleep = STAGE_SLEEP if sleeps is None else sleeps

    async def _fake_call_json_llm(*, stage: str, **_kw):
        started = time.monotonic()
        wall_started = time.time()
        await asyncio.sleep(stage_sleep.get(stage, DEFAULT_SLEEP))
        elapsed = time.monotonic() - started
        if main_blows_up and stage == "main_compression":
            raise RuntimeError("основное сжатие упало")
        calls.append({"stage": stage, "started": started, "finished": time.monotonic()})
        payload = STAGE_PAYLOAD.get(stage, {})
        request = ts._llm_request_record(stage=stage, model_id="fake",
                                         started_at=wall_started, elapsed_s=elapsed,
                                         usage={})
        return payload, request, json.dumps(payload)

    async def _fake_prepare(**_kw):
        return _prepared()

    monkeypatch.setattr(ts, "_call_json_llm", _fake_call_json_llm)
    monkeypatch.setattr(ts, "_prepare_common_scope", _fake_prepare)

    if segment_blows_up:
        async def _blows_up(**_kw):
            raise RuntimeError("сегментная стадия упала на старте")
            # Тело обязано остаться генератором: подмена должна быть того же
            # рода, что и подменяемое, иначе `async for` упадёт не там.
            yield {}

        monkeypatch.setattr(ts, "_extract_segment_breakdown_two_stage", _blows_up)

    return calls


def _stream(**overrides):
    """Сам генератор движка — тестам, которым нужно бросить его недочитанным."""
    kwargs = {
        "doc_id": "psb", "company_type": "bank", "index": SimpleNamespace(),
        "include_segments": True, "use_alias_enrichment": False,
        "segment_metrics_override": {"NET_INCOME"},
    }
    kwargs.update(overrides)
    return ts._extract_stream_two_stage_full_context(**kwargs)


def _run(monkeypatch, *, segment_blows_up: bool = False,
         event_times: list[float] | None = None, **overrides) -> tuple[list[dict], list[dict]]:
    """Прогон движка на подставной модели; возвращает события и окна вызовов."""
    calls = _patch_engine(monkeypatch, segment_blows_up=segment_blows_up)

    async def _drain() -> list[dict]:
        events = []
        async for event in _stream(**overrides):
            events.append(event)
            if event_times is not None:
                event_times.append(time.monotonic())
        return events

    return asyncio.run(_drain()), calls


def _done(events: list[dict]) -> dict:
    tail = [event for event in events if event.get("type") == "done"]
    assert tail, "прогон не дошёл до `done` — сравнивать нечего"
    return tail[-1]


def test_the_two_compressions_overlap(monkeypatch):
    """ГЛАВНОЕ: сегментное сжатие стартовало ДО конца основного."""
    _events, calls = _run(monkeypatch)
    by_stage = {call["stage"]: call for call in calls}
    assert {"main_compression", "segment_compression"} <= set(by_stage), (
        f"обеих стадий сжатия в прогоне нет, а есть {sorted(by_stage)} — "
        "сторож проверяет не тот путь")
    assert by_stage["segment_compression"]["started"] < by_stage["main_compression"]["finished"], (
        "сегментное сжатие снова ждёт основного: на ПСБ это 83 с очереди "
        "на ровном месте — стадия не зависит от результата основного")


def test_the_stage_seconds_exceed_the_run(monkeypatch):
    """Сумма секунд стадий больше времени прогона — то же, но по диагностике.

    Это ровно тот разрез, которым меряют (`extract_result.run_stages`): пока
    стадии стоят в очередь, `llm_s` и `wall_s` равны.
    """
    events, _calls = _run(monkeypatch)
    diag = _done(events)["_diag"]
    assert diag["llm_s"] > diag["wall_s"], (
        f"секунды стадий {diag['llm_s']} не превысили время прогона "
        f"{diag['wall_s']} — значит вызовы снова идут один за другим")


def test_the_segment_rows_still_come_after_the_flat_ones(monkeypatch):
    """Порядок событий не поехал: сегменты по-прежнему позади плоских строк."""
    events, _calls = _run(monkeypatch)
    metrics = [i for i, e in enumerate(events)
               if e.get("type") == "metric" and not e.get("segment")]
    segments = [i for i, e in enumerate(events)
                if e.get("type") == "metric" and e.get("segment")]
    assert metrics and segments, (
        f"плоских строк {len(metrics)}, сегментных {len(segments)} — сторож "
        "порядка проверяет пустоту")
    assert min(segments) > max(metrics), (
        "сегментная строка приехала раньше плоских — таблица на экране "
        "соберётся в другом порядке, чем собиралась до параллельного запуска")
    stages = [e.get("stage") for e in events if e.get("type") == "status"]
    assert stages.index("two_stage_segment_compression") > stages.index("two_stage_extraction"), (
        "отметка сегментной стадии обогнала отметку извлечения — на экране "
        "статусы пойдут вразнобой")


def test_a_segment_failure_does_not_kill_the_run(monkeypatch):
    """Сегментная стадия упала раньше основной — прогон дожил до `done`."""
    events, _calls = _run(monkeypatch, segment_blows_up=True)
    done = _done(events)
    assert [e for e in events if e.get("type") == "metric"], (
        "плоские показатели пропали вместе с сегментной стадией — падение "
        "параллельной задачи утащило весь прогон")
    assert "сегментная стадия упала" in done["_diag"].get("segment_error", ""), (
        f"отказ сегментной стадии потерян: {done['_diag'].get('segment_error')!r}")


def test_an_abandoned_generator_takes_the_segment_stage_with_it(monkeypatch):
    """ГЛАВНОЕ ПОСЛЕ ПАРАЛЛЕЛЬНОСТИ: бросили генератор — стадия гаснет.

    ПРОБНИК, КОТОРЫЙ ЭТО КУПИЛ. Прочитать события до статуса извлечения,
    закрыть генератор (`aclose`) — и подождать. Без уборки сегментная задача
    переживает генератор и доходит до конца: в модель уезжают
    `segment_compression` с ПОЛНЫМ текстом отчёта и следом
    `segment_extract_batch_001`. Это деньги владельца и место в очереди
    провайдера за работу, которую уже некому прочитать. Бросают генератор
    буднично: «Стоп» пользователя, конец хода по таймауту, отказ ниже по
    цепочке.

    До параллельного запуска изъяна не было: стадию крутил сам генератор, и
    `aclose()` останавливал её на первом же `await`.
    """
    calls = _patch_engine(monkeypatch, sleeps={"main_compression": 0.05,
                                               "segment_compression": 0.5})

    async def _abandon() -> list[str]:
        gen = _stream()
        async for event in gen:
            if event.get("stage") == "two_stage_extraction":
                break
        await gen.aclose()
        # Дольше, чем живёт сегментное сжатие подставной модели: если задачу не
        # погасили, вызов успеет дойти до конца и попасть в `calls`.
        await asyncio.sleep(0.7)
        return [call["stage"] for call in calls]

    stages = asyncio.run(_abandon())
    assert "main_compression" in stages, (
        f"сторож бросает генератор не там, где думает: вызовы {stages}")
    assert "segment_compression" not in stages, (
        "брошенный генератор оставил сегментную стадию жить: она сходила в "
        f"модель с полным текстом отчёта уже без потребителя ({stages})")


def test_a_failed_main_compression_puts_out_the_segment_stage(monkeypatch):
    """Основное сжатие упало — сегментное не нужно, гасить СРАЗУ.

    Отмена стоит ДО отдачи `error` наружу, а не в уборке на выходе: потребитель
    может не вернуться за следующим событием вовсе, и тогда уборка не наступит,
    пока генератор не соберёт сборщик мусора. Всё это время задача жжёт токены.
    """
    calls = _patch_engine(monkeypatch, main_blows_up=True,
                          sleeps={"main_compression": 0.05, "segment_compression": 0.5})

    async def _stop_at_error() -> tuple[list[str], list[dict]]:
        gen = _stream()
        events = []
        async for event in gen:
            events.append(event)
            if event.get("type") == "error":
                break
        # Генератор ЖИВ и висит на `yield` ошибки — уборка на выходе ещё не
        # наступила, гасить стадию некому, кроме ветки отказа.
        await asyncio.sleep(0.7)
        stages = [call["stage"] for call in calls]
        await gen.aclose()
        return stages, events

    stages, events = asyncio.run(_stop_at_error())
    assert events and events[-1]["type"] == "error", (
        f"прогон не отдал ошибку основного сжатия, последнее событие {events[-1:]}")
    assert "segment_compression" not in stages, (
        "основное сжатие упало, а сегментное продолжило идти в модель: "
        f"вызовы {stages}")


def test_switched_off_segments_never_start_the_stage(monkeypatch):
    """Сегменты выключены — параллельной задачи не заводим вовсе.

    Раньше ошибка в этом условии стоила лишнего разбора в конце прогона. Теперь
    она стоит вызова модели по полному тексту отчёта на первой же секунде — и
    молча, потому что события стадии в таком прогоне всё равно никто не ждёт.
    """
    events, calls = _run(monkeypatch, disable_segments=True)
    assert "main_compression" in [call["stage"] for call in calls], (
        "прогон не дошёл до основного сжатия — сторож проверяет не тот путь")
    assert "segment_compression" not in [call["stage"] for call in calls], (
        "сегменты выключены, а стадия всё равно сходила в модель")
    assert not [e for e in events if e.get("type") == "metric" and e.get("segment")], (
        "в выключенных сегментах приехали сегментные строки")


def test_the_segment_status_is_not_about_the_past(monkeypatch):
    """Надпись на экране описывает то, что происходит СЕЙЧАС.

    После параллельного запуска сегментное сжатие к этому месту давно позади
    (на ПСБ 83 с сегментной цепочки против 407 с основной), а надпись обещала
    «Сжимаем контекст сегментных раскрытий» — пользователю сообщали бы о
    работе, которая закончилась. Сдвинуть саму надпись к старту стадии нельзя,
    не поменяв порядок статусов на экране; значит правдой должно быть слово.
    """
    times: list[float] = []
    events, calls = _run(monkeypatch, event_times=times)
    index = next(i for i, e in enumerate(events)
                 if e.get("stage") == "two_stage_segment_compression")
    finished = next(call["finished"] for call in calls if call["stage"] == "segment_compression")
    assert finished < times[index], (
        "сегментное сжатие ещё идёт, когда про него говорят — тогда старая "
        "надпись не была ложью, и этот сторож надо переписывать вместе с ней")
    message = events[index]["message"].lower()
    assert "сжима" not in message, (
        f"надпись «{events[index]['message']}» обещает сжатие, которое уже "
        "закончилось")


def test_the_cut_says_when_each_call_started(monkeypatch):
    """Разрез прогона называет НАЧАЛО вызова, а не только его длину.

    Пока стадии стояли в очередь, разрез читался сложением: сумма `elapsed_s`
    равна времени прогона — значит очередь. Теперь сумма больше времени прогона
    всегда, и сложение говорит лишь «что-то перекрылось». Пара
    `started_at` + `elapsed_s` даёт отрезок каждого вызова, и перекрытие видно
    поимённо — без этого следующий замер скорости мерить нечем.
    """
    from finengine.skills import extract_result

    events, _calls = _run(monkeypatch)
    calls = extract_result.run_stages(_done(events)["_diag"])["calls"]
    by_stage = {call["stage"]: call for call in calls}
    main, segment = by_stage["main_compression"], by_stage["segment_compression"]
    assert main["started_at"] and segment["started_at"], (
        f"разрез не донёс время старта вызовов: {calls}")
    assert segment["started_at"] < main["started_at"] + main["elapsed_s"], (
        f"по разрезу перекрытия не видно: сегментное сжатие начато в "
        f"{segment['started_at']}, основное шло с {main['started_at']} "
        f"{main['elapsed_s']} с")


def test_the_segment_stage_is_not_fed_the_main_compression():
    """Независимость: результат основного сжатия стадии не передают.

    Разбор дерева, а не поиск по тексту: `compression_override` назван и в
    сигнатуре стадии, и в объяснительной записке этого файла — текстовый
    сторож ловил бы собственную цитату.
    """
    src = textwrap.dedent(inspect.getsource(ts._extract_stream_two_stage_full_context))
    tree = ast.parse(src)
    launches = [node for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_extract_segment_breakdown_two_stage"]
    assert len(launches) == 1, (
        f"вызовов сегментной стадии {len(launches)}, а был один — сторож устарел")
    passed = {kw.arg for kw in launches[0].keywords}
    assert not (passed & {"compression_override", "compression_request_override"}), (
        "сегментной стадии передают результат основного сжатия — тогда "
        "параллельный запуск неверен: она обязана дождаться основного")
