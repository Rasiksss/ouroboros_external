# Ячейка ind_gaap_bank_en — банки, Indian GAAP, отчётность на английском

* стандарт отчётности: **Ind GAAP**
* единица измерения по умолчанию: **INR**
* подпись периода, когда своей в документе нет: **Current**

Всё ниже до первой записи `## <ENUM>` — ПРАВИЛА ЯЧЕЙКИ: как читать
отчётность этого стандарта. Они уезжают в промпт целиком. Правятся
текстом, без программиста. Правила конкретного показателя — в его
записи, поле `hint`.
### Additional guardrails

1. **Indian GAAP / RBI bank statements use local banking vocabulary.** Prefer statutory banking tables with Advances, Deposits, Investments, Borrowings, Other liabilities and provisions, and Profit and Loss Account. Do not force IFRS 9 Stage 1/2/3 labels or US GAAP CECL labels when the report uses Indian GAAP / RBI terminology.

2. **Asset-quality metrics may use NPA/NPI language.** Look for gross NPA, net NPA, provisions for NPAs, standard assets, restructured assets, provision coverage and related RBI disclosures. Map those to the registry metrics carefully instead of substituting generic impairment terms.

3. **Capital and liquidity disclosures can sit outside the main forms.** CET1, Tier 1, CRAR/CAR, RWA, balances with RBI, and money at call and short notice often appear in dedicated regulatory notes. Use those regulatory tables when they are the primary disclosure.

4. **Consolidated-only.** Extract consolidated-only indicators. If a report contains both standalone and consolidated statements, use only the consolidated statement / consolidated notes values unless a metric explicitly asks otherwise.

5. **Indian numbering units matter.** The unit_text MUST capture the exact unit from the source: crore, lakh, arab, million, Rs., INR, or the currency symbol. Do not silently rewrite crore/lakh/arab to million in the model response.

6. **Use statutory figures over investor-summary restatements.** When the annual report contains both statutory schedules and narrative highlights, prefer the statutory schedule / note disclosure.

## CASA_RATIO_DISCLOSED
name_ru: Раскрытый коэффициент CASA
name_en: CASA ratio
aliases_en: CASA ratio; current and savings account ratio; current account savings account ratio
form: Примечания
hint: Use the key ratios / management discussion / deposit profile disclosure. Unit is percent. Prefer a directly disclosed ratio; calculate outside extraction only if current/savings and total deposits are reliable.
example: `| CASA ratio | 12.4% | 11.8% |` → на отчётную дату 12,4
unit_kind: percent
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Disclosed ratio of current and savings account deposits to total deposits.

## CASH_AND_CASH_BALANCES_WITH_CB
name_ru: Обязательные резервы на счетах в центральных банках
name_en: Cash and balances with central bank
aliases_en: cash and balances with Reserve Bank of India; cash in hand; balances with RBI; balances with central banks; cash and balances with central bank
form: Баланс
hint: For Indian banks, use Schedule 6 'Cash and Balances with Reserve Bank of India'. Take the total schedule amount unless a narrower line is explicitly requested.
example: `| Cash and balances with central bank | 188,500 | 165,200 |` → на отчётную дату 188,500
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Cash in hand and balances maintained with the central bank.

## CASH_EQUIVALENTS
name_ru: Денежные средства и их эквиваленты
name_en: Cash and cash equivalents
aliases_en: cash and cash equivalents; cash equivalents; cash and cash equivalents at end of year; cash and balances; cash and bank balances
form: Баланс
hint: Use the Cash Flow Statement closing line if cash and cash equivalents are disclosed there. In Indian bank statements, also check Schedule 6 and Schedule 7, but do not confuse statutory cash/balances schedules with a broader cash-equivalent definition if the cash flow statement gives a narrower total.
example: `| Cash and cash equivalents | 1,204,883 | 1,145,210 |` → на отчётную дату 1,204,883
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Cash, current accounts and short-term highly liquid balances presented as cash and cash equivalents.

## CET1_RATIO_DISCLOSED
name_ru: Норматив CET1 (раскрываемый)
name_en: Common Equity Tier 1 ratio
aliases_en: CET1 ratio; Common Equity Tier 1 ratio; Common Equity Tier 1 capital ratio
form: Примечания
hint: Use the Basel III / capital adequacy disclosure. Unit is percent. Do not use absolute CET1 capital.
example: `| Common Equity Tier 1 ratio | 12.4% | 11.8% |` → на отчётную дату 12,4
unit_kind: percent
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Disclosed Common Equity Tier 1 capital ratio.

## CHANGES_OCF_ASSETS_LIABILITIES
name_ru: Изменения в оборотном капитале
name_en: Changes in operating assets and liabilities
aliases_en: changes in operating assets and liabilities; working capital changes; adjustments for operating assets and liabilities
form: ОДДС
hint: Use the Cash Flow Statement subtotal if explicitly disclosed. If no subtotal is printed, derive only from adjacent cash flow subtotals; do not blindly sum all asset and liability movements across the statement.
example: `| Changes in operating assets and liabilities | (145,300) | 88,200 |` → за отчётный период (145,300)
unit_kind: money
sign: любой
period_type: duration
standard: Indian GAAP / RBI
formula_default_abs_behavior: нет

Net cash-flow effect of changes in operating assets and liabilities.

