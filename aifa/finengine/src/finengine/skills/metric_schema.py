"""Effective metric registries compiled from versioned data layers.

Slice 13 replaces complete per-cell copies with deterministic composition::

    reporting standard -> company type/industry -> language -> customer

The compiled result keeps the legacy runtime shape consumed by extraction and
calculation, while ``registry_composition`` retains source/version provenance
for every effective field and freezes the exact version in ``RunPassport``.
The built-in source is ``src/finengine/data/registry_layers/manifest.json``.

The effective envelope remains::

    {"extractable": [{enum, name_ru, name_en, aliases_ru | aliases_en (по языку
                      ячейки), description, hint, standard, value_basis,
                      period_type, formula_default_abs_behavior}, ...],
     "calculated":  [{enum, name_ru, formula, deps}, ...]}

``metrics.json`` stays as the registry for the **non-cell** arches (consensus
/ AIFA baseline / role scope); it does not regenerate built-in layers.

РЕЕСТР — ДАННЫЕ, А НЕ КОД (#153)
================================
Файлы ячеек лежат ВНУТРИ пакета, поэтому любая правка методолога до #153 стоила
сборки образа и рестарта: обещание «правится текстом» работало для методики
анализа и не работало для 77 показателей и полусотни формул. Теперь путь к базе
задаётся настройкой ``FINENGINE_REGISTRY_DIR``. Внешний каталог обязан быть
тем же versioned layer set с ``manifest.json``; полного ``<ячейка>.json``
compatibility path больше нет. Поставка по умолчанию состоит только из слоёв.

БЕЗ РЕСТАРТА. Разбор кэшируется, но кэш держится на ОТПЕЧАТКЕ файла (путь +
mtime_ns + размер), а не на «загрузили один раз за жизнь процесса»: правка
внешнего файла подхватывается следующим же прогоном. Отпечаток пересчитывается
на КАЖДЫЙ вызов — это один ``stat``. Хэш содержимого не берём: он стоит чтения
всего файла, то есть ровно того, чего кэш и должен избежать; mtime_ns вместе с
размером промахивается только на правке той же длины внутри одной наносекунды.

Замер 02.08.2026 на локальном диске (ifrs_nf_ru, 88 КБ): разбор+валидация
0,46 мс, ``stat`` 0,002 мс, копия результата 0,40 мс. То есть НА ЛОКАЛЬНОМ ДИСКЕ
кэш почти ничего не экономит — копию всё равно приходится отдавать (см. ниже), и
выигрыш съедается ею. Кэш оставлен потому, что каталог из настройки — это, по
замыслу задачи, ВНЕШНИЙ том (bind/NFS-монтирование): там ``stat`` остаётся
дешёвым, а чтение 88 КБ дешёвым быть перестаёт. Если реестр так и останется
жить на локальном диске, кэш можно снять — правка без рестарта от него не
зависит, разбор на каждый вызов даёт то же самое.

СЛОМАННЫЙ ВНЕШНИЙ ФАЙЛ — ОТКАЗ, А НЕ ОТКАТ НА ВСТРОЕННЫЙ. Откат выглядел бы
успешным прогоном: таблица собралась, числа правдоподобны — и только состав
показателей молча не тот, который методолог только что опубликовал. Это ровно
класс молчаливых поломок, с которым борется репозиторий (#137), а лог такой
подмены увидит кто угодно, кроме автора правки. Поэтому ``RegistryError`` с
именем файла, строкой и причиной. Отказ ЛОКАЛЕН: он поднимается из вызова, а не
из импорта, ломается только эта ячейка, процесс живёт, предыдущая версия файла
возвращается откатом правки. ``RegistryError`` наследует ``ValueError`` — старые
обработчики продолжают ловить; наследовать ``FileNotFoundError`` НЕЛЬЗЯ: на нём
у вызывающих висит фоллбэк «файла ячейки нет → реестр по типу компании», и
сломанный файл уехал бы в этот самый молчаливый откат.

Строго проверяется и КОНВЕРТ файла, а не только строки (``RegistryFile``):
опечатка в имени ключа верхнего уровня теряет молча не одну подсказку, а
половину реестра целиком.

ТИХИХ ОТКАТОВ НЕТ НИ ОДНОГО. Три способа промахнуться мимо внешнего реестра
дают три РАЗНЫХ громких ответа: файл сломан → отказ (``RegistryError``);
каталога нет → работаем на встроенных + warning; файл в каталоге назван не как
ячейка → работаем на встроенных + warning про этот файл. Warning'и
ПОВТОРЯЮТСЯ раз в ``_WARN_REPEAT_SEC``: одна строка за жизнь процесса читается
как разовый сбой, а не как «реестр не читается до сих пор».
"""
from __future__ import annotations

