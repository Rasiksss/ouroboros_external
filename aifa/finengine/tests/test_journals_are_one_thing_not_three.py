"""Журналы: три вида одной формы, одно поведение на обе реализации.

🔴 ЗАЧЕМ. Следы хода, аудит тулов и расход модели писались тремя модулями, и
каждый заводил своё: свой каталог, свой `mkdir`, свой разбор строки, свою
обработку битой. Форма при этом у всех одна — дописать строку JSON в
`<вид>/<владелец>/<ключ>.jsonl` и прочитать файл целиком. Это тот самый класс
«одно решение в трёх местах», которым мы платим весь месяц.

Правило `docs/rules/architecture.md` 3.1 (MUST) запрещает локальные лог-файлы,
3.2 требует, чтобы запрос не зависел от реплики: на двух репликах лента одного
хода разъезжается, и `/admin/trace` показывает половину.

Проверки идут на ОБЕИХ реализациях одним набором — расхождение здесь дефект.
"""
from __future__ import annotations

import pytest

from finengine.store import journals as J


@pytest.fixture(params=["файлы", "база"])
def хранилище(request, tmp_path, monkeypatch):
    from finengine.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    if request.param == "файлы":
        return J.LocalJournalStore()
    # Базу спрашиваем ЗДЕСЬ, а не в сигнатуре: вариант «файлы» её не
    # требует, и общий запрос пропускал бы файловую ветку без причины.
    return J.SqlJournalStore(request.getfixturevalue('бд'))


def test_entries_come_back_in_the_order_they_were_written(хранилище):
    """🔴 ЛЕНТА ИДЁТ В ПОРЯДКЕ ПОЯВЛЕНИЯ, А НЕ ПО ЧАСАМ. Мутация: сортировать
    по `ts` вместо `id` — тест краснеет.

    Время у записей ниже идёт ВСПЯТЬ, и это не выдумка: `ts` берётся из
    `time.time()`, а системные часы двигает NTP. Лента, перемешавшая «вызвал
    тул» и «получил ответ», хуже отсутствующей — по ней делают выводы о
    причинах.

    ⚠️ ПЕРВАЯ РЕДАКЦИЯ ЭТОГО ТЕСТА НИЧЕГО НЕ ПРОВЕРЯЛА. В ней у трёх записей
    было ОДИНАКОВОЕ `ts`, и мутация «сортировать по ts» её не покраснила:
    SQLite на равных ключах отдаёт строки в порядке rowid, то есть случайно
    правильно. Ловушка ровно та, ради которой мы гоняем отрицательный
    контроль, — сторож выглядел зелёным и не различал ничего.
    """
    for время, имя in ((300.0, "начало"), (200.0, "вызов"), (100.0, "ответ")):
        хранилище.append(J.TRACES, "7", "сессия-1", {"ts": время, "event": имя})

    assert [з["event"] for з in хранилище.read(J.TRACES, "7", "сессия-1")] == [
        "начало", "вызов", "ответ"]


def test_kinds_do_not_leak_into_each_other(хранилище):
    """Три вида в одной таблице — и не смешиваются."""
    хранилище.append(J.TRACES, "7", "к", {"ts": 1.0, "что": "след"})
    хранилище.append(J.AUDIT, "7", "к", {"ts": 1.0, "что": "аудит"})
    хранилище.append(J.LLM_TRACES, "7", "к", {"ts": 1.0, "что": "расход"})

    assert [з["что"] for з in хранилище.read(J.TRACES, "7", "к")] == ["след"]
    assert [з["что"] for з in хранилище.read(J.AUDIT, "7", "к")] == ["аудит"]
    assert [з["что"] for з in хранилище.read(J.LLM_TRACES, "7", "к")] == ["расход"]


def test_another_user_does_not_see_the_journal(хранилище):
    """Расход и следы — тоже чужая информация."""
    хранилище.append(J.LLM_TRACES, "7", "проект", {"ts": 1.0, "cost_usd": 3.0})

    assert хранилище.read(J.LLM_TRACES, "8", "проект") == []
    assert хранилище.keys(J.LLM_TRACES, "8") == []


def test_keys_lists_every_project_of_the_owner(хранилище):
    """Обход расхода по всем проектам опирается на этот перечень."""
    хранилище.append(J.LLM_TRACES, "7", "проект-1", {"ts": 1.0})
    хранилище.append(J.LLM_TRACES, "7", "проект-1", {"ts": 2.0})
    хранилище.append(J.LLM_TRACES, "7", "проект-2", {"ts": 3.0})

    assert хранилище.keys(J.LLM_TRACES, "7") == ["проект-1", "проект-2"]


def test_an_empty_journal_is_a_list_not_a_crash(хранилище):
    assert хранилище.read(J.AUDIT, "7", "нет-такого") == []
    assert хранилище.keys(J.AUDIT, "7") == []


def test_an_unserializable_payload_does_not_kill_the_entry(хранилище):
    """В след кладут что угодно — журнал не имеет права падать на этом.

    Прежняя файловая ветка звала `json.dumps(..., default=str)` именно за этим.
    Мутация: убрать `default=str` в ветке базы — тест краснеет.
    """
    class Своё:
        def __repr__(self): return "<своё>"

    хранилище.append(J.TRACES, "7", "с", {"ts": 1.0, "объект": Своё()})

    assert "своё" in хранилище.read(J.TRACES, "7", "с")[0]["объект"]