## CLIENT_DEPOSITS
name_ru: Депозиты клиентов
name_en: Customer deposits
aliases_en: deposits; customer deposits; total deposits; deposits from customers
form: Баланс
hint: Use Schedule 3 'Deposits'. Take the total deposits line, not one component such as demand deposits, savings deposits or term deposits.
example: `| Customer deposits | 11,204,500 | 9,880,100 |` → на отчётную дату 11,204,500
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Total deposits accepted by the bank from customers and other depositors.

## COMMISSION_EXCHANGE_BROKERAGE
name_ru: Комиссии, обменные операции и брокерские доходы
name_en: Commission, exchange and brokerage
aliases_en: commission exchange and brokerage; commission, exchange and brokerage; commission income; exchange income; brokerage income
form: P&L
hint: Use Schedule 14 'Other Income' and the line 'Commission, Exchange and Brokerage'. Do not use the full Other Income total.
example: `| Commission, exchange and brokerage | 12,345 | 11,200 |` → за отчётный период 12,345
unit_kind: money
sign: положительный
period_type: duration
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Income from commission, exchange and brokerage as a component of other income.

## COMMON_EQUITY_TIER_ONE_CAPITAL
name_ru: Основной капитал первого уровня
name_en: Common Equity Tier 1 capital
aliases_en: Common Equity Tier 1 capital; CET1 capital; Common Equity Tier 1
form: Примечания
hint: Use the Basel III / Pillar 3 / capital adequacy disclosure. Take the absolute CET1 capital amount, not the CET1 ratio percentage.
example: `| Common Equity Tier 1 capital | 1,402,300 | 1,288,900 |` → на отчётную дату 1,402,300
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Absolute amount of Common Equity Tier 1 capital used for Basel/RBI capital adequacy purposes.

## CONTINGENT_LIABILITIES_TOTAL
name_ru: Условные обязательства
name_en: Contingent liabilities
aliases_en: contingent liabilities; total contingent liabilities; Schedule 12; claims against the bank not acknowledged as debts; guarantees; acceptances and endorsements
form: Примечания
hint: Use Schedule 12 'Contingent Liabilities'. Take the total schedule amount, not a single component such as guarantees or forward exchange contracts.
example: `| Contingent liabilities | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Total off-balance sheet contingent liabilities disclosed by the bank.

## CORPORATE_LOANS
name_ru: Корпоративные кредиты
name_en: Corporate loans
aliases_en: corporate loans; corporate advances; commercial loans; loans to corporates; advances to corporate borrowers; wholesale advances
form: Примечания
hint: Use loan/advance portfolio segmentation notes. Do not use total advances, retail advances, or priority sector advances unless the table explicitly identifies a corporate/wholesale borrower segment.
example: `| Corporate loans | 5,902,100 | 5,118,400 |` → на отчётную дату 5,902,100
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Loans or advances to corporate / commercial borrowers.

## COST_TO_INCOME_RATIO
name_ru: Коэффициент затрат к доходам
name_en: Cost-to-income ratio
aliases_en: CIR; Cost-to-Income Ratio; Cost / income ratio
form: Примечания
hint: Extract only directly reported actual CIR. Do not calculate it from income and expenses or use a target or forecast ratio.
example: `| Cost-to-income ratio | 12.4% | 11.8% |` → за отчётный период 12,4
unit_kind: percent
sign: положительный
period_type: duration
standard: Indian GAAP / RBI
formula_default_abs_behavior: нет

Bank-reported ratio of operating expenses to operating income for the period.

## CRAR_RATIO_DISCLOSED
name_ru: Раскрытый коэффициент достаточности капитала (CRAR)
name_en: Capital to risk-weighted assets ratio
aliases_en: CRAR; capital adequacy ratio; CAR; capital to risk weighted assets ratio; total capital ratio
form: Примечания
hint: Use the capital adequacy / Basel III disclosure. Unit is percent. Do not confuse with CET1 ratio or Tier 1 ratio.
example: `| Capital to risk-weighted assets ratio | 12.4% | 11.8% |` → на отчётную дату 12,4
unit_kind: percent
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Disclosed total regulatory capital ratio to risk-weighted assets.

## DEBT_SECURITIES_ISSUED
name_ru: Выпущенные долговые ценные бумаги
name_en: Debt securities issued
aliases_en: debt securities issued; bonds issued; notes issued; certificates of deposit; debentures issued; debt instruments
form: Баланс
hint: Use Schedule 4 'Borrowings' or debt instruments notes. Include bonds, notes, debentures and certificates of deposit where they represent issued debt. Do not use investment securities held as assets.
example: `| Debt securities issued | 402,800 | 355,100 |` → на отчётную дату 402,800
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Debt instruments issued by the bank and outstanding at the reporting date.

## DEFERRED_TAX_ASSETS
name_ru: Отложенные налоговые активы
name_en: Deferred tax assets
aliases_en: deferred tax assets; net deferred tax asset; deferred tax asset
form: Баланс
hint: Use the tax note or the balance sheet line if separately presented. Do not net with deferred tax liabilities unless the report presents only a net deferred tax asset.
example: `| Deferred tax assets | 12,480 | 9,902 |` → на отчётную дату 12,480
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Income tax asset arising from deductible temporary differences and carried forward tax items.

## DEFERRED_TAX_LIABILITIES
name_ru: Отложенные налоговые обязательства
name_en: Deferred tax liabilities
aliases_en: deferred tax liabilities; net deferred tax liability; deferred tax liability
form: Баланс
hint: Use the tax note or the balance sheet line if separately presented. Do not net with deferred tax assets unless the report presents only a net deferred tax liability.
example: `| Deferred tax liabilities | 8,140 | 7,655 |` → на отчётную дату 8,140
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Income tax liability arising from taxable temporary differences.

## DEMAND_DEPOSITS
name_ru: Депозиты до востребования
name_en: Demand deposits
aliases_en: demand deposits; deposits repayable on demand; current deposits
form: Баланс
hint: Use Schedule 3 'Deposits' and take the demand deposits total. Do not include savings or term deposits unless the report aggregates them explicitly.
example: `| Demand deposits | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Deposits repayable on demand.