import copy
import logging
import time
from pathlib import Path
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

log = logging.getLogger(__name__)

_BUILTIN_REGISTRY_DIR = Path(__file__).resolve().parent.parent / "data" / "registry"
_BUILTIN_LAYER_DIR = Path(__file__).resolve().parent.parent / "data" / "registry_layers"

PROFILE_IDS: tuple[str, ...] = (
    "ifrs_nf_ru",
    "ifrs_bank_ru",
    # РСБУ: российская бухгалтерская отчётность. Перенесена из форк-базы
    # 15.08.2026 — до этого отчёт по формам 0710001/0710002 попадал в ячейку
    # МСФО, где ни кодов строк, ни правила «прочерк в составной строке ≠ ноль»
    # нет, и извлечение шло по чужим подсказкам.
    "rsbu_nf_ru",
    "rsbu_bank_ru",
    "us_gaap_nf_en",
    "us_gaap_bank_en",
    "ifrs_nf_en",
    "ifrs_bank_en",
    # Индийские стандарты: Ind AS (нефинансовые) и Indian GAAP (банки).
    # Реестры перенесены из FinEngine — до этого отчёты по ним попадали в
    # ячейку по умолчанию и роль работала не по своему набору показателей.
    "ind_as_nf_en",
    "ind_gaap_bank_en",
)

VALID_PERIOD_TYPES = {"", "point", "duration"}
# Из каких документов показатель вообще можно брать. Пришло с реестрами РСБУ
# (71 запись из 111) — набор и значение по умолчанию те же, что в форк-базе.
VALID_METRIC_DOCUMENT_TYPES = {
    "financial_report", "press_release", "transcript", "presentation", "equity_research",
}


def _formula_carries_period_length(formula: object) -> bool:
    """Поднимает ли формула величину до года сама — вопрос к ДВИЖКУ, не к схеме.

    Импорт ленивый и другого быть не может: `bulk_extractor` при загрузке
    читает `merge_aliases` из этого же модуля, и обычный импорт наверху файла
    замкнул бы кольцо. Зовётся один раз на строку реестра при загрузке ячейки —
    горячего пути здесь нет.
    """
    from finengine.skills.bulk_extractor.formula_engine import (  # noqa: PLC0415
        formula_carries_period_length,
    )

    return formula_carries_period_length(formula)


class MetricDef(BaseModel):
    """One extractable metric row. ``extra=forbid`` so a typo in a cell file
    (e.g. ``hnit``) fails loudly instead of silently doing nothing."""

    model_config = ConfigDict(extra="forbid")

    enum: str
    name_ru: str = ""
    name_en: str = ""
    aliases_ru: list[str] = Field(default_factory=list)
    aliases_en: list[str] = Field(default_factory=list)
    description: str = ""
    hint: str = ""
    standard: str = ""
    value_basis: str | None = None
    period_type: str = ""
    # Единица ИЗВЛЕКАЕМОГО показателя, когда она известна заранее и не выводится
    # из отчёта: норматив достаточности лежит в примечании уже процентом («15,7»
    # означает 15,7 %), а не в деньгах отчёта. Пусто — единицу называет сам
    # документ, как у всех денежных строк.
    #
    # ЗАЧЕМ ПОЛЕ (21.08.2026). Знание это в реестре БЫЛО — прозой в подсказке
    # («Единица — проценты»), то есть только для модели. Раскладка роли,
    # раскладывающая проценты долей, прочитать прозу не может: строка «CET1, %»
    # печатала 1570 % вместо 15,7 %. Проза остаётся модели, поле — коду.
    unit: str = ""
    formula_default_abs_behavior: bool = False
    # ── Срез 6a (norm.unit) ──
    # Явная подсказка канонической семантики величины, когда её нельзя вывести
    # из уже разобранного текста ячейки/значения. Пусто = norm.unit работает
    # только с тем, что уже дал `bulk_extractor.value_parsing` (масштаб/знак/
    # текст единицы самой ячейки) — поле НЕ обязательно для существования
    # атома, это опциональное переопределение реестра, не новый источник
    # домысливания: заполняется только там, где кто-то реально сверил
    # значение с реестром. Знаковая семантика намеренно НЕ дублируется здесь
    # отдельным полем — `formula_default_abs_behavior` выше уже покрывает
    # ровно то решение о знаке, которое нужно подстановке в формулу
    # (`formula_engine`, см. его докстринг); заводить второе, похожее поле
    # под тем же смыслом было бы дублирующим источником истины.
    unit_kind: str = ""  # "money" | "percent" | "ratio" | "days" | "count" | ""
    scale_hint: str = ""  # "thousand" | "million" | "billion" | ""
    # Отсутствие поля сохраняет доклассификаторное поведение: показатель берут
    # только из финансовой отчётности, прочие источники подключаются явно.
    document_types: list[str] = Field(default_factory=lambda: ["financial_report"])


