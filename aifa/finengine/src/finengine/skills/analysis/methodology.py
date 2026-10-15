"""Методология анализа как ДАННЫЕ с адресами — база продукта + правки пользователя.

ЗАЧЕМ. Пользователь правит методику словами: «работающие активы считай так-то»,
«горизонт устойчивости не 6 месяцев, а 8», «NIM у нас считают по средним за
квартал». Пока методика — питоновские строки-константы, единственный способ учесть
такую правку — дописать её в промпт отдельным абзацем. Это костыль: правка ничем не
связана с местом, которое исправляет, две правки одного правила противоречат друг
другу молча, а обновление базовой методики их не замечает. Поэтому методика здесь
разобрана на АДРЕСУЕМЫЕ элементы: у каждого правила, формулы и раздела есть
стабильный id, и правка пользователя — это дельта к конкретному id.

ТРИ СЛОЯ (решение Nate 31.07):
  L1 — данные прогона (реестр показателей): числа;
  L2 — ЭТОТ слой: методика. База (`BASE`) поставляется с продуктом и написана по
       правилам банка; пользователь накладывает поверх ДЕЛЬТЫ — только то, что
       поменял, а не копию всей методики. Дельта хранит текст базы, против которой
       делалась: обновили базу — пользователю показывается расхождение, и он сам
       решает, брать новое или оставить своё;
  L3 — код скиллов (правится через `fe_skill_editor`, с ревью и деплоем).

ПОЧЕМУ ДЕЛЬТЫ, А НЕ КОПИЯ. Копия форка методики замерзает в момент снятия: наши
исправления формул до пользователя больше не доезжают, а он об этом не знает.
Дельта же адресна — расходится только то, что он трогал, и ровно об этом ему и
сообщают.

БАЙТ-В-БАЙТ. Разбор на элементы не должен был изменить ни одного промпта: тексты
здесь — те же самые, а `framework.build_prompt` собирает их в прежнем порядке.
Проверяется эталонами в `tests/golden/methodology/` (снимок ДО разбора).

ГДЕ ЛЕЖИТ БАЗА (с 09.08.2026). Не здесь. Тексты переехали в файлы
`station-skills/aifa-analyze/method/**.md`, а этот модуль стал ЗАГРУЗЧИКОМ.
Причина простая: пока база была списком `Element(...)` в питоне, поправить
правило мог только разработчик и только выпуском версии. Теперь методолог
правит текстовый файл, и следующий прогон читает новое — кэш ключуется
временем правки файлов, а не «один раз за жизнь процесса».

ПРО ФОРМАТ — С ПОПРАВКОЙ, КОТОРУЮ СТОИТ ПРОЧИТАТЬ. Сначала здесь был YAML, и
обоснование звучало так: «у методики есть поля, а тексты обязаны переноситься
побайтно; markdown такие пробелы съедает». Утверждение было завышенным.
Markdown как формат ничего не съедает — съедает РАЗБОР, а его пишем мы сами;
разбор просто не был написан. Настоящая трудность была одна: границы куска
между заголовками надо задать явно, иначе ведущий и хвостовой переводы строк
(а они значимы — промпт склеивается конкатенацией) зависят от того, сколько
пустых строк методолог случайно оставил.

Цена YAML вскрылась в тот же день: загрузчик потянул `pyyaml`, которого нет в
песочном наборе библиотек станции, и КАЖДЫЙ анализ падал `ModuleNotFoundError`
после того, как все сторожа доставки отчитались зелёным. Библиотека ради чтения
собственных текстов — лишнее место, где версия обязана совпасть.

Теперь markdown и ноль сторонних библиотек, а трудность с краями снята прямо:
значимые переводы строк объявляются полем `newline`, а не набранными пробелами.
Что переезд ничего не изменил, доказано теми же 12 эталонами побайтно.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path

# ─────────────────────────── единица методики ───────────────────────────


@dataclass(frozen=True)
class Element:
    """Адресуемый кусок методики.

    `id` — стабилен и переживает переформулировки текста: именно на него ссылается
    правка пользователя. Переименование id = потеря якоря, поэтому id не меняем,
    даже если текст переписан целиком.
    """

    id: str
    title: str  # человекочитаемое имя — по нему адресуют словами и показывают в UI
    text: str
    kind: str = "rule"  # rule | formula | section | definition
    bullet: str = "•"  # «•» — пункт верхнего уровня, «–» — вложенный
    append: tuple[str, ...] = ()  # тексты этих элементов дописываются в конец
    # Ярлык роли раздела в КАРТОЧКЕ анализа (#148). До него карточка узнавала
    # блоки по русским ЗАГОЛОВКАМ, а заголовок — обычный текст, который
    # пользователь вправе переписать: переименовал «Вердикт» → блок карточки
    # молча исчезал, при безупречном отчёте и без единой ошибки. Ярлык
    # переживает переименование, потому что связан с id, а не со словами.
    role: str = ""  # verdict | kpi | summary | conclusions | "" (свободный раздел)
    # Почему этот элемент НЕЛЬЗЯ править (#148). Не пометка «осторожно», а
    # механизм: правка защищённого элемента ОТБИВАЕТСЯ этой самой строкой.
    # Защищаем ровно то, точные слова чего читает ПРОГРАММА: сменил их —
    # ломается разбор, и ни одна проверка об этом не скажет.
    protected: str = ""
    # Показатель реестра, который этот текст ОПИСЫВАЕТ (#146). До появления этой
    # ссылки текст методики и формула реестра были двумя независимыми правдами
    # об одной величине: Cost/Income в тексте — «расходы ÷ доход», в реестре —
    # «доход ÷ расходы», и оба уезжали в один промпт как равно-авторитетные.
    # Связь держит `tests/test_methodology_registry_parity.py`: описали формулу
    # текстом — она обязана существовать в реестре и считаться в ту же сторону.
    registry_enum: str = ""


class MethodologyError(ValueError):
    """Правка ссылается в пустоту или противоречит базе — говорим, а не молчим."""


# ──────────────── база: ФАЙЛЫ методики, а не питон-константы ────────────────
#
# ЗАЧЕМ ПЕРЕЕХАЛИ (09.08.2026). Пока база лежала списком `Element(...)` прямо
# здесь, поправить правило мог только разработчик и только выпуском новой
# версии. Требование владельца — методолог правит текст сам. Поэтому тексты
# живут в `station-skills/aifa-analyze/method/**.yaml`, а этот модуль их читает.
#
# ⚠️ НАПРАВЛЕНИЕ ВАЖНО. Файлы — ИСТОЧНИК, питон — загрузчик. Обратное
# («генерируем файлы из питона») выглядит так же аккуратно, но оставляет
# источник в коде, и правка методолога по-прежнему требует разработчика.
#
# ПОЧЕМУ ТАМ, А НЕ РЯДОМ С ПАКЕТОМ. Методику читает станция, а в станцию едет
# полка — архив каталогов `src/finengine` и `station-skills`. Файл, положенный
# в любое третье место, до станции физически не доедет.
#
# ПОЧЕМУ КЭШ ПО ВРЕМЕНИ ПРАВКИ, А НЕ НАВСЕГДА. Кэш «один раз за жизнь процесса»
# у нас уже был на полке станции и стоил шрама #155: правку доставили, а прогон
# считал по старому, при этом паспорт называл новый отпечаток. Здесь ключ кэша —
# самое позднее время правки файлов: поправил текст, следующий прогон читает
# новое, перезапуск не нужен.

_METHOD_ENV = "FE_METHOD_DIR"


def _method_root() -> Path:
    """Каталог с файлами методики. Не нашли — отказ, а не тихая пустота."""
    env = os.environ.get(_METHOD_ENV, "").strip()
    if env:
        p = Path(env)
        if (p / "_order.md").is_file():
            return p
        raise MethodologyError(
            f"{_METHOD_ENV}={env!r}, но файла методики _order.md там нет")
    # `<...>/finengine/skills/analysis/methodology.py` → `<...>` = корень пакетов.
    pkg_root = Path(__file__).resolve().parents[3]
    candidates = (
        # Репозиторий и контейнер бэкенда: пакеты в `src/`, рядом `station-skills/`.
        pkg_root.parent / "station-skills" / "aifa-analyze" / "method",
        # Станция: полка распакована так, что рядом с `finengine` лежит
        # `aifa_station_skills` (имя арк-каталога задаёт station_driver).
        pkg_root / "aifa_station_skills" / "aifa-analyze" / "method",
    )
    for c in candidates:
        if (c / "_order.md").is_file():
            return c
    raise MethodologyError(
        "не найдены файлы методики (искали: "
        + ", ".join(str(c) for c in candidates)
        + f"). Задать явно: {_METHOD_ENV}=<каталог>")


_FILES = ("shared/common.md", "profiles/non_financial.md",
          "profiles/bank.md", "prompt/frame.md")

# ПРИЗМЫ (канон §3): кредитный, риск-, факторный анализ, план-факт. Их состав
# заводит методолог, добавляя файл, а не разработчик, правя `_FILES`.
#
# ⚠️ ГЛОБ УЗКИЙ СОЗНАТЕЛЬНО. Соблазн заменить `_FILES` общим сканированием
# каталога велик и он неверен: тогда источником методики становится ЛЮБОЙ .md,
# случайно оставленный рядом, — черновик, README, копия перед правкой. Тексты
# уезжают в промпт, и заметить это можно только по отчёту.
_LENS_GLOB = "lens/*.md"


def _method_files(root: Path) -> tuple[str, ...]:
    """Файлы методики: постоянный костяк + призмы, сколько бы их ни лежало."""
    lens = sorted(p.relative_to(root).as_posix() for p in root.glob(_LENS_GLOB))
    return (*_FILES, *lens)


def _fingerprint(root: Path) -> tuple:
    """Отпечаток набора файлов: пути и время последней правки.

    Пути входят в отпечаток не для порядка: он же — ключ кэша, и добавленная
    призма обязана менять его сама, иначе новый файл увидит только рестарт.
    """
    names = (*_method_files(root), "_order.md")
    return tuple((n, (root / n).stat().st_mtime_ns) for n in names)


# ─────────────────────────── разбор markdown ───────────────────────────
#
# ПОЧЕМУ MARKDOWN, А НЕ YAML (переезд 09.08.2026, второй за день). Первый заход
# положил методику в YAML — ради полей и точных краёв текста. Цена вскрылась в
# тот же день на живом прогоне: загрузчик потянул `pyyaml`, которого нет в
# песочном наборе библиотек станции, и КАЖДЫЙ анализ падал `ModuleNotFoundError`
# уже после того, как все сторожа доставки отчитались зелёным. Библиотека ради
# чтения собственных текстов — это третье место, где версия обязана совпасть.
#
# Здесь ни одной сторонней библиотеки: формат разбирается двумя правилами.
#   • Элемент начинается строкой «## <id>».
#   • Дальше идут поля «ключ: значение» — до первой ПУСТОЙ строки.
#   • Всё после пустой строки и до следующего «## » — текст элемента.
#
# КРАЯ ТЕКСТА объявляются полем `newline`, а не пробелами в файле. Это не
# формальность: у 13 элементов из 63 ведущий или хвостовой перевод строки
# значим (промпт склеивается конкатенацией), и в YAML он задавался индикаторами
# блока — то есть лишний пробел в файле молча менял промпт. Теперь наоборот:
# что методолог набрал по краям, роли не играет, а значимые края видны словом.

_FIELDS = ("title", "kind", "bullet", "role", "registry_enum", "append",
           "newline", "protected")
_FIELD_LINE = re.compile(r"^(" + "|".join(_FIELDS) + r"):[ ]?(.*)$")
_EDGE = {"leading": ("\n", ""), "trailing": ("", "\n"), "both": ("\n", "\n")}


def _parse_elements(text: str, where: str) -> list[Element]:
    """«## id» + поля + текст → элементы. Непонятное — отказ, а не пропуск."""
    out: list[Element] = []
    blocks = re.split(r"(?m)^##[ ]+", text)[1:]   # до первого «## » — пояснение
    for block in blocks:
        lines = block.split("\n")
        eid = lines[0].strip()
        fields: dict[str, str] = {}
        i = 1
        for i in range(1, len(lines)):           # noqa: B007 — i нужен после цикла
            if not lines[i].strip():
                break
            m = _FIELD_LINE.match(lines[i])
            if not m:
                raise MethodologyError(
                    f"{where}, элемент «{eid}»: строка {lines[i]!r} не поле и не "
                    f"текст. Поля идут сразу за заголовком и кончаются ПУСТОЙ "
                    f"строкой; известные: {', '.join(_FIELDS)}")
            fields[m.group(1)] = m.group(2)
        else:
            raise MethodologyError(
                f"{where}, элемент «{eid}»: нет пустой строки после полей — "
                f"непонятно, где кончаются поля и начинается текст")
        body = "\n".join(lines[i + 1:]).strip("\n")
        edge = fields.get("newline", "")
        if edge and edge not in _EDGE:
            raise MethodologyError(
                f"{where}, элемент «{eid}»: newline={edge!r}; "
                f"допустимо: {', '.join(_EDGE)}")
        lead, trail = _EDGE.get(edge, ("", ""))
        out.append(Element(
            id=eid,
            title=fields.get("title", ""),
            text=lead + body + trail,
            kind=fields.get("kind", "rule"),
            bullet=fields.get("bullet", "•"),
            append=tuple(x.strip() for x in fields.get("append", "").split(",") if x.strip()),
            role=fields.get("role", ""),
            protected=fields.get("protected", ""),
            registry_enum=fields.get("registry_enum", ""),
        ))
    return out