def test_a_broken_line_does_not_hide_the_rest(tmp_path, monkeypatch):
    """Файл журнала мог оборваться на перезапуске — лента из-за этого не пропадает."""
    from finengine.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    файл = tmp_path / J.TRACES / "7" / "с.jsonl"
    файл.parent.mkdir(parents=True)
    файл.write_text('{"ts": 1.0, "n": 1}\n{оборвал\n{"ts": 2.0, "n": 2}\n', encoding="utf-8")

    assert [з["n"] for з in J.LocalJournalStore().read(J.TRACES, "7", "с")] == [1, 2]


def test_a_failing_journal_never_kills_the_turn(tmp_path, monkeypatch, caplog):
    """🔴 ЖУРНАЛ НЕ ИМЕЕТ ПРАВА УРОНИТЬ ХОД, но и молчать об отказе не должен.

    Мутация: убрать перехват в `journals.append` — тест краснеет с тем самым
    исключением, которое иначе прилетело бы пользователю вместо ответа.
    """
    class Сломанное:
        def append(self, *a, **k): raise RuntimeError("база недоступна")
        def read(self, *a, **k): raise RuntimeError("база недоступна")
        def keys(self, *a, **k): raise RuntimeError("база недоступна")

    monkeypatch.setattr(J, "_cached", ("подмена", Сломанное()))
    monkeypatch.setattr(J, "get_journal_store", lambda: Сломанное())

    J.append(J.TRACES, owner=7, key="с", entry={"ts": 1.0})
    assert J.read(J.TRACES, owner=7, key="с") == []
    assert J.keys(J.TRACES, owner=7) == []
    assert "база недоступна" in caplog.text


def test_the_segment_is_built_the_same_way_for_both():
    """Ключ собирается одной функцией — иначе после переезда «следов нет»."""
    assert J.safe_segment("7") == "7"
    assert "/" not in J.safe_segment("../../etc")


def test_the_switch_defaults_to_files(бд, tmp_path, monkeypatch):
    from finengine.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "journals_database_url", "")
    J._cached = None
    assert isinstance(J.get_journal_store(), J.LocalJournalStore)

    monkeypatch.setattr(settings, "journals_database_url", бд)
    J._cached = None
    assert isinstance(J.get_journal_store(), J.SqlJournalStore)
    J._cached = None


def test_the_migration_does_not_duplicate_on_a_second_run(бд, tmp_path):
    """Повторный запуск переезда не удваивает ленту."""
    from finengine.store.migrate_journals import migrate

    файл = tmp_path / J.TRACES / "7" / "с.jsonl"
    файл.parent.mkdir(parents=True)
    файл.write_text('{"ts": 1.0, "n": 1}\n{оборвал\n{"ts": 2.0, "n": 2}\n', encoding="utf-8")

    цель = J.SqlJournalStore(бд)
    отчёт = migrate(tmp_path, цель)

    assert отчёт == {"files": 1, "lines": 3, "migrated": 2,
                     "skipped_files": 0, "bad_lines": 1}
    assert [з["n"] for з in цель.read(J.TRACES, "7", "с")] == [1, 2]

    повтор = migrate(tmp_path, цель)
    assert повтор["skipped_files"] == 1 and повтор["migrated"] == 0
    assert len(цель.read(J.TRACES, "7", "с")) == 2


# ───────── отметка времени не роняет запись ─────────


@pytest.mark.parametrize("отметка,ждём", [
    (1787739438.5, 1787739438.5),
    (1787739438, 1787739438.0),
    ("1787739438.5", 1787739438.5),
    ("2026-08-15T12:46:58", None),   # ISO — разбирается, значение не важно
    (None, 0.0),
    ("", 0.0),
    ("мусор", 0.0),
])
def test_any_timestamp_is_accepted(хранилище, отметка, ждём):
    """🔴 НЕРАЗОБРАННАЯ ОТМЕТКА — НЕ ПОВОД ТЕРЯТЬ ЗАПИСЬ.

    Здесь стояло `float(ts or 0.0)`. У журналов прогонов `ts` — строка ISO
    (`"2026-08-15T12:46:58"`), а не число epoch: так их писали с самого начала.
    `float()` на такой строке бросает, запись не ложится, а сторож уровнем выше
    проглатывает отказ — как и должен, журнал не вправе уронить ход. Итог:
    тишина и потерянные записи.

    Поймано переездом: из четырёх журналов доехал ОДИН (сырьё станции, у него
    `ts` числом), а 411 записей извлечений, чата и разборов не легли НИ ОДНОЙ.
    Живые записи терялись бы так же.

    Мутация: вернуть `float(...)` — тест краснеет на ISO и на «мусоре».
    """
    хранилище.append(J.AUDIT, "7", "к", {"ts": отметка, "что": "запись"})

    записи = хранилище.read(J.AUDIT, "7", "к")
    assert len(записи) == 1, f"запись с отметкой {отметка!r} потеряна"
    assert записи[0]["что"] == "запись"


def test_records_keep_their_order_even_with_unparsable_timestamps(хранилище):
    """И порядок не рушится: он держится номером записи, а не временем."""
    for i, ts in enumerate(("мусор", "2026-08-15T12:46:58", 1.0, None)):
        хранилище.append(J.AUDIT, "7", "порядок", {"ts": ts, "n": i})

    assert [з["n"] for з in хранилище.read(J.AUDIT, "7", "порядок")] == [0, 1, 2, 3]