class CalcDef(BaseModel):
    """One calculated (derived) metric row."""

    model_config = ConfigDict(extra="forbid")

    enum: str
    name_ru: str = ""
    # Поля из реестров FinEngine (в т.ч. индийских ячеек). Схема запрещает
    # лишние ключи, поэтому их отсутствие роняло загрузку всей ячейки —
    # держим тот же набор, что и в источнике.
    name_en: str = ""
    formula: str = ""
    deps: list[str] = Field(default_factory=list)
    hint: str = ""
    role: str = ""
    source: str = ""
    # ── смысл величины, который из формулы не выводится (#146) ──
    # `unit_kind` — подпись единицы, когда структура формулы врёт: сумма дней
    # оборачиваемости выглядит как сумма денег, а «расход ÷ коэффициент» — как
    # безразмерное отношение. Пусто = выводим из формулы (см.
    # `bulk_extractor.unit_for_calculated`). Валюту сюда писать НЕЛЬЗЯ: тот же
    # показатель считается и по us_gaap в долларах, единицу подставляет прогон.
    unit_kind: str = ""
    # `annualize` — привести к году простым множителем 12/N на промежуточной
    # отчётности. Ставится ТОЛЬКО там, где поток делится на остаток (NIM, CoR,
    # рентабельности): у отношения двух потоков множитель сокращается сам.
    annualize: bool = False
    # ── границы правдоподобия, которые ставит АВТОР показателя (Р7, 24.08.2026) ──
    #
    # У реестровых показателей диапазоны заданы прямо в effective registry;
    # Case-local формула объявляет их в том же контракте.
    # Владелец спросил ровно про это: «написала формулу, значение получилось 500
    # миллиардов, и выглядит подозрительно — чтобы она могла себя перепроверить».
    #
    # Ответ в духе остального: границы знает ТОТ, КТО ПРИДУМАЛ показатель.
    # Модель, сочиняя коэффициент, представляет его нормальный размах — пусть и
    # объявит. Пусто — проверки нет, и это законно: не у всякой величины есть
    # осмысленный диапазон.
    min_value: float | None = None
    max_value: float | None = None
    # ── Срез 6a (verify.bounds) ──
    # Мягкая пара РЯДОМ с жёсткой `min_value`/`max_value` — источник данных:
    # перенесённые при Срезе 6a диапазоны 9 банковских коэффициентов, не новые
    # числа и не скрытый список в коде.
    # Жёсткая граница (`min_value`/`max_value`) помечает факт недействительным;
    # эта — только предупреждение, факт публикуется как есть.
    soft_min_value: float | None = None
    soft_max_value: float | None = None

    @model_validator(mode="after")
    def _bounds_make_sense(self) -> "CalcDef":
        """Перевёрнутый диапазон — не придирка: он молча не сработает никогда."""
        if (self.min_value is not None and self.max_value is not None
                and self.min_value > self.max_value):
            raise ValueError(
                f"{self.enum}: min_value {self.min_value} больше max_value "
                f"{self.max_value} — такой диапазон не поймает ничего")
        if (self.soft_min_value is not None and self.soft_max_value is not None
                and self.soft_min_value > self.soft_max_value):
            raise ValueError(
                f"{self.enum}: soft_min_value {self.soft_min_value} больше soft_max_value "
                f"{self.soft_max_value} — такой диапазон не поймает ничего")
        return self

    @model_validator(mode="after")
    def _one_annualization_not_two(self) -> "CalcDef":
        """`annualize` и `ANNUAL(...)` в СВОЕЙ формуле — двойной множитель.

        ДВА РАЗНЫХ МЕХАНИЗМА, И ОБА ПОДНИМАЮТ ДО ГОДА. `ANNUAL(X)` в формуле
        поднимает ПОТОК В ЗНАМЕНАТЕЛЕ (чистый долг / EBITDA), раскрываясь в
        `formula_for_period` ещё до разбора выражения. Признак `annualize`
        поднимает ГОТОВОЕ ЧИСЛО при выдаче — там, где поток стоит в числителе
        (ROE, NIM). Порознь каждый прав; вместе на квартале они дают ×16.

        Молча такую запись пропускать нельзя: сегодня ни в одной из десяти
        ячеек пары нет, но реестр правится ТЕКСТОМ и без программиста, и
        аналитик, дописавший `ANNUAL(...)` показателю с признаком, увидел бы
        не отказ, а правдоподобное число. Отказ на загрузке называет файл и
        показатель — этого хватает, чтобы починить за минуту.

        ⚠️ ГРАНИЦА ОБЕЩАНИЯ. Запись видит ТОЛЬКО СВОЮ формулу: соседних строк
        ячейки у неё нет. Длину периода легко унаследовать через зависимость —
        «денежный цикл = сумма трёх оборачиваемостей», и каждая из трёх несёт
        `DAYS_IN_PERIOD`. Этот случай ловит проверка ячейки целиком
        (`_refuse_annualize_on_top_of_period_length`), и класс дефекта закрыт
        ДВУМЯ проверками, а не одной.
        """
        # Выражения берутся у самого движка: врозь эта проверка и раскрытие
        # формулы уже разошлись (см. `formula_carries_period_length`).
        if self.annualize and _formula_carries_period_length(self.formula):
            raise ValueError(
                f"{self.enum}: признак `annualize` стоит вместе с ANNUAL(...) "
                f"или DAYS_IN_PERIOD в формуле «{self.formula}» — приведение к "
                "году применилось бы дважды (на квартале ×16 вместо ×4). "
                "Оставить ОДИН механизм: признак — когда поток в числителе, "
                "ANNUAL(...) — когда поток в знаменателе")
        return self


