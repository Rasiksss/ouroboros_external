# Ячейка us_gaap_bank_en — банки, US GAAP, отчётность на английском

* стандарт отчётности: **US GAAP**
* единица измерения по умолчанию: **млн. $**
* подпись периода, когда своей в документе нет: **Current**

Всё ниже до первой записи `## <ENUM>` — ПРАВИЛА ЯЧЕЙКИ: как читать
отчётность этого стандарта. Они уезжают в промпт целиком. Правятся
текстом, без программиста. Правила конкретного показателя — в его
записи, поле `hint`.
### Additional guardrails

1. **CECL, not IFRS-9 ECL.** Under US GAAP (ASC 326, effective 2020) banks use **Current Expected Credit Loss** — a single lifetime-loss model for ALL loans, not the IFRS 9 Stage 1/2/3 three-bucket structure. Don't look for "Stage 1/2/3" in a 10-K; do look for:
   - **Allowance for credit losses (ACL)** — the CECL allowance on loans HFI
   - **Provision for credit losses (PCL)** — period charge to P&L
   - Legacy term **Allowance for loan and lease losses (ALLL)** in pre-2020 comparatives
   ASC 326 also covers AFS securities, off-balance-sheet credit exposures, and reinsurance recoverables.

2. **Basel III adapted by US regulators.** Capital ratios disclosed:
   - **CET1 ratio** (Common Equity Tier 1)
   - **Tier 1 capital ratio**
   - **Total capital ratio**
   - **SLR** (Supplementary Leverage Ratio) — applies to large banks
   - **LCR** (Liquidity Coverage Ratio), **NSFR** (Net Stable Funding Ratio) — for G-SIBs
   These typically live in the "Capital Management" or "Regulatory Capital" notes, separate from the P&L / balance sheet. Russian Central Bank capital normatives do NOT apply here.

3. **Loan portfolio breakdown.** When the registry asks for a corporate vs retail split and the report provides loan classes, map:
   - Commercial & Industrial (C&I)
   - Commercial Real Estate (CRE)
   - Residential mortgage / 1-4 family
   - Home Equity
   - Credit Cards
   - Auto
   - Construction
   C&I + CRE + Construction are corporate; residential + consumer (cards + auto + home equity) are retail.

4. **Restatements / mergers.** Trust the current filing's restated comparatives over an old standalone filing.

5. **Audited statements over MD&A.** Audited numbers usually live in **Item 8 (Financial Statements and Supplementary Data)** and the **Notes to Consolidated Financial Statements**. Item 7 (MD&A) discusses results in prose; treat its numbers as illustrative — prefer Item 8 figures when both exist.

## CASH_AND_CASH_BALANCES_WITH_CB
name_ru: Обязательные резервы на счетах в центральных банках
name_en: Cash and cash balances with central bank
aliases_en: Mandatory reserves with central bank; Reserve deposits with central bank
form: Баланс
hint: Балансовый отчёт банка, актив. Часто первая строка раздела активов.
example: `| Cash and cash balances with central bank | 188,500 | 165,200 |` → на отчётную дату 188,500
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Обязательные резервы и средства банка на счетах в центральных банках.

## CASH_EQUIVALENTS
name_ru: Денежные средства и их эквиваленты
name_en: Cash and cash equivalents
aliases_en: Cash and equivalents; Cash and cash equivalents at end of period
form: Баланс
hint: Оборотные активы баланса или последняя строка ОДДС. Возвращай все явно раскрытые даты cash and cash equivalents at end of period, включая сравнительные даты, которых нет в балансе. Не путай с bank deposits/time deposits и marketable securities.
example: `| Cash and cash equivalents | 1,204,883 | 1,145,210 |` → на отчётную дату 1,204,883
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Касса, расчётные счета и краткосрочные высоколиквидные инвестиции (срок ≤ 3 мес.).

## CET1_RATIO_DISCLOSED
name_ru: Норматив CET1 (раскрываемый)
name_en: CET1 ratio (disclosed)
aliases_en: CET1 capital ratio; Common equity Tier 1 ratio
form: Примечания
hint: Примечание 'Достаточность капитала'. Единица — проценты.
example: `| CET1 ratio (disclosed) | 12.4% | 11.8% |` → на отчётную дату 12,4
unit_kind: percent
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Раскрываемый банком норматив достаточности базового капитала первого уровня.

