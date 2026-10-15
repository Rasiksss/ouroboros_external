"""Пятая копия формул (`data/metrics.json`) не расходится с ячейками ДАЛЬШЕ.

ЧТО ЭТО ЗА ФАЙЛ. `src/finengine/data/metrics.json` — легаси-реестр, сгенерённый
из Excel. Его читает `skills/metric_registry.py` (`CALCULATED_BANK`,
`CALCULATED_NON_FINANCIAL`), а те — ДВА движка извлечения, у которых параметра
`registry_override` нет вовсе:

* `src/finengine/skills/aifa/extractor.py` (`calculated_defs = CALCULATED_BANK`)
* `src/finengine/skills/aifa_baseline/extractor.py` (то же)

Оба выбираются пользователем: `api/main.py` разводит `arch == "aifa"` /
`"aifa_baseline"`, тип объявлен как выбор в `models/query.py`. То есть один и
тот же ROE выходит процентом на станционном пути (ячейки
`data/registry/*.json`) и долей на этих двух.

ЗАЧЕМ СТОРОЖ, А НЕ ПРАВКА. Свести файл вслепую значило бы узаконить заодно и
СТАРЫЕ расхождения: в нём COST_OF_RISK_GENERAL с `ABS()`, снятым в #151, и
своя версия TAX_EXPENDITURE. Что делать с пятой копией — снести или свести —
решает владелец. Пока решения нет, расхождение обязано хотя бы не расти молча:
до перевода доходностей в проценты расходились 5 enum'ов, после — 16.

Сравниваем не текст, а то, что видит ВЫЧИСЛИТЕЛЬ (`_normalise_formula_expr`):
иначе тест ловил бы тире `–` против `-` и молчал бы про сотню.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: снять `* 100%` у ROE в любой ячейке
`data/registry/*.json` — enum уходит из списка, тест красен «сошлись, а
записаны как расходящиеся»; дописать сотню любому enum'у, которого в списке
нет (например ASSET_TURNOVER) — тест красен «расходятся, а в списке их нет».
"""
from __future__ import annotations

import pytest

from finengine.skills import bulk_extractor as bx
from finengine.skills.metric_registry import CALCULATED_BANK, CALCULATED_NON_FINANCIAL
from finengine.skills.metric_schema import PROFILE_IDS, registry_for_profile

# Расходились ДО перевода доходностей в проценты (21.08) — чужие дефекты,
# каждый со своей причиной, и «свести» их нельзя одним движением.
РАСХОЖДЕНИЯ_ДО_ПРАВКИ_ПРОЦЕНТОВ = {
    "COST_OF_RISK_GENERAL",  # в metrics.json ABS(), в ячейках унарный минус (#151)
    "TAX_EXPENDITURE",       # EBT - NET_INCOME против INCOME_TAX
    "CAPEX",                 # rsbu_nf_ru: COMBINED_ASSET_PURCHASES
    "EBITDA",                # rsbu_nf_ru: PROFIT_FROM_SALES вместо OPERATING_PROFIT
    "FREE_CASH_FLOW_FIRM",   # rsbu_nf_ru: своя формула через проценты к уплате
}

# Добавила правка процентов. Десять — «та же формула, только без сотни»;
# OPERATING_INCOME_CIR — компенсация потребителя (`/ (CIR / 100)`), которая
# легаси-копии не нужна, потому что там CIR остался долей.
ДОБАВИЛА_ПРАВКА_ПРОЦЕНТОВ = {
    "ROE", "LOANS_TO_DEPOSITS", "DIVIDEND_PAYOUT_RATIO", "COST_TO_INCOME_RATIO",
    "NET_INTEREST_MARGIN", "COST_TO_ASSETS", "STAGE3_RATIO", "STAGE3_COVERAGE",
    "RETAIL_DEPOSIT_SHARE", "RWA_DENSITY",
    "OPERATING_INCOME_CIR",
}

ИЗВЕСТНЫЕ_РАСХОЖДЕНИЯ = РАСХОЖДЕНИЯ_ДО_ПРАВКИ_ПРОЦЕНТОВ | ДОБАВИЛА_ПРАВКА_ПРОЦЕНТОВ

