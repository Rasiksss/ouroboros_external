# Оглавление ячейки us_gaap_bank_en — ВСЕ показатели одним списком

Выбирай ПО СМЫСЛУ и передавай ГОТОВЫЕ имена (ENUM). Полная статья
показателя — грепом по `us_gaap_bank_en.md`. Файл сгенерирован из
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
RIGHT_OF_USE_ASSETS — Активы в форме права пользования
NET_INCOME — Чистая прибыль
INTEREST_PAID — Уплаченные проценты
INTEREST_RECEIVED — Полученные проценты
DIVIDENDS — Дивиденды
PRINCIPAL_REPAYMENT_OF_LEASE_LIABILITIES — Погашение обязательств по аренде
TOTAL_LOANS_TO_CLIENTS — Кредиты клиентам
CORPORATE_LOANS — Корпоративные кредиты
INDIVIDUALS_LOANS — Розничные кредиты
CLIENT_DEPOSITS — Депозиты клиентов
COMMON_EQUITY_TIER_ONE_CAPITAL — Основной капитал первого уровня
NET_INTEREST_INCOME — Чистый процентный доход
NON_INTEREST_INCOME_BANKS — Непроцентный доход
TOTAL_INVESTMENT_BANKING_FEES — Доход от инвестиционно-банковского бизнеса
OPERATING_INCOME_BANKS — Операционный доход банков
TOTAL_CREDIT_ALLOWANCE — Резервы под обесценение кредитов и авансов клиентам
OPERATING_EXPENSES_BANKS — Операционные расходы банков
OTHER_OPERATING_INCOME — Прочие Операционные Доходы
INCOME_TAX — Расход по текущему налогу на прибыль
EBT — Прибыль до налогообложения
INTERBANK_DEPOSITS — Средства в банках
INTERBANK_LOANS — Средства банков
SECURITIES_ASSETS — Финансовые инструменты (Актив)
OTHER_ASSETS — Прочие активы
TOTAL_ASSOCIATE_INVESTMENTS — Инвестиции в ассоциированные и совместные предприятия
INDIVIDUALS_DEPOSITS — Средства Физических лиц
CORPORATE_DEPOSITS — Средства корпоративных клиентов
GOVERNMENTAL_DEPOSITS — Средства государственных органов власти
JUNIOR_DEBT — Субординированная задолженность
OTHER_LIABILITIES — Прочие обязательства
SHARE_CAPITAL — Уставной капитал
SHARE_PREMIUM — Эмиссионный доход
TREASURY_SHARES — Собственные акции, выкупленные у акционеров
RETAINED_EARNINGS — Нераспределенная прибыль
NON_CONTROL_INTEREST — Неконтрольные доли участия
DERIVATIVE_LIABILITIES — Производные финансовые обязательства
CASH_AND_CASH_BALANCES_WITH_CB — Обязательные резервы на счетах в центральных банках
FUNDS_FROM_CENTRAL_BANK — Средства, предоставленные ЦБ
INTEREST_INCOME_ER — Процентные доходы, рассчитываемые по методу эффективной процентной ставки
INTEREST_EXPENSE_ER — Процентные расходы, рассчитываемые по методу эффективной процентной ставки
FEE_AND_COMMISSION_INCOME — Комиссионный доход
FEE_AND_COMMISSION_EXPENSE — Комиссионный расход
REPO_PLEDGED_SECURITIES — Ценные бумаги, заложенные по договорам репо
RISK_WEIGHTED_ASSETS — Активы, взвешенные по риску (RWA)
CET1_RATIO_DISCLOSED — Норматив CET1 (раскрываемый)
TIER1_RATIO_DISCLOSED — Норматив Tier 1 (раскрываемый)
EPS — Прибыль на одну обыкновенную акцию
DIVIDENDS_RECEIVED — Дивиденды полученные
CHANGE_IN_EXPECTED_CREDIT_LOSSES — Изменение резерва по ожидаемым кредитным убыткам
DEBT_SECURITIES_ISSUED — Выпущенные долговые ценные бумаги
LEASE_LIABILITIES_BANKS — Обязательства по аренде
NET_INCOME_SHAREHOLDERS — Прибыль, приходящаяся на акционеров материнской компании
EQUITY_SHAREHOLDERS — Капитал, приходящийся на акционеров материнской компании

## Расчётные (считаются формулой по извлечённым)

