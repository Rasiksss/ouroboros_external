"""Справочник показателей — текст, и правка текста работает без программиста.

ЗАЧЕМ ЭТОТ ФАЙЛ. Уточнение владельца 15.08.2026 задаёт три сценария приёмки:
человек правит справочник руками; агент дописывает себе показатель сам; набор
показателей хода собирается ВЫБОРКОЙ из справочника, а не фиксированным списком
в коде. Здесь проверяется, что форма записи их выдерживает, а не только что
загрузчик что-то читает.

ФОРМА: файл на ЯЧЕЙКУ (`catalogue/<ячейка>.md`), внутри записи подряд, каждая с
заголовка `## <ENUM>`. Решение владельца 15.08: файл на КАЖДУЮ запись отвергнут
(158 файлов на две ячейки), потому что радиус поражения ограничен разделением
папок — поставляемая против пользовательской, — а не дроблением внутри папки.

Главное свойство формы: ошибка в ОДНОЙ записи не роняет остальные. Оно НЕ от
отдельных файлов, как я сначала записал, а от границы `## <ENUM>` — по ней
записи режутся и сломанная пропускается по одной. У json-реестра этого нет и
быть не может: одна запятая уносит все 77 показателей ячейки
(`skills/metric_schema.py`, `RegistryError` на весь файл).

ПРОВЕРКА СТОРОЖА ОТКАТОМ:
* заменить в `catalogue._read` пропуск битой записи (`continue`) на `raise` —
  `test_broken_record_does_not_take_the_others_down` обязан покраснеть;
* убрать отпечаток из ключа кэша (`_fingerprint`) —
  `test_new_record_is_visible_without_restart` обязан покраснеть;
* вернуть в `Catalogue.select` тихий пропуск ненайденного имени вместо отказа —
  `test_unknown_name_is_a_refusal_not_a_silent_gap` обязан покраснеть.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from finengine.skills.extraction import catalogue as cat
from finengine.skills.metric_schema import PROFILE_IDS

# Пять показателей, положенных в справочник образцом формы. Не «какие-нибудь»:
# на них стоит любой финансовый разбор, и именно их владелец назвал в приёмке.
SHIPPED = ("EBITDA", "NET_INCOME", "SALES_REVENUE", "TOTAL_ASSETS", "TOTAL_EQUITY")
CELL = "ifrs_nf_ru"


@pytest.fixture
def cell_file(tmp_path, monkeypatch):
    """Копия поставляемого справочника, которую тест вправе портить."""
    dst = tmp_path / "catalogue"
    shutil.copytree(cat.catalogue_root(), dst)
    monkeypatch.setenv(cat.CATALOGUE_ENV, str(dst))
    return dst / f"{CELL}.md"


def _replace(path: Path, enum: str, record: str) -> None:
    """Заменить запись целиком — так её правит и человек, и `fe_skill_editor`."""
    text = path.read_text(encoding="utf-8")
    start = text.index(f"\n## {enum}\n")
    nxt = text.find("\n## ", start + 1)
    tail = text[nxt:] if nxt >= 0 else "\n"
    path.write_text(text[:start] + "\n" + record.strip("\n") + "\n" + tail, encoding="utf-8")


@pytest.mark.parametrize("cell", PROFILE_IDS)
def test_shipped_records_are_readable_and_self_contained(cell: str):
    """Форма записи: поставляемые показатели читаются и несут своё.

    ⚠️ ПРОВЕРКА НЕ ПРИБИТА К СОСТАВУ. Первая редакция требовала РАВЕНСТВА пяти
    образцам — и покраснела в тот же день, когда справочник пополнили до 150
    записей. Сторож обязан охранять ФОРМУ записи, а не число записей: состав
    растёт по замыслу (владелец правит текстом, агент дописывает сам), и
    сторож, краснеющий на штатном пополнении, учит его игнорировать.

    ⚠️ И НЕ ПРИБИТА К ОДНОЙ ЯЧЕЙКЕ. Вторая редакция стояла на `ifrs_nf_ru` и
    молчала о шести пустых ячейках из восьми — та же дыра «между частями не
    отвечал никто», что дала недостачу семи записей.
    """
    c = cat.load(cell)
    assert not c.problems, f"{cell}: поставляемый справочник с проблемами: {c.problems}"
    if cell == CELL:
        got = {m.enum for m in c.select()}
        assert set(SHIPPED) <= got, f"пропали образцовые записи: {set(SHIPPED) - got}"

    for m in c.select():
        # Самодостаточность — условие сценария 3: выбранная запись уезжает в
        # прогон БЕЗ остального справочника, и всё нужное обязано быть в ней.
        assert m.name_ru and m.description, f"{cell}/{m.enum}"
        assert m.hint, f"{cell}/{m.enum}: не сказано, ГДЕ искать"
        assert m.example, f"{cell}/{m.enum}: нет примера"
        assert m.form and m.unit_kind and m.sign, f"{cell}/{m.enum}"
        assert m.period_type in ("point", "duration"), f"{cell}/{m.enum}"


def test_shipped_records_repeat_the_registry_word_for_word():
    """Данные ПЕРЕЕЗЖАЮТ, а не переписываются по памяти.

    Пока справочник и json-реестр ячейки живут рядом, расхождение текста — это
    два ответа на один вопрос: подсказку правят в одном месте, а в промпт едет
    другая. Проверяем ровно перенесённые пять; остальные 72 подключатся к этой
    же проверке по мере переноса.

    Новые поля (`form`, `example`, `sign`, `unit_kind`) и абзацы описания сверх
    первого сюда не входят: в реестре их нет, ради них справочник и заводится.
    """
    from finengine.skills.metric_schema import registry_for_profile

    rows = {r["enum"]: r for r in registry_for_profile(CELL)["extractable"]}
    for m in cat.load(CELL).select():
        row = rows[m.enum]
        assert m.name_ru == row["name_ru"], m.enum
        assert m.name_en == row["name_en"], m.enum
        assert list(m.aliases_ru) == row["aliases_ru"], m.enum
        assert m.hint == row["hint"], m.enum
        assert m.description.split("\n\n")[0] == row["description"], m.enum
        assert m.period_type == row["period_type"], m.enum
        assert m.value_basis == (row["value_basis"] or ""), m.enum
        assert m.formula_default_abs_behavior == row["formula_default_abs_behavior"], m.enum


def test_record_address_is_the_heading(cell_file):
    """Адрес показателя — заголовок `## <ENUM>`, и он один на файл.

    Заголовок не в форме имени показателя (строчные, пробелы, кириллица) — не
    запись, а чей-то раздел: под таким адресом прогон показатель не найдёт, и
    сказать об этом надо тогда же, а не на прогоне.
    """
    text = cell_file.read_text(encoding="utf-8")
    for enum in SHIPPED:
        assert f"\n## {enum}\n" in text, f"{enum} не найден в файле ячейки"

    _replace(cell_file, "TOTAL_ASSETS", "## Итого активы\nname_ru: Итого\n\nОписание.")
    c = cat.load(CELL)
    assert [p.enum for p in c.problems] == ["ИТОГО АКТИВЫ"]
    assert "не имя показателя" in c.problems[0].why
    # Адрес правки — файл и СТРОКА: искать запись в файле на 92 КБ иначе нечем.
    assert c.problems[0].path.startswith(str(cell_file) + ":")


def test_a_broken_record_points_at_its_line(cell_file):
    """Место поломки называется строкой, и строка — та самая."""
    _replace(cell_file, "EBITDA", "## EBITDA\nname_ru: Прибыль\nхинт: не то поле\n\nОписание.")
    problem = cat.load(CELL).problems[0]
    lineno = int(problem.path.rsplit(":", 1)[1])
    line = cell_file.read_text(encoding="utf-8").split("\n")[lineno - 1]
    assert line == "## EBITDA", f"номер строки указывает не на заголовок записи: {line!r}"


@pytest.mark.parametrize("cell", PROFILE_IDS)
def test_every_cell_carries_its_reading_rules(cell: str):
    """У каждой ячейки есть ПРАВИЛА: как читать отчётность этого стандарта.

    Это ответ на вопрос владельца 15.08 «куда положить _RSBU_NF_RU_SPECIFICS».
    В форк-базе такие правила лежат питон-константой внутри
    `extractors/_<ячейка>.py` и правятся только программистом; у нас — текстом
    в преамбуле файла ячейки, до первой записи.

    Почему не полем записи: правило относится к ЯЧЕЙКЕ. Тринадцать правил РСБУ
    полем легли бы в каждую из 62 записей, и правка означала бы 62 правки.

    Пустые правила опаснее, чем кажется: для РСБУ там формы 0710001/0710002,
    «не переводи в миллионы» и «прочерк в составной строке ≠ ноль». Без них
    извлечение идёт по подсказкам МСФО и молча даёт не те числа.
    """
    rules = cat.load(cell).rules
    assert len(rules) > 300, f"{cell}: правил {len(rules)} б — это не правила, а заглушка"
    assert cell in rules, f"{cell}: правила не называют свою ячейку"


@pytest.mark.parametrize("cell", PROFILE_IDS)
def test_rules_do_not_swallow_a_record(cell: str):
    """`## ` в правилах съела бы соседнюю запись — заголовки только `#` и `###`.

    ⚠️ ЭТО НЕ ГИПОТЕТИКА. Первый перенос правил положил в преамбулу заголовок
    `## РСБУ / …`, и `ifrs_nf_ru` с `ifrs_bank_ru` молча потеряли по записи:
    разбор счёл заголовок правил началом показателя.
    """
    text = (cat.catalogue_root() / f"{cell}.md").read_text(encoding="utf-8")
    rules = text.split("\n## ", 1)[0]
    bad = [ln for ln in rules.split("\n") if ln.startswith("## ")]
    assert not bad, (
        f"{cell}: в правилах заголовок с двумя решётками {bad!r} — он будет "
        "прочитан как начало записи показателя. Понизить до `###`.")


def test_subset_by_names_keeps_the_asked_order():
    """Выборка — по именам и в порядке просьбы (сценарий 3)."""
    asked = ["NET_INCOME", "sales_revenue", "NET_INCOME"]  # регистр и дубль просьбы
    assert [m.enum for m in cat.select(CELL, asked)] == ["NET_INCOME", "SALES_REVENUE"]


def test_unknown_name_is_a_refusal_not_a_silent_gap():
    """Молча выкинутый из набора показатель выглядит как «его нет в отчёте»."""
    with pytest.raises(cat.CatalogueError, match="нет показателя"):
        cat.select(CELL, ["SALES_REVENUE", "НЕТ_ТАКОГО"])


def test_unknown_cell_is_a_refusal():
    with pytest.raises(cat.CatalogueError, match="неизвестная ячейка"):
        cat.load("ifrs_nf_rru")


def test_broken_record_does_not_take_the_others_down(cell_file):
    """ГЛАВНАЯ проверка формы: опечатка в одном показателе не снимает остальные.

    Ровно этого не умеет один json на ячейку — там пропущенная запятая уносит
    весь состав, и человек, поправивший подсказку, теряет прогон целиком. Здесь
    записи лежат в ОДНОМ файле и всё равно режутся по одной: свойство держит
    граница `## <ENUM>`, а не то, что запись лежала отдельным файлом.
    """
    _replace(cell_file, "EBITDA", "## EBITDA\nname_ru: Прибыль\nхинт: не то поле\n\nОписание.")

    c = cat.load(CELL)
    alive = [m.enum for m in c.select()]
    assert "EBITDA" not in alive, "битая запись просочилась в прогон"
    assert set(SHIPPED) - {"EBITDA"} <= set(alive), "битая запись унесла соседей"
    assert len(alive) >= 50, "битая запись унесла файл целиком"
    assert [p.enum for p in c.problems] == ["EBITDA"]
    # Причина названа так, чтобы правка не требовала чтения кода.
    assert "не поле и не описание" in c.problems[0].why


def test_asking_for_a_broken_record_says_what_is_broken(cell_file):
    """Пропущенный показатель просили — отказ обязан назвать ПРИЧИНУ порчи.

    «Такого показателя нет» отправило бы человека искать ошибку не там: он
    только что своими руками эту запись и написал.
    """
    _replace(cell_file, "NET_INCOME",
             "## NET_INCOME\nname_ru: Чистая прибыль\nperiod_type: поток\n\nОписание.")

    with pytest.raises(cat.CatalogueError, match="запись не читается"):
        cat.select(CELL, ["NET_INCOME"])


def test_program_read_fields_are_checked(cell_file):
    """Проверяются значения тех полей, точные слова которых читает ПРОГРАММА."""
    _replace(cell_file, "TOTAL_EQUITY",
             "## TOTAL_EQUITY\nname_ru: Капитал\nvalue_basis: брутто\n\nОписание.")
    problems = cat.load(CELL).problems
    assert [p.enum for p in problems] == ["TOTAL_EQUITY"]
    assert "value_basis" in problems[0].why


def test_a_note_above_the_records_is_not_a_record(cell_file):
    """Пояснение в начале файла не уезжает в прогон и не считается поломкой.

    Файл ячейки открывает человек, и первое, что он должен там увидеть, — что
    это за файл и как его править. Стань преамбула «битой записью», справочник
    жаловался бы на собственную инструкцию.
    """
    cell_file.write_text(
        "# Заметки методолога\n\nОбсудить с Алексеем состав.\n"
        + cell_file.read_text(encoding="utf-8"), encoding="utf-8")
    c = cat.load(CELL)
    assert set(SHIPPED) <= {m.enum for m in c.select()}
    assert not c.problems


def test_empty_catalogue_is_a_refusal(cell_file):
    """Пустой справочник = прогон по нулю показателей, внешне штатный."""
    cell_file.write_text("# Справочник\n\nЗаписи вынесли, вернуть.\n", encoding="utf-8")
    with pytest.raises(cat.CatalogueError, match="пуст"):
        cat.load(CELL)


def test_missing_catalogue_is_a_loud_refusal(tmp_path, monkeypatch):
    """Каталога нет — отказ с адресом поиска, а не тихая пустота."""
    monkeypatch.setenv(cat.CATALOGUE_ENV, str(tmp_path / "нет-такого-каталога"))
    with pytest.raises(cat.CatalogueError, match="справочника там нет"):
        cat.load(CELL)


def test_new_record_is_visible_without_restart(cell_file):
    """Сценарии 1 и 2: дописали запись — следующий прогон уже умеет показатель.

    Без этого весь переезд бессмыслен: человек (или агент через
    `fe_skill_editor`) правил бы справочник, а прогон работал по старому составу
    до перезапуска бэкенда — шрам #155.

    Дописываем В КОНЕЦ ФАЙЛА, потому что именно так добавляют показатель: ни
    человеку, ни тулу не нужно искать место среди 77 записей.
    """
    # Имя ЗАВЕДОМО отсутствующее: `INVENTORIES` тут стоял до пополнения
    # справочника и перестал быть «которого ещё нет» — тест проверял бы пустоту.
    with pytest.raises(cat.CatalogueError, match="нет показателя"):
        cat.select(CELL, ["NO_SUCH_METRIC_EVER"])

    with cell_file.open("a", encoding="utf-8") as f:
        f.write(
            "\n## NO_SUCH_METRIC_EVER\n"
            "name_ru: Запасы\n"
            "form: Баланс\n"
            "hint: Баланс, раздел оборотных активов.\n"
            "example: `| Запасы | 88 100 | 74 900 |` → 88 100\n"
            "unit_kind: money\n"
            "sign: положительный\n"
            "period_type: point\n"
            "\n"
            "Сырьё, материалы, незавершённое производство и готовая продукция.\n")

    added = cat.select(CELL, ["NO_SUCH_METRIC_EVER"])[0]
    assert added.name_ru == "Запасы"
    assert added.period_type == "point"
