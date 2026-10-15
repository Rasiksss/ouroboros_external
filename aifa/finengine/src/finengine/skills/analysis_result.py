"""Сборка РЕЗУЛЬТАТА анализа: карточка, паспорт, артефакт, журнал (#244, Ш17).

ЗАЧЕМ ОТДЕЛЬНЫЙ МОДУЛЬ, А НЕ ХЕНДЛЕР ТУЛА. У извлечения такой модуль уже есть
(`extract_result.finalize_extraction`), и им пользуются ОБА пути: хендлер
`fe_extract_metrics` и канал хода (`api/station_turn._finish_run`), который
поднимает комнату гейтвеем. У анализа второго пути пока нет — и не появится,
пока сборка живёт внутри `_h_fe_analyze`: снеси тул, и результат разбора
доставлять станет нечем. Пятьсот раз повторённая в проекте болезнь «сделанное
не доезжает до места работы» здесь имела бы худший вид — разбор посчитан,
деньги за модель потрачены, показывать нечего.

ЧТО СЮДА ПЕРЕЕХАЛО БЕЗ ИЗМЕНЕНИЯ ПОВЕДЕНИЯ: графики, разбор карточки, отказ на
пустой карточке (#178), паспорт версий (#155), артефакт с провенансом и запись
в сквозной журнал прогонов.

ЧТО СОЗНАТЕЛЬНО НЕ ПЕРЕЕХАЛО: проверка прав. `require_user_ctx` — это правило
СЛОЯ ТУЛОВ (УЭК-АУА-24/25), и тянуть `llm.tools` в `skills/` значило бы развернуть
направление зависимостей (правило `docs/rules/architecture.md`: storage → skills
→ routes). Поэтому сюда приезжает готовая пара «пользователь, проект» либо
None — и None означает ровно одно: артефакт не сохраняем, всё остальное делаем.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# Порог «есть ли текст, за который можно отвечать» (#178).
#
# ⚠️ 200 → 80 СНИЖЕН НАМЕРЕННО. Прежние 200 применялись только к тексту, который
# НЕ разобрался; теперь проверка одна на всех, и на 200 знаках она резала
# законный короткий отчёт (сторож test_normal_report_passes_through: вердикт +
# таблица из одной строки + вывод = 197 знаков). 80 ловит вырожденный случай —
# пусто, «Не удалось.», «Анализ:» — и не трогает короткий, но настоящий разбор.
#
# И честно про границы: длиной обрыв генерации НЕ ловится. В инциденте 11.08
# станция вернула 24 779 знаков — любой порог тут бесполезен. От обрыва защищает
# `openrouter.ModelSilent` и запись finish_reason, а не арифметика над длиной.
MIN_REPORT_CHARS = 80


@dataclass
class AnalysisOutcome:
    """Всё, что видит пользователь, плюс следы прогона.

    `empty` — отчёта нет: карточка «собралась», но показывать в ней нечего.
    Это ОТКАЗ, а не результат, и решает, как о нём сказать, вызывающий: тул
    отвечает JSON-ошибкой, канал хода — красным чипом.
    """

    report_markdown: str
    report_structured: dict | None
    artifact_run_id: str = ""
    cell: str = ""
    versions: dict = field(default_factory=dict)
    empty: bool = False
    report_head: str = ""


def _build_card(*, report_md: str, company_name: str, company_type: str,
                standard: str, language: str, methodology: Any, doc_id: str,
                external_rows: list[dict], source_text: str,
                grounded_metrics: list[dict]) -> dict | None:
    """Структура карточки для фронта. Модель к JSON не принуждаем — разбираем
    её markdown; `report_markdown` при этом остаётся как есть для чата и PDF."""
    from finengine.skills.analysis import framework as _fw_risk
    from finengine.skills.analysis.charts import build_charts
    from finengine.skills.analysis.parse import parse_report

    # Графики строятся ПО ТОМУ ЖЕ набору показателей, что уехал в промпт
    # заземлением (#121), а не по тексту отчёта: столбик и строка таблицы
    # обязаны быть одним и тем же числом.
    try:
        charts = build_charts(grounded_metrics, company_type)
    except Exception:  # noqa: BLE001 — график не смеет ронять готовый отчёт
        log.exception("анализ: спецификации графиков не собраны")
        charts = []

    try:
        return parse_report(
            report_md, company=company_name, period="",
            company_type=company_type, standard=standard, language=language,
            # Та же методика, по которой писался отчёт (#148): переименовал
            # пользователь раздел — карточка узнает его по ярлыку роли, а не по
            # заголовку.
            methodology=methodology,
            # Документ, по которому шёл разбор (#157). Без него ссылка «PDF
            # стр. N» знает номер страницы, но не знает, в каком документе его
            # открывать, — и прыгать некуда.
            doc_id=doc_id,
            # #159: строки внешнего контекста кладём ТЕ ЖЕ, что ушли в промпт, —
            # прямо от сборщика, а НЕ разбором markdown-ответа. Разница
            # принципиальная: ссылку и дату писал код по ответу источника, и
            # модель не может их переписать даже случайно.
            external_context=external_rows,
            # ── УЛИКИ ДЛЯ УРОВНЯ РИСКА (#157-C) ──
            # Обе из кода, ни одна не из текста отчёта модели. Оговорку о
            # непрерывности ищем в ИСХОДНОМ распознанном тексте: в markdown-е её
            # может не оказаться (модель вправе о ней не написать), а уровень
            # риска ошибиться не вправе.
            going_concern_pages=_fw_risk.going_concern_pages(source_text),
            broken_metrics=[
                str(m.get("enum") or "") for m in grounded_metrics
                if str(m.get("sanity") or "").strip()
            ],
            charts=charts)
    except Exception:  # noqa: BLE001
        log.exception("анализ: карточка не разобрана, doc=%s", doc_id[:8])
        return None


def _is_empty(card: dict | None, report_md: str) -> tuple[str, bool]:
    """(текст отчёта, пуст ли он). См. MIN_REPORT_CHARS и #178."""
    _card = card or {}
    head = str(_card.get("report_md") or report_md or "").strip()
    legacy = bool(
        str(_card.get("verdict") or "").strip()
        or _card.get("sections") or _card.get("kpi") or _card.get("conclusions")
    )
    return head, (len(head) < MIN_REPORT_CHARS and not legacy)


