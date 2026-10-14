# Ячейка ind_as_nf_en — нефинансовые организации, Ind AS, отчётность на английском

* стандарт отчётности: **Ind AS**
* единица измерения по умолчанию: **INR**
* подпись периода, когда своей в документе нет: **Current**

Всё ниже до первой записи `## <ENUM>` — ПРАВИЛА ЯЧЕЙКИ: как читать
отчётность этого стандарта. Они уезжают в промпт целиком. Правятся
текстом, без программиста. Правила конкретного показателя — в его
записи, поле `hint`.
### Additional guardrails

1. **Ind AS statutory column wins.** Many Indian annual reports mention other accounting principles generally accepted in India alongside Ind AS. When standalone/local-GAAP and Ind AS presentations appear side by side, use the Ind AS / consolidated statutory numbers.

2. **Consolidated-only is a hard rule.** Extract consolidated-only indicators. If a report contains both standalone and consolidated statements, use only the consolidated statement / consolidated notes values unless a metric explicitly asks otherwise. Standalone statements often appear earlier in Indian annual reports; keep searching for the later consolidated statements and ignore every table, note or auditor section headed "Standalone". If the same metric appears in an early Board's Report/standalone section and later in consolidated financial statements or consolidated notes, choose the later consolidated value. Use standalone values only when the entire report has no consolidated statements.

3. **Indian numbering units matter.** The unit_text MUST capture the exact unit from the source: crore, lakh, arab, million, Rs., INR, or the currency symbol. Do not silently rewrite crore/lakh/arab to million in the model response.

4. **March year-end is common.** Keep literal period headers such as "Year ended 31 March 2025" or "Half year ended 30 September 2025" and let Python normalize them later.

5. **Statutory over adjusted.** Use reported / statutory values unless the registry explicitly asks for an adjusted metric. Do not swap in management-adjusted EBITDA or similar non-GAAP presentations.

6. **Cash-flow outflow signs.** For cash-flow outflows such as CAPEX, dividends paid and principal lease repayments, prefer the signed row from the primary Cash Flow Statement. Do not strip parentheses or minus signs, and do not replace a signed cash-flow row with a positive MD&A/note total.

7. **Gross profit in Ind AS.** If "Gross profit" is not printed, use the direct-cost block in the Statement of Profit and Loss: revenue from operations less cost of materials consumed, purchases of stock-in-trade, changes in inventories and similar direct service/product costs. Do not derive gross profit from operating profit or profit before tax.

8. **Financial income is not total other income.** Use finance income / interest income only. Do not use total other income, other operating income, fair value gains, foreign exchange gains or sale-of-investment gains unless the finance-income note explicitly includes them.

9. **Operating profit is statutory.** Prefer an explicit statutory Statement of Profit and Loss subtotal before finance income, finance costs and tax. If the statutory P&L does not print operating profit/profit from operations, use the consolidated segment result/profit TOTAL before finance costs and tax. Do not use EBITDA, profit before tax or management adjusted metrics.

10. **Mixed quarterly table headers.** In quarterly-result tables, read each column header independently. If one column says "Quarter ended 30 June 2024", keep that literal quarter header even when a neighboring column says "Year ended 31 March 2025"; never relabel a comparative quarter as "Year ended 30 June 2024" by copying the annual header.

11. **Annual comparative columns are independent.** In annual statements with current and prior columns such as "31 March 2025" and "31 March 2024", read each column separately. Never copy the current-year cell into the prior-year period just because the row label is the same.

Ind AS non-financial segment specifics:
- Use consolidated segment, product and geography disclosures; ignore standalone-only segment tables when consolidated disclosures are available.
- Segment rows may appear in the notes to consolidated financial statements, revenue disaggregation notes or management discussion tables.
- Product/geography rows are valid segment disclosures when they are explicit rows or columns in the source.
- For geographic revenue, prefer a segmental information / geographical market table that reconciles to total revenue from operations, including other operating revenue. Do not replace it with a revenue-from-contracts-only disaggregation note when both are present.
- Keep geography labels as shown in the chosen table. If the chosen segmental table says "Outside India", do not rename it to "Others".
- For OPERATING_PROFIT, use the segment result/profit row only inside the segment stage; do not use EBITDA, PBT or adjusted management profit as a substitute.

## ACCOUNTS_PAYABLE
name_ru: Кредиторская задолженность
name_en: Accounts payable
aliases_en: Accounts payable; trade payables; trade and other payables; payables to suppliers; creditors
form: Баланс
hint: Use the Balance Sheet line trade payables or accounts payable. In Schedule III, check the trade payables note. Do not include borrowings, provisions or other financial liabilities unless the line is a combined trade and other payables total.
example: `| Accounts payable | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Amounts payable to suppliers and other trade creditors, normally presented as trade payables.

## ACCOUNTS_RECEIVABLE
name_ru: Дебиторская задолженность
name_en: Accounts receivable
aliases_en: Accounts receivable; trade receivables; trade and other receivables; receivables from customers; receivables net
form: Баланс
hint: Use the Balance Sheet line trade receivables or accounts receivable, net. In Schedule III, check the trade receivables note. Exclude advances, prepaid expenses and tax receivables if separately disclosed.
example: `| Accounts receivable | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Amounts receivable from customers for goods or services, normally net of loss allowance.

## ADJUSTMENTS_OPERATING_CASH_FLOWS
name_ru: Корректировки
name_en: Operating cash flow adjustments
aliases_en: Operating cash flow adjustments; adjustments for non-cash items; adjustments to reconcile profit to cash flows; non-cash adjustments
form: ОДДС
hint: Use the indirect Cash Flow Statement. Extract the subtotal of adjustments before working capital movements if printed; otherwise sum only the adjustment block. Exclude working capital movements, tax, interest and investing/financing cash flows.
example: `| Operating cash flow adjustments | 12,345 | 11,200 |` → за отчётный период 12,345
unit_kind: money
sign: любой
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: нет