def _parse_order(text: str) -> dict[str, list[str]]:
    """«## группа» + строки «- id» → {группа: [id]}."""
    out: dict[str, list[str]] = {}
    for block in re.split(r"(?m)^##[ ]+", text)[1:]:
        lines = block.split("\n")
        out[lines[0].strip()] = [ln[2:].strip() for ln in lines[1:]
                                 if ln.startswith("- ") and ln[2:].strip()]
    return out


@lru_cache(maxsize=4)
def _read(root_str: str, _fp: tuple) -> tuple[dict, dict, dict, tuple, dict]:
    """Прочитать базу с диска. Ключ кэша включает отпечаток — правка видна сразу.

    Состав файлов берётся ИЗ ОТПЕЧАТКА, а не считается заново: иначе кэш
    ключевался бы одним списком, а читался другой, и призма, добавленная между
    двумя вызовами, попала бы в промпт мимо ключа.
    """
    root = Path(root_str)
    by_id: dict[str, Element] = {}
    for rel, _mtime in _fp:
        if rel == "_order.md":
            continue
        for el in _parse_elements((root / rel).read_text(encoding="utf-8"), rel):
            if el.id in by_id:
                raise MethodologyError(f"элемент «{el.id}» объявлен дважды ({rel})")
            by_id[el.id] = el
    order = _parse_order((root / "_order.md").read_text(encoding="utf-8"))
    # Порядок каталога задаётся файлом: он же — порядок показа методологу.
    # Элемент, забытый в списке, попадает в конец, а не пропадает.
    seq = [i for i in order.get("catalogue", ()) if i in by_id]
    seq += [i for i in by_id if i not in set(seq)]
    elements = {i: by_id[i] for i in seq}
    framework = {tuple(k.split(": ", 1)[1].split("|", 1)): tuple(v)
                 for k, v in order.items() if k.startswith("framework: ")}
    analytical = {k.split(": ", 1)[1]: tuple(v)
                  for k, v in order.items() if k.startswith("analytical: ")}
    # Призма объявляется ДВУМЯ группами: «lens: <имя>» — её правила (дописываются
    # к аналитической глубине ветки), «framework: <имя>|<глубина>» — её разделы
    # (встраиваются в каркас). Вторая группа разбирается тем же кодом, что и
    # каркасы отрасли: у неё та же форма и та же строгость.
    lens = {k.split(": ", 1)[1]: tuple(v)
            for k, v in order.items() if k.startswith("lens: ")}
    comparability = tuple(order.get("comparability", ()))
    _validate(elements, framework, analytical, comparability, lens)
    return elements, framework, analytical, comparability, lens


