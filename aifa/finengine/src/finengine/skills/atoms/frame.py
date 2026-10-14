"""`id.frame` — зафиксировать стандарт/тип компании/язык как рамку запуска
(A6/A13, docs/build-plan/11-srez-01-run-passport.md).

Тонкая обёртка поверх уже существующего `extractors._classifier.detect_profile`/
`profile_from_axes`/`override_profile_standard` — правила классификации здесь
не переписываются (карточка `id.frame`: «существующие правила классификации не
переписывать без необходимости»), только упаковка `Profile` в контракт
`ClassificationResult`, который дальше замораживается в `RunPassport`.
"""
from __future__ import annotations

from finengine.contracts import ClassificationResult
from finengine.skills.extractors._classifier import (
    Profile,
    detect_profile,
    override_profile_standard,
    profile_from_axes,
)

# `nf_industry` — та же группа маркеров, что `_classifier._MARKER_GROUPS`
# использует как СВИДЕТЕЛЬСТВО оси company_type; здесь те же фразы читаются
# второй раз как явное ОТРАСЛЕВОЕ поле (id.frame: «добавить явное поле
# отрасли»). Список не расширяем и не переизобретаем — только нормализуем
# RU/EN варианты одной отрасли к одному коду.
_INDUSTRY_LABELS: dict[str, str] = {
    "manufacturing": "manufacturing", "производство": "manufacturing",
    "retail": "retail", "розничная торговля": "retail",
    "wholesale": "wholesale",
    "software": "software",
    "semiconductor": "semiconductor",
    "oil and gas": "oil_and_gas",
    "mining": "mining", "добыча": "mining",
    "transportation": "transportation", "транспорт": "transportation",
    "automotive": "automotive",
    "pharmaceutical": "pharmaceutical",
    "construction": "construction", "строительство": "construction",
    "real estate": "real_estate", "недвижимость": "real_estate",
}


def _industry_from_profile(profile: Profile) -> str:
    """Отрасль нефинансовой компании — по тем же маркерам, что уже отличили её
    от банка (`detect_company_type`'s `signals["groups"]["nf_industry"]`), не
    вторым сканированием текста. У банков отрасль не определяется — сама
    группа маркеров называется "нефинансовая" по построению классификатора.
    """
    if profile.company_type != "non_financial":
        return ""
    groups = (profile.signals or {}).get("company_type", {}).get("groups", {})
    hits: dict[str, int] = groups.get("nf_industry") or {}
    if not hits:
        return ""
    top_phrase = max(hits, key=hits.get)
    return _INDUSTRY_LABELS.get(top_phrase, "")


def _to_contract(profile: Profile, *, override_source: dict[str, str] | None = None) -> ClassificationResult:
    return ClassificationResult(
        profile_id=profile.profile_id,
        standard=profile.standard,
        company_type=profile.company_type,
        language=profile.language,
        confidence_low=profile.confidence_low,
        reason=profile.reason,
        signals=profile.signals,
        axis_confidence_low=profile.axis_confidence_low,
        override_source=override_source or {},
        industry=_industry_from_profile(profile),
    )


def classify_frame(
    text: str,
    *,
    standard_text: str | None = None,
    standard_override: str | None = None,
    company_type_override: str | None = None,
) -> ClassificationResult:
    """Определить (стандарт, тип компании, язык) один раз для запуска.

    `standard_override` — явная пользовательская поправка оси стандарта
    (снимает `axis_confidence_low["standard"]`); применяется существующим
    `override_profile_standard`, а не отдельной веткой здесь. `company_type_override`
    зеркалит ту же идею для оси типа компании, тем же `profile_from_axes`, каким
    уже пользуется `override_profile_standard` — своей ветки пересборки
    `profile_id` не заводим. Атом классифицирует ФИНАНСОВЫЙ контекст документа,
    а не его технический тип — за это отвечает `doc.probe`, и результаты двух
    атомов не смешиваются.
    """
    profile = detect_profile(text, standard_text=standard_text)
    override_source: dict[str, str] = {}
    if standard_override:
        profile = override_profile_standard(profile, standard_override)
        override_source["standard"] = "user"
    if company_type_override and company_type_override != profile.company_type:
        axis_low = dict(profile.axis_confidence_low)
        axis_low["company_type"] = False
        profile = profile_from_axes(
            standard=profile.standard,
            company_type=company_type_override,
            language=profile.language,
            reason=f"явное переопределение типа компании: {company_type_override}; {profile.reason}",
            signals=profile.signals,
            axis_confidence_low=axis_low,
        )
        override_source["company_type"] = "user"
    return _to_contract(profile, override_source=override_source)
