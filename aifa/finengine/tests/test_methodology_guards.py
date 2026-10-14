"""Защиты методики: правку можно отбить, карточку — нельзя обнулить молча (#148).

ЗАЧЕМ. Методика стала правимой словами (#123), и вместе с этим открылись четыре
способа сломать разбор ТИХО — без ошибки, без уведомления, при безупречном на вид
отчёте. Текст правки при этом сочиняет модель из реплики в чате, ложится на диск и
действует во ВСЕ будущие анализы.

  1. Переписать правило, точные слова которого читает программа («норма»/
     «внимание»/«риск») → карточка становится сплошным «нет данных».
  2. Переименовать раздел → карточка теряет блок: она узнавала разделы по русским
     ЗАГОЛОВКАМ, а заголовок — обычный правимый текст.
  3. Переключить язык изложения → карточка пустеет целиком: словарь знал только
     русские слова.
  4. Сохранить две правки одновременно → одна пропадает БЕЗ ошибки.

Проверки идут через ПУБЛИЧНЫЙ путь (apply / parse_report / save_delta), а не через
внутренние помощники: ровно на этом сгорел #137 — тест ходил своей дорогой, был
зелёным, а на проде не работало.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

import pytest

from finengine.skills.analysis import methodology as md


# ───────────────────── дыра 1: неприкосновенные правила ─────────────────────


def test_protected_rule_cannot_be_replaced():
    with pytest.raises(md.MethodologyError) as e:
        md.apply([{"op": "replace", "id": "common.rule.flags",
                   "text": "ставь зелёный/жёлтый/красный"}])
    assert "нельзя" in str(e.value)


def test_protected_rule_cannot_be_disabled():
    with pytest.raises(md.MethodologyError):
        md.apply([{"op": "disable", "id": "common.rule.flags"}])


def test_refusal_explains_the_reason_and_offers_a_way():
    """Отказ без объяснения агент перескажет как «не получилось», и пользователь
    останется без правила И без понимания."""
    with pytest.raises(md.MethodologyError) as e:
        md.apply([{"op": "replace", "id": "common.rule.flags", "text": "иначе"}])
    msg = str(e.value)
    assert "карточка" in msg.lower(), "в отказе не сказано, ЧТО именно сломается"
    assert "правило" in msg.lower(), "в отказе не предложено, что делать вместо"


def test_protection_holds_at_run_time_too():
    """`strict=False` (прогон) прощает потерянный якорь, но НЕ снимает защиту:
    иначе правку было бы достаточно записать в обход, и на прогоне она бы легла."""
    m = md.apply([{"op": "replace", "id": "common.rule.flags", "text": "иначе"}],
                 strict=False)
    assert m.elements["common.rule.flags"].text == md.base().elements["common.rule.flags"].text
    assert any(n.get("kind") == "lost_anchor" for n in m.notices), (
        "отбитая правка обязана быть ВИДНА в уведомлениях, а не раствориться")


def test_user_element_can_never_be_born_protected():
    """Свой элемент не должен уметь стать неприкосновенным: иначе пользователь
    (или модель за него) заперла бы правило от самого себя."""
    m = md.apply([{"op": "add", "id": "user.rule.mine", "group": "comparability",
                   "title": "Моё", "text": "считать так", "protected": "нельзя"}])
    assert m.elements["user.rule.mine"].protected == ""


def test_ordinary_rules_stay_editable():
    """Защита — узкий класс, а не запрет на настройку: иначе #123 обесценен."""
    m = md.apply([{"op": "replace", "id": "bank.formula.nim",
                   "text": "NIM у нас по средним за квартал"}])
    assert "по средним за квартал" in m.elements["bank.formula.nim"].text


# ─────────────── дыра 2: карточка по ярлыку, а не по заголовку ───────────────


def test_every_card_block_has_a_section_that_feeds_it():
    """Ярлык без раздела = блок карточки, который никогда не заполнится."""
    roles = {el.role for el in md.base().elements.values() if el.role}
    assert roles == set(md.CARD_ROLE_TITLES), (
        f"ярлыки разделов и словарь блоков разошлись: {roles ^ set(md.CARD_ROLE_TITLES)}")


@pytest.mark.parametrize("ct", ["bank", "non_financial"])
@pytest.mark.parametrize("depth", ["quick", "full"])
def test_each_framework_feeds_the_card(ct, depth):
    """В КАЖДОМ каркасе должны быть разделы под вердикт, итог и выводы — иначе
    карточка этого каркаса частично пуста by design, и никто об этом не знает."""
    m = md.base()
    roles = {m.elements[eid].role for eid in m.framework[(ct, depth)]}
    assert {"verdict", "summary", "conclusions"} <= roles, (
        f"{ct}/{depth}: карточке нечем заполнить блоки {{'verdict','summary','conclusions'}} - {roles}")