Subtotal or sum of non-cash and other adjustments in the indirect operating cash flow reconciliation before working capital movements.

## ASSET_PURCHASES
name_ru: Приобретение основных средств
name_en: PPE purchases
aliases_en: PPE purchases; purchase of property plant and equipment; payments for property plant and equipment; capital expenditure on tangible assets
form: ОДДС
hint: Use the Cash Flow Statement investing section. Extract purchases/payments for property, plant and equipment. If PPE and intangibles are combined in one line, leave this metric null and use COMBINED_ASSET_PURCHASES.
example: `| PPE purchases | (91,300) | (80,500) |` → за отчётный период (91,300)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Cash outflow for purchase of property, plant and equipment.

## ASSET_SALES_PROCEEDS
name_ru: Поступления от продажи основных средств и нематериальных активов
name_en: Proceeds from disposal of PPE and Intangible assets
aliases_en: Proceeds from disposal of PPE and Intangible assets; proceeds from sale of property plant and equipment; proceeds from disposal of PPE; proceeds from disposal of fixed assets; proceeds from sale of intangible assets
form: ОДДС
hint: Use the Cash Flow Statement investing section. Extract proceeds from disposal of PPE and intangible assets. Include both if separately disclosed and the metric is intended as a combined total.
example: `| Proceeds from disposal of PPE and Intangible assets | 2,900 | 1,400 |` → за отчётный период 2,900
unit_kind: money
sign: положительный
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Cash proceeds from disposal of property, plant and equipment and intangible assets.

## ASSOCIATE_INCOMES
name_ru: Поступления от выбытия компаний Группы
name_en: Proceeds from disposal of group companies
aliases_en: Proceeds from disposal of group companies; proceeds from disposal of subsidiaries; proceeds from disposal of associates; sale of investments in subsidiaries or associates
form: P&L
hint: Use the Cash Flow Statement investing section. Extract cash proceeds from disposal of subsidiaries, associates, joint ventures or group companies. Do not use equity-accounted profit or the carrying amount of investments.
example: `| Proceeds from disposal of group companies | 3,214 | (1,050) |` → за отчётный период 3,214
unit_kind: money
sign: любой
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Cash proceeds from disposal of subsidiaries, associates, joint ventures or other group companies.

## ASSOCIATE_INVESTMENTS
name_ru: Вложения в ассоциированные организации (общества) и совместные предприятия
name_en: Associate investments
aliases_en: Associate investments; investments in associates; investments in joint ventures; equity-accounted investees; investments accounted for using the equity method
form: Баланс
hint: Use the Balance Sheet non-current investments note. Extract investments in associates or joint ventures carried under the equity method. Do not use share of profit or dividends received.
example: `| Associate investments | 18,402 | 16,950 |` → на отчётную дату 18,402
unit_kind: money
sign: положительный
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Carrying amount of investments in associates or joint ventures.

## BANK_DEPOSITS
name_ru: Банковские депозиты
name_en: Bank deposits
aliases_en: Bank deposits; term deposits with banks; fixed deposits with banks; short-term bank deposits; deposits with banks
form: Баланс
hint: Use notes to cash, bank balances or current financial assets. Take bank deposits or fixed deposits not classified as cash equivalents. Do not include ordinary cash and bank balances already included in CASH_EQUIVALENTS.
example: `| Bank deposits | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Term or fixed deposits placed with banks that are not classified as cash equivalents.

## CAPEX
name_ru: Капитальные затраты
name_en: Capital expenditure
aliases_en: Capital expenditure; capital expenditures; capex; payments for property plant and equipment; purchase of property plant and equipment and intangible assets
form: ОДДС
hint: Use the Cash Flow Statement or MD&A. Prefer a disclosed capital expenditure total if present; otherwise combine purchases of PPE and intangible assets, net of disposal proceeds only if the metric definition/source presents net capex.
example: `| Capital expenditure | (98,400) | (87,100) |` → за отчётный период (98,400)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Capital expenditure for property, plant and equipment and intangible assets, as disclosed or derived from investing cash flows.

## CAPITALIZED_INTEREST
name_ru: Капитализированные и уплаченные проценты
name_en: Capitalized interest
aliases_en: Capitalized interest; capitalised interest; borrowing costs capitalised; borrowing costs capitalized
form: Примечания
hint: Use borrowings, finance costs or PPE/CWIP notes. Extract borrowing costs capitalised during the period. Do not use cash interest paid or total finance costs.
example: `| Capitalized interest | 2,140 | 1,870 |` → за отчётный период 2,140
unit_kind: money
sign: положительный
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Borrowing costs capitalised into qualifying assets during the period.

## CASH_EQUIVALENTS
name_ru: Денежные средства и их эквиваленты
name_en: Cash and cash equivalents
aliases_en: Cash and cash equivalents; cash equivalents; cash and bank balances; cash and cash equivalents at end of period; closing cash and cash equivalents
form: Баланс
hint: Use the Balance Sheet or the closing cash and cash equivalents line in the Cash Flow Statement. Return explicitly disclosed closing dates, including comparative dates. Do not include term deposits or securities unless classified as cash equivalents.
example: `| Cash and cash equivalents | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Cash on hand, bank balances and short-term highly liquid investments classified as cash and cash equivalents.

## CHANGES_OCF_ASSETS_LIABILITIES
name_ru: Изменения в оборотном капитале
name_en: Changes in working capital
aliases_en: Changes in working capital; changes in operating assets and liabilities; working capital movements; movement in working capital
form: ОДДС
hint: Use the indirect Cash Flow Statement. Extract the subtotal of working capital changes if printed; otherwise sum only working capital movement lines. Exclude non-cash adjustments, interest, tax and investing/financing cash flows.
example: `| Changes in working capital | 12,345 | 11,200 |` → за отчётный период 12,345
unit_kind: money
sign: любой
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: нет