def _validate(elements, framework, analytical, comparability, lens) -> None:
    """Порядок ссылается только на существующие элементы.

    Опечатка в `_order.md` иначе выкинула бы раздел из отчёта МОЛЧА — то есть
    методолог правил бы файл, а отчёт худел без единого слова.
    """
    refs = set(comparability)
    for ids in framework.values():
        refs |= set(ids)
    for ids in analytical.values():
        refs |= set(ids)
    for ids in lens.values():
        refs |= set(ids)
    for e in elements.values():
        refs |= set(e.append)
    missing = sorted(refs - set(elements))
    if missing:
        raise MethodologyError(
            "в _order.md названы несуществующие элементы: " + ", ".join(missing))


def _load() -> tuple[dict, dict, dict, tuple, dict]:
    root = _method_root()
    return _read(str(root), _fingerprint(root))

ANALYTICAL_HEADER = "АНАЛИТИЧЕСКАЯ ГЛУБИНА (обязательно, даже кратко):\n"
COMPARABILITY_HEADER = "СОПОСТАВИМОСТЬ ПЕРИОДОВ (проверь ПЕРЕД сравнением и коэффициентами):\n"

# Куда пользователь может добавить своё правило. Ключ — то, что он назовёт словами
# («в анализ банка», «в сопоставимость»), значение — список, куда встраивается id.
GROUPS = ("analytical.bank", "analytical.non_financial", "comparability")