# Цена разъезда в числах: на путях `arch=aifa` и `arch=aifa_baseline` эти
# показатели выходят РОВНО В СТО РАЗ меньше, чем на станционном.
УЕХАЛИ_В_СТО_РАЗ = (ДОБАВИЛА_ПРАВКА_ПРОЦЕНТОВ - {"OPERATING_INCOME_CIR"}) | {
    "COST_OF_RISK_GENERAL"
}

БАНКОВСКИЕ = [p for p in PROFILE_IDS if "_bank_" in p]
НЕФИНАНСОВЫЕ = [p for p in PROFILE_IDS if "_bank_" not in p]


def _как_видит_вычислитель(formula: object) -> str:
    return "".join(bx._normalise_formula_expr(str(formula)).split())


def _расхождения() -> dict[str, set[str]]:
    legacy = {
        "bank": {e: _как_видит_вычислитель(f) for e, _n, f, _d in CALCULATED_BANK},
        "non_financial": {
            e: _как_видит_вычислитель(f) for e, _n, f, _d in CALCULATED_NON_FINANCIAL
        },
    }
    ячейки = {
        pid: {
            str(e).strip().upper(): _как_видит_вычислитель(f)
            for e, _n, f, _d in registry_for_profile(pid)["calculated"]
        }
        for pid in PROFILE_IDS
    }
    out: dict[str, set[str]] = {}
    for вид, cells in (("bank", БАНКОВСКИЕ), ("non_financial", НЕФИНАНСОВЫЕ)):
        for enum, лег in legacy[вид].items():
            for pid in cells:
                яч = ячейки[pid].get(enum)
                if яч is not None and яч != лег:
                    out.setdefault(enum, set()).add(pid)
    return out


def test_the_legacy_copy_diverges_exactly_where_we_know_it_does():
    """Список расхождений — ДАННЫЕ на виду, а не молчаливый факт."""
    расходятся = set(_расхождения())
    новые = sorted(расходятся - ИЗВЕСТНЫЕ_РАСХОЖДЕНИЯ)
    сошлись = sorted(ИЗВЕСТНЫЕ_РАСХОЖДЕНИЯ - расходятся)
    assert not новые, (
        f"пятая копия формул разошлась с ячейками ещё в {len(новые)} показателях: {новые}. "
        f"Правка ячейки — половина работы: `arch=aifa` и `arch=aifa_baseline` читают "
        f"metrics.json и покажут человеку другое число"
    )
    assert not сошлись, (
        f"расхождение ушло — снимите имена из списка, иначе сторож охраняет пустоту: {сошлись}"
    )


@pytest.mark.parametrize("enum", sorted(УЕХАЛИ_В_СТО_РАЗ))
def test_the_price_of_the_divergence_is_exactly_a_hundred(enum):
    """Цена названа числом: ровно сотня, а не «примерно расходятся».

    Если однажды разойдутся ещё и слагаемые, множитель перестанет быть
    сотней — и это уже другой разговор с владельцем, а не та же задача.
    """
    legacy = {e: _как_видит_вычислитель(f) for e, _n, f, _d in CALCULATED_BANK}
    legacy.update(
        {e: _как_видит_вычислитель(f) for e, _n, f, _d in CALCULATED_NON_FINANCIAL}
    )
    лег = legacy[enum]
    ячейки = [
        _как_видит_вычислитель(f)
        for pid in PROFILE_IDS
        for e, _n, f, _d in registry_for_profile(pid)["calculated"]
        if str(e).strip().upper() == enum
    ]
    assert ячейки, f"{enum} исчез из ячеек — список УЕХАЛИ_В_СТО_РАЗ протух"
    assert all("*100" in яч for яч in ячейки), (
        f"{enum}: в ячейках сотни больше нет, а в metrics.json её не было — "
        f"расхождение перестало быть сотенным"
    )
    assert "*100" not in лег, f"{enum}: metrics.json уже свели — снимите имя из списка"