def _passport(cell: str, methodology: Any) -> dict:
    """Отпечатки ПРАВИЛ прогона (#155): реестр и методика.

    Ячейка = реестр показателей, которым анализ ЗАЗЕМЛЯЛСЯ; методика — правила,
    по которым он рассуждал. Отчёт двигают оба, поэтому в паспорте оба: иначе
    «поехало из-за реестра или из-за методики» не различить.

    Best-effort и это КОНСТРУКЦИЯ, а не дисциплина: отчёт к этому моменту уже
    посчитан, станция отработала, деньги за модель потрачены — уронить всё это
    из-за отпечатка нельзя.
    """
    try:
        from finengine.skills import versioning as _ver
        return {
            "registry": _ver.registry_fingerprint(cell),
            "methodology": _ver.methodology_fingerprint(methodology),
        }
    except Exception:  # noqa: BLE001 — паспорт не смеет ронять готовый отчёт
        log.exception("анализ: паспорт версий не снят")
        return {}


def _grounding_counts(grounded_metrics: list[dict]) -> tuple[int | None, int | None]:
    """Сколько показателей заземлило прогон и сколько строк рассматривалось.

    Считаем НАЙДЕННЫЕ, а не все прогнанные строки реестра: движок эмитит и
    ненайденные (`found=false`), и «заземлено 75» при трёх реальных числах —
    ровно то враньё, ради борьбы с которым паспорт и заводится. Пусто —
    возвращаем None, а не выдуманный ноль.
    """
    if not grounded_metrics:
        return None, None
    return (sum(1 for m in grounded_metrics if m.get("found")), len(grounded_metrics))