# ────────────────────────── эффективная методика ──────────────────────────


@dataclass(frozen=True)
class Methodology:
    """База + правки пользователя, уже разрешённые в готовый к сборке вид."""

    elements: dict[str, Element]
    framework: dict[tuple[str, str], tuple[str, ...]]
    analytical: dict[str, tuple[str, ...]]
    comparability: tuple[str, ...]
    # Правила призм: имя призмы → id правил. Разделы призмы живут в `framework`
    # под ключом (имя призмы, глубина) — той же формы, что и каркасы отрасли.
    lens: dict[str, tuple[str, ...]] = field(default_factory=dict)
    changes: tuple[dict, ...] = ()  # что именно поменял пользователь — для провенанса
    notices: tuple[dict, ...] = ()  # база разошлась с той, против которой правили
    # Сами применённые дельты. Нужны, чтобы переслать методику в станцию: там она
    # пересобирается из ТОЙ ЖЕ базы теми же дельтами, а не приезжает копией
    # текстов — копия разъехалась бы с базой при первом же нашем исправлении.
    applied: tuple[dict, ...] = ()

    def element(self, eid: str) -> Element:
        el = self.elements.get(eid)
        if el is None:
            raise MethodologyError(f"нет элемента методики «{eid}»")
        return el

    def text_of(self, eid: str) -> str:
        el = self.element(eid)
        return el.text + "".join(self.element(a).text for a in el.append)