Subtotal or sum of changes in working capital or operating assets and liabilities in operating cash flow.

## COMBINED_ASSET_PURCHASES
name_ru: Приобретение основных средств и нематериальных активов
name_en: Fixed asset and intangible asset purchases
aliases_en: Fixed asset and intangible asset purchases; purchase of property plant and equipment and intangible assets; payments for fixed assets; purchase of fixed assets; capital expenditure on tangible and intangible assets
form: ОДДС
hint: Use only when the Cash Flow Statement presents one combined line for purchase of PPE and intangible/fixed assets. Do not duplicate separate PPE and intangible purchase lines.
example: `| Fixed asset and intangible asset purchases | (98,400) | (87,100) |` → за отчётный период (98,400)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Combined cash outflow for purchase of property, plant and equipment and intangible assets where not split.

## COMMITTED_CREDIT_FACILITIES
name_ru: Неиспользованные кредитные линии
name_en: Committed credit facilities
aliases_en: Committed credit facilities; undrawn borrowing facilities; unused credit lines; available credit facilities; revolving credit facility available
form: Примечания
hint: Use liquidity risk, borrowings or capital management notes. Extract undrawn committed facilities or available credit lines, preserving unit and scale exactly as disclosed in narrative text.
example: `| Committed credit facilities | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Committed but undrawn borrowing facilities available to the entity.

## COSTS_EXPENSES
name_ru: Себестоимость
name_en: Costs and expenses
aliases_en: Costs and expenses; cost of materials consumed; cost of goods sold; cost of sales; cost of services; purchase of stock-in-trade; changes in inventories of finished goods work-in-progress and stock-in-trade
form: P&L
hint: Use the Statement of Profit and Loss and cost notes. Prefer cost of sales/cost of revenue if disclosed. For Schedule III presentation, use direct cost lines such as cost of materials consumed, purchases of stock-in-trade and changes in inventories only when they form the cost-of-sales block.
example: `| Costs and expenses | (435,501) | (400,869) |` → за отчётный период (435,501)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Direct costs of goods or services sold, or the closest cost-of-sales subtotal disclosed by the entity.

## DEFERRED_TAX_ASSETS
name_ru: Отложенные налоговые активы
name_en: Deferred tax assets
aliases_en: Deferred tax assets; deferred tax asset; DTA
form: Баланс
hint: Use the Balance Sheet or tax note. Extract deferred tax assets. Do not net against deferred tax liabilities unless the statement presents only a net balance.
example: `| Deferred tax assets | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Deferred tax assets arising from deductible temporary differences, tax losses or tax credits.

## DEFERRED_TAX_LIABILITIES
name_ru: Отложенные налоговые обязательства
name_en: Deferred tax liabilities
aliases_en: Deferred tax liabilities; deferred tax liability; DTL
form: Баланс
hint: Use the Balance Sheet or tax note. Extract deferred tax liabilities. Do not net against deferred tax assets unless the statement presents only a net balance.
example: `| Deferred tax liabilities | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Deferred tax liabilities arising from taxable temporary differences.

## DEPRECIATION_AMORTIZATION
name_ru: Амортизация материальных и нематериальных активов
name_en: Depreciation and amortization
aliases_en: Depreciation and amortization; depreciation and amortisation; depreciation expense; amortisation expense; amortization expense
form: ОДДС
hint: Use the Statement of Profit and Loss, cash flow adjustments, or notes. Include depreciation and amortisation. Do not include impairment losses unless the source combines them in the same printed line.
example: `| Depreciation and amortization | 40,100 | 37,600 |` → за отчётный период 40,100
unit_kind: money
sign: положительный
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Depreciation and amortisation expense for property, plant and equipment and intangible assets during the period.

## DERIVATIVE_LIABILITIES
name_ru: Производные финансовые обязательства
name_en: Derivatives
aliases_en: Derivatives; derivative financial liabilities; derivative liabilities; financial derivatives liabilities
form: Баланс
hint: Use the Balance Sheet or financial instruments note. Extract derivative financial liabilities measured at fair value. Do not net against derivative assets unless only a net liability is presented.
example: `| Derivatives | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Fair value of derivative financial liabilities.

## DIVIDENDS
name_ru: Дивиденды
name_en: Dividends
aliases_en: Dividends; dividends paid; cash dividends paid; total dividends paid; dividend outflow
form: ОДДС
hint: Use the Cash Flow Statement. Extract total cash dividends paid. If dividends to owners and NCI are separately disclosed, sum only those dividend cash outflow lines.
example: `| Dividends | 12,345 | 11,200 |` → за отчётный период 12,345
unit_kind: money
sign: отрицательный
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Total cash dividends paid by the group during the period.

## DIVIDENDS_PAID_NONCONTROLLING_INTERESTS
name_ru: Дивиденды, выплаченные держателям неконтролирующих долей
name_en: Dividends paid to non-controlling interests
aliases_en: Dividends paid to non-controlling interests; dividends paid to NCI; dividends paid to minority interests
form: ОДДС
hint: Use the Cash Flow Statement financing section. Extract dividends paid to non-controlling interests separately from parent shareholder dividends.
example: `| Dividends paid to non-controlling interests | (2,400) | (2,100) |` → за отчётный период (2,400)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Cash dividends paid to holders of non-controlling interests.

## DIVIDENDS_RECEIVED
name_ru: Дивиденды полученные
name_en: Dividends received
aliases_en: Dividends received; dividend income received; cash dividends received
form: ОДДС
hint: Use the Cash Flow Statement investing section. Extract cash dividends received from investments, associates or joint ventures. Do not use dividend income accrued in P&L.
example: `| Dividends received | 1,900 | 1,350 |` → за отчётный период 1,900
unit_kind: money
sign: положительный
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Cash dividends received from associates, joint ventures or other investments.