class VerificationRule(BaseModel):
    """Одно правило `verify.identity` (Срез 6a, 16a): концепт, для которого
    сверяются извлечённое и расчётное значения, и допустимая относительная
    погрешность. Пустой `registry["verification_rules"]` — законное,
    ожидаемое состояние большинства ячеек сегодня (карточка `verify.identity`
    прямо запрещает придумывать правила, которых нет)."""

    model_config = ConfigDict(extra="forbid")

    tolerance: float = Field(gt=0.0)
    note: str = ""


class RegistryFile(BaseModel):
    """Конверт файла ячейки: два обязательных списка — и ничего больше.

    ЗАЧЕМ. Строку реестра схема проверяла строго (``extra=forbid``), а конверт
    не проверял никто: ``data.get("calculated") or []`` одинаково молча
    принимал и отсутствие ключа, и опечатку в нём. Файл
    ``{"extractable": [...], "calculatd": [...]}`` — опечатка В ОДНУ БУКВУ —
    грузился без исключения и без строки в логе, отдавая реестр БЕЗ расчётных
    показателей: анализ терял все коэффициенты и считал их в уме (#137). Файл
    ``{}`` проходил так же молча, и живой путь собирал состав из НУЛЯ
    показателей. Порча конверта — такая же порча реестра, как порча строки, и
    ответ на неё тот же: ``RegistryError`` (обоснование отказа вместо отката —
    в шапке модуля).

    Оба ключа ОБЯЗАТЕЛЬНЫ, пустой список пишется явно (``"calculated": []``):
    сделать ключ необязательным значило бы вернуть опечатке право пройти
    молча — «ключа нет» и «ключ назван неверно» на входе неразличимы.
    """

    model_config = ConfigDict(extra="forbid")

    extractable: list[dict]
    calculated: list[dict]
    # Комментарий методолога прямо в файле — единственный ключ сверх двух
    # списков; он есть во всех поставляемых файлах ячеек.
    readme: str = Field("", alias="_README")
    # Срез 6a (verify.identity): необязательный ключ, в отличие от
    # extractable/calculated — отсутствие ЦЕЛИКОМ означает «для этой ячейки
    # правил тождества пока нет», легитимное состояние, а не порчу файла.
    verification_rules: dict[str, dict] = Field(default_factory=dict)