## CHANGE_IN_EXPECTED_CREDIT_LOSSES
name_ru: Изменение резерва по ожидаемым кредитным убыткам
name_en: Change in ECL
aliases_en: Movement in ECL allowance; Changes in ECL allowance
form: P&L
hint: P&L банка или Примечание 'Финансовые активы'. Сумма движений ECL за период. В interim-отчетах с колонками 3M/Q и 6M/YTD возвращай обе группы периодов, не только YTD.
example: `| Change in ECL | (128,400) | (96,200) |` → за отчётный период (128,400)
unit_kind: money
sign: любой
period_type: duration
standard: Любой
formula_default_abs_behavior: нет

Изменение оценочных резервов ECL за период (по всем финансовым активам, не только кредитам).

## CLIENT_DEPOSITS
name_ru: Депозиты клиентов
name_en: Total deposits
aliases_en: Due to customers; Customer deposits
form: Баланс
hint: Балансовый отчёт банка, ключевое обязательство. Детализация в Примечании 'Средства клиентов'.
example: `| Total deposits | 11,204,500 | 9,880,100 |` → на отчётную дату 11,204,500
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Совокупный объём средств клиентов на счетах и депозитах банка.

## COMMON_EQUITY_TIER_ONE_CAPITAL
name_ru: Основной капитал первого уровня
name_en: Common equity tier 1 capital
aliases_en: CET1 capital; Common equity Tier 1 capital ratio
form: Примечания
hint: Примечание 'Достаточность капитала' / 'Управление капиталом'.
example: `| Common equity tier 1 capital | 1,402,300 | 1,288,900 |` → на отчётную дату 1,402,300
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Базовый капитал первого уровня по Базель III.

## CORPORATE_DEPOSITS
name_ru: Средства корпоративных клиентов
name_en: Corporate deposits
aliases_en: Corporate deposits; Business deposits; Deposits from legal entities
form: Примечания
hint: Примечание 'Средства клиентов' — разбивка по сегментам.
example: `| Corporate deposits | 4,102,300 | 3,640,700 |` → на отчётную дату 4,102,300
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Депозиты юридических лиц.

## CORPORATE_LOANS
name_ru: Корпоративные кредиты
name_en: Total commercial loans and leases
aliases_en: Corporate loans; Commercial loans
form: Примечания
hint: Примечание 'Кредиты клиентам' — разбивка по сегментам клиентов.
example: `| Total commercial loans and leases | 5,902,100 | 5,118,400 |` → на отчётную дату 5,902,100
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Кредиты, выданные юридическим лицам.

## DEBT_SECURITIES_ISSUED
name_ru: Выпущенные долговые ценные бумаги
name_en: Debt securities issued
aliases_en: Bonds issued; Notes and bonds issued
form: Баланс
hint: Балансовый отчёт банка, обязательство. Детализация в Примечании 'Долговые ценные бумаги'.
example: `| Debt securities issued | 402,800 | 355,100 |` → на отчётную дату 402,800
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Облигации и прочие долговые ценные бумаги, эмитированные банком.

## DEFERRED_TAX_ASSETS
name_ru: Отложенные налоговые активы
name_en: Deferred tax assets
aliases_en: Deferred income tax assets
form: Баланс
hint: Внеоборотные активы. Подробности в Примечании 'Налог на прибыль' / 'Отложенные налоги'.
example: `| Deferred tax assets | 12,480 | 9,902 |` → на отчётную дату 12,480
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Сумма налогов на прибыль к уменьшению в будущих периодах за счёт вычитаемых временных разниц.

## DEFERRED_TAX_LIABILITIES
name_ru: Отложенные налоговые обязательства
name_en: Deferred tax liabilities
aliases_en: Deferred income tax liabilities
form: Баланс
hint: Долгосрочные обязательства баланса. Подробности в Примечании 'Налог на прибыль' / 'Отложенные налоги'.
example: `| Deferred tax liabilities | 8,140 | 7,655 |` → на отчётную дату 8,140
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Налоги на прибыль, подлежащие уплате в будущих периодах из-за налогооблагаемых временных разниц.

## DERIVATIVE_LIABILITIES
name_ru: Производные финансовые обязательства
name_en: Derivatives
aliases_en: Derivative financial instruments; Derivative liabilities
form: Баланс
hint: Балансовый отчёт. Также в Примечании 'Финансовые инструменты' / 'Управление рисками'.
example: `| Derivatives | 44,200 | 51,900 |` → на отчётную дату 44,200
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Обязательства по производным финансовым инструментам, оцениваемые по справедливой стоимости.