## DERIVATIVE_LIABILITIES
name_ru: Производные финансовые обязательства
name_en: Derivative liabilities
aliases_en: derivative liabilities; derivative financial liabilities; negative fair value of derivatives; derivatives payable
form: Баланс
hint: Use financial instrument / derivative notes or balance sheet disclosures. Do not use notional amounts or contingent liability amounts for outstanding forward exchange contracts.
example: `| Derivative liabilities | 44,200 | 51,900 |` → на отчётную дату 44,200
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Fair value liability arising from derivative financial instruments.

## DIVIDENDS
name_ru: Дивиденды
name_en: Dividends paid or proposed
aliases_en: dividends; dividend paid; proposed dividend; interim dividend; final dividend
form: ОДДС
hint: Use the appropriations section of the Profit and Loss Account, statement of changes in equity or cash flow statement, depending on the report wording. Do not confuse dividend per share with total dividend amount.
example: `| Dividends paid or proposed | (210,400) | (165,000) |` → за отчётный период (210,400)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Dividend amount disclosed as paid, proposed or appropriated for the period.

## DIVIDENDS_RECEIVED
name_ru: Дивиденды полученные
name_en: Dividends received
aliases_en: dividends received; dividend income; income from dividends
form: ОДДС
hint: Use the Cash Flow Statement if the report explicitly presents dividends received. If only statutory P&L is available, use Schedule 13/14 investment income only when it is clearly identified as dividend income.
example: `| Dividends received | 4,800 | 3,900 |` → за отчётный период 4,800
unit_kind: money
sign: положительный
period_type: duration
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Dividends received or dividend income recognised during the period.

## EBT
name_ru: Прибыль до налогообложения
name_en: Profit before tax
aliases_en: profit before tax; profit before taxation; PBT; earnings before tax
form: P&L
hint: Use the Profit and Loss Account line immediately before tax expense. Preserve losses as negative values.
example: `| Profit before tax | 633,200 | 539,900 |` → за отчётный период 633,200
unit_kind: money
sign: любой
period_type: duration
standard: Indian GAAP / RBI
formula_default_abs_behavior: нет

Profit before income tax for the reporting period.

## EPS
name_ru: Прибыль на одну обыкновенную акцию
name_en: Basic earnings per share
aliases_en: basic earnings per share; earnings per share; EPS; basic EPS
form: P&L
hint: Use the Profit and Loss Account or EPS note. Prefer basic EPS over diluted EPS. Unit should be currency per share, not the report scaling used for balance sheet figures.
example: `| Basic earnings per share | 23,45 | 19,87 |` → за отчётный период 23,45
unit_kind: money
sign: любой
period_type: duration
standard: Indian GAAP / RBI
formula_default_abs_behavior: нет

Basic earnings per ordinary equity share for the reporting period.

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

## FUNDS_FROM_CENTRAL_BANK
name_ru: Средства, предоставленные ЦБ
name_en: Borrowings from central bank
aliases_en: borrowings from Reserve Bank of India; borrowings from RBI; refinance from RBI; funds from central bank; central bank borrowings
form: Баланс
hint: Use Schedule 4 'Borrowings' and the component from Reserve Bank of India / central bank. Do not confuse with balances maintained with RBI on the asset side.
example: `| Borrowings from central bank | 320,000 | 145,000 |` → на отчётную дату 320,000
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Funding borrowed from the central bank.

## GOODWILL
name_ru: Гудвил
name_en: Goodwill
aliases_en: goodwill; goodwill on consolidation
form: Баланс
hint: Use the balance sheet or business combination / consolidation note. Do not include other intangible assets if goodwill is disclosed separately.
example: `| Goodwill | 21,300 | 21,300 |` → на отчётную дату 21,300
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Goodwill arising on acquisition or consolidation.

## GOVERNMENT_SECURITIES
name_ru: Государственные ценные бумаги
name_en: Government securities
aliases_en: government securities; GOI securities; central government securities; state government securities; treasury bills
form: Баланс
hint: Use Schedule 8 'Investments' and the government securities line. Do not use the full investments total or securities pledged under repo.
example: `| Government securities | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Investments in government securities held by the bank.

## GROSS_NPA
name_ru: Валовые необслуживаемые активы (NPA)
name_en: Gross non-performing assets
aliases_en: gross NPA; gross NPAs; gross non-performing assets; gross non-performing advances; GNPA
form: Примечания
hint: Use the asset quality / NPA disclosure. Do not substitute Stage 3 loans unless the report explicitly reconciles Stage 3 to NPA; Indian GAAP/RBI NPA classification is the native source.
example: `| Gross non-performing assets | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Gross balance of non-performing assets or non-performing advances before provisions.