class RegistryError(ValueError):
    """Файл реестра есть, но не читается или не проходит схему.

    Отдельный тип, чтобы «файла ячейки нет» (``FileNotFoundError``, законный
    фоллбэк на реестр по типу компании) и «файл сломан» (отказ) не сливались в
    одно исключение: слитые, они дают ровно ту молчаливую подмену, ради
    невозможности которой всё и делалось.
    """


def _company_type_for_profile(profile_id: str) -> str:
    """Cell id carries the company type: ``*_bank_*`` → bank, else NF."""
    return "bank" if "_bank_" in (profile_id or "") else "non_financial"


# Жалобы «настройка задана, а читается не то»: ключ жалобы → момент последней.
# ПОВТОРЯЕМ, а не «один раз за жизнь процесса». Разовая жалоба звучит как
# сообщение о разовом сбое: внешний том может отвалиться на работающем
# процессе, и одна строка с последующей бессрочной тишиной означала бы, что
# реестр не читается ДО СИХ ПОР, а лог об этом молчит. Повтор раз в
# `_WARN_REPEAT_SEC`: чаще — лог превращается в шум, а шум читают так же, как
# молчание; реже — сутки работы не на том реестре без единой строки.
_WARN_REPEAT_SEC = 300.0
_LAST_WARNED: dict[str, float] = {}


def _warn_repeatedly(key: str, message: str, *args: object) -> None:
    """Один и тот же warning не чаще раза в `_WARN_REPEAT_SEC` на ключ."""
    now = time.monotonic()
    last = _LAST_WARNED.get(key)
    if last is not None and now - last < _WARN_REPEAT_SEC:
        return
    _LAST_WARNED[key] = now
    log.warning(message, *args)


def _external_registry_dir() -> Path | None:
    """Внешний каталог реестра из настройки, если он задан И существует."""
    from finengine.config import settings  # noqa: PLC0415

    raw = str(getattr(settings, "registry_dir", "") or "").strip()
    if not raw:
        return None
    directory = Path(raw).expanduser()
    if not directory.is_dir():
        # Настройку задали осознанно — значит, каталога ждали. Промолчать здесь
        # значит отдать встроенный реестр человеку, уверенному, что читают его
        # файлы: опечатка в пути неотличима от «правка не сработала».
        _warn_repeatedly(
            f"нет каталога: {raw}",
            "FINENGINE_REGISTRY_DIR=%s: каталога нет — реестр читается "
            "ВСТРОЕННЫЙ (%s), внешние правки не применяются",
            raw, _BUILTIN_REGISTRY_DIR,
        )
        return None
    return directory


def _warn_about_files_nobody_reads(directory: Path) -> None:
    """Сказать про файлы внешнего каталога, которые не читает никто.

    ЗАЧЕМ. Опечатку в имени КАТАЛОГА мы кричим, а опечатка в имени ФАЙЛА ячейки
    (`ifrs_nf_rru.json`, `ifrs_nf_ru.jsn`) уходила в тот же тихий откат на
    поставку совершенно молча — при том что для методолога она ровно так же
    неотличима от «моя правка не сработала».

    Жалуемся не на «файла ячейки нет» — это законно и обыденно: вынести наружу
    ОДНУ ячейку и оставить семь на поставке — рабочий случай, и warning на
    каждую из семи был бы шумом. Жалуемся на ЛИШНИЙ файл: имя, не совпавшее ни
    с одной ячейкой, в этом каталоге не имеет назначения — либо это опечатка,
    либо мусор, и в обоих случаях сказать о нём дешевле, чем промолчать.

    Каталог перебирается только в этой ветке (файла ячейки нет), то есть ровно
    там, где мы вот-вот молча уйдём на встроенный реестр.
    """
    try:
        stray = sorted(
            p.name for p in directory.iterdir()
            if not p.name.startswith(".") and p.is_file()
        )
    except OSError:
        return  # каталог исчез в гонке — про это скажет следующий вызов
    for name in stray:
        _warn_repeatedly(
            f"лишний файл: {directory / name}",
            "%s: каталог реестра не содержит manifest.json; файл не читает "
            "layer compiler. Правка НЕ применяется, используются встроенные слои",
            directory / name,
        )