## DIVIDENDS_SHAREHOLDERS
name_ru: Дивиденды акционерам
name_en: Dividends paid to shareholders
aliases_en: Dividends paid to shareholders; dividend paid to owners; equity dividend paid; dividends to owners of the parent
form: ОДДС
hint: Use the Cash Flow Statement financing section or statement of changes in equity. Extract dividends paid to owners/shareholders of the parent. Preserve cash outflow sign.
example: `| Dividends paid to shareholders | (30,000) | (27,500) |` → за отчётный период (30,000)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Cash dividends paid to shareholders of the parent company.

## DOUBTFUL_ACCOUNTS_ALLOWANCE
name_ru: Резерв под ожидаемые кредитные убытки (дебиторская задолженность)
name_en: Allowance for doubtful accounts
aliases_en: Allowance for doubtful accounts; allowance for doubtful debts; loss allowance on trade receivables; expected credit loss allowance on receivables
form: Примечания
hint: Use the trade receivables or financial instruments note. Extract loss allowance/ECL allowance for trade receivables or doubtful debts. Do not use bad debt expense unless the metric requested is a flow.
example: `| Allowance for doubtful accounts | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: любой
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Loss allowance or allowance for doubtful accounts related to trade receivables.

## EBITDA
name_ru: Прибыль до вычета процентов, налогов, износа и амортизации
name_en: EBITDA
aliases_en: EBITDA; adjusted EBITDA; OIBDA; earnings before interest tax depreciation and amortisation
form: Примечания
hint: Use segment reporting, MD&A, key metrics or reconciliations. Extract only explicitly disclosed EBITDA/OIBDA. Do not calculate from operating profit and D&A unless this metric is being handled as calculated.
example: `| EBITDA | (2,400) | 55,000 |` → за отчётный период (2,400)
unit_kind: money
sign: любой
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: нет

Earnings before interest, tax, depreciation and amortisation, when explicitly disclosed.

## EBT
name_ru: Прибыль до налогообложения
name_en: EBT
aliases_en: EBT; profit before tax; PBT; earnings before tax; profit before income tax
form: P&L
hint: Use the Statement of Profit and Loss. Take profit before tax/PBT immediately before tax expense. Losses should remain negative.
example: `| EBT | 86,820 | 81,298 |` → за отчётный период 86,820
unit_kind: money
sign: любой
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: нет

Profit or loss before tax.

## EPS
name_ru: Прибыль на одну обыкновенную акцию
name_en: Earnings per ordinary share
aliases_en: Earnings per ordinary share; basic earnings per share; earnings per share; EPS; basic EPS
form: P&L
hint: Use the Statement of Profit and Loss. Extract basic earnings per share, not diluted EPS. Unit is currency per share, not the general table scale.
example: `| Earnings per ordinary share | 12,47 | 11,03 |` → за отчётный период 12,47
unit_kind: money
sign: любой
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: нет

Basic earnings per ordinary share for the period.

## EQUITY_SHAREHOLDERS
name_ru: Капитал, приходящийся на акционеров материнской компании
name_en: Equity attributable to owners of the parent
aliases_en: Equity attributable to equity holders of the parent; Total shareholders' equity attributable to the parent; Equity attributable to owners of the Company
form: Баланс
hint: Баланс, раздел КАПИТАЛ: «Итого капитал, приходящийся на акционеров материнской компании» — строка ДО «Неконтролирующие доли». НЕ путать с итогом капитала (тот включает неконтролирующие доли). Нет отдельной строки — значит неконтролирующих долей нет, бери итог капитала.
example: `| Equity attributable to owners of the parent | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: любой
period_type: point
standard: Любой
formula_default_abs_behavior: нет

Собственный капитал за вычетом неконтролирующих долей — часть капитала, принадлежащая акционерам материнской компании.

## FINANCIAL_EXPENSES
name_ru: Финансовые расходы
name_en: Financial expenses
aliases_en: Financial expenses; finance costs; interest expense; borrowing costs; interest and finance charges
form: P&L
hint: Use the Statement of Profit and Loss. Extract finance costs/finance expenses. Preserve source sign conventions for expense lines.
example: `| Financial expenses | (23,780) | (19,004) |` → за отчётный период (23,780)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Finance costs such as interest expense and other borrowing costs.

## FINANCIAL_INCOME
name_ru: Финансовые доходы
name_en: Financial income
aliases_en: Financial income; finance income; interest income; other finance income
form: P&L
hint: Use the Statement of Profit and Loss. Extract finance income, normally after operating profit. Do not use cash interest received if a P&L finance income line exists.
example: `| Financial income | 8,402 | 6,118 |` → за отчётный период 8,402
unit_kind: money
sign: положительный
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Finance income such as interest income and other financial income.

## FUTURE_CAPEX_LIABILITIES
name_ru: Обязательства будущих периодов по капитальным затратам
name_en: Future capital liabilities
aliases_en: Future capital liabilities; capital commitments; future capital commitments; commitments for capital expenditure; contractual commitments for capital expenditure
form: Примечания
hint: Use commitments notes. Extract capital commitments or contractual commitments for capital expenditure not provided for. Do not include routine purchase commitments unless they are capital in nature.
example: `| Future capital liabilities | 12,800 | 9,450 |` → на отчётную дату 12,800
unit_kind: money
sign: положительный
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Contracted capital expenditure commitments not yet recognised as liabilities.