## DIVIDENDS
name_ru: Дивиденды
name_en: Dividends
aliases_en: Dividends paid
form: ОДДС
hint: ОДДС, раздел «Финансовая деятельность»: «Дивиденды выплаченные»/«Дивиденды уплаченные» за период. Обычно денежный отток — сохраняй минус/скобки из источника. Не бери объявленные дивиденды из примечания о событиях после отчётной даты и не бери дивиденды полученные — это DIVIDENDS_RECEIVED.
example: `| Dividends | (210,400) | (165,000) |` → за отчётный период (210,400)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Денежные выплаты дивидендов акционерам за период — отток по финансовой деятельности.

## DIVIDENDS_RECEIVED
name_ru: Дивиденды полученные
name_en: Dividends received
aliases_en: Cash dividends received
form: ОДДС
hint: ОДДС, раздел 'Инвестиционная деятельность'.
example: `| Dividends received | 4,800 | 3,900 |` → за отчётный период 4,800
unit_kind: money
sign: положительный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Денежные поступления дивидендов от ассоциированных и прочих компаний.

## EBT
name_ru: Прибыль до налогообложения
name_en: EBT
aliases_en: Profit before tax; Income before income taxes; Pre-tax income
form: P&L
hint: P&L банка, предпоследняя строка перед налогом на прибыль. Если у банка убыток, в источнике обычно стоят скобки или минус; сохраняй знак/скобки как в источнике.
example: `| EBT | 633,200 | 539,900 |` → за отчётный период 633,200
unit_kind: money
sign: любой
period_type: duration
standard: Любой
formula_default_abs_behavior: нет

Прибыль до вычета налога на прибыль — финальная строка P&L перед налогом.

## EPS
name_ru: Прибыль на одну обыкновенную акцию
name_en: Earnings per ordinary share
aliases_en: Earnings per share; EPS; Basic EPS; Basic earnings per share
form: P&L
hint: P&L: строка Basic earnings per share после net income. Бери Basic, не Diluted; значение может быть дробным и обычно имеет единицу валюта/share, не millions.
example: `| Earnings per ordinary share | 23,45 | 19,87 |` → за отчётный период 23,45
unit_kind: money
sign: любой
period_type: duration
standard: Любой
formula_default_abs_behavior: нет

Чистая прибыль на одну обыкновенную акцию (валюта на акцию, не в миллионах).

## EQUITY_SHAREHOLDERS
name_ru: Капитал, приходящийся на акционеров материнской компании
name_en: Equity attributable to owners of the parent
aliases_en: Equity attributable to equity holders of the parent; Total shareholders' equity attributable to the parent; Equity attributable to owners of the Company
form: Баланс
hint: Баланс, раздел КАПИТАЛ: «Итого капитал, приходящийся на акционеров материнской компании» — строка ДО «Неконтролирующие доли». НЕ путать с итогом капитала (тот включает неконтролирующие доли). Нет отдельной строки — значит неконтролирующих долей нет, бери итог капитала.
example: `| Equity attributable to owners of the parent | 1,499,604 | 1,390,200 |` → на отчётную дату 1,499,604
unit_kind: money
sign: любой
period_type: point
standard: Любой
formula_default_abs_behavior: нет

Собственный капитал за вычетом неконтролирующих долей — часть капитала, принадлежащая акционерам материнской компании.

## FEE_AND_COMMISSION_EXPENSE
name_ru: Комиссионный расход
name_en: Fee and comission expense
aliases_en: Fee expense; Commission expense
form: P&L
hint: P&L банка, после комиссионных доходов. Бери строку комиссионных расходов и сохраняй знак/скобки как в источнике.
example: `| Fee and comission expense | (56,400) | (47,100) |` → за отчётный период (56,400)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Расходы по комиссионным операциям (в т.ч. платежи системам, контрагентам).

## FEE_AND_COMMISSION_INCOME
name_ru: Комиссионный доход
name_en: Fee and comission income
aliases_en: Fee income; Commission income
form: P&L
hint: P&L банка. Детализация в Примечании 'Комиссионные доходы и расходы'.
example: `| Fee and comission income | 244,800 | 212,300 |` → за отчётный период 244,800
unit_kind: money
sign: положительный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Доходы от комиссионных и расчётно-кассовых операций.

## FUNDS_FROM_CENTRAL_BANK
name_ru: Средства, предоставленные ЦБ
name_en: Funds provided by the Central Bank
aliases_en: Loans from central bank; Borrowings from the central bank
form: Баланс
hint: Балансовый отчёт банка, обязательство. Также в Примечании 'Привлечённые средства'.
example: `| Funds provided by the Central Bank | 320,000 | 145,000 |` → на отчётную дату 320,000
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Кредиты и депозиты, полученные банком от центрального банка.

