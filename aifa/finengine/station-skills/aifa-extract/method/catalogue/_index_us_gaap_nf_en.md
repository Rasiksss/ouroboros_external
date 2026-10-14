# Оглавление ячейки us_gaap_nf_en — ВСЕ показатели одним списком

Выбирай ПО СМЫСЛУ и передавай ГОТОВЫЕ имена (ENUM). Полная статья
показателя — грепом по `us_gaap_nf_en.md`. Файл сгенерирован из
реестра движка; руками не править.

## Извлекаемые (ищутся в отчёте)

MODIFIED_AUDITORS_OPINION — Наличие модифицированного мнения аудитора
LONG_TERM_DEBT — Долгосрочные кредиты и займы
LONG_TERM_LEASE_LIABILITIES — Долгосрочные обязательства по аренде
SHORT_TERM_BORROWINGS — Краткосрочные кредиты и займы
SHORT_TERM_LEASE_LIABILITIES — Краткосрочные обязательства по аренде
CASH_EQUIVALENTS — Денежные средства и их эквиваленты
BANK_DEPOSITS — Банковские депозиты
PERPETUAL_BONDS — Бессрочные облигации
ACCOUNTS_PAYABLE — Кредиторская задолженность
ACCOUNTS_RECEIVABLE — Дебиторская задолженность
INVENTORIES — Запасы
TOTAL_CURRENT_ASSETS — Итого оборотных активов
TOTAL_CURRENT_LIABILITIES — Итого краткосрочных обязательств
TOTAL_ASSETS — Итого активы
PPE — Основные средства
DEFERRED_TAX_ASSETS — Отложенные налоговые активы
DEFERRED_TAX_LIABILITIES — Отложенные налоговые обязательства
INTANGIBLE_ASSETS — Нематериальные активы
GOODWILL — Гудвил
TOTAL_EQUITY — Итого собственный капитал
RIGHT_OF_USE_ASSETS — Активы в форме права пользования
SALES_REVENUE — Выручка
OPERATING_PROFIT — Операционная прибыль
COSTS_EXPENSES — Себестоимость
NET_INCOME — Чистая прибыль
EBT — Прибыль до налогообложения
CAPITALIZED_INTEREST — Капитализированные и уплаченные проценты
GROSS_PROFIT — Валовая прибыль
OPERATING_EXPENSES — Операционные расходы
DEPRECIATION_AMORTIZATION — Амортизация материальных и нематериальных активов
CAPEX — Капитальные затраты
INTEREST_PAID — Уплаченные проценты
INTEREST_RECEIVED — Полученные проценты
DIVIDENDS_SHAREHOLDERS — Дивиденды акционерам
DIVIDENDS_PAID_NONCONTROLLING_INTERESTS — Дивиденды, выплаченные держателям неконтролирующих долей
DIVIDENDS — Дивиденды
SHARES_BUYBACK — Выкуп собственных акций
NET_OPERATING_CASH_FLOW — Чистый операционный денежный поток
SUBSIDIARY_ACQUISITIONS — Приобретение дочерних организаций (обществ) за вычетом (минусом) денежных средств и за вычетом (минусом) полученных денежных средств
ASSOCIATE_INVESTMENTS — Вложения в ассоциированные организации (общества) и совместные предприятия
ASSOCIATE_INCOMES — Доходы от ассоциированных компаний
ASSET_PURCHASES — Приобретение основных средств
INTANGIBLE_ASSET_PURCHASES — Приобретение нематериальных активов
COMBINED_ASSET_PURCHASES — Приобретение основных средств и нематериальных активов
ASSET_SALES_PROCEEDS — Поступления от продажи основных средств и нематериальных активов
FINANCIAL_INCOME — Финансовые доходы
FINANCIAL_EXPENSES — Финансовые расходы
PRINCIPAL_REPAYMENT_OF_LEASE_LIABILITIES — Погашение обязательств по аренде
ADJUSTMENTS_OPERATING_CASH_FLOWS — Корректировки
CHANGES_OCF_ASSETS_LIABILITIES — Изменения в оборотном капитале
EBITDA — Прибыль до вычета процентов, налогов, износа и амортизации
GUARANTEES — Гарантии, поручительства
COMMITTED_CREDIT_FACILITIES — Неиспользованные кредитные линии
FUTURE_CAPEX_LIABILITIES — Обязательства будущих периодов по капитальным затратам
DOUBTFUL_ACCOUNTS_ALLOWANCE — Резерв под ожидаемые кредитные убытки (дебиторская задолженность)
INCOME_TAX — Расход по текущему налогу на прибыль
SECURITIES_ASSETS — Финансовые инструменты (Актив)
TOTAL_ASSOCIATE_INVESTMENTS — Инвестиции в ассоциированные и совместные предприятия
SECURITIES_LIABILITIES — Финансовые инструменты (Обязательства)
LONG_TERM_ROU — Долгосрочные активы в форме права пользования
SHORT_TERM_ROU — Краткосрочные активы в форме права пользования
ROU_DEPRECIATION — Амортизация активов в форме права пользования
INTEREST_PAID_LEASE_LIABILITIES — Уплаченные проценты по лизингу
SHARE_CAPITAL — Уставной капитал
SHARE_PREMIUM — Эмиссионный доход
TREASURY_SHARES — Собственные акции, выкупленные у акционеров
RETAINED_EARNINGS — Нераспределенная прибыль
NON_CONTROL_INTEREST — Неконтрольные доли участия
DERIVATIVE_LIABILITIES — Производные финансовые обязательства
INSURANCE_LIABILITIES — Обязательства по договорам страхования
INSURANCE_INCOME — Выручка по страховой деятельности
INSURANCE_EXPENSE — Расходы по страховой деятельности
NET_OTHER_RESERVES — Чистое восстановление прочих резервов
EPS — Прибыль на одну обыкновенную акцию
DIVIDENDS_RECEIVED — Дивиденды полученные
NET_INCOME_SHAREHOLDERS — Прибыль, приходящаяся на акционеров материнской компании
EQUITY_SHAREHOLDERS — Капитал, приходящийся на акционеров материнской компании

