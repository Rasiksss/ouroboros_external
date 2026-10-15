"""Справочник показателей: файл на ЯЧЕЙКУ, запись на показатель, загрузчик здесь.

ЗАЧЕМ. Уточнение владельца 15.08.2026 (`docs/EXTRACTION_V2_DESIGN.md`, раздел в
конце) требует трёх вещей, ни одна из которых не выполнима, пока состав
показателей — это питон-константы `EXTRACTABLE_NON_FINANCIAL`/`EXTRACTABLE_BANK`
(`skills/metric_registry.py:52-53`) и восемь json-файлов ячейки
(`skills/metric_schema.py`):

  1. человек добавляет показатель руками — без программиста и без выкатки;
  2. агент дописывает показатель себе сам (`fe_skill_editor`) и с этого момента
     умеет его извлекать;
  3. «извлеки мне прибыль» — гейтвейная модель читает справочник, отбирает ВСЕ
     показатели прибыли со всеми подсказками и отдаёт набор в запуск; «77
     показателей» перестаёт быть числом в коде и становится «всё, что лежит в
     каталоге ячейки».

ФОРМА: ОДИН ФАЙЛ MARKDOWN НА ЯЧЕЙКУ (`catalogue/<ячейка>.md`), внутри — записи
подряд. Запись начинается заголовком `## <ENUM>`, дальше поля `ключ: значение`
до первой ПУСТОЙ строки, дальше описание — до следующего `## ` или до конца
файла. Текст до первого `## ` — преамбула для человека, записью не считается.
Разбор — две регулярки, ноль сторонних библиотек.

🔴 РЕШЕНИЕ ВЛАДЕЛЬЦА 15.08, ПОСЛЕ ТОГО КАК ФАЙЛ НА ЗАПИСЬ БЫЛ СДЕЛАН И ОТВЕРГНУТ.
Первая редакция клала каждый показатель в отдельный файл — 158 файлов на две
ячейки. Довод был: агент дописывает показатель СОЗДАНИЕМ файла, и испортить
соседей физически нечем. Владелец его снял, и по делу: агент правит справочник
только по просьбе человека и только в ПОЛЬЗОВАТЕЛЬСКОЙ папке, а поставляемая
остаётся нетронутым откатом. Радиус поражения ограничен разделением папок —
дробить внутри папки незачем, а 158 файлов правит руками только тот, кто уже
знает, в каком из них искать.

⚠️ И ЕЩЁ ОДИН МОЙ ДОВОД БЫЛ ПРОСТО НЕВЕРЕН. Здесь стояло, что отдельные файлы
нужны, «чтобы битая запись не роняла остальные». Не нужны: записи разделены
строкой `## <ENUM>`, и в одном файле они режутся по той же границе — сломанная
секция пропускается по одной, как раньше пропускался файл. Свойство сохранено
целиком, см. рубеж 3 ниже.

Почему именно markdown, а не иначе:

* **не питон.** Правка состава = выпуск версии и рестарт; ни человек, ни агент
  кода не правят. Ошибка в одной строке — `SyntaxError` на импорте, то есть
  падает ВСЁ извлечение, а не один показатель.
* **не json.** Одна пропущенная запятая или кавычка роняет файл целиком — все 77
  показателей ячейки разом (сегодня так и есть: `metric_schema` отвечает на порчу
  `RegistryError` на весь файл, и это верное решение ДЛЯ ФАЙЛА-СПИСКА). Дописать
  запись в конец списка — самая ошибкоопасная операция, какая бывает у модели:
  запятая ставится не туда, где курсор. Многострочная подсказка требует `\\n`
  вместо переводов строки — человек так не пишет, а увидев экранирование, боится
  трогать. Комментария «почему так» в json нет вовсе.
* **не yaml.** Библиотеки нет в песочном наборе станции — это уже стоило нам
  шрама #202: загрузчик методики потянул `pyyaml`, и КАЖДЫЙ анализ падал
  `ModuleNotFoundError` после того, как все сторожа доставки отчитались зелёным.
  Плюс значимые отступы: сдвинутая на пробел строка меняет смысл соседей молча.
* **markdown.** Дописать показатель = дописать абзац в конец файла, и это
  единственная операция правки, которую человек делает не глядя на синтаксис
  (сравнить с «поставить запятую в нужное место json-списка»). Многострочную
  подсказку пишут переводами строк, а не `\\n`. Комментарий «почему так» лежит
  прямо в описании. Читают одно и то же и модель (обычный текст с подписями),
  и скрипт (две регулярки).

ЧЕТЫРЕ РУБЕЖА ОТКАЗА (по образцу `analysis/methodology.py`, с одним осознанным
отличием на третьем):

  1. ГДЕ ЛЕЖИТ. Каталога справочника нет — отказ со списком мест, где искали, и
     с именем переменной окружения. Тихая пустота дала бы прогон «по нулю
     показателей», внешне неотличимый от штатного.
  2. ЯЧЕЙКА. Неизвестный `profile_id`, нет файла ячейки, в файле ни одной
     записи, все записи битые — отказ с путём. Пустой справочник не бывает
     законным: он означает молча не тот состав показателей.
  3. ЗАПИСЬ. 🔴 ОТЛИЧИЕ ОТ ОБРАЗЦА: битая запись НЕ роняет остальные. Она
     пропускается, а её имя и причина называются вслух — в логе и в
     `Catalogue.problems`. Это прямое требование сценария 1: опечатка человека в
     одном показателе не имеет права снять с прогона другие 76.
  4. ОТБОР. Запросили имя, которого в справочнике нет, — отказ, а не тихий
     пропуск: молча выкинутый из набора показатель выглядит как «его нет в
     отчёте». Если имя пропало из-за битой записи, отказ называет ПРИЧИНУ порчи,
     а не «такого показателя нет» — иначе человек ищет ошибку не там.

КЭШ ПО ВРЕМЕНИ ПРАВКИ, а не «один раз за жизнь процесса» — шрам #155: правку
доставили, прогон считал по старому, а паспорт называл новый отпечаток. Ключ
кэша — путь, размер и `mtime_ns` файла ячейки, поэтому дописанная запись видна
СЛЕДУЮЩЕМУ прогону без рестарта.

ЧЕГО ЗДЕСЬ НЕТ. Кода, который судит о смысле: ни отбора показателей по словам
пользователя (это делает модель, читая эти же записи), ни порогов, ни весов.
Проверяются значения ровно трёх полей — тех, точные слова которых читает
ПРОГРАММА (`period_type`, `value_basis`, `formula_default_abs_behavior`);
остальные поля переносятся как написаны.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

from finengine.skills.metric_schema import PROFILE_IDS, VALID_PERIOD_TYPES

log = logging.getLogger(__name__)

# Переменные окружения. Первая указывает на сам справочник, вторая — на каталог
# методики навыка извлечения (`FE_EXTRACT_METHOD_DIR` из проекта §5), внутри
# которого справочник лежит подкаталогом. Держим обе, чтобы не заводить второй
# способ сказать одно и то же: кто задал методику целиком, получает и справочник.
CATALOGUE_ENV = "FE_EXTRACT_CATALOGUE_DIR"
METHOD_ENV = "FE_EXTRACT_METHOD_DIR"

_SKILL_PARTS = ("aifa-extract", "method", "catalogue")


class CatalogueError(ValueError):
    """Справочник не читается или из него просят несуществующее — говорим вслух."""


@dataclass(frozen=True)
class Metric:
    """Одна запись справочника — самодостаточное описание показателя.

    Самодостаточность — требование сценария 3: гейтвейная модель отбирает
    подмножество, и выбранные записи уезжают в прогон БЕЗ остального
    справочника. Значит всё, что нужно для извлечения этого показателя, обязано
    лежать в нём самом: имя, синонимы, форма отчётности, где искать, пример,
    единица, знак, базис.
    """

    enum: str
    name_ru: str
    name_en: str = ""
    aliases_ru: tuple[str, ...] = ()
    aliases_en: tuple[str, ...] = ()
    description: str = ""
    hint: str = ""
    example: str = ""
    form: str = ""
    unit_kind: str = ""
    sign: str = ""
    standard: str = ""
    value_basis: str = ""
    period_type: str = ""
    formula_default_abs_behavior: bool = False


@dataclass(frozen=True)
class Problem:
    """Запись, которую не удалось прочитать: что именно и почему.

    Возвращается вместе с исправными записями — иначе «пропустили» и «не было»
    неразличимы, и правка человека молча не применяется.
    """

    enum: str  # имя из заголовка секции; «?» — если не читается и он
    path: str  # файл ячейки и номер строки заголовка: править идут туда
    why: str


@dataclass(frozen=True)
class Catalogue:
    """Справочник одной ячейки: правила ячейки, записи, список пропущенных."""

    profile_id: str
    root: Path
    metrics: tuple[Metric, ...]
    rules: str = ""
    problems: tuple[Problem, ...] = ()

    def get(self, enum: str) -> Metric | None:
        key = _key(enum)
        return next((m for m in self.metrics if m.enum == key), None)

    def select(self, names: object = None) -> tuple[Metric, ...]:
        """Подмножество по именам. Порядок — КАК ПОПРОСИЛИ (рубеж 4).

        `names=None` — весь справочник ячейки, по алфавиту: порядок файлов в
        каталоге ничего не значит, и делать вид, что значит, нельзя — сегодня на
        неохраняемом допущении «порядок реестра = порядок форм отчётности»
        держится нарезка на шарды (`bulk_extractor.py:4300-4301`).

        Порядок запрошенных имён сохраняется, потому что он осмысленный: его
        задаёт тот, кто отбирал показатели по смыслу.
        """
        if names is None:
            return tuple(sorted(self.metrics, key=lambda m: m.enum))
        out: list[Metric] = []
        for raw in dict.fromkeys(_key(n) for n in names):  # дубли просьбы — не ошибка
            metric = self.get(raw)
            if metric is None:
                raise CatalogueError(self._why_no(raw))
            out.append(metric)
        return tuple(out)

    def _why_no(self, enum: str) -> str:
        broken = next((p for p in self.problems if p.enum == enum), None)
        if broken is not None:
            return (f"показатель «{enum}» есть в справочнике {self.profile_id}, но "
                    f"его запись не читается: {broken.why} ({broken.path})")
        return (f"в справочнике {self.profile_id} нет показателя «{enum}»; "
                f"записей: {len(self.metrics)}, каталог: {self.root}")


# ───────────────────────────── где лежит (рубеж 1) ─────────────────────────────


def _from_env() -> tuple[str, Path] | None:
    raw = os.environ.get(CATALOGUE_ENV, "").strip()
    if raw:
        return CATALOGUE_ENV, Path(raw)
    raw = os.environ.get(METHOD_ENV, "").strip()
    if raw:
        return METHOD_ENV, Path(raw) / "catalogue"
    return None


def _looks_like_catalogue(path: Path) -> bool:
    """Признак справочника — файл хотя бы одной ЯЧЕЙКИ.

    Проверять просто «каталог существует» нельзя: опечатка в пути тогда даёт
    пустой справочник вместо отказа, а это ровно то, ради невозможности чего
    здесь всё и написано.
    """
    return any((path / f"{pid}.md").is_file() for pid in PROFILE_IDS)


def catalogue_root() -> Path:
    """Каталог справочника. Не нашли — отказ со списком мест, где искали."""
    from_env = _from_env()
    if from_env is not None:
        name, path = from_env
        if _looks_like_catalogue(path):
            return path
        raise CatalogueError(
            f"{name} указывает на {path}, но справочника там нет: ждали файл "
            f"ячейки ({', '.join(pid + '.md' for pid in PROFILE_IDS)})")
    # `<...>/finengine/skills/extraction/catalogue.py` → `<...>` = корень пакетов.
    pkg_root = Path(__file__).resolve().parents[3]
    candidates = (
        # Репозиторий и контейнер бэкенда: пакеты в `src/`, рядом `station-skills/`.
        pkg_root.parent.joinpath("station-skills", *_SKILL_PARTS),
        # Станция: полка распакована так, что рядом с `finengine` лежит
        # `aifa_station_skills` (имя арк-каталога задаёт station_driver).
        pkg_root.joinpath("aifa_station_skills", *_SKILL_PARTS),
    )
    for c in candidates:
        if _looks_like_catalogue(c):
            return c
    raise CatalogueError(
        "не найден справочник показателей (искали: "
        + ", ".join(str(c) for c in candidates)
        + f"). Задать явно: {CATALOGUE_ENV}=<каталог>")


# ────────────────────────────── разбор записи ──────────────────────────────
#
# Два правила, ноль сторонних библиотек (обоснование — в шапке модуля).
#   • Запись начинается строкой «## <ENUM>» и кончается перед следующей такой.
#   • Дальше поля «ключ: значение» — до первой ПУСТОЙ строки.
#   • Всё после пустой строки и до конца записи — описание показателя.
#
# Отличие от разбора методики: конец записи тоже закрывает блок полей. Там края
# текста значимы (промпт склеивается конкатенацией), поэтому пустая строка
# обязательна; здесь описание всё равно обрезается по краям, а запись без
# завершающего перевода строки — обычный результат правки через тул, и терять
# из-за него запись целиком было бы платой ни за что.

_FIELDS = ("name_ru", "name_en", "aliases_ru", "aliases_en", "form", "hint",
           "example", "unit_kind", "sign", "standard", "value_basis",
           "period_type", "formula_default_abs_behavior")
_FIELD_LINE = re.compile(r"^(" + "|".join(_FIELDS) + r"):[ ]?(.*)$")
_HEAD = re.compile(r"^## +(\S.*)$")
_ENUM = re.compile(r"^[A-Z][A-Z0-9_]*$")
_YES_NO = {"": False, "да": True, "нет": False}
_VALUE_BASIS = ("", "gross", "net")


def _key(enum: object) -> str:
    """Имя показателя в сравнимом виде: реестр пишет их ЗАГЛАВНЫМИ."""
    return str(enum or "").strip().upper()


def _split(raw: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in raw.split(";") if p.strip())


def _parse(text: str, where: str) -> Metric:
    """Текст файла → запись. Непонятное — отказ с местом и причиной."""
    lines = text.split("\n")
    head = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if head is None or not lines[head].startswith("## "):
        raise CatalogueError(
            f"{where}: запись обязана начинаться строкой «## <ENUM>»; "
            f"первая непустая строка: "
            f"{(lines[head] if head is not None else '<файл пуст>')!r}")
    enum = lines[head][3:].strip()

    fields: dict[str, str] = {}
    i = head + 1
    while i < len(lines) and lines[i].strip():
        m = _FIELD_LINE.match(lines[i])
        if not m:
            raise CatalogueError(
                f"{where}: строка {lines[i]!r} не поле и не описание. Поля идут "
                f"сразу за заголовком и кончаются ПУСТОЙ строкой; известные: "
                f"{', '.join(_FIELDS)}")
        if m.group(1) in fields:
            raise CatalogueError(
                f"{where}: поле «{m.group(1)}» объявлено дважды — непонятно, "
                f"какое из двух значений верное")
        fields[m.group(1)] = m.group(2).strip()
        i += 1
    description = "\n".join(lines[i + 1:]).strip("\n")

    if not fields.get("name_ru"):
        raise CatalogueError(
            f"{where}: нет поля name_ru — без имени показатель нечем назвать ни "
            f"в промпте, ни пользователю")
    period_type = fields.get("period_type", "")
    if period_type not in VALID_PERIOD_TYPES:
        raise CatalogueError(
            f"{where}: period_type={period_type!r}; допустимо: "
            f"{', '.join(sorted(x for x in VALID_PERIOD_TYPES if x))} или пусто")
    value_basis = fields.get("value_basis", "")
    if value_basis not in _VALUE_BASIS:
        raise CatalogueError(
            f"{where}: value_basis={value_basis!r}; допустимо: gross, net или пусто")
    abs_behavior = fields.get("formula_default_abs_behavior", "")
    if abs_behavior not in _YES_NO:
        raise CatalogueError(
            f"{where}: formula_default_abs_behavior={abs_behavior!r}; "
            f"допустимо: да, нет или пусто")

    return Metric(
        enum=enum,
        name_ru=fields["name_ru"],
        name_en=fields.get("name_en", ""),
        aliases_ru=_split(fields.get("aliases_ru", "")),
        aliases_en=_split(fields.get("aliases_en", "")),
        description=description,
        hint=fields.get("hint", ""),
        example=fields.get("example", ""),
        form=fields.get("form", ""),
        unit_kind=fields.get("unit_kind", ""),
        sign=fields.get("sign", ""),
        standard=fields.get("standard", ""),
        value_basis=value_basis,
        period_type=period_type,
        formula_default_abs_behavior=_YES_NO[abs_behavior],
    )


# ───────────────────────── чтение ячейки (рубежи 2 и 3) ─────────────────────────


def _split_rules_and_sections(text: str) -> tuple[str, tuple[tuple[int, str], ...]]:
    """Файл ячейки → правила ячейки и записи показателей.

    ПРАВИЛА ЯЧЕЙКИ — весь текст ДО первой строки `## <ENUM>`. Это не заметка для
    человека, а часть промпта: как читать отчётность ЭТОГО стандарта. Для РСБУ
    там формы 0710001/0710002, коды строк, «прочерк в составной строке не
    означает ноль»; в вышестоящем репозитории то же самое лежит питон-константой
    `_RSBU_NF_RU_SPECIFICS` в `extractors/_rsbu_nf_ru.py`, и правит её только
    программист.

    Почему тут, а не отдельным полем записи (вопрос владельца 15.08): правило
    относится к ЯЧЕЙКЕ, а не к показателю. Полем записи те же 13 правил РСБУ
    легли бы в каждую из 62 записей, и правка означала бы 62 правки.

    Заголовки внутри правил пишутся `#` и `###` — `## ` зарезервирована под
    начало записи и в правилах не встречается.
    """
    lines = text.split("\n")
    heads = [i for i, ln in enumerate(lines) if _HEAD.match(ln)]
    rules = "\n".join(lines[:heads[0]] if heads else lines).strip()
    bounds = heads[1:] + [len(lines)]
    return rules, tuple((h + 1, "\n".join(lines[h:end])) for h, end in zip(heads, bounds))


def _fingerprint(cell: Path) -> tuple:
    """Отпечаток файла ячейки: время правки и размер.

    Отпечаток — ключ кэша, поэтому дописанная запись обязана менять его сама,
    иначе новый показатель увидит только рестарт (шрам #155).
    """
    st = cell.stat()
    return (st.st_mtime_ns, st.st_size)


@lru_cache(maxsize=8)
def _read(cell_str: str, profile_id: str, _fp: tuple) -> Catalogue:
    """Прочитать ячейку с диска. Ключ кэша включает отпечаток — правка видна сразу."""
    cell = Path(cell_str)
    try:
        text = cell.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # Рубеж 2, а не 3: не читается ФАЙЛ, а не одна запись в нём. Пропустить
        # это молча значило бы прогон по нулю показателей вместо отказа.
        raise CatalogueError(f"справочник ячейки {profile_id} не читается ({cell}): {exc}") from exc

    rules, sections = _split_rules_and_sections(text)
    metrics: list[Metric] = []
    problems: list[Problem] = []
    seen: dict[str, str] = {}
    for lineno, chunk in sections:
        where = f"{cell}:{lineno}"
        head = _HEAD.match(chunk.split("\n", 1)[0])
        named = _key(head.group(1)) if head else "?"
        try:
            metric = _parse(chunk, where)
            if not _ENUM.match(metric.enum):
                raise CatalogueError(
                    f"{where}: «{metric.enum}» не имя показателя — ждали ЗАГЛАВНЫЕ "
                    f"латиницей и подчёркивания (`## SALES_REVENUE`)")
            if metric.enum in seen:
                raise CatalogueError(
                    f"{where}: показатель «{metric.enum}» уже объявлен в "
                    f"{seen[metric.enum]} — непонятно, какая из двух записей верна")
        except CatalogueError as exc:
            # Рубеж 3: пропускаем ОДНУ запись и называем её вслух. Отказ на весь
            # справочник здесь означал бы, что опечатка человека в одном
            # показателе снимает с прогона все остальные.
            problems.append(Problem(enum=named, path=where, why=str(exc)))
            log.warning("справочник %s: запись %s пропущена — %s", profile_id, where, exc)
            continue
        seen[metric.enum] = where
        metrics.append(metric)

    if not metrics:
        raise CatalogueError(
            f"справочник ячейки {profile_id} пуст ({cell}): "
            + (f"ни одна из {len(problems)} записей не читается — "
               + "; ".join(p.why for p in problems)
               if problems else "в файле нет ни одной строки «## <ENUM>»"))
    return Catalogue(profile_id=profile_id, root=cell, metrics=tuple(metrics),
                     rules=rules, problems=tuple(problems))


def load(profile_id: str) -> Catalogue:
    """Справочник ячейки. Ячейки нет или она пуста — отказ (рубеж 2)."""
    pid = str(profile_id or "").strip()
    if pid not in PROFILE_IDS:
        raise CatalogueError(
            f"неизвестная ячейка «{pid}»; есть: {', '.join(PROFILE_IDS)}")
    cell = catalogue_root() / f"{pid}.md"
    if not cell.is_file():
        raise CatalogueError(
            f"нет справочника ячейки {pid}: файла {cell} не существует")
    catalogue = _read(str(cell), pid, _fingerprint(cell))
    # Slice 13 moved the cross-language aliases that the live engine already
    # merged at runtime into the canonical effective registry.  The catalogue
    # remains allowed to be richer, but must not silently discard those
    # canonical aliases when it materializes the same metric.
    from finengine.skills.metric_schema import registry_for_profile  # noqa: PLC0415

    by_enum = {row["enum"]: row for row in registry_for_profile(pid)["extractable"]}

    def _union(own: tuple[str, ...], canonical: object) -> tuple[str, ...]:
        result = list(own)
        seen = {item.strip().casefold() for item in result}
        for item in canonical or []:
            value = str(item).strip()
            if value and value.casefold() not in seen:
                result.append(value)
                seen.add(value.casefold())
        return tuple(result)

    metrics = tuple(
        replace(
            metric,
            aliases_ru=_union(metric.aliases_ru, by_enum.get(metric.enum, {}).get("aliases_ru")),
            aliases_en=_union(metric.aliases_en, by_enum.get(metric.enum, {}).get("aliases_en")),
        )
        for metric in catalogue.metrics
    )
    return replace(catalogue, metrics=metrics)


def registry_rows(profile_id: str, names: object = None) -> list[dict]:
    """Справочник → строки в том виде, в каком их ждёт движок извлечения.

    МОСТ ПЕРЕКЛЮЧЕНИЯ, ЕЩЁ НЕ ВКЛЮЧЁННЫЙ. Боевой путь (`aifa-extract/scripts/
    extract.py:86`) берёт строки из `registry_for_profile`; здесь то же самое
    собирается из справочника. Ключи — ровно те, что читает промпт
    (`bulk_extractor.py:4216-4223`), плюс `document_types`, который читает отбор
    по типу документа.

    ⚠️ ПОЧЕМУ ПЕРЕКЛЮЧЕНИЕ НЕ СДЕЛАНО ЗАОДНО (замер 15.08.2026). Сверили 4266
    полей по десяти ячейкам: 4067 совпадают, 199 у справочника БОГАЧЕ (109
    английских синонимов, 64 описания с добавленными абзацами, 26 полей там, где
    реестр молчит), противоречий — НОЛЬ. То есть переключение ничего не теряет,
    но промпт меняет в 199 местах, а это правка качества, а не переезд. Её место
    — за бенчмарком, ровно как со всеми прошлыми переключениями.

    `form`, `example`, `unit_kind`, `sign` в строку кладутся, но промпт их пока
    не читает: добавить их в промпт — та же правка качества, отдельным шагом.
    """
    out: list[dict] = []
    for m in select(profile_id, names):
        out.append({
            "enum": m.enum,
            "name_ru": m.name_ru,
            "name_en": m.name_en,
            "aliases_ru": list(m.aliases_ru),
            "aliases_en": list(m.aliases_en),
            "description": m.description,
            "hint": m.hint,
            "standard": m.standard,
            "value_basis": m.value_basis or None,
            "period_type": m.period_type,
            "formula_default_abs_behavior": m.formula_default_abs_behavior,
            "form": m.form,
            "example": m.example,
            "unit_kind": m.unit_kind,
            "sign": m.sign,
        })
    return out


def select(profile_id: str, names: object = None) -> tuple[Metric, ...]:
    """Подмножество показателей ячейки — по именам, в запрошенном порядке.

    Это вход сценария 3: гейтвейная модель прочитала справочник, отобрала по
    смыслу («извлеки мне прибыль») и передала сюда имена. Кода, который решает,
    что относится к прибыли, здесь нет и быть не должно.
    """
    return load(profile_id).select(names)

# ── ОГЛАВЛЕНИЕ ЯЧЕЙКИ ДЛЯ МОДЕЛИ (решение владельца 31.08.2026) ───────────────
#
# «Я хочу, чтобы схожесть семантики проверяла сама LLM. Пишу „извлеки доходик“ —
# модель должна понять, что по смыслу это наш показатель „выручка“».
#
# Детерминированный мост слов (`requested_metrics.resolve_requested`) на такое
# не способен по построению: у «доходик» и «Выручка» нет ни одной общей буквы.
# Семантику сопоставляет модель — а для этого ей нужен ПОЛНЫЙ список имён
# ячейки одним небольшим файлом: полные статьи справочника (~100 КБ) в один
# `read`/`cat` не влезают, и раньше модель грепала вслепую.
#
# Оглавление СТРОИТСЯ ИЗ EFFECTIVE-РЕЕСТРА ДВИЖКА, а не из справочника .md:
# движок читает скомпилированные слои, и оглавление обязано совпадать с тем,
# по чему реально пойдёт извлечение. В нём ОБА вида показателей — и это чинит
# вторую давнюю дыру: расчётных в справочнике .md нет вовсе, и «чистый долг»
# модель в нём не находила, честно решая «в реестре нет».
#
# Файлы `<ячейка>.index.md` лежат в справочнике и коммитятся; паритет с
# реестром держит сторож `tests/test_catalogue_index_matches_registry.py`.

_INDEX_SYNONYMS = 4  # синонимов на строку: узнаваемость без простыни


def index_lines(profile_id: str) -> list[str]:
    """Строки оглавления ячейки: `ENUM — имя | син: …` (+ раздел расчётных)."""
    from finengine.skills.metric_schema import registry_for_profile  # noqa: PLC0415

    reg = registry_for_profile(profile_id)
    out = [
        f"# Оглавление ячейки {profile_id} — ВСЕ показатели одним списком",
        "",
        "Выбирай ПО СМЫСЛУ и передавай ГОТОВЫЕ имена (ENUM). Полная статья",
        f"показателя — грепом по `{profile_id}.md`. Файл сгенерирован из",
        "реестра движка; руками не править.",
        "",
        "## Извлекаемые (ищутся в отчёте)",
        "",
    ]
    for m in reg["extractable"]:
        имя = str(m.get("name_ru") or m.get("name_en") or "").strip()
        # Preserve the published index byte-for-byte. Before Slice 13 the EN
        # profile files had no aliases_ru; cross-language aliases were merged
        # only later on the extraction path. They now live in the effective
        # registry, but changing this model-facing catalogue is a separate
        # quality decision, not part of registry composition.
        aliases_ru = m.get("aliases_ru") or [] if profile_id.endswith("_ru") else []
        син = [str(x).strip() for x in aliases_ru[:_INDEX_SYNONYMS] if str(x).strip()]
        хвост = f" | син: {'; '.join(син)}" if син else ""
        out.append(f"{str(m['enum']).strip()} — {имя}{хвост}")
    out += ["", "## Расчётные (считаются формулой по извлечённым)", ""]
    for c in reg["calculated"]:
        out.append(f"{str(c[0]).strip()} — {str(c[1]).strip()} | формула: {str(c[2]).strip()}")
    return out