## GOODWILL
name_ru: Гудвил
name_en: Goodwill
aliases_en: Goodwill
form: Баланс
hint: Внеоборотные активы баланса. Подробнее — в Примечании 'Гудвил' / 'Объединение бизнеса'.
example: `| Goodwill | 21,300 | 21,300 |` → на отчётную дату 21,300
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Превышение цены приобретения дочерней компании над справедливой стоимостью её чистых идентифицируемых активов.

## GOVERNMENTAL_DEPOSITS
name_ru: Средства государственных органов власти
name_en: Governmental deposits
aliases_en: Government deposits; Deposits from government entities
form: Примечания
hint: Примечание 'Средства клиентов' — разбивка по сегментам.
example: `| Governmental deposits | 222,000 | 224,000 |` → на отчётную дату 222,000
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Депозиты госорганов и муниципальных образований.

## INCOME_TAX
name_ru: Расход по текущему налогу на прибыль
name_en: Income tax expense
aliases_en: Tax expense; Provision for income taxes; Income tax provision (benefit)
form: P&L
hint: P&L: итоговая строка income tax provision/benefit перед net income. Не бери cash paid for taxes и не заменяй итоговую строку отдельными current/deferred tax components.
example: `| Income tax expense | (124,300) | (108,900) |` → за отчётный период (124,300)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Сумма налога на прибыль за период (текущий + отложенный).

## INDIVIDUALS_DEPOSITS
name_ru: Средства Физических лиц
name_en: Consumer deposits
aliases_en: Retail deposits; Deposits from individuals
form: Примечания
hint: Примечание 'Средства клиентов' — разбивка по сегментам.
example: `| Consumer deposits | 6,880,200 | 6,015,400 |` → на отчётную дату 6,880,200
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Депозиты и вклады физических лиц.

## INDIVIDUALS_LOANS
name_ru: Розничные кредиты
name_en: Total consumer loans and leases
aliases_en: Retail loans; Consumer loans; Loans to individuals
form: Примечания
hint: Примечание 'Кредиты клиентам' — разбивка по сегментам.
example: `| Total consumer loans and leases | 3,938,100 | 3,297,300 |` → на отчётную дату 3,938,100
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Кредиты, выданные физическим лицам (ипотека, потребкредиты, автокредиты).

## INTANGIBLE_ASSETS
name_ru: Нематериальные активы
name_en: Intangible assets
aliases_en: Intangible assets; Other intangible assets; Intangibles
form: Баланс
hint: Внеоборотные активы баланса (отдельная строка после гудвила). Детализация в Примечании 'Нематериальные активы'.
example: `| Intangible assets | 54,200 | 48,900 |` → на отчётную дату 54,200
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Идентифицируемые неденежные активы без физической субстанции — лицензии, патенты, программное обеспечение, бренды.

## INTERBANK_DEPOSITS
name_ru: Средства в банках
name_en: Interbank deposits
aliases_en: Due from banks; Interbank placements; Deposits with banks
form: Баланс
hint: Балансовый отчёт банка, актив.
example: `| Interbank deposits | 402,100 | 355,600 |` → на отчётную дату 402,100
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Денежные средства, размещённые в других банках.

## INTERBANK_LOANS
name_ru: Средства банков
name_en: Interbank loans
aliases_en: Due to banks; Interbank borrowings; Deposits from banks
form: Баланс
hint: Балансовый отчёт банка. Может быть отдельной строкой от кредитов клиентам.
example: `| Interbank loans | 288,700 | 310,400 |` → на отчётную дату 288,700
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Кредиты, выданные другим банкам.

## INTEREST_EXPENSE_ER
name_ru: Процентные расходы, рассчитываемые по методу эффективной процентной ставки
name_en: Interest expense, calculated on the basis of effective interest rate
aliases_en: Interest expense calculated using the effective interest method; Interest expense (EIR basis)
form: P&L
hint: P&L банка, после процентных доходов. Бери строку процентных расходов и сохраняй знак/скобки как в источнике.
example: `| Interest expense, calculated on the basis of effective interest rate | (689,900) | (515,600) |` → за отчётный период (689,900)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Процентные расходы, рассчитанные по методу эффективной процентной ставки.