## GOODWILL
name_ru: Гудвил
name_en: Goodwill
aliases_en: Goodwill; goodwill on consolidation
form: Баланс
hint: Use the Balance Sheet or business combinations/goodwill note. Extract goodwill carrying amount only, not total intangible assets.
example: `| Goodwill | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Goodwill recognised on acquisition of subsidiaries or businesses.

## GROSS_PROFIT
name_ru: Валовая прибыль
name_en: Gross profit
aliases_en: Gross profit; gross margin; gross profit before operating expenses
form: P&L
hint: Use the Statement of Profit and Loss. Take explicit gross profit or an obvious subtotal after cost of sales and before operating expenses. Do not derive from revenue and operating profit if cost of sales is not disclosed.
example: `| Gross profit | 168,900 | 151,402 |` → за отчётный период 168,900
unit_kind: money
sign: любой
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: нет

Revenue less cost of sales or equivalent direct costs.

## GUARANTEES
name_ru: Гарантии, поручительства
name_en: Guarantees
aliases_en: Guarantees; corporate guarantees; financial guarantees; guarantees issued
form: Примечания
hint: Use commitments and contingent liabilities notes. Extract guarantees or financial guarantees issued. Do not include general legal claims unless included in the guarantees line.
example: `| Guarantees | 4,300 | 4,300 |` → на отчётную дату 4,300
unit_kind: money
sign: положительный
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Guarantees, financial guarantees or similar off-balance sheet commitments issued by the entity.

## INCOME_TAX
name_ru: Расход по текущему налогу на прибыль
name_en: Income tax expense
aliases_en: Income tax expense; current tax expense; current income tax; tax expense current portion
form: P&L
hint: Use the Statement of Profit and Loss or tax note. Extract current tax expense. Do not replace it with total tax expense if current and deferred tax are separately available.
example: `| Income tax expense | (18,400) | (16,950) |` → за отчётный период (18,400)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Current income tax expense for the period, excluding deferred tax unless explicitly presented together as current tax.

## INSURANCE_EXPENSE
name_ru: Расходы по страховой деятельности
name_en: Insurance service expense
aliases_en: Insurance service expense; insurance claims expense; insurance contract expense
form: P&L
hint: Use only if the entity has insurance operations. Extract insurance service expense or claims expense. Otherwise return null.
example: `| Insurance service expense | (33,900) | (30,210) |` → за отчётный период (33,900)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Insurance service expense or claims expense, only for entities with insurance operations.

## INSURANCE_INCOME
name_ru: Выручка по страховой деятельности
name_en: Insurance service income
aliases_en: Insurance service income; insurance revenue; insurance premium income; earned premium income
form: P&L
hint: Use only if the entity has insurance operations. Extract insurance revenue/service income. Otherwise return null.
example: `| Insurance service income | 41,300 | 37,850 |` → за отчётный период 41,300
unit_kind: money
sign: положительный
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Insurance revenue or insurance service income, only for entities with insurance operations.

## INSURANCE_LIABILITIES
name_ru: Обязательства по договорам страхования
name_en: Insurance contracts liabilities
aliases_en: Insurance contracts liabilities; insurance contract liabilities; insurance liabilities; insurance reserves
form: Баланс
hint: Use only if the entity has insurance operations. Extract insurance contract liabilities. Otherwise return null.
example: `| Insurance contracts liabilities | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Insurance contract liabilities, only for entities with insurance operations.

## INTANGIBLE_ASSETS
name_ru: Нематериальные активы
name_en: Intangible assets
aliases_en: Intangible assets; intangibles; other intangible assets; intangible assets excluding goodwill
form: Баланс
hint: Use the Balance Sheet or intangible assets note. Exclude goodwill if separately disclosed and exclude right-of-use assets.
example: `| Intangible assets | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Identifiable non-monetary assets without physical substance, excluding goodwill if presented separately.

## INTANGIBLE_ASSET_PURCHASES
name_ru: Приобретение нематериальных активов
name_en: Intangible asset purchases
aliases_en: Intangible asset purchases; purchase of intangible assets; payments for intangible assets; software purchases; intangible asset additions
form: ОДДС
hint: Use the Cash Flow Statement investing section. Extract purchases/payments for intangible assets. If intangibles are combined with PPE in one line, leave this metric null and use COMBINED_ASSET_PURCHASES.
example: `| Intangible asset purchases | (7,100) | (6,600) |` → за отчётный период (7,100)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Cash outflow for purchase of intangible assets.

## INTEREST_PAID
name_ru: Уплаченные проценты
name_en: Interest paid
aliases_en: Interest paid; finance costs paid; interest paid in cash
form: ОДДС
hint: Use the Cash Flow Statement. Extract cash interest paid. Do not use finance costs from the Statement of Profit and Loss unless the cash flow amount is unavailable and the source clearly equates them.
example: `| Interest paid | (21,050) | (18,300) |` → за отчётный период (21,050)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Cash paid for interest during the period.

## INTEREST_PAID_LEASE_LIABILITIES
name_ru: Уплаченные проценты по лизингу
name_en: Interest paid on lease liabilities
aliases_en: Interest paid on lease liabilities; interest on lease liabilities; finance cost on lease liabilities; interest element of lease payments
form: ОДДС
hint: Use the Cash Flow Statement or lease note. Extract interest paid on lease liabilities or finance cost on lease liabilities. Do not include principal repayment.
example: `| Interest paid on lease liabilities | (1,820) | (1,640) |` → за отчётный период (1,820)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Cash interest or finance cost component of lease payments.

## INTEREST_RECEIVED
name_ru: Полученные проценты
name_en: Interest received
aliases_en: Interest received; interest income received; interest received in cash
form: ОДДС
hint: Use the Cash Flow Statement. Extract cash interest received. Do not use finance income unless the cash flow amount is unavailable and the source clearly equates them.
example: `| Interest received | 4,600 | 3,900 |` → за отчётный период 4,600
unit_kind: money
sign: положительный
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Cash received as interest during the period.

## INVENTORIES
name_ru: Запасы
name_en: Inventories
aliases_en: Inventories; inventory; stock-in-trade; raw materials; work-in-progress; finished goods
form: Баланс
hint: Use the Balance Sheet current assets line inventories and the inventories note. Include raw materials, WIP, finished goods, traded goods and stores/spares if part of the inventory total.
example: `| Inventories | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Raw materials, work-in-progress, finished goods, traded goods and other inventories held for sale or production.