def base() -> Methodology:
    """Методика в поставке — без чьих-либо правок, прочитанная из файлов."""
    elements, framework, analytical, comparability, lens = _load()
    return Methodology(
        elements=dict(elements),
        framework=dict(framework),
        analytical=dict(analytical),
        comparability=comparability,
        lens=dict(lens),
    )


def _group_list(m: Methodology, group: str) -> tuple[str, ...]:
    if group == "comparability":
        return m.comparability
    kind, _, ct = group.partition(".")
    if kind == "analytical" and ct in m.analytical:
        return m.analytical[ct]
    raise MethodologyError(
        f"нет группы «{group}»; доступны: {', '.join(GROUPS)}"
    )


def _with_group(m: Methodology, group: str, ids: tuple[str, ...]) -> Methodology:
    if group == "comparability":
        return replace(m, comparability=ids)
    ct = group.partition(".")[2]
    analytical = dict(m.analytical)
    analytical[ct] = ids
    return replace(m, analytical=analytical)


def apply(deltas: list[dict], m: Methodology | None = None, *, strict: bool = True) -> Methodology:
    """Наложить правки пользователя на базу.

    `strict=True` (создание правки) — FAIL-CLOSED: правка, потерявшая якорь (id,
    которого в базе больше нет), это ОШИБКА, а не тихий пропуск. Молча
    проигнорировать значило бы считать по базовой методике, пока пользователь
    уверен, что считают по его.

    `strict=False` (прогон) — потерянный якорь не роняет анализ: мы сами
    переписали базу, ронять из-за этого чужую работу нельзя. Правка не
    применяется, но уезжает в `notices` и оттуда в провенанс карточки, чтобы
    потеря была ВИДНА, а не растворилась.

    Расхождение базы (текст правила у нас изменился, правка на месте) — не ошибка
    ни в одном режиме: правка применяется, а в `notices` уезжает пара «на чём
    правил / что стало», чтобы предложить смёржить наше обновление.
    """
    m = m or base()
    changes: list[dict] = []
    notices: list[dict] = []
    applied: list[dict] = []

    for d in deltas or []:
        try:
            m, change, note = _apply_one(m, d)
        except MethodologyError as e:
            if strict:
                raise
            notices.append({
                "kind": "lost_anchor", "id": str(d.get("id") or ""),
                "op": str(d.get("op") or ""), "problem": str(e),
                "yours": str(d.get("text") or ""),
            })
            continue
        changes.append(change)
        applied.append(dict(d))
        if note:
            notices.append(note)

    return replace(m, changes=tuple(changes), notices=tuple(notices),
                   applied=tuple(applied))


