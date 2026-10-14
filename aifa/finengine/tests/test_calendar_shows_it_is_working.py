"""Двадцать минут работы не выглядят молчанием (25.08.2026).

ЖАЛОБА ВЛАДЕЛЬЦА: «собери календарь за 1 день — поставил, не увидел ответа».

Окно при этом доехало: в логе прода запросы к Finnhub шли
`from=2026-08-24&to=2026-08-26`, то есть ровно ±1 день — вчерашняя правка
работает. Молчал ЧАТ.

ПРИЧИНА. Прогресс тикал на событии `events`, а оно шлётся только `if merged` —
то есть лишь для компаний, у которых события НАШЛИСЬ. На узком окне их почти
ни у кого нет: чат получал «Сканирую 89 компаний…» и замолкал на двадцать
минут. Со стороны это неотличимо от зависшего агента.

🔴 И ГЛАВНОЕ, ЧТО СТОИТ СКАЗАТЬ ВСЛУХ: узкое окно скан НЕ УСКОРЯЕТ. Работа
пропорциональна числу КОМПАНИЙ (89 IR-страниц, каждую читает модель), а не
ширине окна. «За 1 день» сужает фильтр дат, а не объём обхода — и просьба,
звучащая как «быстро», идёт те же ~20 минут. Поэтому в сообщении о старте
теперь названо ожидаемое время.

Заодно из сообщений убраны зашитые «±14 дней»: окно задаётся вызовом или
сценарием владельца, и на просьбе «за 1 день» эта строка врала в трёх местах —
в прогрессе, в итоговом статусе и в карточке артефакта.
"""
from __future__ import annotations

import inspect
import re

from finengine.llm import tools as T
from finengine.skills import ir_calendar


def test_progress_ticks_for_every_company_not_only_for_hits():
    """Отрицательный контроль: убрал `company_done` из потока — краснеет.

    Именно привязка тика к найденным событиям делала узкое окно молчаливым.
    """
    src = inspect.getsource(ir_calendar.build_calendar_stream)
    i = src.index('"type": "company_done"')
    j = src.index("if merged:", i - 800 if i > 800 else 0)
    assert i < j, (
        "отметка о компании шлётся ВНУТРИ `if merged` — на узком окне событий "
        "почти нет, и чат снова замолчит")


def test_the_chat_shows_the_walk_not_just_the_catch():
    """Вторая половина связки: поток шлёт — обработчик обязан показывать."""
    src = inspect.getsource(T._h_ir_calendar_run)
    assert '"company_done"' in src, "чат не слушает ход обхода"
    assert "обработано" in src, "ход обхода не превращается в сообщение"


def test_the_start_message_names_the_real_window():
    """Отрицательный контроль: вернул «окно ±14 дней» — краснеет.

    На просьбе «за 1 день» эта строка врала бы в первом же сообщении.
    """
    src = inspect.getsource(T._h_ir_calendar_run)
    i = src.index('if t == "started"')
    around = src[i:i + 700]
    assert "window_back" in around and "window_fwd" in around, (
        "стартовое сообщение снова печатает зашитое окно")


def test_the_start_message_warns_about_the_duration():
    """Узкое окно не ускоряет обход — пользователь должен узнать это ДО того,
    как двадцать минут покажутся поломкой."""
    src = inspect.getsource(T._h_ir_calendar_run)
    i = src.index('if t == "started"')
    assert "мин" in src[i:i + 700], "ожидаемое время не названо"


def test_no_hardcoded_window_in_user_facing_text():
    """Окно печаталось константой в трёх местах: прогресс, итоговый статус и
    карточка артефакта. Все три врут, как только окно задано."""
    # Смотрим на СТРОКОВЫЕ ЛИТЕРАЛЫ через AST, а не на текст функции:
    # «±14» осталось в комментариях, объясняющих саму эту правку, и текстовая
    # проверка краснела бы на собственном объяснении — охраняя слова вместо
    # поведения. Докстринг исключён по той же причине.
    import ast

    tree = ast.parse(inspect.getsource(T._h_ir_calendar_run).lstrip())
    fn = tree.body[0]
    doc = ast.get_docstring(fn, clean=False)
    said = [
        n.value for n in ast.walk(fn)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and n.value != doc
    ]
    guilty = [t for t in said if "±14" in t or "±14 дней" in t]
    assert not guilty, (
        f"в тексте для пользователя снова зашито окно: {guilty} — оно врёт на "
        "любой заданной ширине")


def test_the_date_never_wraps_in_the_table():
    """Отрицательный контроль: вернул обычный пробел — краснеет.

    Разметка таблицы верна, но колонку «Дата» браузер сжимает до минимума, и
    «24 авг» ломалось на две строки: «24» сверху, «авг» снизу. Строки таблицы
    становились вдвое выше. Выравнивание markdown ширину не задаёт, поэтому
    дату держим вместе неразрывным пробелом.
    """
    from datetime import date

    y = date.today().year
    for iso in (f"{y}-08-24", f"{y + 1}-03-15"):
        out = T._fmt_event_date(iso)
        assert " " not in out, (
            f"дата «{out}» содержит обычный пробел — колонка снова разорвёт её")
        assert "\u00a0" in out, f"дата «{out}» потеряла неразрывный пробел"


def test_the_year_appears_when_the_window_crosses_it():
    """Год опускался потому, что окно было ±14 дней и год во всех строках один.
    Теперь окно до 120 дней в каждую сторону — строки могут разойтись по годам,
    и «4 авг» станет двусмысленным."""
    from datetime import date

    this_year = date.today().year
    assert T._fmt_event_date(f"{this_year}-08-04") == "4\u00a0авг"
    other = T._fmt_event_date(f"{this_year + 1}-03-15")
    assert other.endswith(str(this_year + 1)), (
        f"дата из другого года без года: {other}")
    assert T._fmt_event_date("мусор") == "мусор"


def test_the_artifact_records_the_window_it_used():
    """Карточка артефакта — то, что владелец увидит в списке автоматизаций
    завтра. «±14 дней» там означало бы, что скан был другим."""
    src = inspect.getsource(T._h_ir_calendar_run)
    m = re.search(r"lookahead_days\":\s*([^,}]+)", src)
    assert m and "fwd_days" in m.group(1), (
        "в артефакт пишется зашитая ширина окна, а не использованная")
