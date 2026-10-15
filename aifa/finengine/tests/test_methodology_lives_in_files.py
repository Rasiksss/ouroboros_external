"""База методики — ФАЙЛЫ, и правка файла видна без перезапуска.

ЗАЧЕМ ЭТОТ ФАЙЛ. Требование владельца: методику правит методолог, а не
разработчик. Пока тексты лежали списком `Element(...)` в питоне, правка
означала выпуск версии. С 09.08.2026 они в
`station-skills/aifa-analyze/method/**.md`, а модуль их читает.

Эталоны `tests/golden/methodology/` доказывают, что переезд ничего не изменил
в промпте. Здесь доказывается то, ради чего он затевался: файл РАБОТАЕТ как
источник, и правка доезжает.

ОТДЕЛЬНО ПРО КЭШ. У нас уже был шрам (#155): полка станции кэшировалась «один
раз за жизнь процесса», правку доставляли, а прогон считал по старому — и
паспорт при этом называл новый отпечаток. Поэтому кэш здесь ключуется временем
правки файлов, и это проверяется, а не подразумевается.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: убрать отпечаток из ключа кэша (`_fingerprint`) —
`test_edit_is_visible_without_restart` обязан покраснеть.
"""
from __future__ import annotations

import os
import shutil

import pytest

from finengine.skills.analysis import framework as F
from finengine.skills.analysis import methodology as md

OCR = "===PAGE 1===\nОтчет о прибылях и убытках\nВыручка 100\n"


def _prompt(**kw) -> str:
    # Таблица заземления обязательна (#238): промпта без неё больше не бывает.
    return F.build_prompt(company_name="Тест", company_type="non_financial",
                          standard="ifrs", depth="full", ocr_text=OCR,
                          metrics_table="SALES_REVENUE | Выручка | 100\n", **kw)


def test_base_is_read_from_files_not_from_python():
    """В модуле не осталось питон-констант с базой."""
    for gone in ("_E", "BASE_ELEMENTS", "BASE_FRAMEWORK", "BASE_ANALYTICAL",
                 "BASE_COMPARABILITY"):
        assert not hasattr(md, gone), (
            f"{gone} всё ещё в питоне — значит источник остался в коде, "
            f"и методолог по-прежнему правит программу"
        )
    root = md._method_root()
    assert (root / "_order.md").is_file()
    assert md.base().elements, "из файлов не прочитан ни один элемент"


def test_every_element_has_an_id_and_text():
    """Пустой текст в файле — это молча исчезнувшее правило."""
    for eid, e in md.base().elements.items():
        assert e.text.strip(), f"у элемента «{eid}» пустой текст"
        assert e.id == eid


def test_order_referring_to_a_missing_element_is_an_error(tmp_path):
    """Опечатка в порядке сборки обязана быть отказом, а не тихой пропажей раздела.

    Молчание здесь означало бы, что методолог правит файл, а отчёт худеет без
    единого слова — ровно тот класс дефектов, который у нас уже случался.
    """
    src = md._method_root()
    dst = tmp_path / "method"
    shutil.copytree(src, dst)
    order = (dst / "_order.md").read_text(encoding="utf-8")
    (dst / "_order.md").write_text(
        order.replace("## comparability\n\n", "## comparability\n\n- нет.такого.элемента\n"),
        encoding="utf-8")

    old = os.environ.get(md._METHOD_ENV)
    os.environ[md._METHOD_ENV] = str(dst)
    try:
        with pytest.raises(md.MethodologyError, match="несуществующие элементы"):
            md.base()
    finally:
        if old is None:
            os.environ.pop(md._METHOD_ENV, None)
        else:
            os.environ[md._METHOD_ENV] = old


def test_missing_directory_is_a_loud_refusal(tmp_path):
    """Файлов нет — отказ с адресом поиска, а не пустая методика.

    Пустая методика дала бы отчёт без единого правила, выглядящий штатным.
    """
    old = os.environ.get(md._METHOD_ENV)
    os.environ[md._METHOD_ENV] = str(tmp_path / "нет-такого-каталога")
    try:
        with pytest.raises(md.MethodologyError, match="_order.md"):
            md.base()
    finally:
        if old is None:
            os.environ.pop(md._METHOD_ENV, None)
        else:
            os.environ[md._METHOD_ENV] = old


def test_edit_is_visible_without_restart(tmp_path):
    """ГЛАВНАЯ проверка: поправил файл — следующий прогон читает новое.

    Без этого весь переезд бессмысленен: методолог правил бы текст, а прогон
    считал бы по старому до перезапуска бэкенда.
    """
    src = md._method_root()
    dst = tmp_path / "method"
    shutil.copytree(src, dst)

    old = os.environ.get(md._METHOD_ENV)
    os.environ[md._METHOD_ENV] = str(dst)
    try:
        before = _prompt()
        # pin-ok: не проверка формулировки, а ЯКОРЬ для подстановки канарейки —
        # тест сам заменит эту строку ниже и убедится, что правка доехала.
        assert "ЖЁСТКИЕ ПРАВИЛА" in before

        f = dst / "prompt" / "frame.md"
        f.write_text(
            f.read_text(encoding="utf-8").replace("ЖЁСТКИЕ ПРАВИЛА:",
                                                  "АРБУЗ-СТОРОЖ-09-08:"),
            encoding="utf-8")
        # Время правки должно отличаться от прочитанного: на быстрых машинах
        # две записи подряд могут попасть в одну наносекунду штампа.
        os.utime(f, ns=(0, f.stat().st_mtime_ns + 1_000_000))

        after = _prompt()
        # pin-ok: канарейку вписал сам тест — предмет здесь сброс кэша методики,
        # а не текст промпта.
        assert "АРБУЗ-СТОРОЖ-09-08" in after, (
            "правка файла не доехала до промпта — кэш держит старое содержимое, "
            "и методолог правит впустую"
        )
        assert "ЖЁСТКИЕ ПРАВИЛА" not in after
    finally:
        if old is None:
            os.environ.pop(md._METHOD_ENV, None)
        else:
            os.environ[md._METHOD_ENV] = old