## LONG_TERM_DEBT
name_ru: Долгосрочные кредиты и займы
name_en: Long-term debt
aliases_en: Long-term debt; non-current borrowings; long-term borrowings; borrowings non-current; non-current debt
form: Баланс
hint: Use the Balance Sheet, non-current liabilities, and notes to borrowings. Prefer non-current borrowings or long-term debt. Do not include current borrowings or lease liabilities if they are separately disclosed.
example: `| Long-term debt | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Borrowings and debt obligations due after more than twelve months.

## LONG_TERM_LEASE_LIABILITIES
name_ru: Долгосрочные обязательства по аренде
name_en: Long-term lease liabilities
aliases_en: Long-term lease liabilities; non-current lease liabilities; lease liabilities non-current; non-current portion of lease liabilities
form: Баланс
hint: Use the Balance Sheet or lease note. Take only the non-current lease liability balance. Do not use right-of-use assets or lease payment cash flows.
example: `| Long-term lease liabilities | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Lease liabilities due after more than twelve months under Ind AS 116.

## MODIFIED_AUDITORS_OPINION
name_ru: Наличие модифицированного мнения аудитора
name_en: Modified auditor's opinion
aliases_en: Modified auditor's opinion; modified auditor opinion; modified audit opinion; qualified opinion; adverse opinion; disclaimer of opinion; independent auditor report
form: Примечания
hint: Use the Independent Auditor’s Report. Return 0 for an unmodified opinion and 1 for a qualified, adverse or disclaimer opinion. Do not create comparative periods unless a separate auditor’s opinion is disclosed for them.
example: `| Modified auditor's opinion | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: count
sign: любой
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: нет

Text flag indicating whether the independent auditor issued a modified opinion for the current reporting period.

## NET_INCOME
name_ru: Чистая прибыль
name_en: Net income
aliases_en: Net income; profit for the year; profit for the period; net profit; profit after tax; PAT
form: P&L
hint: Use the Statement of Profit and Loss. Take profit for the period/year after tax before attribution to owners and non-controlling interests. Losses should remain negative.
example: `| Net income | (12,480) | 31,002 |` → за отчётный период (12,480)
unit_kind: money
sign: любой
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: нет

Profit or loss for the period after tax before attribution to owners and non-controlling interests.

## NET_INCOME_SHAREHOLDERS
name_ru: Прибыль, приходящаяся на акционеров материнской компании
name_en: Profit attributable to owners of the parent
aliases_en: Profit attributable to equity holders of the parent; Net income attributable to shareholders of the parent; Profit for the year attributable to owners of the Company; Net income attributable to controlling interest
form: P&L
hint: ОПУ, блок распределения прибыли в самом низу: «Прибыль, приходящаяся на: акционеров материнской компании / неконтролирующие доли» — бери ПЕРВУЮ строку. Если распределения нет (у группы нет дочерних с неконтролирующей долей), прибыль совпадает с чистой прибылью — бери её.
example: `| Profit attributable to owners of the parent | 62,900 | 58,120 |` → за отчётный период 62,900
unit_kind: money
sign: любой
period_type: duration
standard: Любой
formula_default_abs_behavior: нет

Часть чистой прибыли за период, приходящаяся на акционеров материнской компании, БЕЗ неконтролирующих долей.

## NET_OPERATING_CASH_FLOW
name_ru: Чистый операционный денежный поток
name_en: Net operating cash flow
aliases_en: Net operating cash flow; net cash generated from operating activities; net cash from operating activities; cash flow from operating activities
form: ОДДС
hint: Use the Cash Flow Statement subtotal for net cash generated from or used in operating activities. Do not calculate if a subtotal is printed.
example: `| Net operating cash flow | 143,200 | 128,700 |` → за отчётный период 143,200
unit_kind: money
sign: любой
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: нет

Net cash generated from or used in operating activities.

## NET_OTHER_RESERVES
name_ru: Чистое восстановление прочих резервов
name_en: Net other provisions and reserves
aliases_en: Net other provisions and reserves; net provision reversals; net other provisions; provision for litigation and other obligations; other provisions net
form: ОДДС
hint: Use provisions or contingencies notes and P&L if a net provision creation/reversal line is disclosed. Exclude tax, ECL on receivables and finance costs unless explicitly included in the same printed line.
example: `| Net other provisions and reserves | 1,240 | (3,800) |` → за отчётный период 1,240
unit_kind: money
sign: любой
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: нет

Net creation or reversal of other provisions or reserves, excluding tax and financial asset impairment unless explicitly included.

## NON_CONTROL_INTEREST
name_ru: Неконтрольные доли участия
name_en: Non-controlling interests
aliases_en: Non-controlling interests; NCI; minority interests
form: Баланс
hint: Use the Balance Sheet equity section. Extract non-controlling interests, which may be positive or negative.
example: `| Non-controlling interests | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: любой
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: нет

Equity in subsidiaries not attributable to owners of the parent.

## OPERATING_EXPENSES
name_ru: Операционные расходы
name_en: Operating expenses
aliases_en: Operating expenses; employee benefits expense; other expenses; selling and distribution expenses; administrative expenses; total operating expenses
form: P&L
hint: Use the Statement of Profit and Loss. If no single operating expenses line exists, sum relevant operating expense lines between gross profit and operating profit. Exclude finance costs, tax and cost of sales.
example: `| Operating expenses | (58,300) | (51,100) |` → за отчётный период (58,300)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Operating expenses other than cost of sales, such as employee benefits, selling, administrative and other operating expenses.