def _guard_protected(m: Methodology, eid: str, op: str) -> None:
    """Неприкосновенный элемент — правка ОТБИВАЕТСЯ, а не помечается (#148).

    Текст правки сочиняет модель из реплики в чате, и ложится он на диск во все
    будущие анализы, без диффа пользователю. Там, где от точных слов зависит
    ПРОГРАММА, одна неудачная переформулировка ломает разбор молча: отчёт
    остаётся безупречным, а карточка обнуляется. Поэтому здесь отказ с причиной,
    а не предупреждение: предупреждение — это пометка, а не механизм.
    """
    el = m.elements.get(eid)
    if el is None or not el.protected:
        return
    what = "выключить" if op == "disable" else "переписать"
    raise MethodologyError(
        f"«{el.title}» {what} нельзя: {el.protected}. "
        f"Скажите, ЧТО должно измениться в разборе, — поправим то правило, "
        f"которое за это отвечает."
    )


def _apply_one(m: Methodology, d: dict) -> tuple[Methodology, dict, dict | None]:
    """Одна правка → (новая методика, что изменилось, уведомление или None)."""
    op = str(d.get("op") or "").strip()
    eid = str(d.get("id") or "").strip()
    note: dict | None = None
    _guard_protected(m, eid, op)

    if op == "replace":
        cur = m.elements.get(eid)
        if cur is None:
            raise MethodologyError(
                f"правка ссылается на «{eid}», которого нет в методике — "
                "он был переименован или удалён; выберите элемент заново"
            )
        new_text = str(d.get("text") or "").strip()
        if not new_text:
            raise MethodologyError(f"правка «{eid}» пустая — нечего применять")
        was = d.get("base_was")
        if was is not None and was != cur.text:
            note = {
                "id": eid, "title": cur.title, "kind": "base_moved",
                "base_was": was, "base_now": cur.text, "yours": new_text,
            }
        els = dict(m.elements)
        els[eid] = replace(cur, text=new_text)
        m = replace(m, elements=els)
        change = {"id": eid, "title": cur.title, "op": "replace", "why": d.get("why") or ""}

    elif op == "disable":
        if eid not in m.elements:
            raise MethodologyError(
                f"нельзя выключить «{eid}» — такого элемента в методике нет"
            )
        title = m.elements[eid].title
        analytical = {ct: tuple(i for i in ids if i != eid)
                      for ct, ids in m.analytical.items()}
        framework = {k: tuple(i for i in ids if i != eid)
                     for k, ids in m.framework.items()}
        m = replace(m, analytical=analytical, framework=framework,
                    comparability=tuple(i for i in m.comparability if i != eid))
        change = {"id": eid, "title": title, "op": "disable", "why": d.get("why") or ""}

    elif op == "add":
        if not eid.startswith("user."):
            raise MethodologyError(
                f"свой элемент должен иметь id с префиксом «user.» (получено «{eid}») — "
                "иначе он столкнётся с элементом базовой методики"
            )
        if eid in m.elements:
            raise MethodologyError(f"элемент «{eid}» уже есть — используйте replace")
        group = str(d.get("group") or "").strip()
        ids = _group_list(m, group)
        el = Element(
            id=eid,
            title=str(d.get("title") or eid),
            text=str(d.get("text") or "").strip(),
            kind=str(d.get("kind") or "rule"),
        )
        if not el.text:
            raise MethodologyError(f"элемент «{eid}» пустой — нечего добавлять")
        after = str(d.get("after") or "").strip()
        if after and after not in ids:
            raise MethodologyError(
                f"нельзя поставить после «{after}» — его нет в группе «{group}»"
            )
        pos = ids.index(after) + 1 if after else len(ids)
        els = dict(m.elements)
        els[eid] = el
        m = _with_group(replace(m, elements=els), group, ids[:pos] + (eid,) + ids[pos:])
        change = {"id": eid, "title": el.title, "op": "add", "why": d.get("why") or ""}

    else:
        raise MethodologyError(
            f"неизвестная правка «{op}»; допустимы: replace, add, disable"
        )

    return m, change, note