## GROSS_NPA_RATIO_DISCLOSED
name_ru: Раскрытый коэффициент валовых NPA
name_en: Gross NPA ratio
aliases_en: gross NPA ratio; GNPA ratio; gross NPAs to gross advances; gross non-performing assets ratio; % gross NPAs to gross advances
form: Примечания
hint: Use the disclosed asset-quality ratio. Unit is percent. Do not return the absolute gross NPA amount.
example: `| Gross NPA ratio | 12.4% | 11.8% |` → на отчётную дату 12,4
unit_kind: percent
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Disclosed ratio of gross NPAs to gross advances or equivalent denominator.

## INCOME_TAX
name_ru: Расход по текущему налогу на прибыль
name_en: Income tax expense
aliases_en: tax expense; provision for tax; income tax; current tax; taxation
form: P&L
hint: Use the Profit and Loss Account or tax note. Prefer the tax expense charged to profit for the period; if the report separates current and deferred tax, do not substitute one component for the total tax expense unless the enum is explicitly requested as current tax.
example: `| Income tax expense | (124,300) | (108,900) |` → за отчётный период (124,300)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Income tax expense for the reporting period.

## INDIVIDUALS_LOANS
name_ru: Розничные кредиты
name_en: Retail loans
aliases_en: retail loans; consumer loans; personal loans; housing loans; vehicle loans; loans to individuals; retail advances
form: Примечания
hint: Use loan/advance portfolio segmentation notes. In Indian reports, this may appear as retail advances. Do not use total advances or priority sector advances. Prefer gross exposure if the segmentation table is on a gross basis; otherwise preserve the disclosed basis.
example: `| Retail loans | 3,938,100 | 3,297,300 |` → на отчётную дату 3,938,100
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Gross or carrying amount of loans to individual / retail borrowers, depending on the report's segmentation basis.

## INTANGIBLE_ASSETS
name_ru: Нематериальные активы
name_en: Intangible assets
aliases_en: intangible assets; software; computer software; other intangible assets
form: Баланс
hint: Use the intangible assets note or the relevant line within fixed assets / other assets. Exclude goodwill, tangible fixed assets and right-of-use assets if separately disclosed.
example: `| Intangible assets | 54,200 | 48,900 |` → на отчётную дату 54,200
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Carrying amount of identifiable non-physical assets, excluding goodwill when goodwill is separately disclosed.

## INTERBANK_DEPOSITS
name_ru: Средства в банках
name_en: Balances with banks and money at call
aliases_en: balances with banks; money at call; money at short notice; balances with banks and money at call and short notice; interbank placements
form: Баланс
hint: Use Schedule 7 'Balances with banks and money at call and short notice'. Take the relevant asset balance; do not use borrowings from other banks.
example: `| Balances with banks and money at call | 402,100 | 355,600 |` → на отчётную дату 402,100
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Amounts placed with other banks or available as money at call and short notice.

## INTERBANK_LOANS
name_ru: Средства банков
name_en: Borrowings from banks
aliases_en: borrowings from banks; inter-bank borrowings; borrowings from other banks; amounts due to banks
form: Баланс
hint: Use Schedule 4 'Borrowings' and the component from other banks or interbank borrowings. Do not use deposits or balances placed with banks on the asset side.
example: `| Borrowings from banks | 288,700 | 310,400 |` → на отчётную дату 288,700
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Amounts borrowed from other banks or interbank counterparties.

## INTEREST_EARNED_TOTAL
name_ru: Процентные доходы
name_en: Interest earned
aliases_en: interest earned; total interest earned; interest income; Schedule 13
form: P&L
hint: Use Schedule 13 'Interest Earned'. Take the total schedule amount, not one component such as interest on advances or income on investments.
example: `| Interest earned | 12,345 | 11,200 |` → за отчётный период 12,345
unit_kind: money
sign: положительный
period_type: duration
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Total interest income earned by the bank during the period.

## INTEREST_EXPENDED_TOTAL
name_ru: Процентные расходы
name_en: Interest expended
aliases_en: interest expended; total interest expended; interest expense; Schedule 15
form: P&L
hint: Use Schedule 15 'Interest Expended'. Take the total schedule amount, not one component such as interest on deposits or borrowings.
example: `| Interest expended | 12,345 | 11,200 |` → за отчётный период 12,345
unit_kind: money
sign: положительный
period_type: duration
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Total interest expense incurred by the bank during the period.

## JUNIOR_DEBT
name_ru: Субординированная задолженность
name_en: Subordinated debt
aliases_en: subordinated debt; subordinated bonds; subordinated liabilities; Tier 2 bonds; lower Tier 2 bonds
form: Баланс
hint: Use Schedule 4 'Borrowings', capital instruments notes, or Basel III capital disclosures. Do not include senior bonds or ordinary deposits.
example: `| Subordinated debt | 210,000 | 210,000 |` → на отчётную дату 210,000
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Subordinated borrowings with lower repayment priority, often eligible for regulatory capital.