def registry_path_for_profile(profile_id: str) -> Path:
    """Canonical layer manifest used for diagnostics."""
    external = _external_registry_dir()
    if external is not None:
        manifest = external / "manifest.json"
        if manifest.is_file():
            return manifest
        _warn_about_files_nobody_reads(external)
    return _BUILTIN_LAYER_DIR / "manifest.json"


def _refuse_annualize_on_top_of_period_length(
    source: str,
    calculated: list[tuple],
    calculated_meta: dict[str, dict],
) -> None:
    """Признак `annualize` НАД показателем, который уже знает длину периода.

    Вторая половина запрета из `CalcDef._one_annualization_not_two`, и без неё
    первая закрывает не класс дефекта, а один его случай. Строка видит только
    свою формулу, а длина периода приезжает ПО ЗАВИСИМОСТИ: «денежный цикл =
    оборачиваемость дебиторки + запасов − кредиторки», у самого цикла в формуле
    ни `ANNUAL(...)`, ни `DAYS_IN_PERIOD` нет, а у всех трёх слагаемых есть.

    ЗАМЕРЕНО (21.08.2026) на `ifrs_nf_ru`, квартал (`DAYS_IN_PERIOD` = 91 день),
    входы 1000/800/250/200/100: поставленный циклу признак даёт 136,5 дня вместо
    34,125 — цикл в полтора раза длиннее самого квартала, и ни слова об ошибке.

    Обход — по тем же именам, которые подставляет вычислитель
    (`_formula_metric_tokens`), и только по РАСЧЁТНЫМ строкам: у извлекаемых
    формулы нет, длине периода взяться неоткуда.
    """
    # Тот же токенизатор, что у вычислителя: свой разбор формулы разошёлся бы с
    # тем, что движок реально подставляет. Импорт ленивый — кольцо, см.
    # `_formula_carries_period_length`.
    from finengine.skills.bulk_extractor.formula_engine import (  # noqa: PLC0415
        _formula_metric_tokens,
    )

    formula_by_enum = {
        str(enum).strip().upper(): str(formula or "")
        for enum, _name, formula, _deps in calculated
    }
    for enum, meta in calculated_meta.items():
        if not meta.get("annualize"):
            continue
        parent: dict[str, str] = {enum: ""}
        queue = [enum]
        while queue:
            current = queue.pop(0)
            for dep in sorted(_formula_metric_tokens(formula_by_enum.get(current, ""))):
                if dep in parent or dep not in formula_by_enum:
                    continue
                parent[dep] = current
                if not _formula_carries_period_length(formula_by_enum[dep]):
                    queue.append(dep)
                    continue
                chain = [dep]
                while parent[chain[-1]]:
                    chain.append(parent[chain[-1]])
                raise RegistryError(
                    f"{source}: у расчётного показателя {enum} стоит признак "
                    f"`annualize`, а длину периода уже знает его зависимость "
                    f"{dep} («{formula_by_enum[dep]}») — путь "
                    f"{' → '.join(reversed(chain))}. Приведение к году "
                    "применилось бы дважды: на квартале ×16 вместо ×4. "
                    "Оставить ОДИН механизм: признак — когда поток в "
                    "числителе, ANNUAL(...)/DAYS_IN_PERIOD — когда в знаменателе"
                )