# ──────────────────────────────── сборка блоков ────────────────────────────────


def _bullets(m: Methodology, header: str, ids: tuple[str, ...]) -> str:
    out = [header]
    for eid in ids:
        el = m.element(eid)
        prefix = "• " if el.bullet == "•" else "  – "
        out.append(f"{prefix}{m.text_of(eid)}\n")
    return "".join(out)


# Раздел, которым отчёт кончается всегда. Разделы призмы встраиваются ПЕРЕД
# ним: выводы обязаны стоять последними, иначе призма отодвинет их в середину
# отчёта и читатель найдёт вердикт где угодно, но не в конце.
_CONCLUSIONS_ID = "common.sec.conclusions"


def lens_ids(m: Methodology, names: tuple[str, ...], group: str) -> tuple[str, ...]:
    """Id элементов запрошенных призм. Неизвестная призма — ОТКАЗ, а не пропуск.

    `group` — «rules» (правила к аналитической глубине) или «sections|<глубина>»
    (разделы каркаса). Пустой список у известной призмы законен: у неё может не
    быть, скажем, своих правил при наличии разделов.

    Молчаливый пропуск здесь означал бы отчёт БЕЗ кредитной призмы, внешне
    неотличимый от отчёта с ней: те же разделы, тот же тон, просто нет того,
    ради чего призму просили.
    """
    out: list[str] = []
    for name in names:
        key = str(name).strip()
        if not key:
            continue
        if key not in m.lens:
            raise MethodologyError(
                f"нет призмы «{key}»; объявлены: "
                + (", ".join(sorted(m.lens)) or "ни одной"))
        if group == "rules":
            out.extend(m.lens[key])
        else:
            out.extend(m.framework.get((key, group.partition("|")[2])) or ())
    return tuple(dict.fromkeys(out))