## LCR_DISCLOSED
name_ru: Раскрытый коэффициент покрытия ликвидностью (LCR)
name_en: Liquidity coverage ratio
aliases_en: liquidity coverage ratio; LCR; average LCR
form: Примечания
hint: Use the liquidity coverage / Basel III liquidity disclosure. Unit is percent. Prefer the disclosed average or period-end LCR according to the table label; do not use HQLA or net cash outflows for this enum.
example: `| Liquidity coverage ratio | 12.4% | 11.8% |` → на отчётную дату 12,4
unit_kind: percent
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Disclosed liquidity coverage ratio.

## MODIFIED_AUDITORS_OPINION
name_ru: Наличие модифицированного мнения аудитора
name_en: Modified auditor's opinion
aliases_en: modified opinion; qualified opinion; adverse opinion; disclaimer of opinion; independent auditor's report
form: Примечания
hint: Use the Independent Auditor's Report. Return 0 for an unmodified opinion and 1 for a qualified, adverse, or disclaimer opinion. Do not create comparative periods unless a separate auditor's opinion is disclosed for them.
example: `| Modified auditor's opinion | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: count
sign: любой
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: нет

Text flag indicating whether the auditor issued a modified opinion for the current reporting period.

## NET_INCOME
name_ru: Чистая прибыль
name_en: Net income
aliases_en: net profit; profit after tax; profit for the year; profit for the period; net profit after tax
form: P&L
hint: Use the Profit and Loss Account line after tax and before appropriations/transfers. In interim statements, preserve the exact period columns disclosed. Losses should retain the source sign.
example: `| Net income | 508,900 | 431,002 |` → за отчётный период 508,900
unit_kind: money
sign: любой
period_type: duration
standard: Indian GAAP / RBI
formula_default_abs_behavior: нет

Profit after tax for the reporting period before appropriation.

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
aliases_en: net interest income; NII; interest earned less interest expended
form: P&L
hint: Indian statutory P&L normally gives Schedule 13 'Interest Earned' and Schedule 15 'Interest Expended'. Use a disclosed net interest income subtotal if present; otherwise it can be calculated from those two native lines outside extraction.
example: `| Net interest income | 712,400 | 640,100 |` → за отчётный период 712,400
unit_kind: money
sign: любой
period_type: duration
standard: Indian GAAP / RBI
formula_default_abs_behavior: нет

Interest earned minus interest expended for the period.

## NET_INVESTMENTS
name_ru: Чистые инвестиции
name_en: Net investments
aliases_en: net investments; investments net of provisions; net value of investments; Schedule 8 net investments
form: Баланс
hint: Use Schedule 8 'Investments'. Prefer the net investments figure after provision for depreciation. Do not use gross investments if a net figure is separately disclosed.
example: `| Net investments | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Carrying amount of investments after provision for depreciation / impairment where disclosed.