def test_disabling_a_card_section_is_allowed_but_visible():
    """Выключить раздел — право пользователя. Молча потерять блок — нет:
    ярлык роли позволяет НАЗВАТЬ последствие, а не обнаружить его глазами."""
    m = md.apply([{"op": "disable", "id": "common.sec.conclusions"}])
    assert "common.sec.conclusions" not in m.framework[("bank", "quick")]
    assert md.card_role_of(m, "common.sec.conclusions") == "conclusions"
    assert md.CARD_ROLE_TITLES["conclusions"] == "Выводы"


# ─────────────── дыра 3: язык изложения не обнуляет карточку ───────────────


def test_concurrent_saves_do_not_lose_a_delta(tmp_path, monkeypatch):
    """Две правки одновременно — обе обязаны сохраниться.

    Пропажа здесь не даёт ни ошибки, ни следа: пользователь видит «записал» и
    считает, что методика поменялась. Воркеры уже параллельны, так что случай
    не гипотетический.
    """
    from finengine.config import settings
    from finengine.skills.analysis import personal as p

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    ids = ["bank.formula.nim", "bank.formula.cor", "bank.formula.cir",
           "bank.formula.roe", "nf.rule.liquidity", "nf.rule.cash_quality"]

    with ThreadPoolExecutor(max_workers=len(ids)) as pool:
        list(pool.map(
            lambda eid: p.save_delta(7, {"op": "replace", "id": eid, "text": f"по-моему {eid}"}),
            ids))

    saved = {str(d.get("id")) for d in p.load(7)}
    assert saved == set(ids), f"правки потерялись молча: {set(ids) - saved}"


def test_protected_rule_is_refused_before_it_touches_the_disk(tmp_path, monkeypatch):
    """Проверка идёт ДО записи: сломанная правка не должна лечь на диск и
    отравить все последующие анализы."""
    from finengine.config import settings
    from finengine.skills.analysis import personal as p

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    with pytest.raises(md.MethodologyError):
        p.save_delta(9, {"op": "replace", "id": "common.rule.flags", "text": "зелёный"})
    assert p.load(9) == []


# ─────────── побочно: имена видов анализа берутся из реестра ───────────


def test_operating_procedure_carries_no_route():
    """Отрицательный контроль к #237: карта «намерение → инструмент» не вернулась.

    Инструкция хода не имеет права называть инструменты по имени в связке с
    поводом их взять — иначе описания навыков снова перестают решать (#199).
    Исключение — `ask_user`: это не маршрут работы, а политика переспроса.
    """
    from finengine.llm.openclaw import _OPERATING_PROCEDURE

    named = set(re.findall(r"\b((?:fe|ir|kb)_[a-z_]+|daily_news_digest|"
                           r"factor_analysis|tavily_search)\b", _OPERATING_PROCEDURE))
    assert not named, f"в инструкцию хода вернулась маршрутизация: {sorted(named)}"


# ─────── определения коэффициентов ≠ обещание того, чего в таблице нет ───────
# #151, решение владельца 02.08.2026. Банковский блок объявляет четыре формулы
# словами «по этой базе они посчитаны кодом в таблице заземления». У индийской
# банковской ячейки Cost/Income, NIM и стоимости риска в реестре НЕТ — по такому
# отчёту фраза была прямой неправдой: модель получала определение величины,
# которой в таблице не будет, и рядом запрет считать самой.


def test_definitions_of_missing_coefficients_are_dropped():
    """Сверяем по ТЕКСТАМ элементов, а не по словам: «прокси-NIM» законно стоит
    в правиле дисциплины «нет данных», и поиск подстроки принял бы правило за
    определение."""
    m = md.base()
    have = {"ROE"}  # индийская ячейка: из четырёх запиненных есть только ROE
    block = md.analytical_block(m, "bank", have)
    assert m.elements["bank.formula.roe"].text[:40] in block, "ROE выкинут зря"
    for eid in ("bank.formula.nim", "bank.formula.cor", "bank.formula.cir"):
        assert m.elements[eid].text[:40] not in block, (
            f"определение {eid} осталось, хотя считать его нечем")


def test_rules_are_never_dropped_only_definitions():
    """Фильтр смотрит на `registry_enum` — связь текста с показателем реестра.
    У правил её нет, и они обязаны остаться при любом наборе."""
    m = md.base()
    block = md.analytical_block(m, "bank", set())
    for rule_id in ("bank.rule.no_data_discipline", "bank.rule.capital",
                    "bank.rule.wording"):
        head = m.elements[rule_id].text[:40]
        assert head in block, f"правило {rule_id} выкинуто вместе с формулами"


def test_full_registry_keeps_everything():
    """Русская банковская ячейка считает все четыре — блок не должен худеть."""
    m = md.base()
    have = {m.elements[e].registry_enum for e in m.analytical["bank"]
            if m.elements[e].registry_enum}
    assert md.analytical_block(m, "bank", have) == md.analytical_block(m, "bank")


