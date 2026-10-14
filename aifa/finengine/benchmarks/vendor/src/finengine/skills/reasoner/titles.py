"""English chain and step headings for the reasoner (general + bank chains).

The chain definitions in `chains.py` are Russian throughout — titles, aims,
reasoning questions. Only the titles reach the user: `runner.py` renders them
as markdown headings ("## chain", "### step") inside the answer, so an English
answer would otherwise sit under Russian headings.

Keyed by the exact Russian title so `chains.py` stays untouched — that file is
1700 lines of prompt material, and this table is meant to be deleted whole
when the chain pipeline goes away.

Metric enum tokens (OPERATING_PROFIT, [NET_OPERATING_CASH_FLOW], ...) are kept
verbatim: `runner._translate_enums` substitutes them for registry names after
this lookup runs.
"""
from __future__ import annotations

from finengine.i18n import reply_locale

_TITLES_EN: dict[str, str] = {
    'Рост EBITDA и стабильность': 'EBITDA growth and stability',
    'Маржинальность EBITDA': 'EBITDA margin',
    'Динамика OPERATING_PROFIT за период': 'OPERATING_PROFIT dynamics over the period',
    'Маржинальность по OPERATING_PROFIT ': 'OPERATING_PROFIT margin',
    'Выявление причин расхождений': 'Identifying the causes of the gap',
    'Анализ неденежных корректировок': 'Analysis of non-cash adjustments',
    'Оценка типичности корректировок': 'Assessing how typical the adjustments are',
    'Влияние изменений оборотного капитала на денежный поток': 'Impact of working capital changes on cash flow',
    'Оценка эффективности управления оборотным капиталом': 'Assessing working capital management efficiency',
    'Выявление причин расхождений прибыли и денежного потока': 'Identifying the causes of the profit-to-cash-flow gap',
    'Анализ влияния изменений оборотного капитала': 'Analysis of the impact of working capital changes',
    'Выявление разовых и нетипичных корректировок': 'Identifying one-off and non-recurring adjustments',
    'Объём капитальных затрат': 'Capital expenditure volume',
    'Динамика и стратегия капитальных инвестиций': 'Capital investment dynamics and strategy',
    'Анализ приобретения дочерних и ассоциированных компаний': 'Analysis of subsidiary and associate acquisitions',
    'Анализ расходов на приобретение дочерних компаний': 'Analysis of spending on subsidiary acquisitions',
    'Регулярность практики приобретения дочерних компаний': 'How regular subsidiary acquisitions are',
    'Приобретение дочерних компаний': 'Subsidiary acquisitions',
    'Рассчитать разницу между [NET_OPERATING_CASH_FLOW] и [NET_CAPEX + приобретение компаний] ': 'Calculate the difference between [NET_OPERATING_CASH_FLOW] and [NET_CAPEX + acquisitions of companies]',
    'Текущая оборачиваемость активов': 'Current asset turnover',
    'Динамика оборачиваемости активов': 'Asset turnover dynamics',
    'Сравнение с отраслевыми показателями': 'Comparison with industry benchmarks',
    'Текущая рентабельность активов': 'Current return on assets',
    'Динамика рентабельности активов': 'Return on assets dynamics',
    'Сравнительный анализ с отраслью': 'Comparison against the industry',
    'Сравнительный анализ ROA с отраслью': 'ROA comparison against the industry',
    'Комплексная оценка эффективности активов': 'Overall assessment of asset efficiency',
    'Текущая оборачиваемость основных средств': 'Current fixed asset turnover',
    'Динамика оборачиваемости основных средств': 'Fixed asset turnover dynamics',
    'Текущий финансовый цикл (Cash Conversion Cycle)': 'Current cash conversion cycle',
    'Динамика финансового цикла': 'Cash conversion cycle dynamics',
    'Текущий уровень ликвидности': 'Current liquidity level',
    'Динамика коэффициента ликвидности': 'Liquidity ratio dynamics',
    'Расчет финансового цикла (Cash Conversion Cycle)': 'Calculating the cash conversion cycle',
    'Анализ динамики финансового цикла': 'Analysis of cash conversion cycle dynamics',
    'Сравнение финансового цикла с отраслью': 'Cash conversion cycle vs. the industry',
    'Расчет коэффициента текущей ликвидности': 'Calculating the current ratio',
    'Анализ динамики ликвидности': 'Analysis of liquidity dynamics',
    'Сравнение ликвидности с отраслью': 'Liquidity vs. the industry',
    'Комплексный анализ управления оборотным капиталом': 'Comprehensive analysis of working capital management',
    'Текущая рентабельность собственного капитала (ROE)': 'Current return on equity (ROE)',
    'Динамика рентабельности собственного капитала': 'Return on equity dynamics',
    'Сравнительный анализ ROE с отраслью': 'ROE comparison against the industry',
    'Расчет чистой маржи (Net Income Margin)': 'Calculating net income margin',
    'Расчет оборачиваемости активов (Asset Turnover)': 'Calculating asset turnover',
    'Расчет рентабельности активов (ROA)': 'Calculating return on assets (ROA)',
    'Расчет рентабельности собственного капитала (ROE)': 'Calculating return on equity (ROE)',
    'Анализ факторов ROE через модель Дюпона': 'DuPont breakdown of ROE drivers',
    'Текущий уровень долговой нагрузки': 'Current leverage level',
    'Динамика долговой нагрузки': 'Leverage dynamics',
    'Коэффициент NET_DEBT_TO_EBITDA_RATIO': 'NET_DEBT_TO_EBITDA_RATIO ratio',
    'Динамика показателя NET_DEBT_TO_EBITDA_RATIO за весь интервал': 'NET_DEBT_TO_EBITDA_RATIO dynamics over the whole interval',
    'Сравнение  NET_DEBT_TO_EBITDA_RATIO с индустрией и аналогами': 'NET_DEBT_TO_EBITDA_RATIO vs. the industry and peers',
    'Коэффициент покрытия процентов (Interest Coverage Ratio, ICR, Times Interest Earned; TIE)': 'Interest coverage ratio (ICR, Times Interest Earned; TIE)',
    'Динамика коэффициента покрытия процентов за 3-5 лет': 'Interest coverage ratio dynamics over 3-5 years',
    'Сравнение ICR с индустрией или компаниями-аналогами': 'ICR vs. the industry or peer companies',
    'Анализ EBITDA (Рентабельность до учета амортизации)': 'EBITDA analysis (profitability before depreciation and amortisation)',
    'Анализ операционной прибыли (EBIT)': 'Operating profit analysis (EBIT)',
    'Анализ расхождений прибыли и денежного потока': 'Profit-to-cash-flow gap analysis',
    'Анализ влияния оборотного капитала на денежный поток': 'Analysis of working capital impact on cash flow',
    'Анализ разовых и нетипичных корректировок': 'Analysis of one-off and non-recurring adjustments',
    'Анализ капитальных затрат (CAPEX)': 'Capital expenditure analysis (CAPEX)',
    'CAPEX (Капитальные затраты)': 'CAPEX (capital expenditure)',
    'Анализ приобретений дочерних компаний (M&A)': 'Subsidiary acquisition analysis (M&A)',
    'Анализ оборачиваемости активов': 'Asset turnover analysis',
    'Анализ рентабельности активов (ROA)': 'Return on assets analysis (ROA)',
    'Комплексный анализ эффективности активов': 'Comprehensive asset efficiency analysis',
    'Анализ оборачиваемости основных средств': 'Fixed asset turnover analysis',
    'Анализ финансового цикла (Cash Conversion Cycle)': 'Cash conversion cycle analysis',
    'Анализ текущей ликвидности': 'Current liquidity analysis',
    'Комплексный анализ оборотного капитала': 'Comprehensive working capital analysis',
    'Анализ рентабельности собственного капитала (ROE)': 'Return on equity analysis (ROE)',
    'Факторный анализ ROE (Модель Дюпона)': 'ROE driver analysis (DuPont model)',
    'Анализ долговой нагрузки (Debt-to-Equity)': 'Leverage analysis (debt-to-equity)',
    'Анализ чистого долга к EBITDA': 'Net debt to EBITDA analysis',
    'Анализ покрытия процентов (ICR)': 'Interest coverage analysis (ICR)',
    'Динамика чистого процентного дохода': 'Net interest income dynamics',
    'Net Interest Margin (NIM)': 'Net Interest Margin (NIM)',
    'Структура портфеля по стадиям обесценения (МСФО 9)': 'Loan book by impairment stage (IFRS 9)',
    'Покрытие резервами по стадиям (ECL coverage)': 'Provision coverage by stage (ECL coverage)',
    'NPL ratio и cost of risk': 'NPL ratio and cost of risk',
    'Структура капитала и RWA': 'Capital structure and RWA',
    'Tier 1 и общая достаточность капитала': 'Tier 1 and total capital adequacy',
    'Структура клиентских средств': 'Structure of customer funds',
    'Loan-to-Deposit ratio (LDR)': 'Loan-to-Deposit ratio (LDR)',
    'Долговые ценные бумаги и оптовое фондирование': 'Debt securities and wholesale funding',
    'ROE банка': 'Bank ROE',
    'ROA банка': 'Bank ROA',
    'Cost-to-Income ratio': 'Cost-to-Income ratio',
    'Доля высоколиквидных активов': 'Share of highly liquid assets',
    'Securities portfolio и обремененные активы': 'Securities portfolio and encumbered assets',
    'LCR / NSFR (если раскрыты)': 'LCR / NSFR (if disclosed)',
    'Анализ чистой процентной маржи (NIM)': 'Net interest margin analysis (NIM)',
    'Качество кредитного портфеля (Stage 1/2/3, NPL)': 'Loan book quality (Stage 1/2/3, NPL)',
    'Достаточность капитала (CET1, Tier 1, RWA)': 'Capital adequacy (CET1, Tier 1, RWA)',
    'Структура фондирования (LDR, депозитная база)': 'Funding structure (LDR, deposit base)',
    'Рентабельность банка (ROE, ROA, CIR)': 'Bank profitability (ROE, ROA, CIR)',
    'Ликвидность и портфель ценных бумаг': 'Liquidity and securities portfolio',
}


def localize_title(title: str) -> str:
    """Chain or step heading in the current reply locale.

    An untranslated title falls through unchanged rather than raising — a
    Russian heading is a cosmetic defect, a failed chain run is not.
    """
    if reply_locale() == "ru":
        return title
    return _TITLES_EN.get(title, title)