## NET_NPA
name_ru: Чистые необслуживаемые активы (NPA)
name_en: Net non-performing assets
aliases_en: net NPA; net NPAs; net non-performing assets; net non-performing advances; NNPA
form: Примечания
hint: Use the asset quality / NPA disclosure. Do not calculate from gross NPA and provisions unless the disclosed table clearly provides the required components and no direct net NPA line is available.
example: `| Net non-performing assets | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Non-performing assets after deducting related provisions according to the bank's disclosed NPA framework.

## NET_NPA_RATIO_DISCLOSED
name_ru: Раскрытый коэффициент чистых NPA
name_en: Net NPA ratio
aliases_en: net NPA ratio; NNPA ratio; net NPAs to net advances; net non-performing assets ratio; % net NPAs to net advances
form: Примечания
hint: Use the disclosed asset-quality ratio. Unit is percent. Do not return the absolute net NPA amount.
example: `| Net NPA ratio | 12.4% | 11.8% |` → на отчётную дату 12,4
unit_kind: percent
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Disclosed ratio of net NPAs to net advances or equivalent denominator.

## NON_CONTROL_INTEREST
name_ru: Неконтрольные доли участия
name_en: Non-controlling interest
aliases_en: non-controlling interest; minority interest; NCI
form: Баланс
hint: Use the consolidated balance sheet equity section. This item may be absent in standalone bank statements; return null when not separately disclosed.
example: `| Non-controlling interest | 12,400 | (3,100) |` → на отчётную дату 12,400
unit_kind: money
sign: любой
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: нет

Equity attributable to shareholders other than the parent company in consolidated statements.

## OPERATING_EXPENSES_BANKS
name_ru: Операционные расходы банков
name_en: Operating expenses
aliases_en: operating expenses; total operating expenses; operating expenditure; payments to and provisions for employees; other operating expenses
form: P&L
hint: Use Schedule 16 'Operating Expenses' or the Profit and Loss Account total for operating expenses. Preserve expense sign if the source presents expenses as negative amounts.
example: `| Operating expenses | (452,800) | (401,300) |` → за отчётный период (452,800)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Total operating expenses of the bank, including employee costs and other operating expenses.

## OPERATING_PROFIT_BEFORE_PROVISIONS
name_ru: Операционная прибыль до резервов
name_en: Operating profit before provisions
aliases_en: operating profit; operating profit before provisions; pre-provision operating profit; PPOP; profit before provisions and contingencies
form: P&L
hint: Use the Profit and Loss Account subtotal before 'Provisions and Contingencies'. Do not use operating income/revenue; this is a profit subtotal after operating expenses.
example: `| Operating profit before provisions | 12,345 | 11,200 |` → за отчётный период 12,345
unit_kind: money
sign: любой
period_type: duration
standard: Indian GAAP / RBI
formula_default_abs_behavior: нет

Profit before provisions and contingencies, usually after interest and operating expenses.

## OTHER_ASSETS
name_ru: Прочие активы
name_en: Other assets
aliases_en: other assets; Schedule 11; other asset items; miscellaneous assets
form: Баланс
hint: Use Schedule 11 'Other Assets'. Take the total schedule amount, not changes in other assets from the cash flow statement.
example: `| Other assets | 145,900 | 132,400 |` → на отчётную дату 145,900
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Assets not classified into the main banking balance sheet schedules.

## OTHER_INCOME_TOTAL
name_ru: Прочие доходы
name_en: Other income
aliases_en: other income; total other income; Schedule 14
form: P&L
hint: Use Schedule 14 'Other Income'. Take the total schedule amount. Do not use only commission, exchange, brokerage, profit on sale of investments, or miscellaneous income unless the total is absent and explicitly derivable.
example: `| Other income | 12,345 | 11,200 |` → за отчётный период 12,345
unit_kind: money
sign: положительный
period_type: duration
standard: Indian GAAP / RBI
formula_default_abs_behavior: нет

Total non-interest income presented in the statutory banking profit and loss statement.

## OTHER_LIABILITIES
name_ru: Прочие обязательства
name_en: Other liabilities and provisions
aliases_en: other liabilities; other liabilities and provisions; Schedule 5; bills payable; inter-office adjustments; provisions
form: Баланс
hint: Use Schedule 5 'Other Liabilities and Provisions'. Take the total schedule amount unless a more specific enum is requested.
example: `| Other liabilities and provisions | 188,300 | 170,200 |` → на отчётную дату 188,300
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Liabilities and provisions not classified as deposits, borrowings or capital.

## PERPETUAL_BONDS
name_ru: Бессрочные облигации
name_en: Perpetual bonds / Additional Tier 1 instruments
aliases_en: perpetual bonds; perpetual debt instruments; Additional Tier 1 bonds; AT1 bonds; Basel III Additional Tier 1 instruments; perpetual non-cumulative preference shares
form: Баланс
hint: Use capital instruments, borrowings or Basel III capital notes. Do not include ordinary term bonds, Tier 2 bonds, share capital or reserves.
example: `| Perpetual bonds / Additional Tier 1 instruments | 45,000 | 45,000 |` → на отчётную дату 45,000
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Perpetual capital instruments, typically eligible as Additional Tier 1 capital or similar regulatory capital.

## PPE
name_ru: Основные средства
name_en: Property, plant and equipment
aliases_en: fixed assets; property and equipment; premises; furniture and fixtures; other fixed assets; property, plant and equipment
form: Примечания
hint: Use Schedule 10 'Fixed Assets' or the fixed assets note. Take the net block / written-down value, not gross cost. Exclude intangible assets, goodwill and right-of-use assets if they are disclosed separately.
example: `| Property, plant and equipment | 312,400 | 298,700 |` → на отчётную дату 312,400
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Carrying amount of tangible fixed assets used by the bank.

## PRIORITY_SECTOR_ADVANCES
name_ru: Кредиты приоритетным секторам
name_en: Priority sector advances
aliases_en: priority sector advances; priority sector lending; PSL advances; advances to priority sector
form: Примечания
hint: Use priority sector lending disclosures or Schedule 9 category disclosures where present. Do not include all retail advances unless explicitly classified as priority sector.
example: `| Priority sector advances | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Advances classified under the priority sector lending framework.

## PROFIT_LOSS_ON_EXCHANGE_TRANSACTIONS
name_ru: Прибыль или убыток от обменных операций
name_en: Profit or loss on exchange transactions
aliases_en: profit on exchange transactions; loss on exchange transactions; net profit on exchange transactions; net loss on exchange transactions; foreign exchange income; forex income
form: P&L
hint: Use Schedule 14 'Other Income' or the FX trading note. Preserve the sign: profit is positive, loss is negative. Do not include broad trading income unless exchange transactions are separately identified.
example: `| Profit or loss on exchange transactions | 12,345 | 11,200 |` → за отчётный период 12,345
unit_kind: money
sign: любой
period_type: duration
standard: Indian GAAP / RBI
formula_default_abs_behavior: нет

Net result from foreign exchange / exchange transactions disclosed in other income.

## PROVISIONS_AND_CONTINGENCIES
name_ru: Расходы на резервы и условные обязательства
name_en: Provisions and contingencies
aliases_en: provisions and contingencies; provisions & contingencies; provision and contingencies; provisions
form: P&L
hint: Use the Profit and Loss Account line 'Provisions and Contingencies'. It usually appears after operating profit and before profit before tax. Do not replace it with IFRS ECL change or a closing allowance balance.
example: `| Provisions and contingencies | 12,345 | 11,200 |` → за отчётный период 12,345
unit_kind: money
sign: положительный
period_type: duration
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Total P&L charge for provisions and contingencies in the statutory banking profit and loss statement.