## OPERATING_PROFIT
name_ru: Операционная прибыль
name_en: Operating profit
aliases_en: Operating profit; profit from operations; earnings before finance costs and tax; EBIT; profit before finance costs and tax
form: P&L
hint: Use the Statement of Profit and Loss or segment/MD&A tables. Take profit from operations or EBIT before finance income, finance costs and tax. Do not substitute profit before tax.
example: `| Operating profit | 110,600 | 100,302 |` → за отчётный период 110,600
unit_kind: money
sign: любой
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: нет

Profit from operations before finance income, finance costs and tax.

## PERPETUAL_BONDS
name_ru: Бессрочные облигации
name_en: Perpetual bonds
aliases_en: Perpetual bonds; perpetual debt instruments; perpetual securities; perpetual subordinated bonds
form: Баланс
hint: Use equity or borrowings notes. Extract only explicitly perpetual bonds or similar perpetual instruments. Do not use ordinary bonds or redeemable debentures.
example: `| Perpetual bonds | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Perpetual bonds or similar perpetual instruments disclosed in equity or borrowings depending on classification.

## PPE
name_ru: Основные средства
name_en: Property, plant and equipment
aliases_en: Property, plant and equipment; property plant and equipment; PPE; tangible assets; plant and equipment
form: Баланс
hint: Use the Balance Sheet or property, plant and equipment note. Take carrying amount/net block. Exclude right-of-use assets, intangible assets and goodwill if separately disclosed.
example: `| Property, plant and equipment | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Carrying amount of property, plant and equipment excluding right-of-use assets if separately disclosed.

## PRINCIPAL_REPAYMENT_OF_LEASE_LIABILITIES
name_ru: Погашение обязательств по аренде
name_en: Repayment of principal portion of lease liabilities
aliases_en: Repayment of principal portion of lease liabilities; payment of lease liabilities; principal elements of lease payments; repayment of lease liabilities
form: ОДДС
hint: Use the Cash Flow Statement financing section or lease note. Extract principal portion of lease payments only. Do not include lease interest paid.
example: `| Repayment of principal portion of lease liabilities | (12,400) | (11,100) |` → за отчётный период (12,400)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Principal portion of lease payments classified as financing cash flows.

## RETAINED_EARNINGS
name_ru: Нераспределенная прибыль
name_en: Retained Earnings
aliases_en: Retained Earnings; surplus in statement of profit and loss; accumulated profits; accumulated deficit
form: Баланс
hint: Use the equity section or statement of changes in equity. Extract retained earnings/surplus/accumulated deficit at the reporting date. Do not use profit for the period.
example: `| Retained Earnings | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: любой
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: нет

Accumulated retained earnings, surplus or deficit at the reporting date.

## RIGHT_OF_USE_ASSETS
name_ru: Активы в форме права пользования
name_en: Right-of-use assets
aliases_en: Right-of-use assets; ROU assets; right of use assets; assets under leases
form: Баланс
hint: Use the Balance Sheet or lease note under Ind AS 116. Extract right-of-use assets only. Do not use lease liabilities or ordinary PPE unless ROU assets are embedded and no separate split is provided.
example: `| Right-of-use assets | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Carrying amount of assets representing the right to use leased underlying assets under Ind AS 116.

## ROU_DEPRECIATION
name_ru: Амортизация активов в форме права пользования
name_en: Right-of-use asset depreciation
aliases_en: Right-of-use asset depreciation; depreciation of right-of-use assets; amortisation of right-of-use assets; ROU depreciation; depreciation on leased assets
form: ОДДС
hint: Use the lease note or depreciation note. Extract depreciation or amortisation of right-of-use assets only. Do not use total depreciation and amortisation.
example: `| Right-of-use asset depreciation | 6,900 | 6,100 |` → за отчётный период 6,900
unit_kind: money
sign: положительный
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Depreciation charge on right-of-use assets for the period.

## SALES_REVENUE
name_ru: Выручка
name_en: Sales revenue
aliases_en: Sales revenue; revenue from operations; revenue; revenue from sale of products; revenue from services; income from operations
form: P&L
hint: Use the Statement of Profit and Loss. In India, prefer the Schedule III label revenue from operations. For segment revenue, prefer revenue from external customers and exclude inter-segment revenue if separately disclosed.
example: `| Sales revenue | 604,401 | 552,271 |` → за отчётный период 604,401
unit_kind: money
sign: положительный
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Revenue from ordinary activities, normally presented as revenue from operations for Indian companies.

## SECURITIES_ASSETS
name_ru: Финансовые инструменты (Актив)
name_en: Investment securities and other financial assets
aliases_en: Investment securities and other financial assets; investment securities; financial assets investments; marketable securities; current investments; non-current investments
form: Баланс
hint: Use the Balance Sheet and financial assets/investments notes. Extract securities or investment financial assets. Do not include cash equivalents, bank deposits or investments in associates if separately classified.
example: `| Investment securities and other financial assets | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Debt or equity investments and securities held as financial assets.

## SECURITIES_LIABILITIES
name_ru: Финансовые инструменты (Обязательства)
name_en: Financial instrument liabilities
aliases_en: Financial instrument liabilities; financial liabilities at fair value through profit or loss; derivative or trading financial liabilities; securities liabilities
form: Баланс
hint: Use the Balance Sheet and financial instruments notes. Extract trading/FVTPL/derivative financial liabilities where separately disclosed. Do not use ordinary borrowings or issued debt unless explicitly classified as securities liabilities.
example: `| Financial instrument liabilities | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Financial liabilities classified as trading, fair value through profit or loss, derivative or similar instrument liabilities.

