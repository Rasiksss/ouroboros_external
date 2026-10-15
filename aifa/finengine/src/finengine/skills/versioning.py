"""Отпечаток реестра и методики — «по каким ПРАВИЛАМ это посчитано» (#155).

ЗАЧЕМ. За одни сутки реестр менялся трижды: развёрнут Cost/Income, у стоимости
риска снят ABS (сменился ЗНАК), добавлены три показателя. Отчёты, снятые
накануне, перестали сходиться с новыми — и объяснить пользователю «почему
поехало» было нечем: в выдаче стояла версия КОДА (git_sha), а формулы живут не в
коде, а в json-реестрах ячеек и в элементах методики. Правка формулы деплой не
двигает, значит git_sha на неё не реагирует вовсе. Впереди переезд, где реестр и
методика меняются постоянно, — поэтому паспорт прогона обязан нести признак
версии ПРАВИЛ.

ПОЧЕМУ ОТПЕЧАТОК, А НЕ НОМЕР. Номер версии надо поднимать руками, а руки
забывают. Забытый номер ХУЖЕ отсутствующего: два разных реестра предъявляют одну
и ту же версию, и паспорт из свидетеля превращается в лжесвидетеля. Отпечаток
считается по самому содержимому — забыть его нельзя, соврать им нельзя.

ПОЧЕМУ КАНОНИЧЕСКИЙ JSON. Отпечаток обязан молчать на форматировании: если он
меняется от перестановки ключей, переотступа или прогона через другой сериализатор,
им перестанут пользоваться — «оно всё время разное». Поэтому перед хешированием
данные приводятся к канонической форме: ключи отсортированы, отступов нет,
кириллица не экранируется.

ПОЧЕМУ ПОРЯДОК СПИСКОВ ЗНАЧИМ. Ключи в объекте порядка не имеют, а списки —
имеют: порядок строк реестра доезжает до промпта извлечения, а порядок id в
`framework`/`analytical` — это порядок разделов отчёта. Перестановка списка меняет
результат, значит обязана менять и отпечаток.

ПОЧЕМУ НЕ ХЕШ ФАЙЛА ЯЧЕЙКИ. Первая версия хешировала ровно файл ячейки — и врала
дважды.
  1. НЕПОЛНО. С #147 строки ячейки СЛИВАЮТСЯ с синонимами общего реестра
     (`data/metrics.json`): к ячейке добавляется 169 латинских синонимов у 78
     показателей `ifrs_bank_ru` и 192 у 75 в `ifrs_nf_ru`, и они влияют на
     узнавание. Подмена `aliases_en` у TOTAL_ASSETS в общем реестре меняла
     строки, уходящие в промпт, а отпечаток оставался байт-в-байт.
  2. НЕ ИЗ ТОГО ИСТОЧНИКА. Прогон исполняется в станции из ПОЛКИ, собранной один
     раз за жизнь процесса, а отпечаток читал диск заново. Правка реестра «без
     деплоя» давала станцию по старым правилам и паспорт с новым отпечатком.
Поэтому отпечаток снимается с того, что ФАКТИЧЕСКИ подаётся движку: строки после
слияния + расчётные определения, собранные ТЕМИ ЖЕ функциями и из ТОЙ ЖЕ полки.
Лживый паспорт хуже отсутствующего.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

# Тот же каталог, из которого прогон РЕАЛЬНО берёт реестр (metric_schema).
# Считать отпечаток по своей копии пути значило бы получить честный хеш чужого
# файла — паспорт про соседнюю ячейку.
# Каталог ПОСТАВКИ. С #153 ячейка может читаться из внешнего каталога, но
# Старый каталог нужен только как compatibility marker для тестов и реальных
# legacy shelf/external profiles; built-in путь компилируется из layers.
from finengine.skills.metric_schema import _BUILTIN_REGISTRY_DIR as _REGISTRY_DIR
_DEFAULT_LEGACY_REGISTRY_DIR = Path(_REGISTRY_DIR)

# Общий реестр — вторая половина старого shelf/external формата: legacy adapter
# сливает из него aliases так же, как до Slice 13.
_METRICS_PATH = Path(_REGISTRY_DIR).parent / "metrics.json"
# Имена внутри полки — ровно те, под которыми станция их и импортирует
# (`_shelf_roots`: корень пакета finengine кладётся под именем "finengine").
_SHELF_CELL = "finengine/data/registry/{profile_id}.json"
_SHELF_BASE = "finengine/data/metrics.json"


def _digest(obj: object) -> str:
    """sha256 по канонической форме — см. «ПОЧЕМУ КАНОНИЧЕСКИЙ JSON» в шапке."""
    blob = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _shelf_read(arcname: str) -> bytes | None:
    """Байты с полки, если полка в этом процессе вообще бывает.

    ⚠️ В СТАНЦИИ ЕЁ НЕ БЫВАЕТ, И ЭТО НЕ СБОЙ. Полку собирает бэкенд, чтобы
    отправить её сюда; модуль сборки (`station_driver`) тянет
    `finengine.api.station_progress_ingest`, а тот — FastAPI, которого в образе
    станции нет (`deploy/workstation-aifa/Dockerfile`: httpx, openai, pydantic,
    pandas, numpy, openpyxl, simpleeval, fuzzywuzzy — и всё).

    До этой правки ImportError улетал в общий `except` вокруг
    `registry_fingerprint`, и станция получала не отпечаток, а запись «правила не
    прочитаны». То есть паспорт со стороны исполнителя был невозможен физически.
    Отсутствие модуля полки — это ровно «этот процесс полкой не пользуется»,
    и отвечать на него надо чтением диска, а не отказом.
    """
    try:
        from finengine.skills import station_driver as _sd  # noqa: PLC0415 — цикл импорта
    except ImportError:
        return None
    return _sd.shelf_read(arcname)


def registry_fingerprint(profile_id: str) -> dict:
    """Отпечаток ПРАВИЛ ИЗВЛЕЧЕНИЯ: `{cell, sha256, source, extractable, calculated}`.

    Хешируются не файлы, а СТРОКИ, которые движок реально подаёт модели: реестр
    ячейки, разобранный его же схемой, слитый с синонимами общего реестра (#147),
    плюс определения расчётных показателей. Всё это уезжает в промпт целиком —
    формулы, подсказки, описания, синонимы — и всё двигает результат.

    Legacy source — полка, уехавшая в станцию. Built-in source — тот же compiled
    effective registry, который замораживается в ``RunPassport``.

    Правила не прочитаны — пишем это словами. Пустое поле в паспорте читается как
    «версия не менялась», то есть как утверждение, которого мы не делали.
    """
    try:
        # A real pre-Slice-13 shelf/external profile remains a compatibility
        # consumer.  Built-in execution no longer reads the ten copied files:
        # it fingerprints the compiled effective registry itself.
        cell_bytes = _shelf_read(_SHELF_CELL.format(profile_id=profile_id))
        base_bytes = _shelf_read(_SHELF_BASE)
        legacy_dir = Path(_REGISTRY_DIR)
        use_legacy_disk = legacy_dir != _DEFAULT_LEGACY_REGISTRY_DIR
        if cell_bytes is not None and base_bytes is not None:
            cell_raw, base_raw, source = json.loads(cell_bytes), json.loads(base_bytes), "shelf"
            compiled = _compiled_legacy_fingerprint(profile_id, cell_raw, base_raw, source)
        elif use_legacy_disk:
            cell_path = legacy_dir / f"{profile_id}.json"
            cell_raw = json.loads(cell_path.read_text(encoding="utf-8"))
            base_raw = json.loads(_METRICS_PATH.read_text(encoding="utf-8"))
            source = "disk"
            compiled = _compiled_legacy_fingerprint(profile_id, cell_raw, base_raw, source)
        else:
            from finengine.skills.metric_schema import compiled_registry_for_profile  # noqa: PLC0415

            compiled = compiled_registry_for_profile(profile_id)
            source = "disk"
    except Exception as e:  # noqa: BLE001 — паспорт не смеет ронять готовый прогон
        return {"cell": profile_id, "sha256": "", "source": "",
                "problem": f"правила извлечения не прочитаны — версия неизвестна: {e}"}
    return {
        "cell": profile_id,
        "sha256": compiled.fingerprint,
        "source": source,
        "extractable": len(compiled.extractable),
        "calculated": len(compiled.calculated),
        "precedence_version": compiled.precedence_version,
    }


def _compiled_legacy_fingerprint(
    profile_id: str,
    cell_raw: dict,
    base_raw: dict,
    source: str,
):
    """Turn the actual old shelf bytes into the same compiled snapshot type."""
    from finengine.skills.bulk_extractor import _merge_aliases_from_base  # noqa: PLC0415
    from finengine.skills.metric_schema import CalcDef, MetricDef, RegistryFile  # noqa: PLC0415
    from finengine.skills.registry_composition import (  # noqa: PLC0415
        RegistrySelection,
        compiled_registry_from_effective_snapshot,
    )

    envelope = RegistryFile.model_validate(cell_raw)
    selection = RegistrySelection.from_profile_id(profile_id)
    rows = [MetricDef.model_validate(row).model_dump() for row in envelope.extractable]
    rows = _merge_aliases_from_base(
        rows,
        selection.company_type,
        base_rows=(base_raw.get("extractable") or {}).get(selection.company_type) or [],
    )
    effective = {
        "extractable": [MetricDef.model_validate(row).model_dump() for row in rows],
        "calculated": [CalcDef.model_validate(row).model_dump() for row in envelope.calculated],
        "verification_rules": envelope.verification_rules,
    }
    content_version = _digest({"cell": cell_raw, "base": base_raw})
    return compiled_registry_from_effective_snapshot(
        selection,
        effective,
        layer_id=f"legacy.{source}.{profile_id}",
        layer_version=f"sha256:{content_version}",
    )


def station_fingerprint(profile_id: str) -> dict:
    """Отпечаток правил, снятый САМОЙ СТАНЦИЕЙ, с честным ярлыком источника.

    ЗАЧЕМ ОТДЕЛЬНОЙ ФУНКЦИЕЙ (#190, К6a). После сноса тулового пути полку в
    процессе бэкенда собирать станет некому, и `registry_fingerprint` тихо
    съедет на `source="disk"` — то есть назовёт отпечаток файлов бэкенда,
    а не того, по чему считала станция. Ловушка молчаливая: сторож
    `tests/test_run_passport_versions.py` её не видит, его фикстура строит полку
    принудительно.

    Лечится тем, что отпечаток снимает ИСПОЛНИТЕЛЬ. В станции на диске лежит
    распакованная полка читается как source=`"disk"` — формально верно, а по
    смыслу наоборот: это и есть исполнявшиеся правила. Ярлык
    переставляется здесь, а не в станционном скрипте, чтобы словарь источников
    целиком принадлежал этому модулю: три значения, и все три объясняются в
    одном месте (см. `passport_rows`).
    """
    fp = registry_fingerprint(profile_id)
    if fp.get("sha256") and fp.get("source") == "disk":
        fp["source"] = "station"
    return fp


def _methodology_shape(m) -> dict:  # noqa: ANN001 — methodology.Methodology, импорт ленивый
    """Всё, что методика доносит до промпта, — в виде, пригодном для хеша.

    Берутся ВСЕ поля элемента (`dataclasses.asdict`), а не только текст: `role`
    правит разбор карточки, `append` — склейку текстов, `protected` — что вообще
    разрешено править. Служебные `changes`/`notices`/`applied` не берутся: это
    рассказ о правках, а не сами правила; они уже едут в паспорт отдельными
    полями.
    """
    return {
        "elements": {eid: dataclasses.asdict(el) for eid, el in m.elements.items()},
        # Ключ framework — кортеж (тип компании, глубина); в json кортеж ключом
        # быть не может, склеиваем в строку.
        "framework": {f"{ct}/{depth}": list(ids) for (ct, depth), ids in m.framework.items()},
        "analytical": {ct: list(ids) for ct, ids in m.analytical.items()},
        "comparability": list(m.comparability),
    }


def methodology_fingerprint(m=None) -> dict:  # noqa: ANN001 — см. выше
    """Отпечаток МЕТОДИКИ: что поставили мы и что реально применилось.

    Два хеша, а не один, потому что вопросов тоже два: «по какой версии базовой
    методики» и «была ли она этому пользователю личной». `personal` выводится из
    СРАВНЕНИЯ хешей, а не из списка правок: дельта, которая ничего не изменила
    (например потеряла якорь), не сможет выдать себя за применённую.

    Функция ТОТАЛЬНА (как `registry_fingerprint`): к моменту сборки паспорта
    отчёт уже посчитан, и ронять его из-за неснятого отпечатка нельзя. Не вышло —
    говорим словами, а `personal` остаётся null: «неизвестно» ≠ «базовая».
    """
    try:
        from finengine.skills.analysis import methodology as md  # noqa: PLC0415 — цикл импорта

        base_sha = _digest(_methodology_shape(md.base()))
        if m is None:
            return {"base_sha256": base_sha, "effective_sha256": base_sha, "personal": False}
        effective_sha = _digest(_methodology_shape(m))
    except Exception as e:  # noqa: BLE001 — паспорт не смеет ронять готовый отчёт
        return {"base_sha256": "", "effective_sha256": "", "personal": None,
                "problem": f"отпечаток методики не снят: {e}"}
    return {
        "base_sha256": base_sha,
        "effective_sha256": effective_sha,
        "personal": effective_sha != base_sha,
    }


def passport_rows(versions: dict) -> list[tuple[str, str]]:
    """Тот же паспорт человеку — парами «подпись → значение» для листа Excel.

    Пользователь сравнивает вчерашнюю выгрузку с сегодняшней в самом файле, а не
    в журнале на сервере, — значит признак версии обязан быть в файле. Формат
    плоский: любые новые поля `versions` показываются как есть, чтобы лист не
    приходилось править вслед за каждым дополнением паспорта.

    «НЕИЗВЕСТНО» И «БАЗОВАЯ» — РАЗНЫЕ ОТВЕТЫ. `methodology_fingerprint` при
    неснятом отпечатке возвращает `personal=None` и текст `problem`, и первая
    версия этого рендера печатала на них «базовая», а `problem` проглатывала: то
    есть словарь честно говорил «не знаю», а лист пользователю уверенно называл
    базовую методику. Ровно то же и у реестра.
    """
    rows: list[tuple[str, str]] = []
    reg = versions.get("registry") or {}
    if reg:
        rows.append(("Ячейка реестра", str(reg.get("cell") or "")))
        rows.append(("Отпечаток реестра", str(reg.get("sha256") or "НЕИЗВЕСТЕН")))
        rows.append(("Показателей в реестре",
                     f"{reg.get('extractable', '?')} извлекаемых, "
                     f"{reg.get('calculated', '?')} расчётных"))
        # ИСТОЧНИК ПРАВИЛ — ТРИ РАЗНЫХ ОТВЕТА, а не «полка и всё остальное».
        #
        #   shelf   — байты, которые бэкенд отправил в станцию. Обычный случай,
        #             комментировать нечего;
        #   station — файлы, которые прочитала САМА станция. Доверия не меньше:
        #             у неё на диске лежит распакованная полка, и отпечаток снят
        #             тем, кто по этим правилам и считал;
        #   disk    — файлы на диске бэкенда. Могли разойтись с исполнявшимся, и
        #             молчать об этом значит врать.
        #
        # До 14.08 «station» попадал бы в ветку «не полка» и печатался как
        # подозрительный — то есть самый достоверный источник объявлялся бы
        # сомнительным.
        _src = reg.get("source")
        if _src == "station":
            rows.append(("Источник правил",
                         "станция — отпечаток снят исполнителем прогона"))
        elif _src and _src != "shelf":
            rows.append(("Источник правил",
                         f"{_src} — снят не с полки прогона, "
                         f"мог разойтись с исполнявшимся"))
        if reg.get("problem"):
            rows.append(("Проблема реестра", str(reg["problem"])))
    meth = versions.get("methodology") or {}
    if meth:
        rows.append(("Отпечаток методики",
                     str(meth.get("effective_sha256") or "НЕИЗВЕСТЕН")))
        personal = meth.get("personal")
        rows.append(("Методика базовая/личная",
                     "НЕИЗВЕСТНО — отпечаток методики не снят" if personal is None
                     else "личная (с правками пользователя)" if personal
                     else "базовая"))
        if meth.get("problem"):
            rows.append(("Проблема методики", str(meth["problem"])))
    return rows
