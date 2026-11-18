# Оглавление ячейки ind_gaap_bank_en — ВСЕ показатели одним списком

Выбирай ПО СМЫСЛУ и передавай ГОТОВЫЕ имена (ENUM). Полная статья
показателя — грепом по `ind_gaap_bank_en.md`. Файл сгенерирован из
реестра движка; руками не править.

## Извлекаемые (ищутся в отчёте)

MODIFIED_AUDITORS_OPINION — Наличие модифицированного мнения аудитора
CASH_EQUIVALENTS — Денежные средства и их эквиваленты
TOTAL_ASSETS — Итого активы
PPE — Основные средства
DEFERRED_TAX_ASSETS — Отложенные налоговые активы
DEFERRED_TAX_LIABILITIES — Отложенные налоговые обязательства
INTANGIBLE_ASSETS — Нематериальные активы
GOODWILL — Гудвил
TOTAL_EQUITY — Итого собственный капитал
NET_INCOME — Чистая прибыль
DIVIDENDS — Дивиденды
CHANGES_OCF_ASSETS_LIABILITIES — Изменения в оборотном капитале
TOTAL_LOANS_TO_CLIENTS — Кредиты клиентам
CLIENT_DEPOSITS — Депозиты клиентов
COMMON_EQUITY_TIER_ONE_CAPITAL — Основной капитал первого уровня
OPERATING_EXPENSES_BANKS — Операционные расходы банков
INCOME_TAX — Расход по текущему налогу на прибыль
EBT — Прибыль до налогообложения
INTERBANK_DEPOSITS — Средства в банках
INTERBANK_LOANS — Средства банков
OTHER_ASSETS — Прочие активы
TOTAL_ASSOCIATE_INVESTMENTS — Инвестиции в ассоциированные и совместные предприятия
JUNIOR_DEBT — Субординированная задолженность
OTHER_LIABILITIES — Прочие обязательства
SHARE_CAPITAL — Уставной капитал
SHARE_PREMIUM — Эмиссионный доход
RETAINED_EARNINGS — Нераспределенная прибыль
NON_CONTROL_INTEREST — Неконтрольные доли участия
DERIVATIVE_LIABILITIES — Производные финансовые обязательства
CASH_AND_CASH_BALANCES_WITH_CB — Обязательные резервы на счетах в центральных банках
FUNDS_FROM_CENTRAL_BANK — Средства, предоставленные ЦБ
REPO_PLEDGED_SECURITIES — Ценные бумаги, заложенные по договорам репо
RISK_WEIGHTED_ASSETS — Активы, взвешенные по риску (RWA)
CET1_RATIO_DISCLOSED — Норматив CET1 (раскрываемый)
TIER1_RATIO_DISCLOSED — Норматив Tier 1 (раскрываемый)
EPS — Прибыль на одну обыкновенную акцию
DIVIDENDS_RECEIVED — Дивиденды полученные
DEBT_SECURITIES_ISSUED — Выпущенные долговые ценные бумаги
INDIVIDUALS_LOANS — Розничные кредиты
NET_INTEREST_INCOME — Чистый процентный доход
CORPORATE_LOANS — Корпоративные кредиты
PERPETUAL_BONDS — Бессрочные облигации
TOTAL_GROSS_LOANS_TO_CLIENTS — Валовые кредиты клиентам
GROSS_NPA — Валовые необслуживаемые активы (NPA)
NET_NPA — Чистые необслуживаемые активы (NPA)
GROSS_NPA_RATIO_DISCLOSED — Раскрытый коэффициент валовых NPA
NET_NPA_RATIO_DISCLOSED — Раскрытый коэффициент чистых NPA
PROVISION_COVERAGE_RATIO — Коэффициент покрытия резервами
PROVISION_FOR_NPA — Резерв под необслуживаемые активы (NPA)
PROVISIONS_AND_CONTINGENCIES — Расходы на резервы и условные обязательства
INTEREST_EARNED_TOTAL — Процентные доходы
INTEREST_EXPENDED_TOTAL — Процентные расходы
OTHER_INCOME_TOTAL — Прочие доходы
OPERATING_PROFIT_BEFORE_PROVISIONS — Операционная прибыль до резервов
DEMAND_DEPOSITS — Депозиты до востребования
SAVINGS_BANK_DEPOSITS — Сберегательные депозиты
TERM_DEPOSITS — Срочные депозиты
CASA_RATIO_DISCLOSED — Раскрытый коэффициент CASA
PRIORITY_SECTOR_ADVANCES — Кредиты приоритетным секторам
GOVERNMENT_SECURITIES — Государственные ценные бумаги
NET_INVESTMENTS — Чистые инвестиции
PROVISION_FOR_DEPRECIATION_ON_INVESTMENTS_CLOSING — Резерв под снижение стоимости инвестиций
TIER1_CAPITAL — Капитал первого уровня
TIER2_CAPITAL — Капитал второго уровня
CRAR_RATIO_DISCLOSED — Раскрытый коэффициент достаточности капитала (CRAR)
LCR_DISCLOSED — Раскрытый коэффициент покрытия ликвидностью (LCR)
CONTINGENT_LIABILITIES_TOTAL — Условные обязательства
STATUTORY_RESERVE — Обязательный резерв
COMMISSION_EXCHANGE_BROKERAGE — Комиссии, обменные операции и брокерские доходы
PROFIT_LOSS_ON_EXCHANGE_TRANSACTIONS — Прибыль или убыток от обменных операций
NET_INCOME_SHAREHOLDERS — Прибыль, приходящаяся на акционеров материнской компании
EQUITY_SHAREHOLDERS — Капитал, приходящийся на акционеров материнской компании
COST_TO_INCOME_RATIO — Коэффициент затрат к доходам

