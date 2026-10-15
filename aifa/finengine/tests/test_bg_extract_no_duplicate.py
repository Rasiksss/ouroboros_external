"""Фоновое извлечение доставлено — второго прогона движка НЕ будет (#160).

ЗАМЕР, ради которого это написано (прогон Мечела 02.08, цифры настоящие):
почтовый ящик воркера знал в 18:17:19,836 и про 94 показателя, и про
сохранённый Excel на 15 802 байта. Главный агент этого НЕ узнал и в 18:20:59
запустил движок ЗАНОВО — 253,5 секунды работы, те же 94 показателя, и на выходе
ни файла, ни карточки. Около 30% всего прогона (863 с) впустую.

Причина была не в формулировке промпта: сводка уезжает в историю сессии, а
история попадает в промпт только СЛЕДУЮЩИМ ходом — идущий ход выводил статус
фонового прогона из МОЛЧАНИЯ, а молчание неотличимо от «не получилось».

ЧЕМ ХОДИМ. Тем же путём, что пользователь: сначала боевой почтовый ящик
`_bg_extract_mailbox` съедает настоящие события релея станции, потом боевой
`tools.execute("fe_extract_metrics", ...)` — тот самый вызов, который делает
модель. Подменены только внешние опоры (документ, OCR-текст, классификатор) и
сам движок — он здесь ШПИОН: тест проверяет ровно то, звали его или нет.

ОБЕ СТОРОНЫ ИНВАРИАНТА проверяются здесь же: молчание не читается как
«получилось» (нет фонового прогона или он упал — извлекаем как раньше), и
«получилось» не предполагается по умолчанию.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from finengine.api import station_progress_ingest as ingest
from finengine.config import settings
from finengine.llm import tools as t
from finengine.orchestrator import agent as ag
from finengine.skills import grounding_bus as bus

USER = 7
DOC = "doc-mechel"
NONCE = "n0deadbeef"
SRC = Path(__file__).resolve().parents[1] / "src" / "finengine"


@pytest.mark.xfail(
    strict=True,
    reason="ждёт #185 (docs/PLAN_190_ROOM_OWNER.md:430, контроль 8): у "
           "grounding_bus.run_status НЕТ НИ ОДНОГО читателя — сторож дубля "
           "стал односторонним, второй полный прогон по тому же документу "
           "не блокируется ничем",
)
def test_the_guard_has_a_reader_at_all(движок_ast):
    """Самый дешёвый сторож самого дорогого дефекта этого файла.

    ЧТО СЛУЧИЛОСЬ. Пишущая сторона сторожа дубля (#160) жива и зелена:
    `station_run_sink` ставит `run_started`/`run_finished`/`run_failed`. Читающая
    исчезла вместе с хендлером тула (aa13cb1) — и `run_status` не зовёт больше
    НИКТО. Сторож пишет в блокнот, который никто не открывает.

    `station_guard.refuse_if_duplicate` это НЕ закрывает: он про копии внутри
    ОДНОЙ комнаты (`/proc` + файл-замок), а не про «результат уже лежит на
    бэкенде».

    ЦЕНА, ИЗМЕРЕННАЯ НА ПРОГОНЕ МЕЧЕЛА 02.08: 253,5 секунды впустую, около 30%
    всего хода. Дефект известен и записан: `docs/PLAN_190_ROOM_OWNER.md:430`,
    контроль 8 — «сегодня не блокируется вовсе».

    ⚠️ ПЛАН ОПИСЫВАЕТ ДЕФЕКТ ЗЕРКАЛЬНО, и это стоит знать, читая его: там
    сказано «ПИСАТЕЛЕЙ у сторожа в гейтвей-режиме ноль». Сегодня наоборот —
    писатели живы (`station_run_sink.py:214/351/391`), а читателей ноль
    (проверено разбором в дерево по всему `src/`). Сторона поменялась,
    результат тот же: сторож не срабатывает.

    ПОЧЕМУ ОТДЕЛЬНЫМ ТЕСТОМ, А НЕ ПЕРЕСЕЛЕНИЕМ ВОСЬМИ СОСЕДЕЙ. Соседи проверяют
    ПОВЕДЕНИЕ отказа (что именно сказано, чей прогон, когда забыт) — их можно
    переселить только на готового читателя. Этот проверяет, что читатель вообще
    существует, и обязан покраснеть ПЕРВЫМ, как только его заведут.
    """
    # Комментарии режем разбором в дерево: единственное сегодняшнее упоминание
    # `run_status` вне самого определения — комментарий grounding_bus.py:61,
    # и сторож, читающий сырой текст, зеленел бы от него.
    # движок_ast (conftest.py) несёт и station-skills/ — фильтруем на SRC,
    # как раньше делал собственный SRC.rglob() этого теста.
    readers: list[str] = []
    for path, tree in sorted(движок_ast.items()):
        if SRC not in path.parents or path.name == "grounding_bus.py":
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "run_status":
                readers.append(str(path.relative_to(SRC)))
            elif isinstance(node, ast.Name) and node.id == "run_status":
                readers.append(str(path.relative_to(SRC)))

    assert readers, (
        "grounding_bus.run_status не зовёт никто: сторож дубля извлечения "
        "пишет состояние прогона, но проверять его некому")


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    """Чистая шина на каждый тест + журналы тула в tmp, а не в рабочие данные."""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "docs_dir", tmp_path)
    bus._runs.clear()
    bus._worker_docs.clear()
    yield
    bus._runs.clear()
    bus._worker_docs.clear()


class _Index:
    def get_document(self, doc_id):  # noqa: D102, ANN001
        return None


def _arm_tool_supports(monkeypatch) -> None:
    """Внешние опоры тула: документ, OCR-текст, классификатор ячейки."""
    doc = SimpleNamespace(doc_id=DOC, filename="mechel.pdf", owner_id=USER,
                          company_name="Мечел")
    monkeypatch.setattr(t, "_fe_resolve_owned_doc", lambda _id, _ctx: (doc, _Index(), None))

    async def _text(*_a, **_kw):
        return "===PAGE 1===\nОтчёт о финансовом положении\nИтого активы 1 000\n"

    monkeypatch.setattr(t, "_fe_doc_text_wait", _text)
    monkeypatch.setattr(
        "finengine.skills.extractors._classifier.detect_profile",
        lambda _text: SimpleNamespace(company_type="non_financial", standard="ifrs",
                                      language="ru", profile_id="ifrs_nf_ru"),
    )


def _worker_takes_the_text(doc_id: str = DOC, scope: list[str] | None = None) -> None:
    """Воркер забирает OCR-текст в свою комнату — боевой `/internal/doc-text`.

    Это единственный момент, когда бэк узнаёт пару (нонс, doc_id). Ходим сюда
    настоящей ручкой, а не подставляем связь руками: иначе тест доказывал бы
    работу связи, которой на живом пути может и не быть.
    """
    from finengine.api import internal_docs as idocs

    idocs.register_nonce_docs(NONCE, scope or [doc_id])
    (settings.docs_dir / f"{doc_id}.txt").write_text("===PAGE 1===\nтекст\n",
                                                     encoding="utf-8")
    asyncio.run(idocs.doc_text(doc_id, nonce=NONCE, authorization=""))


def test_silent_worker_is_recorded_as_failure_not_as_success(monkeypatch):
    """Воркер подал признак жизни и затих — «не получилось», а не «готово».

    Именно этот случай ломает наивную починку «раз ящик взведён, значит будет
    результат»: агент ждал бы вечно того, чего не будет.
    """
    _arm_tool_supports(monkeypatch)

    _worker_takes_the_text()
    session = ag.Session()
    session.owner_id = USER
    q = ingest.register(NONCE)
    q.put_nowait({"type": "start", "company_name": "Мечел"})
    asyncio.run(ag._bg_extract_mailbox(session, NONCE, q, timeout=0.2))

    assert (bus.run_status(USER, DOC) or {}).get("state") == "failed"


def test_result_is_bound_to_the_document_the_worker_actually_took():
    """Привязка «нонс → документ» берётся из ФАКТА выдачи текста, не из догадки.

    В ходу с несколькими документами догадка связала бы результат с ЧУЖИМ
    отчётом, и агент отчитался бы числами другой компании — молча и уверенно.
    """
    _worker_takes_the_text()

    assert bus.worker_doc(NONCE) == DOC
    assert bus.worker_doc("другой-нонс") == "", "чужой нонс не должен ни с чем связываться"


def test_a_sequence_of_bindings_follows_the_last_one_and_says_so(caplog):
    """Два документа ОДНОГО хода подряд: связь идёт за ПОСЛЕДНИМ, но громко.

    ДЕФЕКТ, НАЗВАННЫЙ РАЗБОРОМ 19.08.2026 (Б8.3): `bind_worker_doc` писал в
    `_worker_docs[nonce]` безусловно, и переезд нонса не видел никто.

    ПОЧЕМУ ОТКАЗ БЫЛ БЫ ХУЖЕ МОЛЧАНИЯ (правка 20.08.2026). Первая редакция
    отклоняла вторую привязку — «первая побеждает». Но все три читателя берут
    связь ЛЕНИВО, в момент своего события (`station_turn._start_body`,
    `station_run_sink.drain`, сторож дубля), поэтому в ПОСЛЕДОВАТЕЛЬНОМ ходе
    отказ приписывал прогон второго документа первому: шапка таблицы и живой
    прогресс показывали бы A, пока считается B. Тест потому и ходит
    ПОСЛЕДОВАТЕЛЬНОСТЬЮ, а не одной парой: на паре обе семантики выглядят
    одинаково правдоподобно, и именно так дефект и проехал.

    ПРОВЕРКА СТОРОЖА ОТКАТОМ: вернуть в `grounding_bus.bind_worker_doc` `return`
    после предупреждения («первая побеждает») — тест краснеет на том, что после
    перехода на второй документ читатель по-прежнему называет первый; убрать
    `log.warning` — краснеет на молчаливом переезде.
    """
    # Только ASCII: `internal_docs._DOC_ID_RE` отбивает прочее 400-й, и тест
    # мерил бы форму имени вместо направления привязки.
    second, third = "doc-second", "doc-third"

    with caplog.at_level("WARNING", logger="finengine.skills.grounding_bus"):
        _worker_takes_the_text(DOC, scope=[DOC, second, third])
        assert bus.worker_doc(NONCE) == DOC, "первый прогон не связался со своим документом"

        _worker_takes_the_text(second, scope=[DOC, second, third])
        assert bus.worker_doc(NONCE) == second, (
            "второй прогон хода читается как первый документ — шапка таблицы и "
            "живой прогресс назовут чужой отчёт")

        _worker_takes_the_text(third, scope=[DOC, second, third])
        assert bus.worker_doc(NONCE) == third, "третий прогон хода отдан предыдущему документу"

    said = [r.getMessage() for r in caplog.records]
    assert len(said) == 2, (
        f"переездов было два, а предупреждений {len(said)} — переезд прошёл молча")
    assert DOC in said[0] and second in said[0], (
        "в первом предупреждении нет ОБОИХ документов — по журналу не разобрать, "
        "откуда куда уехал нонс")
    assert second in said[1] and third in said[1]


def test_rebinding_a_nonce_to_the_same_document_stays_quiet(caplog):
    """Повтор той же пары — не событие: комната вправе забрать текст дважды.

    Без этого сторож ругался бы на штатный ретрай выдачи текста, и предупреждение
    про НАСТОЯЩУЮ перепривязку утонуло бы в шуме.
    """
    _worker_takes_the_text()

    with caplog.at_level("WARNING", logger="finengine.skills.grounding_bus"):
        bus.bind_worker_doc(NONCE, DOC)

    assert bus.worker_doc(NONCE) == DOC
    assert not caplog.records, "штатный повтор выдачи текста поднял тревогу"


def test_unbound_nonce_records_nothing(monkeypatch):
    """Нонс без привязки — статус НЕ пишем: лучше дубль, чем чужие числа.

    Ящиков взводится пул (до 4 на ход), а воркеров может не быть ни одного.
    """
    _arm_tool_supports(monkeypatch)

    session = ag.Session()
    session.owner_id = USER
    q = ingest.register("нонс-без-воркера")
    q.put_nowait({"type": "done", "result": {"company_name": "Мечел", "metrics_found": 94}})
    asyncio.run(ag._bg_extract_mailbox(session, "нонс-без-воркера", q, timeout=5.0))

    assert bus.run_status(USER, DOC) is None
    assert not bus._runs, "статус записан по документу-догадке"


async def _false_intent(_message):
    """Ход без связки «извлечение + анализ» — диспетчер (#116) не вмешивается."""
    return False, False


async def _empty_turn(**_kw):
    """Модель ничего не сделала: ход нужен только ради его НАЧАЛА."""
    yield {"type": "done", "usage": {}}


def test_the_watchman_remembers_longer_than_the_process_it_watches():
    """Инвариант одной строкой: память сторожа СТРОГО длиннее жизни воркера.

    Числа заданы врозь в двух файлах и однажды уже разъехались. Тест держит
    смысл, а не значения: поднимут потолок жизни воркера — покраснеет здесь,
    а не в проде через 253 потерянные секунды.
    """
    assert bus._MEMORY_TTL_SEC > bus.WORKER_LIFETIME_SEC
    assert ag._WORKER_LIFETIME_SEC == bus.WORKER_LIFETIME_SEC, (
        "оркестратор и сторож считают жизнь воркера по-разному")


def test_a_live_background_run_is_never_forgotten_by_someone_elses_turn(monkeypatch):
    """Противовес: чистка «по владельцу» не смеет снимать ИДУЩИЙ прогон.
    Живой воркер — ровно то, ради чего сторож и заведён; сняв его, мы вернули
    бы дубль на 253 секунды, но уже другим путём."""
    _arm_tool_supports(monkeypatch)
    bus.run_started(USER, DOC, source="worker")

    bus.run_forget_finished(USER)

    st = bus.run_status(USER, DOC)
    assert st and st["state"] == "running", "живой прогон забыт — дубль вернётся"