## INTEREST_INCOME_ER
name_ru: Процентные доходы, рассчитываемые по методу эффективной процентной ставки
name_en: Interest income, calculated on the basis of effective interest rate
aliases_en: Interest income calculated using the effective interest method; Interest income (EIR basis)
form: P&L
hint: P&L банка, верхняя строка раздела процентных доходов. Детализация в Примечании 'Процентные доходы и расходы'.
example: `| Interest income, calculated on the basis of effective interest rate | 1,402,300 | 1,155,700 |` → за отчётный период 1,402,300
unit_kind: money
sign: положительный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Процентные доходы, рассчитанные по методу эффективной процентной ставки.

## INTEREST_PAID
name_ru: Уплаченные проценты
name_en: Interest paid
aliases_en: Interest payments; Cash interest paid
form: ОДДС
hint: ОДДС, раздел 'Финансовая деятельность' (или иногда 'Операционная' по выбору учётной политики).
example: `| Interest paid | (312,700) | (241,550) |` → за отчётный период (312,700)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Денежные выплаты процентов по кредитам и займам за период.

## INTEREST_RECEIVED
name_ru: Полученные проценты
name_en: Interest received
aliases_en: Interest receipts; Cash interest received
form: ОДДС
hint: ОДДС, чаще в разделе 'Инвестиционная деятельность' (или 'Операционная').
example: `| Interest received | 884,300 | 712,900 |` → за отчётный период 884,300
unit_kind: money
sign: положительный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Денежные поступления процентов от размещений и финансовых активов.

## JUNIOR_DEBT
name_ru: Субординированная задолженность
name_en: Junior debt
aliases_en: Subordinated debt; Subordinated loans
form: Баланс
hint: Балансовый отчёт банка. Часто учитывается в составе Tier 2 капитала.
example: `| Junior debt | 210,000 | 210,000 |` → на отчётную дату 210,000
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Долг, имеющий более низкий приоритет погашения по сравнению с обычным долгом.

## LEASE_LIABILITIES_BANKS
name_ru: Обязательства по аренде
name_en: Lease liabilities
aliases_en: Lease liabilities
form: Баланс
hint: Балансовый отчёт банка, обязательство. Также в Примечании 'IFRS 16'.
example: `| Lease liabilities | 35,100 | 30,800 |` → на отчётную дату 35,100
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Обязательства по аренде по IFRS 16 у банка (общая сумма, без разбивки на долго/краткосрочные).