## PROVISION_COVERAGE_RATIO
name_ru: Коэффициент покрытия резервами
name_en: Provision coverage ratio
aliases_en: provision coverage ratio; PCR; NPA provision coverage ratio; provisions to gross NPAs
form: Примечания
hint: Use the asset quality / NPA disclosure or key ratios table. Unit is percent. Do not confuse with capital adequacy or liquidity ratios.
example: `| Provision coverage ratio | 12.4% | 11.8% |` → на отчётную дату 12,4
unit_kind: percent
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Disclosed coverage of NPAs by provisions.

## PROVISION_FOR_DEPRECIATION_ON_INVESTMENTS_CLOSING
name_ru: Резерв под снижение стоимости инвестиций
name_en: Provision for depreciation on investments
aliases_en: provision for depreciation on investments; investment depreciation provision; provision on investments; provision for diminution in value of investments
form: Примечания
hint: Use Schedule 8 'Investments' or the investment provision note. This is a closing balance, not the P&L charge for the period unless the source explicitly labels it as a movement/charge.
example: `| Provision for depreciation on investments | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Closing provision balance against investments due to depreciation or diminution in value.

## PROVISION_FOR_NPA
name_ru: Резерв под необслуживаемые активы (NPA)
name_en: Provision for non-performing assets
aliases_en: provision for NPA; provisions for NPAs; provision for non-performing assets; NPA provisions; provision for non-performing advances
form: Примечания
hint: Use the NPA disclosure, provisions note, or the relevant component within 'Provisions and contingencies'. Distinguish between a balance-sheet provision stock and a P&L charge if the table gives both; preserve the disclosed basis.
example: `| Provision for non-performing assets | 12,345 | 11,200 |` → за отчётный период 12,345
unit_kind: money
sign: положительный
period_type: duration
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Provision amount related to non-performing assets or non-performing advances.

## REPO_PLEDGED_SECURITIES
name_ru: Ценные бумаги, заложенные по договорам репо
name_en: Securities pledged under repo
aliases_en: securities pledged under repo; securities sold under repurchase agreements; pledged securities; securities given as collateral
form: Примечания
hint: Use repo, collateral or investment notes. Take only securities pledged/transferred under repo; do not use repo borrowing liabilities or the full investments portfolio.
example: `| Securities pledged under repo | 155,400 | 98,200 |` → на отчётную дату 155,400
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Securities transferred or pledged as collateral under repo or similar arrangements.

## RETAINED_EARNINGS
name_ru: Нераспределенная прибыль
name_en: Retained earnings
aliases_en: retained earnings; balance in profit and loss account; surplus in profit and loss account; accumulated surplus; accumulated deficit
form: Баланс
hint: Use Schedule 2 'Reserves and Surplus', especially the balance in the Profit and Loss Account / surplus line. Do not use net profit for the current period.
example: `| Retained earnings | 1,188,400 | 1,002,900 |` → на отчётную дату 1,188,400
unit_kind: money
sign: любой
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: нет

Accumulated profits retained in equity after appropriations and distributions.

## RISK_WEIGHTED_ASSETS
name_ru: Активы, взвешенные по риску (RWA)
name_en: Risk-weighted assets
aliases_en: risk weighted assets; RWA; total RWA; credit risk weighted assets; market risk weighted assets; operational risk weighted assets
form: Примечания
hint: Use the capital adequacy / Basel III disclosure. Take total RWA, not capital ratios or individual risk-type components unless total is absent and clearly derivable.
example: `| Risk-weighted assets | 12,405,700 | 11,208,300 |` → на отчётную дату 12,405,700
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Total assets and exposures weighted for regulatory capital purposes.

## SAVINGS_BANK_DEPOSITS
name_ru: Сберегательные депозиты
name_en: Savings bank deposits
aliases_en: savings bank deposits; savings deposits; savings account deposits
form: Баланс
hint: Use Schedule 3 'Deposits' and take the savings bank deposits line. Do not include demand/current deposits or term deposits.
example: `| Savings bank deposits | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Savings bank deposits accepted by the bank.

## SHARE_CAPITAL
name_ru: Уставной капитал
name_en: Share capital
aliases_en: share capital; capital; paid-up capital; equity share capital
form: Баланс
hint: Use Schedule 1 'Capital'. Do not use reserves, share premium or total equity.
example: `| Share capital | 67,760 | 67,760 |` → на отчётную дату 67,760
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Paid-up nominal value of issued share capital.

## SHARE_PREMIUM
name_ru: Эмиссионный доход
name_en: Share premium
aliases_en: share premium; securities premium; securities premium account
form: Баланс
hint: Use Schedule 2 'Reserves and Surplus' and the securities premium / share premium line. Do not include share capital or retained earnings.
example: `| Share premium | 228,100 | 228,100 |` → на отчётную дату 228,100
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Premium received over the nominal value of issued shares.

