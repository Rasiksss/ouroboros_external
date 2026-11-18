"""Сборка `RunPassport` из `id.entity`/`id.frame` (Срез 1, B1).

Не пятый атом карточки — клей, который карточка B1 просит явно: «Создать
контракт, обернуть текущую классификацию, передать один и тот же паспорт в
извлечение и анализ». `fe_classify_report`, `aifa-extract` и `aifa-analyze` —
три ОТДЕЛЬНЫХ процесса (инструмент шлюза и два скрипта комнаты станции,
запущенные последовательно), между которыми сегодня нет канала передачи
произвольного JSON бэкенда в комнату (в отличие от `--methodology`/`--external`,
у которых такой канал есть — демон `station-bridge` с доступом к бэкенду и сети,
которого у `RunPassport` пока нет; заводить его — отдельная архитектурная
работа с песочницей/демоном, вне карточек этого среза).

Поэтому «один и тот же паспорт на всех границах» здесь держится не на общей
памяти, а на ДЕТЕРМИНИЗМЕ: все три места вызывают ИМЕННО эту функцию с одними
и теми же входами (текст документа, уже решённая `--cell`, подсказка имени) и
получают побайтово одинаковое сериализованное значение — тест
`tests/atoms/test_run_passport.py` это прямо проверяет. `--cell`, уже решённая
`fe_classify_report`, — это и есть тот самый «один раз на запуск»: `aifa-extract`
и `aifa-analyze` НЕ вызывают `detect_profile(text)` повторно, когда `--cell`
дана (см. `classification_from_cell`).

`prompt_version`/`sandbox_image_version` этот срез не подключает ни к какому
реальному источнику — такого трекинга в репозитории пока нет вообще нигде (не
только для этих атомов), и выдумывать его сверх заказанного было бы
спекулятивным расширением (CLAUDE.md §2). Значения — честные строки-заглушки,
а не подделанные номера версий.
"""
from __future__ import annotations

from finengine.contracts import ClassificationResult, RunPassport
from finengine.skills.atoms.entity import identify_entity
from finengine.skills.atoms.frame import classify_frame

UNVERSIONED = "unversioned"

# Тот же табличный вид (standard, company_type, language), что в докстринге
# `extractors._classifier` (шапка модуля) — источник табличный, не выдуман
# заново; тест `test_run_passport.py::test_cell_axes_table_matches_the_classifier`
# проверяет её самосогласованность с `_compose_profile_id`.
_CELL_TO_AXES: dict[str, tuple[str, str, str]] = {
    "ifrs_nf_ru": ("ifrs", "non_financial", "ru"),
    "ifrs_bank_ru": ("ifrs", "bank", "ru"),
    "rsbu_nf_ru": ("rsbu", "non_financial", "ru"),
    "rsbu_bank_ru": ("rsbu", "bank", "ru"),
    "us_gaap_nf_en": ("us_gaap", "non_financial", "en"),
    "us_gaap_bank_en": ("us_gaap", "bank", "en"),
    "ifrs_nf_en": ("ifrs", "non_financial", "en"),
    "ifrs_bank_en": ("ifrs", "bank", "en"),
    "ind_as_nf_en": ("ind_as", "non_financial", "en"),
    "ind_gaap_bank_en": ("ind_gaap", "bank", "en"),
}


def classification_from_cell(
    cell: str,
    *,
    company_type_override: str | None = None,
    customer_id: str = "",
) -> ClassificationResult:
    """Восстановить `ClassificationResult` из уже принятого решения `--cell`.

    Текст документа здесь НЕ читается: маршрутизация уже решена
    (`fe_classify_report` её вернула, модель передала `--cell` дальше) — читать
    текст снова и вызывать `detect_profile` было бы ровно тем повторным
    определением, которое решение №2 (01-nerushimye-resheniya.md) запрещает на
    рабочем пути.
    """
    if cell not in _CELL_TO_AXES:
        raise ValueError(f"неизвестная ячейка '{cell}' — не входит в PROFILE_IDS")
    standard, company_type, language = _CELL_TO_AXES[cell]
    override_source: dict[str, str] = {}
    if company_type_override:
        company_type = company_type_override
        override_source["company_type"] = "user"
    return ClassificationResult(
        profile_id=cell,
        standard=standard,
        company_type=company_type,
        language=language,
        confidence_low=False,
        reason="зафиксировано через --cell (fe_classify_report); текст повторно не классифицировался",
        override_source=override_source,
        customer_id=customer_id,
    )


def build_run_passport(
    text: str,
    *,
    goal_ir_id: str,
    classification: ClassificationResult | None = None,
    registry_version: str | None = None,
    filename: str = "",
    company_hint: str = "",
    cell: str = "",
    company_type_override: str | None = None,
    standard_override: str | None = None,
    methodology_version: str = "",
    model_version: str = "",
    prompt_version: str = "",
    sandbox_image_version: str = "",
    customer_id: str = "",
) -> RunPassport:
    """Один паспорт на запуск: `id.frame` + `id.entity`, версии зафиксированы.

    `cell` дана → `classification_from_cell` (без чтения текста заново); `cell`
    пуста → это ПЕРВОЕ (не повторное) определение, `classify_frame` читает текст.
    """
    if classification is None:
        classification = (
            classification_from_cell(
                cell,
                company_type_override=company_type_override,
                customer_id=customer_id,
            )
            if cell
            else classify_frame(
                text,
                standard_override=standard_override,
                company_type_override=company_type_override,
            )
        )
        if not cell and customer_id:
            classification = classification.model_copy(update={"customer_id": customer_id})
    if registry_version is None:
        from finengine.skills.metric_schema import (  # noqa: PLC0415
            compiled_registry_for_classification,
        )

        registry_version = compiled_registry_for_classification(classification).version
    entity = identify_entity(text, filename=filename, existing_name=company_hint)
    return RunPassport(
        goal_ir_id=goal_ir_id,
        classification=classification,
        entity=entity,
        registry_version=registry_version,
        methodology_version=methodology_version or UNVERSIONED,
        model_version=model_version or UNVERSIONED,
        prompt_version=prompt_version or UNVERSIONED,
        sandbox_image_version=sandbox_image_version or UNVERSIONED,
    )