## Расчётные (считаются формулой по извлечённым)

CAPEX — Капитальные затраты | формула: ASSET_PURCHASES + INTANGIBLE_ASSET_PURCHASES - ASSET_SALES_PROCEEDS
DIVIDENDS — Дивиденды и уплаченные дивиденды | формула: DIVIDENDS_SHAREHOLDERS + DIVIDENDS_PAID_NONCONTROLLING_INTERESTS
TOTAL_DEBT — Общий долг | формула: LONG_TERM_DEBT + LONG_TERM_LEASE_LIABILITIES + SHORT_TERM_BORROWINGS + SHORT_TERM_LEASE_LIABILITIES
NET_DEBT — Чистый долг | формула: TOTAL_DEBT – CASH_EQUIVALENTS - BANK_DEPOSITS
EBITDA — Прибыль до вычета процентов, налогов, износа и амортизации | формула: OPERATING_PROFIT + DEPRECIATION_AMORTIZATION
RATIO_NET_DEBT_EBITDA — Чистый долг / Прибыль до вычета процентов, налогов, износа и амортизации | формула: NET_DEBT / ANNUAL(EBITDA)
NET_INTEREST_PAID — Чистые уплаченные проценты | формула: INTEREST_PAID – INTEREST_RECEIVED
EBITDA_MARGIN — Рентабельность по EBITDA | формула: EBITDA / SALES_REVENUE * 100%
INVESTMENTS — Инвестиции | формула: SUBSIDIARY_ACQUISITIONS + ASSOCIATE_INVESTMENTS – ASSOCIATE_INCOMES
FREE_CASH_FLOW — Свободный денежный поток | формула: NET_OPERATING_CASH_FLOW – CAPEX
FREE_CASH_FLOW_FIRM — Свободный денежный поток (для кредиторов) | формула: FREE_CASH_FLOW – INVESTMENTS – DIVIDENDS
COMBINED_ASSET_PURCHASES — Приобретение основных средств и нематериальных активов | формула: ASSET_PURCHASES + INTANGIBLE_ASSET_PURCHASES
GROSS_PROFIT — Валовая прибыль | формула: SALES_REVENUE – COSTS_EXPENSES
EBIT_MARGIN — Рентабельность по EBIT | формула: OPERATING_PROFIT / SALES_REVENUE * 100%
DEBT_TO_EQUITY_RATIO — Коэффициент левериджа | формула: TOTAL_DEBT / TOTAL_EQUITY
ACCOUNTS_PAYABLE_TURNOVER — Оборачиваемость кредиторской задолженности | формула: DAYS_IN_PERIOD / (COSTS_EXPENSES / AVG (ACCOUNTS_PAYABLE))
ACCOUNTS_RECEIVABLE_TURNOVER — Оборачиваемость дебиторской задолженности | формула: DAYS_IN_PERIOD / (SALES_REVENUE / AVG (ACCOUNTS_RECEIVABLE))
INVENTORIES_TURNOVER — Оборачиваемость запасов | формула: DAYS_IN_PERIOD / (COSTS_EXPENSES / AVG (INVENTORIES))
CASH_CONVERSION_CYCLE — Финансовый (денежный) цикл | формула: ACCOUNTS_RECEIVABLE_TURNOVER + INVENTORIES_TURNOVER - ACCOUNTS_PAYABLE_TURNOVER
CURRENT_RATIO — Коэффициент текущей ликвидности | формула: TOTAL_CURRENT_ASSETS / TOTAL_CURRENT_LIABILITIES
ROE — Рентабельность собственного капитала | формула: NET_INCOME / AVG (TOTAL_EQUITY) * 100%
ASSETS_TO_EQUITY_RATIO — Финансовый рычаг | формула: TOTAL_ASSETS / TOTAL_EQUITY
INTEREST_COVERAGE_RATIO — Коэффициент покрытия процентов | формула: EBITDA / NET_INTEREST_PAID
NET_INCOME_MARGIN — Рентабельность по чистой прибыли | формула: NET_INCOME / SALES_REVENUE * 100%
ASSET_TURNOVER — Оборачиваемость активов | формула: SALES_REVENUE / AVG (TOTAL_ASSETS)
FIXED_ASSET_TURNOVER — Оборачиваемость основных средств | формула: SALES_REVENUE / AVG (RIGHT_OF_USE_ASSETS + PPE)
ROA — Рентабельность активов | формула: NET_INCOME / AVG (TOTAL_ASSETS) * 100%
TOTAL_CASH_EQUIVALENTS — Денежные средства и краткосрочные депозиты | формула: CASH_EQUIVALENTS + BANK_DEPOSITS
RATIO_TOTAL_DEBT_NOCF — Отношение долга к чистому денежному потоку от операционной деятельности | формула: TOTAL_DEBT / ANNUAL(NET_OPERATING_CASH_FLOW)
EBITDA_TO_INTEREST_PAID — Отношение EBITDA к уплаченным процентам | формула: EBITDA / INTEREST_PAID
FCF_TO_INTEREST_PAID — Отношение свободного денежного потока у процентам уплаченным | формула: FREE_CASH_FLOW / INTEREST_PAID
CASH_TO_SHORT_TERM_DEBT — Отношение денежных средств и эквивалентов к краткосрочному долгу | формула: TOTAL_CASH_EQUIVALENTS / (SHORT_TERM_BORROWINGS + SHORT_TERM_LEASE_LIABILITIES) * 100%
SHORT_TERM_PORTION_OF_DEBT — Краткосрочная часть долга | формула: (SHORT_TERM_BORROWINGS + SHORT_TERM_LEASE_LIABILITIES) / TOTAL_DEBT * 100%
RATIO_TOTAL_DEBT_EBITDA — Отношение общего долга к EBITDA | формула: TOTAL_DEBT / ANNUAL(EBITDA)
TOTAL_LIABILITIES — Итого обязательства | формула: TOTAL_ASSETS - TOTAL_EQUITY
CAPEX_TO_REVENUE — Отношение капитальных затрат к выручке | формула: CAPEX / SALES_REVENUE * 100%
GROSS_MARGIN — Рентабельность по валовой прибыли | формула: GROSS_PROFIT / SALES_REVENUE * 100%
DIVIDEND_PAYOUT_RATIO — Коэффициент выплаты дивидендов | формула: DIVIDENDS / NET_INCOME * 100%
RIGHT_OF_USE_ASSETS — Активы в форме права пользования | формула: SHORT_TERM_ROU + LONG_TERM_ROU
NET_DEBT_IAS_17 — Чистый долг в соответствии с IAS 17 | формула: TOTAL_DEBT – CASH_EQUIVALENTS
TOTAL_DEBT_IAS_17 — Общий долг в соответствии с IAS 17 | формула: LONG_TERM_DEBT + SHORT_TERM_BORROWINGS
TAX_EXPENDITURE — Общий расход по налогу | формула: EBT - NET_INCOME
FREE_CASH_FLOW_GEN — Свободный денежный поток | формула: EBITDA + CHANGES_OCF_ASSETS_LIABILITIES - TAX_EXPENDITURE - CAPEX - NET_INTEREST_PAID - DIVIDENDS + DIVIDENDS_RECEIVED
FREE_CASH_FLOW_DEBT — Свободный денежный поток (для кредиторов) | формула: FREE_CASH_FLOW_GEN – INVESTMENTS – DIVIDENDS_SHAREHOLDERS - PRINCIPAL_REPAYMENT_OF_LEASE_LIABILITIES
ADJ_OCF_TO_INTEREST_PAID — Отношение скорректированного  операционного денежного потока у процентам уплаченным | формула: (FREE_CASH_FLOW_DEBT + CAPEX + INTEREST_PAID)/ INTEREST_PAID
NET_INSURANCE_INCOME — Чистый страховой доход | формула: INSURANCE_INCOME - INSURANCE_EXPENSE
FCF_TO_INTEREST_PAID_CIB — Отношение свободного денежного потока к процентам уплаченным (CIB) | формула: FREE_CASH_FLOW_GEN / INTEREST_PAID
QUICK_RATIO — Коэффициент быстрой ликвидности | формула: (TOTAL_CURRENT_ASSETS - INVENTORIES) / TOTAL_CURRENT_LIABILITIES
WORKING_CAPITAL — Оборотный капитал | формула: TOTAL_CURRENT_ASSETS - TOTAL_CURRENT_LIABILITIES
OCF_TO_EBITDA — Конверсия EBITDA в операционный поток | формула: NET_OPERATING_CASH_FLOW / EBITDA * 100%
ROE_SHAREHOLDERS — Рентабельность капитала акционеров (ROE акционеров) | формула: NET_INCOME_SHAREHOLDERS / AVG (EQUITY_SHAREHOLDERS) * 100%