## STATUTORY_RESERVE
name_ru: Обязательный резерв
name_en: Statutory reserve
aliases_en: statutory reserve; statutory reserves; reserve fund
form: Баланс
hint: Use Schedule 2 'Reserves and Surplus'. Take the statutory reserve / reserve fund closing balance. Do not use the current-period transfer to statutory reserve unless the enum explicitly asks for a transfer flow.
example: `| Statutory reserve | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Statutory reserve balance within reserves and surplus.

## TERM_DEPOSITS
name_ru: Срочные депозиты
name_en: Term deposits
aliases_en: term deposits; time deposits; fixed deposits
form: Баланс
hint: Use Schedule 3 'Deposits' and take the term deposits total. Do not include demand or savings deposits.
example: `| Term deposits | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Deposits with contractual maturity or term.

## TIER1_CAPITAL
name_ru: Капитал первого уровня
name_en: Tier 1 capital
aliases_en: Tier 1 capital; Tier I capital; total Tier 1 capital
form: Примечания
hint: Use the capital adequacy / Basel III disclosure. Take the absolute Tier 1 capital amount, not the Tier 1 ratio percentage.
example: `| Tier 1 capital | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Absolute amount of Tier 1 regulatory capital.

## TIER1_RATIO_DISCLOSED
name_ru: Норматив Tier 1 (раскрываемый)
name_en: Tier 1 capital ratio
aliases_en: Tier 1 ratio; Tier I ratio; Tier 1 capital ratio; Tier I capital ratio
form: Примечания
hint: Use the Basel III / capital adequacy disclosure. Unit is percent. Do not confuse with CET1 ratio or total CRAR/CAR.
example: `| Tier 1 capital ratio | 12.4% | 11.8% |` → на отчётную дату 12,4
unit_kind: percent
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Disclosed Tier 1 capital adequacy ratio.

## TIER2_CAPITAL
name_ru: Капитал второго уровня
name_en: Tier 2 capital
aliases_en: Tier 2 capital; Tier II capital; total Tier 2 capital
form: Примечания
hint: Use the capital adequacy / Basel III disclosure. Take the absolute Tier 2 capital amount, not the Tier 2 ratio or subordinated debt balance unless clearly equal.
example: `| Tier 2 capital | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Absolute amount of Tier 2 regulatory capital.

## TOTAL_ASSETS
name_ru: Итого активы
name_en: Total assets
aliases_en: total assets; assets total; total of assets
form: Баланс
hint: Use the Balance Sheet total assets line. It should reconcile to the sum of asset schedules, including cash and balances, investments, advances, fixed assets and other assets.
example: `| Total assets | 15,204,883 | 13,145,210 |` → на отчётную дату 15,204,883
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Total carrying amount of all assets at the reporting date.

## TOTAL_ASSOCIATE_INVESTMENTS
name_ru: Инвестиции в ассоциированные и совместные предприятия
name_en: Investments in associates and joint ventures
aliases_en: investments in associates; investments in joint ventures; investments in subsidiaries, associates and joint ventures; equity method investments
form: Баланс
hint: Use Schedule 8 'Investments' or the investments note. Take the separate line for subsidiaries, joint ventures and associates if disclosed. Do not include the full investments total.
example: `| Investments in associates and joint ventures | 38,200 | 34,100 |` → на отчётную дату 38,200
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Investments in associates, joint ventures or similar investees, as separately disclosed.

## TOTAL_EQUITY
name_ru: Итого собственный капитал
name_en: Total equity
aliases_en: total equity; shareholders' funds; shareholders' equity; capital and reserves; net worth
form: Баланс
hint: Use the equity total in the Balance Sheet. For Indian bank statutory format, reconcile to Schedule 1 'Capital' plus Schedule 2 'Reserves and Surplus' and any consolidated non-controlling interest line.
example: `| Total equity | 1,512,004 | 1,398,771 |` → на отчётную дату 1,512,004
unit_kind: money
sign: любой
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: нет

Total equity attributable to shareholders and, in consolidated statements, non-controlling interests where applicable.

## TOTAL_GROSS_LOANS_TO_CLIENTS
name_ru: Валовые кредиты клиентам
name_en: Gross loans to customers
aliases_en: gross loans; gross customer loans; gross loans to customers; gross loans and advances; gross advances; advances before provisions; total advances before provisions; gross advances to customers
form: Баланс
hint: Keep the metric conceptually tied to loans/credit exposure. For Indian GAAP, search asset-quality disclosures and Schedule 9 'Advances' notes for 'gross advances' or 'advances before provisions'. Do not use net advances, NPA balances, or provision balances. This enum is intended to remain reusable outside India, where the local wording may be gross loans rather than advances.
example: `| Gross loans to customers | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Gross amount of loans or advances to customers before provisions, allowances or impairment deductions.

## TOTAL_LOANS_TO_CLIENTS
name_ru: Кредиты клиентам
name_en: Loans to customers / net advances
aliases_en: loans to customers; loans and advances to customers; advances; net advances; advances net of provisions; loans and advances; advances to customers; net loans and advances
form: Баланс
hint: For Indian GAAP, use Balance Sheet Schedule 9 'Advances' and take the net advances amount after provisions. Treat 'advances' as the local statutory wording for customer loans. Do not use gross advances, NPA balances, or provision balances for this enum.
example: `| Loans to customers / net advances | 9,840,200 | 8,415,700 |` → на отчётную дату 9,840,200
unit_kind: money
sign: положительный
period_type: point
standard: Indian GAAP / RBI
formula_default_abs_behavior: да

Net carrying amount of loans or advances to customers after related provisions or allowances.