def test_no_filter_means_no_change():
    """Не передали набор — собираем как раньше. Иначе любой путь, который ещё
    не научился считать доступное, молча потерял бы половину методики."""
    m = md.base()
    for ct in ("bank", "non_financial"):
        assert md.analytical_block(m, ct, None) == md.analytical_block(m, ct)


def test_the_analysis_path_feeds_the_filter():
    """Проверка ПУТИ: набор посчитанного должен собираться в run.py и доезжать
    до сборки промпта, иначе фильтр останется красивой функцией без вызова."""
    import inspect  # noqa: PLC0415

    from finengine.skills.analysis import run as _run  # noqa: PLC0415

    src = inspect.getsource(_run.run)
    assert "available_enums" in src, "анализ не собирает набор посчитанного"
    assert "available_enums=" in src, "набор не доезжает до build_prompt"


# ───────── числа расчёта берутся у инструментов, а не из текста отчёта ────────
# 🔴 ЖИВОЙ ПРОГОН 23.08.2026, `severstal-Q1-2025.pdf`, нонс 1a5d94b5.
#
# Модели не хватило совокупных активов для ROCE — она сама не включила их в
# `--metrics`. Вместо «такого показателя в извлечении нет» она написала
# пользователю: «значение взято из баланса отчёта (стр. 7: 536 335 + 331 529)»,
# то есть СЛОЖИЛА два числа из PDF и подставила сумму в расчёт.
#
# Инструкция хода этого не запрещала, и по делу: «не выдумывай числа» — про
# выдумку, а тут не выдумка; «бери из инструментов И КОНТЕКСТА» — прямое
# разрешение взять из текста отчёта.
#
# ⚠️ ЗАПРЕТ УЗКИЙ, И ЭТО НАМЕРЕННО. Читать отчёт и отвечать по нему — законная
# работа, она прямо записана в границах навыка извлечения («что написано на
# странице 42» → отвечай сам). Запрещено ровно одно: подставить добытое вручную
# число в РАСЧЁТ или ТАБЛИЦУ вместо непришедшего показателя — там оно
# неотличимо от извлечённого, ничем не подтверждено и никем не проверено.
#
# ПРОВЕРКА ОТКАТОМ (23.08.2026): убрать абзац из `_OPERATING_PROCEDURE` →
# падает `test_hand_picked_numbers_are_banned_from_calculations`; оставить
# только запрет, сняв оговорку про прямой вопрос → падает
# `test_reading_the_report_for_a_direct_question_stays_allowed`.


def _turn_tail() -> str:
    """Хвост инструкции хода — тот, что дописывается к каждому вопросу."""
    import inspect

    from finengine.orchestrator import agent as _agent

    src = inspect.getsource(_agent)
    start = src.index('"Отвечай на основе контекста')
    return src[start:src.index("\n        )", start)]


def test_ordinary_arithmetic_by_bash_is_still_allowed():
    """Запрет по ПРЕДМЕТУ, а не по инструменту.

    Убери упоминание Bash вовсе — и модель перестанет считать дни между датами
    и доли в разговоре, то есть запрет накроет то, чего дефект не касался.
    """
    tail = _turn_tail()
    # «Bash» — имя инструмента, то есть контракт; формулировка «прочей
    # арифметики» отсюда убрана как пришпиливание текста.
    assert "Bash" in tail, (
        "из инструкции хода пропало упоминание Bash — запрет считать в уме "
        "накроет и обычную арифметику, которой дефект не касался")

# ── УБРАНЫ ТРИ ТЕСТА (решение владельца 25.08.2026) ─────────────────────────
#
# test_hand_picked_numbers_are_banned_from_calculations
# test_reading_the_report_for_a_direct_question_stays_allowed
# test_ratios_are_computed_by_the_skill_not_by_the_model
#
# Все три искали ПОДСТРОКИ в тексте промпта — в `_OPERATING_PROCEDURE` и в
# хвосте инструкции хода: «РАСЧЁТ», «ТАБЛИЦА», «нет в извлечении», «НЕЛЬЗЯ»,
# «странице 42», «навык расчёта, а не ты», «свою формулу».
#
# Довод владельца: «мы будем менять промпты ещё тысячу раз, каждый раз
# переписывать сторожей — бред». Такой тест краснеет не когда правило исчезло,
# а когда его сказали другими словами, — то есть берёт налог за каждую правку
# формулировки и не ловит настоящую пропажу (её тоже можно сделать, оставив
# слова на месте).
#
# ПРАВИЛА ИЗ ПРОМПТА НЕ УДАЛЕНЫ. Снят текстовый сторож над ними: сломать их
# синонимом теперь нельзя, а выкинуть намеренно — это правка промпта, видная
# в ревью.
#
# В этом файле ОСТАЛИСЬ проверки, которые смотрят на ВЫВОД КОДА — сообщения
# об ошибках (`str(e.value)`, `msg`) и разобранные элементы методики
# (`m.elements[...]`). Они от формулировок промпта не зависят.