def _provenance(*, report_md: str, source_text: str, cell: str, versions: dict,
                analysis_type: str, analysis_title: str, depth: str,
                company_type: str, company_name: str, standard: str,
                language: str, model_requested: str, doc_id: str,
                doc_filename: str, methodology: Any,
                grounded_metrics: list[dict]) -> dict:
    """Паспорт прогона целиком — он уезжает в ДВА места (артефакт и журнал).

    Считать его в каждом месте отдельно значило бы получить два расходящихся
    ответа на один вопрос.
    """
    from finengine.skills.analysis import framework as _fw_prov
    # Версия кода — из журнала прогонов, чтобы у артефакта и у записи в
    # `extract_runs.jsonl` была одна метка деплоя, а не две копии чтения
    # переменных окружения, разъезжающиеся при первой правке.
    from finengine.skills.run_logger import _GIT_SHA, _GIT_VER

    grounded, considered = _grounding_counts(grounded_metrics)
    return {
        "git_ver": _GIT_VER, "git_sha": _GIT_SHA,
        "analysis_type": analysis_type, "analysis_title": analysis_title,
        "depth": depth,
        "company_type": company_type, "company_name": company_name,
        "standard": standard, "cell": cell,
        "doc_language": language, "answer_language": _fw_prov.answer_language(),
        # МОДЕЛЬ. Врать здесь нельзя: паспорт читают, чтобы объяснить число.
        # Анализ идёт в станции, а туда `model_id` физически НЕ доезжает —
        # модель выбирает мост по своему FE_RPC_DEFAULT_MODEL, и бэкенд её не
        # знает. Поэтому раздельно: что просили и кто решал. Имя модели — null,
        # а не правдоподобная догадка.
        "model_requested": model_requested or "",
        "model_decided_by": "station-bridge",
        "model": None,
        "doc_id": doc_id, "doc_filename": doc_filename,
        # Хешируем ВЕСЬ текст, а не префикс: сверка «тот ли это отчёт» обязана
        # ловить правку в хвосте, а sha256 по сотне килобайт стоит микросекунды.
        "report_sha256": hashlib.sha256(report_md.encode("utf-8")).hexdigest(),
        "report_chars": len(report_md),
        # Хеш входа — какой именно распознанный текст читала модель.
        "source_text_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        "source_text_chars": len(source_text),
        "grounded_metrics": grounded,
        "grounded_considered": considered,
        # МЕТОДИКА (#123). Паспорт обязан отвечать не только «по каким числам»,
        # но и «по каким ПРАВИЛАМ» посчитан отчёт: у одного пользователя NIM по
        # средним за квартал, у другого базовая формула, и через полгода это
        # единственный способ объяснить разные цифры по одному отчёту.
        # «lost» — правка не применилась (мы переписали базу): молчать об этом
        # нельзя, иначе счёт по базовой методике выдаст себя за счёт по личной.
        "methodology_changes": [
            {"id": c["id"], "title": c["title"], "op": c["op"]}
            for c in getattr(methodology, "changes", ()) or ()
        ],
        "methodology_lost": [
            {"id": n.get("id"), "problem": n.get("problem")}
            for n in getattr(methodology, "notices", ()) or ()
            if n.get("kind") == "lost_anchor"
        ],
        # ВЕРСИЯ ПРАВИЛ (#155). Строки `methodology_changes` отвечают «что
        # пользователь поменял у СЕБЯ», а эти отпечатки — «какой версии реестра
        # и методики был весь прогон». Без них перечисление личных правок
        # бессмысленно: база, к которой они применялись, безымянна.
        "versions": versions,
    }


def _save_artifact(*, scope: tuple[str, str], card: dict | None, report_md: str,
                   provenance: dict, company_name: str, doc_filename: str,
                   analysis_title: str, depth: str, company_type: str,
                   cell: str, grounded: int | None) -> str:
    """Артефакт анализа с провенансом; возвращает `run_id` или пустую строку.

    Извлечение пишет и артефакт, и запись в журнал, а анализ до сих пор жил
    только текстом внутри чат-хода: на вопрос «по каким правилам посчитан отчёт
    от марта» ответить было нечем. Best-effort — анализ уже готов, и падение
    записи не должно его ронять (тот же контракт, что у журнала извлечения).
    """
    try:
        from finengine.store import artifacts as _art

        uid, pid = scope
        meta = _art.save_artifact(
            user_id=uid, project_id=pid, kind="analysis",
            payload={"report_markdown": report_md, "report_structured": card,
                     "provenance": provenance},
            title=f"Анализ — {company_name or doc_filename}",
            summary=f"{analysis_title} ({depth}), {company_type}, ячейка {cell}",
            # Провенанс дублируем в meta.params: список артефактов читает только
            # meta.json, и «по каким правилам» должно отвечаться без раскрытия
            # полного отчёта.
            params=provenance,
            model=str(provenance["model"] or ""),
            # counts — только когда счётчик реально наш (см. `_grounding_counts`).
            counts=({"grounded_metrics": grounded} if grounded is not None else None),
        )
        return meta.run_id
    except Exception:  # noqa: BLE001 — артефакт best-effort, анализ уже посчитан
        log.exception("анализ: артефакт не сохранён")
        return ""