## Расчётные (считаются формулой по извлечённым)

ROE — Рентабельность собственного капитала | формула: NET_INCOME / AVG(TOTAL_EQUITY) * 100%
TAX_EXPENDITURE — Общий расход по налогу | формула: EBT - NET_INCOME
EXTREMELY_LIQUID_ASSETS — Высоколиквидные активы | формула: CASH_EQUIVALENTS + INTERBANK_DEPOSITS
TOTAL_LIABILITIES — Итого обязательства | формула: TOTAL_ASSETS - TOTAL_EQUITY
NET_OTHER_LIABILITIES — Чистые прочие обязательства | формула: OTHER_LIABILITIES - JUNIOR_DEBT
LOANS_TO_DEPOSITS — Отношение кредитов к Депозитам | формула: TOTAL_LOANS_TO_CLIENTS / CLIENT_DEPOSITS * 100%
DIVIDEND_PAYOUT_RATIO — Коэффициент выплаты дивидендов | формула: DIVIDENDS / NET_INCOME * 100%
INTEREST_BEARING_LIABILITIES — Процентные обязательства | формула: CLIENT_DEPOSITS + INTERBANK_LOANS + DEBT_SECURITIES_ISSUED + FUNDS_FROM_CENTRAL_BANK + JUNIOR_DEBT
COST_TO_ASSETS — Затраты к активам | формула: OPERATING_EXPENSES_BANKS / AVG(TOTAL_ASSETS) * 100%
RWA_DENSITY — Плотность RWA | формула: RISK_WEIGHTED_ASSETS / TOTAL_ASSETS * 100%
NET_INTEREST_INCOME — Чистый процентный доход | формула: INTEREST_EARNED_TOTAL - INTEREST_EXPENDED_TOTAL
OPERATING_PROFIT_BEFORE_PROVISIONS — Операционная прибыль до резервов | формула: NET_INTEREST_INCOME + OTHER_INCOME_TOTAL - OPERATING_EXPENSES_BANKS
CASA_DEPOSITS — Депозиты CASA | формула: DEMAND_DEPOSITS + SAVINGS_BANK_DEPOSITS
CASA_RATIO — Коэффициент CASA | формула: CASA_DEPOSITS / CLIENT_DEPOSITS * 100%
GROSS_NPA_RATIO — Коэффициент валовых NPA | формула: GROSS_NPA / TOTAL_GROSS_LOANS_TO_CLIENTS * 100%
NET_NPA_RATIO — Коэффициент чистых NPA | формула: NET_NPA / TOTAL_LOANS_TO_CLIENTS * 100%
PROVISION_COVERAGE_RATIO — Коэффициент покрытия резервами | формула: PROVISION_FOR_NPA / GROSS_NPA * 100%
TOTAL_REGULATORY_CAPITAL — Совокупный регуляторный капитал | формула: TIER1_CAPITAL + TIER2_CAPITAL
CRAR_RATIO — Коэффициент достаточности капитала (CRAR) | формула: TOTAL_REGULATORY_CAPITAL / RISK_WEIGHTED_ASSETS * 100%
ROA — Рентабельность активов | формула: NET_INCOME / AVG (TOTAL_ASSETS) * 100%
ROE_SHAREHOLDERS — Рентабельность капитала акционеров (ROE акционеров) | формула: NET_INCOME_SHAREHOLDERS / AVG (EQUITY_SHAREHOLDERS) * 100%