## SHARES_BUYBACK
name_ru: Выкуп собственных акций
name_en: Shares buyback
aliases_en: Shares buyback; share buyback; buyback of shares; purchase of own shares; repurchase of shares
form: ОДДС
hint: Use the Cash Flow Statement financing section or equity note. Extract cash outflow for buyback/repurchase of shares. Do not use the treasury share balance.
example: `| Shares buyback | 12,345 | 11,200 |` → за отчётный период 12,345
unit_kind: money
sign: отрицательный
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Cash outflow for repurchase or buyback of the entity’s own shares.

## SHARE_CAPITAL
name_ru: Уставной капитал
name_en: Share capital
aliases_en: Share capital; equity share capital; paid-up share capital; issued subscribed and paid-up share capital
form: Баланс
hint: Use the Balance Sheet or equity note. Extract issued/subscribed/paid-up share capital at the reporting date. Do not use total equity.
example: `| Share capital | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Nominal value of issued and paid-up share capital.

## SHARE_PREMIUM
name_ru: Эмиссионный доход
name_en: Share premium
aliases_en: Share premium; securities premium; additional paid-in capital; securities premium account
form: Баланс
hint: Use the equity note or statement of changes in equity. Extract securities premium/share premium. Do not use share capital or retained earnings.
example: `| Share premium | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Securities premium or share premium reserve.

## SHORT_TERM_BORROWINGS
name_ru: Краткосрочные кредиты и займы
name_en: Short-term borrowings and current portion of long-term debt
aliases_en: Short-term borrowings and current portion of long-term debt; short-term borrowings; current borrowings; current maturities of long-term debt; current portion of long-term borrowings; short-term debt
form: Баланс
hint: Use the Balance Sheet, current liabilities, and notes to borrowings. Include current maturities if the line is presented as current borrowings or current portion of borrowings. Exclude current lease liabilities if separately disclosed.
example: `| Short-term borrowings and current portion of long-term debt | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Borrowings due within twelve months, including current maturities of long-term borrowings where presented together.

## SHORT_TERM_LEASE_LIABILITIES
name_ru: Краткосрочные обязательства по аренде
name_en: Short-term lease liabilities
aliases_en: Short-term lease liabilities; current lease liabilities; current portion of lease liabilities; lease liabilities current
form: Баланс
hint: Use the Balance Sheet or lease note. Take only the current portion of lease liabilities. Do not use right-of-use assets or total lease liabilities unless current/non-current split is unavailable.
example: `| Short-term lease liabilities | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Current portion of lease liabilities due within twelve months under Ind AS 116.

## SUBSIDIARY_ACQUISITIONS
name_ru: Приобретение дочерних организаций (обществ) за вычетом (минусом) денежных средств и за вычетом (минусом) полученных денежных средств
name_en: Subsidiary and business acquisitions
aliases_en: Subsidiary and business acquisitions; acquisition of subsidiaries; acquisition of business; purchase of subsidiaries net of cash acquired; business combinations cash outflow
form: ОДДС
hint: Use the Cash Flow Statement investing section. Take acquisition of subsidiaries/businesses net of cash acquired. Do not include purchases of associates unless the line explicitly combines business acquisitions.
example: `| Subsidiary and business acquisitions | 12,345 | 11,200 |` → за отчётный период 12,345
unit_kind: money
sign: отрицательный
period_type: duration
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Cash outflow for acquisition of subsidiaries or businesses, normally net of cash acquired.

## TOTAL_ASSETS
name_ru: Итого активы
name_en: Total assets
aliases_en: Total assets; assets total; total asset base
form: Баланс
hint: Use the Balance Sheet final total assets line. It should equal total equity and liabilities.
example: `| Total assets | 1,204,883 | 1,145,210 |` → на отчётную дату 1,204,883
unit_kind: money
sign: положительный
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Total carrying amount of all assets at the reporting date.

## TOTAL_ASSOCIATE_INVESTMENTS
name_ru: Инвестиции в ассоциированные и совместные предприятия
name_en: Associate investments
aliases_en: Associate investments; investments in associates and joint ventures; associate and joint venture investments; equity method investments; investments accounted for using the equity method
form: Баланс
hint: Use the Balance Sheet or investments note. Extract total investments in associates and joint ventures. Do not use purchases in cash flow or share of profit in P&L.
example: `| Associate investments | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Total investments in associates and joint ventures accounted for using the equity method.

## TOTAL_CURRENT_ASSETS
name_ru: Итого оборотных активов
name_en: Total current assets
aliases_en: Total current assets; current assets total; subtotal current assets
form: Баланс
hint: Use the Balance Sheet subtotal for total current assets under Schedule III Division II. Do not calculate from components if a subtotal is printed.
example: `| Total current assets | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Total assets expected to be realised, sold or consumed within the operating cycle or within twelve months.

## TOTAL_CURRENT_LIABILITIES
name_ru: Итого краткосрочных обязательств
name_en: Total current liabilities
aliases_en: Total current liabilities; current liabilities total; subtotal current liabilities
form: Баланс
hint: Use the Balance Sheet subtotal for total current liabilities under Schedule III Division II. Do not calculate from components if a subtotal is printed.
example: `| Total current liabilities | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: да

Total obligations expected to be settled within the operating cycle or within twelve months.

## TOTAL_EQUITY
name_ru: Итого собственный капитал
name_en: Total equity
aliases_en: Total equity; equity total; total shareholders equity; total equity attributable to owners and non-controlling interests
form: Баланс
hint: Use the Balance Sheet total equity line. Include equity attributable to owners and non-controlling interests where consolidated statements are used.
example: `| Total equity | 512,004 | 498,771 |` → на отчётную дату 512,004
unit_kind: money
sign: любой
period_type: point
standard: Ind AS (IFRS-convergent) / Schedule III Division II
formula_default_abs_behavior: нет

Total equity attributable to owners and non-controlling interests.