async def finalize_analysis(
    *,
    report_md: str,
    doc_id: str,
    doc_filename: str,
    company_name: str,
    company_type: str,
    standard: str,
    language: str,
    cell: str,
    analysis_type: str,
    analysis_title: str,
    depth: str,
    methodology: Any = None,
    external_rows: list[dict] | None = None,
    grounded_metrics: list[dict] | None = None,
    source_text: str = "",
    model_requested: str = "",
    artifact_scope: tuple[str, str] | None = None,
    user_id: int | None = None,
    started_at: float = 0.0,
) -> AnalysisOutcome:
    """Собрать всё, что видит пользователь, и оставить следы прогона.

    `artifact_scope` — пара (user_id, project_id), УЖЕ проверенная слоем тулов.
    None означает «чат вне проекта»: артефакт класть некуда, и это штатная
    ситуация, а не сбой. Журнал при этом пишется всё равно — прогон-то был.
    """
    _external = list(external_rows or [])
    _grounded = list(grounded_metrics or [])
    report_md = report_md or ""

    card = _build_card(
        report_md=report_md, company_name=company_name, company_type=company_type,
        standard=standard, language=language, methodology=methodology,
        doc_id=doc_id, external_rows=_external, source_text=source_text,
        grounded_metrics=_grounded)

    head, empty = _is_empty(card, report_md)
    if empty:
        log.error("анализ: отчёта нет, doc=%s, %d знаков: %r",
                  doc_id[:8], len(head), head[:400])
        return AnalysisOutcome(report_markdown=report_md, report_structured=card,
                               cell=cell, empty=True, report_head=head[:400])

    versions = _passport(cell, methodology)
    provenance = _provenance(
        report_md=report_md, source_text=source_text or "", cell=cell,
        versions=versions, analysis_type=analysis_type,
        analysis_title=analysis_title, depth=depth, company_type=company_type,
        company_name=company_name, standard=standard, language=language,
        model_requested=model_requested, doc_id=doc_id,
        doc_filename=doc_filename, methodology=methodology,
        grounded_metrics=_grounded)

    artifact_run_id = ""
    if artifact_scope is not None:
        grounded, _ = _grounding_counts(_grounded)
        artifact_run_id = _save_artifact(
            scope=artifact_scope, card=card, report_md=report_md,
            provenance=provenance, company_name=company_name,
            doc_filename=doc_filename, analysis_title=analysis_title,
            depth=depth, company_type=company_type, cell=cell, grounded=grounded)
    else:
        # Чат вне проекта — штатная ситуация, а не сбой: некуда класть артефакт.
        log.info("анализ: артефакт не сохранён — ход вне проекта, doc=%s", doc_id[:8])

    # Журнальная половина паспорта. Артефакт лежит в проекте ОДНОГО пользователя,
    # и вопрос «какие прогоны шли по реестру до вчерашней правки» по артефактам
    # не задать — на него отвечает сквозной журнал. Пишем ВНЕ ветки артефакта:
    # чат вне проекта артефакт не получает, но прогон-то был.
    try:
        from finengine.skills.run_logger import log_analysis_run
        log_analysis_run(
            doc_id=doc_id, doc_filename=doc_filename,
            company_name=company_name, company_type=company_type,
            analysis_type=analysis_type, depth=depth, cell=cell,
            user_id=user_id, artifact_run_id=artifact_run_id,
            started_at=started_at, versions=versions,
        )
    except Exception:  # noqa: BLE001 — журнал не должен ронять анализ
        log.exception("анализ: запись в журнал прогонов не сделана")

    return AnalysisOutcome(
        report_markdown=report_md, report_structured=card,
        artifact_run_id=artifact_run_id, cell=cell, versions=versions)