def registry_from_data(profile_id: str, data: dict[str, Any],
                       source: str = "<данные>") -> dict[str, Any]:
    """Разбор ячейки из УЖЕ ПРОЧИТАННЫХ данных, а не из файла на диске.

    Отделено от чтения ради паспорта прогона (#155). Отпечаток правил обязан
    сниматься с того реестра, который РЕАЛЬНО исполнялся, а исполняется копия из
    полки (`station_driver._build_shelf`), уехавшей в станцию, — не файл на диске
    бэкенда, который к тому моменту мог уже поменяться. Разбирать копию из полки
    вторым, похожим кодом значило бы завести второй ответ на тот же вопрос:
    паспорт называл бы состав, которого движок не видел.

    `source` — что писать в сообщениях об ошибке (путь к файлу или «полка»):
    сообщение должно хватать для правки без чтения кода, а данные сюда приходят
    из разных мест.
    """
    if not isinstance(data, dict):
        raise RegistryError(
            f"{source}: реестр ячейки должен быть объектом с ключами "
            f"extractable/calculated, а не {type(data).__name__}"
        )
    # Конверт проверяется ДО строк и так же строго, как строка: опечатка в
    # имени ключа — это молча потерянная половина реестра (см. RegistryFile).
    # Проверка стоит ЗДЕСЬ, а не только в чтении файла: данные из полки идут
    # мимо `_load_registry`, и оставить их без конверта значило бы вернуть ту же
    # дыру во второй половине пути.
    try:
        envelope = RegistryFile.model_validate(data)
    except ValidationError as exc:
        raise RegistryError(
            f"{source}: конверт реестра не проходит схему — в файле ячейки "
            f"должны быть ровно ключи extractable и calculated "
            f"(пустой список пишется явно) — {exc}"
        ) from exc
    path = source  # сообщения ниже писались с путём; смысл тот же

    extractable: list[dict] = []
    for i, row in enumerate(envelope.extractable):
        try:
            metric = MetricDef(**row)
        except (ValidationError, TypeError) as exc:
            raise RegistryError(
                f"{path}: извлекаемый показатель #{i} "
                f"({(row or {}).get('enum') if isinstance(row, dict) else row!r}) "
                f"не проходит схему — {exc}"
            ) from exc
        if metric.period_type not in VALID_PERIOD_TYPES:
            raise RegistryError(
                f"{path}: {profile_id}/{metric.enum}: bad period_type "
                f"{metric.period_type!r}"
            )
        bad_docs = sorted(set(metric.document_types) - VALID_METRIC_DOCUMENT_TYPES)
        if bad_docs or not metric.document_types:
            raise RegistryError(
                f"{path}: {profile_id}/{metric.enum}: bad document_types "
                f"{bad_docs or 'пусто'}; допустимо: "
                f"{', '.join(sorted(VALID_METRIC_DOCUMENT_TYPES))}"
            )
        extractable.append(metric.model_dump())

    calculated: list[tuple] = []
    # Кортеж расчётов (enum, имя, формула, deps) — форма, которую ждёт весь
    # конвейер, и расширять её пятым полем значило бы править каждого
    # потребителя. Поэтому «смысл величины» едет отдельной картой по enum'у.
    calculated_meta: dict[str, dict] = {}
    for i, row in enumerate(envelope.calculated):
        try:
            calc = CalcDef(**row)
        except (ValidationError, TypeError) as exc:
            raise RegistryError(
                f"{path}: расчётный показатель #{i} "
                f"({(row or {}).get('enum') if isinstance(row, dict) else row!r}) "
                f"не проходит схему — {exc}"
            ) from exc
        calculated.append((calc.enum, calc.name_ru, calc.formula, calc.deps))
        has_bounds = any(
            v is not None
            for v in (calc.min_value, calc.max_value, calc.soft_min_value, calc.soft_max_value)
        )
        if calc.unit_kind or calc.annualize or has_bounds:
            calculated_meta[calc.enum.strip().upper()] = {
                "unit_kind": calc.unit_kind,
                "annualize": calc.annualize,
                # Срез 6a (verify.bounds/calc.formula): жёсткая пара уже
                # существовала (Р7, 24.08.2026), но раньше не покидала схему
                # валидации — ни один потребитель её не читал. Мягкая —
                # данные из того же определения показателя.
                "min_value": calc.min_value,
                "max_value": calc.max_value,
                "soft_min_value": calc.soft_min_value,
                "soft_max_value": calc.soft_max_value,
            }

    _refuse_annualize_on_top_of_period_length(path, calculated, calculated_meta)

    verification_rules: dict[str, dict] = {}
    for concept, raw_rule in envelope.verification_rules.items():
        try:
            rule = VerificationRule(**raw_rule)
        except (ValidationError, TypeError) as exc:
            raise RegistryError(
                f"{path}: verification_rules[{concept!r}] не проходит схему — {exc}"
            ) from exc
        verification_rules[concept.strip().upper()] = rule.model_dump()

    return {
        "extractable": extractable,
        "calculated": calculated,
        "calculated_meta": calculated_meta,
        "verification_rules": verification_rules,
        "company_type": _company_type_for_profile(profile_id),
    }