NET_INTEREST_PAID — Чистые уплаченные проценты | формула: INTEREST_PAID – INTEREST_RECEIVED
ROE — Рентабельность собственного капитала | формула: NET_INCOME / AVG (TOTAL_EQUITY) * 100%
TAX_EXPENDITURE — Общий расход по налогу | формула: EBT - NET_INCOME
EXTREMELY_LIQUID_ASSETS — Высоколиквидные активы | формула: CASH_EQUIVALENTS + INTERBANK_DEPOSITS
NET_OTHER_ASSETS — Чистые прочие активы | формула: OTHER_ASSETS - TOTAL_CREDIT_ALLOWANCE
OTHER_OTHER_ASSETS — Прочие нефинансовые активы | формула: NET_OTHER_ASSETS - PPE - INTANGIBLE_ASSETS - TOTAL_ASSOCIATE_INVESTMENTS
NON_CONSUMER_DEPOSITS — Средства Юридических лиц | формула: CORPORATE_DEPOSITS + GOVERNMENTAL_DEPOSITS
TOTAL_LIABILITIES — Итого обязательства | формула: TOTAL_ASSETS - TOTAL_EQUITY
NET_OTHER_LIABILITIES — Чистые прочие обязательства | формула: OTHER_LIABILITIES - JUNIOR_DEBT
CORPORATE_LOANS_TO_DEPOSITS_RATIO — Отношение кредитов к Депозитам (Юр. лица) | формула: CORPORATE_LOANS  / NON_CONSUMER_DEPOSITS * 100
LOANS_TO_DEPOSITS — Отношение кредитов к Депозитам | формула: TOTAL_LOANS_TO_CLIENTS / CLIENT_DEPOSITS * 100%
DIVIDEND_PAYOUT_RATIO — Коэффициент выплаты дивидендов | формула: DIVIDENDS / NET_INCOME * 100%
COST_TO_INCOME_RATIO — Отношение расходов к доходам (Cost/Income) | формула: OPERATING_EXPENSES_BANKS / OPERATING_INCOME_BANKS * 100%
COST_OF_RISK_GENERAL — Стоимость риска (CoR) | формула: -CHANGE_IN_EXPECTED_CREDIT_LOSSES / AVG(TOTAL_LOANS_TO_CLIENTS) * 100%
INTEREST_EARNING_ASSETS — Процентные активы | формула: TOTAL_LOANS_TO_CLIENTS + SECURITIES_ASSETS + INTERBANK_DEPOSITS
INTEREST_BEARING_LIABILITIES — Процентные обязательства | формула: CLIENT_DEPOSITS + INTERBANK_LOANS + DEBT_SECURITIES_ISSUED + FUNDS_FROM_CENTRAL_BANK + JUNIOR_DEBT
NET_INTEREST_MARGIN — Чистая процентная маржа (NIM) | формула: NET_INTEREST_INCOME / AVG (INTEREST_EARNING_ASSETS) * 100%
COST_TO_ASSETS — Затраты к активам | формула: OPERATING_EXPENSES_BANKS / AVG(TOTAL_ASSETS) * 100%
RETAIL_DEPOSIT_SHARE — Доля розничных депозитов | формула: INDIVIDUALS_DEPOSITS / (INDIVIDUALS_DEPOSITS + CORPORATE_DEPOSITS) * 100%
RWA_DENSITY — Плотность RWA | формула: RISK_WEIGHTED_ASSETS / TOTAL_ASSETS * 100%
NET_COMMISSION_INCOME — Чистый комиссионный доход | формула: FEE_AND_COMMISSION_INCOME - FEE_AND_COMMISSION_EXPENSE
ORGANIC_NET_INCOME — Органическая чистая прибыль | формула: (NET_INTEREST_INCOME + NET_COMMISSION_INCOME - TOTAL_CREDIT_ALLOWANCE - OPERATING_EXPENSES_BANKS) * 0,75
NON_REGULAR_NET_INCOME — Нерегулярная чистая прибыль | формула: NET_INCOME - ORGANIC_NET_INCOME - DEFERRED_TAX_ASSETS
OPERATING_INCOME_CIR — Операционный доход, рассчитанный через CIR | формула: OPERATING_EXPENSES_BANKS / (COST_TO_INCOME_RATIO / 100)
ROA — Рентабельность активов | формула: NET_INCOME / AVG (TOTAL_ASSETS) * 100%
ASSET_YIELD — Доходность процентных активов | формула: INTEREST_INCOME_ER / AVG (INTEREST_EARNING_ASSETS) * 100%
FUNDING_COST — Стоимость фондирования | формула: ABS(INTEREST_EXPENSE_ER) / AVG (INTEREST_BEARING_LIABILITIES) * 100%
ROE_SHAREHOLDERS — Рентабельность капитала акционеров (ROE акционеров) | формула: NET_INCOME_SHAREHOLDERS / AVG (EQUITY_SHAREHOLDERS) * 100%