## MODIFIED_AUDITORS_OPINION
name_ru: Наличие модифицированного мнения аудитора
name_en: Modified auditor's opinion
aliases_en: Modified auditor's opinion; Qualified opinion
form: Примечания
hint: Аудиторское заключение независимого аудитора (обычно в начале отчёта). Если 'Мнение не модифицировано' — значение = 0 (нет). Если есть оговорка / отказ / отрицательное мнение — 1 (да).
example: `| Modified auditor's opinion | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: count
sign: любой
period_type: point
standard: Любой
formula_default_abs_behavior: нет

Текстовая отметка: было ли в аудиторском заключении модифицированное мнение (да / нет).

## NET_INCOME
name_ru: Чистая прибыль
name_en: Net income
aliases_en: Net earnings; Net profit; Profit for the period
form: P&L
hint: P&L: бери общий Net income/Profit for the period после налогов, до распределения на attributable to parent и non-controlling interests. В interim-отчетах с колонками 3M/Q и 6M/YTD возвращай обе группы периодов. Если убыток — отрицательное число.
example: `| Net income | 508,900 | 431,002 |` → за отчётный период 508,900
unit_kind: money
sign: любой
period_type: duration
standard: Любой
formula_default_abs_behavior: нет

Финальный финансовый результат компании за период после налогов.

## NET_INCOME_SHAREHOLDERS
name_ru: Прибыль, приходящаяся на акционеров материнской компании
name_en: Profit attributable to owners of the parent
aliases_en: Profit attributable to equity holders of the parent; Net income attributable to shareholders of the parent; Profit for the year attributable to owners of the Company; Net income attributable to controlling interest
form: P&L
hint: ОПУ, блок распределения прибыли в самом низу: «Прибыль, приходящаяся на: акционеров материнской компании / неконтролирующие доли» — бери ПЕРВУЮ строку. Если распределения нет (у группы нет дочерних с неконтролирующей долей), прибыль совпадает с чистой прибылью — бери её.
example: `| Profit attributable to owners of the parent | 496,500 | 425,100 |` → за отчётный период 496,500
unit_kind: money
sign: любой
period_type: duration
standard: Любой
formula_default_abs_behavior: нет

Часть чистой прибыли за период, приходящаяся на акционеров материнской компании, БЕЗ неконтролирующих долей.

## NET_INTEREST_INCOME
name_ru: Чистый процентный доход
name_en: Net interest income
aliases_en: Net interest income; NII
form: P&L
hint: P&L банка, ключевая строка после процентных доходов и расходов. В interim-отчетах с колонками 3M/Q и 6M/YTD возвращай обе группы периодов, не только YTD.
example: `| Net interest income | 712,400 | 640,100 |` → за отчётный период 712,400
unit_kind: money
sign: любой
period_type: duration
standard: Любой
formula_default_abs_behavior: нет

Процентные доходы минус процентные расходы.

## NON_CONTROL_INTEREST
name_ru: Неконтрольные доли участия
name_en: Non-controlling interest
aliases_en: Minority interest; Non-controlling interests
form: Баланс
hint: Раздел КАПИТАЛ баланса (отдельная подстрока). Может быть как положительной, так и отрицательной.
example: `| Non-controlling interest | 12,400 | (3,100) |` → на отчётную дату 12,400
unit_kind: money
sign: любой
period_type: point
standard: Любой
formula_default_abs_behavior: нет

Доля капитала дочерних компаний, не принадлежащая материнской организации.

## NON_INTEREST_INCOME_BANKS
name_ru: Непроцентный доход
name_en: Non-interest income
aliases_en: Non-interest revenue
form: P&L
hint: P&L банка, в части после ЧПД. Если отдельной строкой нет — null.
example: `| Non-interest income | 288,300 | 254,700 |` → за отчётный период 288,300
unit_kind: money
sign: любой
period_type: duration
standard: Любой
formula_default_abs_behavior: нет

Совокупные непроцентные доходы (комиссии + торговые + прочие).

## OPERATING_EXPENSES_BANKS
name_ru: Операционные расходы банков
name_en: Total non-interest expense
aliases_en: Total operating expenses; Non-interest expenses
form: P&L
hint: P&L банка, после операционных доходов и резервов.
example: `| Total non-interest expense | (452,800) | (401,300) |` → за отчётный период (452,800)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Совокупные операционные расходы банка (зарплаты, IT, аренда, прочее).

## OPERATING_INCOME_BANKS
name_ru: Операционный доход банков
name_en: Total revenue, net of interest expense
aliases_en: Operating income; Net revenue
form: P&L
hint: P&L банка, после ЧПД и непроцентных доходов.
example: `| Total revenue, net of interest expense | 1,004,700 | 902,300 |` → за отчётный период 1,004,700
unit_kind: money
sign: любой
period_type: duration
standard: Любой
formula_default_abs_behavior: нет

Совокупные операционные доходы банка (ЧПД + комиссии + прочие, до создания резервов).

## OTHER_ASSETS
name_ru: Прочие активы
name_en: Other assets
aliases_en: Other assets; Prepaid expenses and other assets; Other current assets; Other long-term assets
form: Баланс
hint: Балансовый отчёт: широкая метрика other assets включает current/prepaid and other assets плюс other non-current assets, если они раскрыты отдельно. Не бери изменение этой строки из ОДДС.
example: `| Other assets | 145,900 | 132,400 |` → на отчётную дату 145,900
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Активы, не отнесённые к стандартным категориям баланса.

## OTHER_LIABILITIES
name_ru: Прочие обязательства
name_en: Other liabilities
aliases_en: Other liabilities; Accrued and other liabilities; Other current liabilities; Other long-term liabilities
form: Баланс
hint: Балансовый отчёт: широкая метрика other liabilities включает accrued/other current liabilities плюс other long-term liabilities, если они раскрыты отдельно. Не бери cash-flow change in accrued liabilities.
example: `| Other liabilities | 188,300 | 170,200 |` → на отчётную дату 188,300
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Обязательства, не отнесённые к стандартным категориям баланса.

## OTHER_OPERATING_INCOME
name_ru: Прочие Операционные Доходы
name_en: Other operating income
aliases_en: Other income; Other revenues
form: P&L
hint: P&L банка, в составе непроцентных доходов.
example: `| Other operating income | 14,500 | 11,800 |` → за отчётный период 14,500
unit_kind: money
sign: любой
period_type: duration
standard: Любой
formula_default_abs_behavior: нет

Прочие доходы от операционной деятельности банка.

## PPE
name_ru: Основные средства
name_en: Property, plant and equipment
aliases_en: PP&E; Fixed assets; Property and equipment
form: Примечания
hint: Внеоборотные активы баланса. Также детализация в Примечании 'Основные средства'.
example: `| Property, plant and equipment | 312,400 | 298,700 |` → на отчётную дату 312,400
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Долгосрочные материальные активы — здания, оборудование, транспорт.

## PRINCIPAL_REPAYMENT_OF_LEASE_LIABILITIES
name_ru: Погашение обязательств по аренде
name_en: Repayment of the principle of lease liabilities
aliases_en: Principal payments on lease liabilities; Repayment of lease liabilities
form: ОДДС
hint: ОДДС, раздел 'Финансовая деятельность'. Не путать с INTEREST_PAID_LEASE_LIABILITIES.
example: `| Repayment of the principle of lease liabilities | (9,120) | (8,340) |` → за отчётный период (9,120)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Основная часть лизинговых платежей (без процентов) за период.

## REPO_PLEDGED_SECURITIES
name_ru: Ценные бумаги, заложенные по договорам репо
name_en: Securities pledged under repo
aliases_en: Securities sold under repurchase agreements; Pledged securities
form: Примечания
hint: Балансовый отчёт или Примечание 'Финансовые инструменты'. Внебалансовая или балансовая статья в зависимости от учётной политики.
example: `| Securities pledged under repo | 155,400 | 98,200 |` → на отчётную дату 155,400
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Ценные бумаги, переданные в залог по сделкам репо.

## RETAINED_EARNINGS
name_ru: Нераспределенная прибыль
name_en: Retained Earnings
aliases_en: Accumulated retained earnings; Accumulated deficit; Retained earnings balance
form: Баланс
hint: Раздел капитала: retained earnings / accumulated deficit на конец периода. Не путай с net income за период.
example: `| Retained Earnings | 1,188,400 | 1,002,900 |` → на отчётную дату 1,188,400
unit_kind: money
sign: любой
period_type: point
standard: Любой
formula_default_abs_behavior: нет

Накопленная чистая прибыль за все периоды за вычетом распределённых дивидендов.

## RIGHT_OF_USE_ASSETS
name_ru: Активы в форме права пользования
name_en: ROU
aliases_en: Right-of-use assets; ROU assets
form: Баланс
hint: Внеоборотные активы баланса (отдельная строка). Детализация в Примечании 'Аренда' / 'IFRS 16'.
example: `| ROU | 33,900 | 29,450 |` → на отчётную дату 33,900
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Активы, признанные арендатором по МСФО 16 — это будущие платежи по аренде, дисконтированные на дату начала аренды.

## RISK_WEIGHTED_ASSETS
name_ru: Активы, взвешенные по риску (RWA)
name_en: Risk-weighted assets (RWA)
aliases_en: Risk-weighted assets; RWA
form: Примечания
hint: Примечание 'Достаточность капитала' / 'Управление капиталом'.
example: `| Risk-weighted assets (RWA) | 12,405,700 | 11,208,300 |` → на отчётную дату 12,405,700
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Активы, взвешенные по уровню риска для расчёта нормативов достаточности капитала.

## SECURITIES_ASSETS
name_ru: Финансовые инструменты (Актив)
name_en: Securities
aliases_en: Financial instruments; Investment securities; Marketable securities; Short-term investments; Available-for-sale securities
form: Баланс
hint: Балансовый отчёт или примечание к инвестициям: ценные бумаги / marketable securities / short-term investments на дату баланса. Не путай с cash equivalents и bank deposits/time deposits.
example: `| Securities | 2,104,500 | 1,880,300 |` → на отчётную дату 2,104,500
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Долговые и долевые ценные бумаги, удерживаемые в качестве инвестиций или для торговли.

## SHARE_CAPITAL
name_ru: Уставной капитал
name_en: Share capital
aliases_en: Common stock; Ordinary shares; Capital stock
form: Баланс
hint: Раздел капитала: share capital/common stock/capital stock на конец периода. Не путай с total equity.
example: `| Share capital | 67,760 | 67,760 |` → на отчётную дату 67,760
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Номинальная стоимость выпущенных акций.

## SHARE_PREMIUM
name_ru: Эмиссионный доход
name_en: Share premium
aliases_en: Additional paid-in capital; Capital surplus; APIC
form: Баланс
hint: Раздел капитала: share premium/additional paid-in capital на конец периода.
example: `| Share premium | 228,100 | 228,100 |` → на отчётную дату 228,100
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Превышение цены размещения акций над номинальной стоимостью.

## TIER1_RATIO_DISCLOSED
name_ru: Норматив Tier 1 (раскрываемый)
name_en: Tier 1 ratio (disclosed)
aliases_en: Tier 1 capital ratio
form: Примечания
hint: Примечание 'Достаточность капитала'. Единица — проценты.
example: `| Tier 1 ratio (disclosed) | 12.4% | 11.8% |` → на отчётную дату 12,4
unit_kind: percent
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Раскрываемый банком норматив достаточности капитала первого уровня (в процентах).

## TOTAL_ASSETS
name_ru: Итого активы
name_en: Total assets
aliases_en: Total assets; Sum of assets
form: Баланс
hint: Балансовый отчёт (Statement of Financial Position), нижняя строка раздела АКТИВЫ. Должна равняться сумме внеоборотных и оборотных активов.
example: `| Total assets | 15,204,883 | 13,145,210 |` → на отчётную дату 15,204,883
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Совокупная балансовая стоимость всех активов компании на отчётную дату.

## TOTAL_ASSOCIATE_INVESTMENTS
name_ru: Инвестиции в ассоциированные и совместные предприятия
name_en: Associate investments
aliases_en: Investments in associates and joint ventures; Equity-accounted investments; Equity method investment; Investments in associates; Investments: equity method; Equity method investments
form: Баланс
hint: Внеоборотные активы баланса: investments in associates/JVs/equity method investments на дату баланса. Не путай с purchases of strategic investments в ОДДС.
example: `| Associate investments | 38,200 | 34,100 |` → на отчётную дату 38,200
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Совокупные инвестиции в ассоциированные компании и совместные предприятия.

## TOTAL_CREDIT_ALLOWANCE
name_ru: Резервы под обесценение кредитов и авансов клиентам
name_en: Provision for credit losses
aliases_en: Credit impairment losses; Loan loss provisions; Provision for loan losses
form: Примечания
hint: P&L банка — отдельная строка после процентного дохода. Сохраняй знак/скобки как в источнике.
example: `| Provision for credit losses | (415,200) | (388,100) |` → за отчётный период (415,200)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Расход (за период) на создание/восстановление резервов под кредитные убытки.

## TOTAL_EQUITY
name_ru: Итого собственный капитал
name_en: Total equity
aliases_en: Total shareholders' equity; Stockholders' equity
form: Баланс
hint: Балансовый отчёт, итог раздела КАПИТАЛ. Иногда отдельно 'Капитал, приходящийся на акционеров материнской' + 'Неконтролирующие доли'.
example: `| Total equity | 1,512,004 | 1,398,771 |` → на отчётную дату 1,512,004
unit_kind: money
sign: любой
period_type: point
standard: Любой
formula_default_abs_behavior: нет

Совокупная стоимость собственного капитала, включая капитал акционеров и неконтролирующие доли.

## TOTAL_INVESTMENT_BANKING_FEES
name_ru: Доход от инвестиционно-банковского бизнеса
name_en: Total investment banking fees
aliases_en: Investment banking revenue; IB fees
form: P&L
hint: P&L банка с инвестбанковской деятельностью или Примечание 'Сегментный анализ'.
example: `| Total investment banking fees | 12,400 | 10,900 |` → за отчётный период 12,400
unit_kind: money
sign: положительный
period_type: duration
standard: US GAAP
formula_default_abs_behavior: да

Доходы от инвестиционно-банковской деятельности (M&A, размещения, andewriting).

## TOTAL_LOANS_TO_CLIENTS
name_ru: Кредиты клиентам
name_en: Total loans and leases
aliases_en: Loans and advances to customers; Gross loans
form: Баланс
hint: Балансовый отчёт банка, ключевой актив. Детализация в Примечании 'Кредиты клиентам'.
example: `| Total loans and leases | 9,840,200 | 8,415,700 |` → на отчётную дату 9,840,200
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Совокупный объём кредитов, выданных клиентам банка (за вычетом резервов ECL).

## TREASURY_SHARES
name_ru: Собственные акции, выкупленные у акционеров
name_en: Treasury shares
aliases_en: Treasury stock; Own shares repurchased
form: Баланс
hint: Раздел капитала: treasury shares/treasury stock на конец периода. Обычно уменьшает капитал; не путай с buyback cash-flow за период.
example: `| Treasury shares | (7,900) | (4,300) |` → на отчётную дату (7,900)
unit_kind: money
sign: отрицательный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Стоимость собственных акций, выкупленных у акционеров.