def registry_for_profile(profile_id: str) -> dict[str, Any]:
    """Return the legacy runtime shape from the compiled effective registry."""
    return _runtime_registry(compiled_registry_for_profile(profile_id))


_RUNTIME_BY_VERSION: dict[str, dict[str, Any]] = {}


def _runtime_registry(compiled: Any) -> dict[str, Any]:
    cached = _RUNTIME_BY_VERSION.get(compiled.version)
    if cached is None:
        cached = registry_from_data(
            compiled.profile_id,
            compiled.to_effective_data(),
            source=f"compiled registry {compiled.version}",
        )
        _RUNTIME_BY_VERSION[compiled.version] = cached
    return copy.deepcopy(cached)


def compiled_registry_for_profile(
    profile_id: str,
    *,
    industry: str = "",
    customer_id: str = "",
) -> Any:
    """Compile one profile from the canonical layer compiler."""
    from finengine.skills.registry_composition import RegistrySelection  # noqa: PLC0415

    if profile_id not in PROFILE_IDS:
        raise FileNotFoundError(_BUILTIN_LAYER_DIR / "manifest.json")
    selection = RegistrySelection.from_profile_id(
        profile_id, industry=industry, customer_id=customer_id
    )
    return _compile_selected_registry(selection)


def _compile_selected_registry(selection: Any) -> Any:
    """Compile exactly the axes already fixed by ``id.frame``."""
    from finengine.skills.registry_composition import freeze_registry  # noqa: PLC0415

    external = _external_registry_dir()
    if external is not None:
        manifest = external / "manifest.json"
        if manifest.is_file():
            return freeze_registry(selection, layer_dir=external)
        _warn_about_files_nobody_reads(external)
    return freeze_registry(selection, layer_dir=_BUILTIN_LAYER_DIR)


def compiled_registry_for_classification(classification: Any) -> Any:
    from finengine.skills.registry_composition import RegistrySelection  # noqa: PLC0415

    return _compile_selected_registry(RegistrySelection.from_classification(classification))


def registry_for_passport(passport: Any) -> dict[str, Any]:
    """Resolve definitions by the exact version frozen in RunPassport."""
    from finengine.skills.registry_composition import registry_for_passport as frozen  # noqa: PLC0415

    return _runtime_registry(frozen(passport))


def validate_all_registries() -> dict[str, int]:
    """Compile + validate all 10 effective profile registries. Returns
    ``{profile_id: extractable_count}``; raises on the first bad row."""
    return {pid: len(registry_for_profile(pid)["extractable"]) for pid in PROFILE_IDS}


def merge_aliases(metric: Mapping[str, Any], limit: int) -> list[str]:
    """Синонимы обоих языков под общим потолком — без вытеснения одного другим.

    ПОЧЕМУ ФУНКЦИЯ, А НЕ СРЕЗ НА МЕСТЕ (17.08.2026). Оба сборщика промпта
    писали `(aliases_ru + aliases_en)[:N]`. Срез склеенного списка отдаёт весь
    бюджет первому языку: 17.08 я дописал в `DEPRECIATION_AMORTIZATION` четыре
    русские формулировки — и из промпта молча пропали ВСЕ английские (`D&A`,
    `DD&A`, `Depreciation, depletion and amortization`), хотя их никто не
    трогал. Ячейка русская, но отчёты по МСФО сплошь двуязычные, и английская
    строка в них встречается чаще русской.

    Опасность здесь в тишине: реестр по-прежнему показывает оба списка, тест на
    синонимы зелёный, а до модели половина не доезжает. Поэтому бюджет делится
    пропорционально длинам, и непустому языку гарантировано хотя бы одно место.
    """
    ru = [a for a in (metric.get("aliases_ru") or []) if a and a != "Нет"]
    en = [a for a in (metric.get("aliases_en") or []) if a and a != "Нет"]
    if len(ru) + len(en) <= limit:
        return ru + en
    if not en:
        return ru[:limit]
    if not ru:
        return en[:limit]
    # Место каждому языку: при перекосе 1 к 30 пропорция округляется в весь
    # бюджет, и одинокий синоним меньшинства выпадает — ровно та же потеря,
    # только с другой стороны.
    en_quota = min(len(en), max(1, round(limit * len(en) / (len(ru) + len(en)))), limit - 1)
    return ru[: limit - en_quota] + en[:en_quota]