def analytical_block(m: Methodology, company_type: str,
                     available_enums: set[str] | None = None,
                     lens: tuple[str, ...] = ()) -> str:
    """Блок аналитической глубины. `available_enums` — показатели, которые в
    ЭТОМ прогоне реально посчитаны кодом и лежат в таблице заземления.

    ЗАЧЕМ ФИЛЬТР (#151, решение владельца 02.08.2026). Определения коэффициентов
    заявлены словами «по этой базе они посчитаны кодом в таблице заземления». У
    индийской банковской ячейки Cost/Income, NIM и стоимости риска в реестре
    НЕТ — то есть по индийскому банку эта фраза была прямой неправдой: модель
    получала определения величин, которых в таблице нет, и одновременно запрет
    считать самой. Приходилось нарушать одно из двух.

    Фильтруем не по имени ячейки, а по факту наличия показателя: заведёт команда
    формулы — определения вернутся сами, без правки этого кода. Связь держит
    `Element.registry_enum` (#146); элементы без неё не фильтруются никогда —
    это правила, а не определения.
    """
    ct = "bank" if company_type == "bank" else "non_financial"
    # Правила призмы идут ПОСЛЕ правил отрасли: отрасль задаёт, как читать
    # компанию, призма — на что смотреть дополнительно, и порядок этот же.
    ids = m.analytical[ct] + tuple(
        i for i in lens_ids(m, lens, "rules") if i not in m.analytical[ct])
    if available_enums is not None:
        have = {str(e).strip().upper() for e in available_enums}
        ids = tuple(
            eid for eid in ids
            if not m.element(eid).registry_enum
            or m.element(eid).registry_enum.strip().upper() in have
        )
    return _bullets(m, ANALYTICAL_HEADER, ids)


def comparability_block(m: Methodology) -> str:
    return _bullets(m, COMPARABILITY_HEADER, m.comparability)


def framework_block(m: Methodology, company_type: str, depth: str,
                    lens: tuple[str, ...] = ()) -> str:
    ct = "bank" if company_type == "bank" else "non_financial"
    ids = m.framework.get((ct, depth)) or m.framework[(ct, "quick")]
    extra = tuple(i for i in lens_ids(m, lens, f"sections|{depth}") if i not in ids)
    if extra:
        # Разделы призмы встраиваются в каркас ветки перед выводами (см.
        # `_CONCLUSIONS_ID`). Нет выводов в каркасе — дописываем в конец: это
        # значит, что каркас переписан методологом, и вставлять «перед тем,
        # чего нет» было бы догадкой.
        cut = ids.index(_CONCLUSIONS_ID) if _CONCLUSIONS_ID in ids else len(ids)
        ids = ids[:cut] + extra + ids[cut:]
    return "\n".join(
        f"{i}. **{m.element(eid).title}** — {m.text_of(eid)}"
        for i, eid in enumerate(ids, 1)
    )


def catalogue(m: Methodology | None = None) -> list[dict]:
    """Все адресуемые элементы — для показа пользователю и выбора агентом.

    Без этого агент не может сослаться на правило иначе как пересказом, а
    пользователь — увидеть, что вообще подлежит настройке.
    """
    m = m or base()
    where: dict[str, list[str]] = {}
    for ct, ids in m.analytical.items():
        for eid in ids:
            where.setdefault(eid, []).append(f"анализ ({ct})")
    for eid in m.comparability:
        where.setdefault(eid, []).append("сопоставимость")
    for (ct, depth), ids in m.framework.items():
        for eid in ids:
            where.setdefault(eid, []).append(f"каркас {ct}/{depth}")
    return [
        {"id": el.id, "title": el.title, "kind": el.kind,
         "used_in": where.get(el.id, []), "text": el.text,
         # Видно ДО попытки: агент не предлагает пользователю правку, которая
         # заведомо отобьётся, и не выдаёт отказ за собственную неудачу.
         "protected": el.protected, "card_role": el.role}
        for el in m.elements.values()
        if where.get(el.id) or el.id in {a for e in m.elements.values() for a in e.append}
    ]


# Роли разделов, из которых собирается карточка анализа. Человеческие имена —
# для сообщения пользователю о последствии («карточка потеряет блок …»).
CARD_ROLE_TITLES = {
    "verdict": "Вердикт",
    "kpi": "Ключевые показатели",
    "summary": "Сильные стороны / Зоны внимания",
    "conclusions": "Выводы",
}


def card_role_of(m: Methodology, eid: str) -> str:
    el = m.elements.get(eid)
    return el.role if el else ""
