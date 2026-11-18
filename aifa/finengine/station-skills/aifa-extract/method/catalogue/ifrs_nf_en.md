# Ячейка ifrs_nf_en — нефинансовые организации, МСФО, отчётность на английском

* стандарт отчётности: **IFRS**
* единица измерения по умолчанию: **млн. $**
* подпись периода, когда своей в документе нет: **Current**

Всё ниже до первой записи `## <ENUM>` — ПРАВИЛА ЯЧЕЙКИ: как читать
отчётность этого стандарта. Они уезжают в промпт целиком. Правятся
текстом, без программиста. Правила конкретного показателя — в его
записи, поле `hint`.
### Additional guardrails

1. **Currency presentation matters a lot.** The unit_text MUST capture the exact currency symbol/code — never default to "$" when the table shows GBP/£, EUR/€, JPY/¥, CHF, KRW or another reporting currency.

2. **Non-calendar fiscal years.** Return the non-modified period header ("Year ended 31 March 2024", "Year ended 30 June 2025", etc.) and let user's normalizer decide on FY vs shifted 12M.

3. **"Underlying" / "Adjusted" / "Core" measures are non-IFRS.** Use statutory / reported numbers unless the metric explicitly requires adjusted. When a table has statutory and underlying columns side-by-side, use the statutory column.

4. **Two-column local + IFRS presentation.** If local-GAAP and IFRS columns are shown side-by-side, use the IFRS column.

## ACCOUNTS_PAYABLE
name_ru: Кредиторская задолженность
name_en: Accounts payable
aliases_en: Trade and other payables; Trade payables
form: Баланс
hint: Краткосрочные обязательства баланса. Детализация в Примечании 'Кредиторская задолженность'.
example: `| Accounts payable | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Суммы, причитающиеся поставщикам, подрядчикам и прочим кредиторам.

## ACCOUNTS_RECEIVABLE
name_ru: Дебиторская задолженность
name_en: Accounts receivable
aliases_en: Trade and other receivables; Trade receivables; Trade accounts receivable; Trade accounts receivable, net; Accounts receivable, net
form: Баланс
hint: Оборотные активы баланса. Бери торговую дебиторскую задолженность / accounts receivable, net. Не включай предоплаты, авансы выданные, НДС к возмещению и прочую дебиторскую задолженность, если они раскрыты отдельно.
example: `| Accounts receivable | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Суммы, причитающиеся компании от покупателей и прочих контрагентов.

## ADJUSTMENTS_OPERATING_CASH_FLOWS
name_ru: Корректировки
name_en: Operating cash flow adjustments
aliases_en: Non-cash adjustments; Adjustments for non-cash items; Adjustments; Adjustments to operating cash flows
form: ОДДС
hint: IFRS cash-flow statement: subtotal the adjustment rows after profit for the period and before working-capital movements. If no named subtotal is printed, sum only that adjustments block. Exclude working-capital movements, interest, taxes and investing/financing cash-flow lines.
example: `| Operating cash flow adjustments | 12,345 | 11,200 |` → за отчётный период 12,345
unit_kind: money
sign: любой
period_type: duration
standard: Любой
formula_default_abs_behavior: нет

Subtotal of non-cash/profit adjustments in the operating cash-flow section before working-capital movements.

## ASSET_PURCHASES
name_ru: Приобретение основных средств
name_en: PPE purchases
aliases_en: Purchases of property; plant and equipment; Additions to PP&E
form: ОДДС
hint: ОДДС, раздел 'Инвестиционная деятельность'. Если в отчёте сумма с НМА вместе — используйте COMBINED_ASSET_PURCHASES.
example: `| PPE purchases | (91,300) | (80,500) |` → за отчётный период (91,300)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Денежные расходы на приобретение основных средств за период.

## ASSET_SALES_PROCEEDS
name_ru: Поступления от продажи основных средств и нематериальных активов
name_en: Proceeds from disposal of PPE and Intangible assets
aliases_en: Proceeds from sale of PP&E; Proceeds from disposal of fixed assets; Proceeds from divestitures, net; Proceeds from dispositions; Proceeds from sale of intangible assets; Proceeds from sale of PPE
form: ОДДС
hint: Cash-flow statement, investing activities: cash proceeds from sale/disposal of non-current operating assets. If proceeds from sale of PPE and intangible assets are disclosed separately, sum the relevant rows; do not take only PPE when intangible proceeds are present.
example: `| Proceeds from disposal of PPE and Intangible assets | 2,900 | 1,400 |` → за отчётный период 2,900
unit_kind: money
sign: положительный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Денежные поступления от продажи внеоборотных активов за период.

## ASSOCIATE_INCOMES
name_ru: Доходы от ассоциированных компаний
name_en: Associate incomes
aliases_en: Proceeds from disposal of subsidiaries; Disposal of subsidiaries and associates; Equity income in investee; Equity income from investees; Share of profit of associates
form: P&L
hint: P&L или примечания equity method/investee. Не путать с proceeds from sale/disposal of subsidiaries.
example: `| Associate incomes | 3,214 | (1,050) |` → за отчётный период 3,214
unit_kind: money
sign: любой
period_type: duration
standard: Любой
formula_default_abs_behavior: нет

Доля компании в прибыли/убытке ассоциированных компаний или инвестиций по методу долевого участия.

## ASSOCIATE_INVESTMENTS
name_ru: Вложения в ассоциированные организации (общества) и совместные предприятия
name_en: Associate investments
aliases_en: Investments in associates and joint ventures; Equity-accounted investments
form: Баланс
hint: Внеоборотные активы баланса. Учёт по методу долевого участия. Примечание 'Ассоциированные компании и совместные предприятия'.
example: `| Associate investments | 18,402 | 16,950 |` → на отчётную дату 18,402
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Доли в компаниях, на которые компания оказывает существенное влияние (обычно 20-50% долей).

## BANK_DEPOSITS
name_ru: Банковские депозиты
name_en: Bank deposits
aliases_en: Time deposits; Short-term bank deposits; Bank deposits; Short-term deposits
form: Баланс
hint: Оборотные активы баланса или примечание к денежным средствам/краткосрочным инвестициям. Если строка баланса 'краткосрочные финансовые вложения' раскрыта в примечании как банковские/time deposits, бери эту строку. Не путай с cash equivalents и marketable securities.
example: `| Bank deposits | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Денежные средства, размещённые в банках на срочной основе (обычно < 12 месяцев).

## CAPEX
name_ru: Капитальные затраты
name_en: Capital expenditure
aliases_en: Capex; Capital expenditures; Payments to acquire property, plant and equipment; Capital expenditures total
form: ОДДС
hint: ОДДС или MD&A, раздел инвестиционной деятельности / capital expenditures. Если компания раскрывает итоговый CapEx/capital expenditures напрямую, бери раскрытый итог и все его колонки периодов, включая 3M/Q и 6M/YTD. Иначе используй приобретение ОС плюс НМА. Сохраняй знак/скобки как в источнике; не заменяй CapEx одной строкой покупки ОС, если есть отдельный итоговый CapEx или отдельная покупка НМА.
example: `| Capital expenditure | (98,400) | (87,100) |` → за отчётный период (98,400)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Совокупные капитальные затраты — приобретение ОС + НМА за вычетом продажи.

## CAPITALIZED_INTEREST
name_ru: Капитализированные и уплаченные проценты
name_en: Capitalized interest
aliases_en: Capitalized borrowing costs; Borrowing costs capitalized
form: Примечания
hint: Примечание 'Кредиты и займы' или 'Затраты по займам'. Также может быть в Примечании 'Основные средства'.
example: `| Capitalized interest | 2,140 | 1,870 |` → за отчётный период 2,140
unit_kind: money
sign: положительный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Проценты по займам, капитализированные в стоимость квалифицируемых активов.

## CASH_EQUIVALENTS
name_ru: Денежные средства и их эквиваленты
name_en: Cash and cash equivalents
aliases_en: Cash and equivalents; Cash and cash equivalents at end of period
form: Баланс
hint: Balance sheet, cash-flow statement or cash note. Extract every clearly disclosed date for cash and cash equivalents, not only balance-sheet dates; include comparative beginning/end balances when they are explicitly shown. Do not confuse with bank deposits/time deposits or marketable securities.
example: `| Cash and cash equivalents | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Касса, расчётные счета и краткосрочные высоколиквидные инвестиции (срок ≤ 3 мес.).

## CHANGES_OCF_ASSETS_LIABILITIES
name_ru: Изменения в оборотном капитале
name_en: Changes in working capital
aliases_en: Changes in operating assets and liabilities; Working capital movements; Changes in working capital
form: ОДДС
hint: IFRS cash-flow statement: subtotal the working-capital block, usually named changes in operating assets and liabilities / changes in working capital. If no subtotal is printed, sum only rows in that block. Exclude non-cash adjustments, interest, taxes and investing/financing cash flows.
example: `| Changes in working capital | 12,345 | 11,200 |` → за отчётный период 12,345
unit_kind: money
sign: любой
period_type: duration
standard: Любой
formula_default_abs_behavior: нет

Subtotal of working-capital movements inside operating cash flow.

## COMBINED_ASSET_PURCHASES
name_ru: Приобретение основных средств и нематериальных активов
name_en: Fixed asset purchases
aliases_en: Purchase of PP&E and intangible assets; Capital expenditures
form: ОДДС
hint: ОДДС, раздел 'Инвестиционная деятельность'. Заполнять только если ASSET_PURCHASES и INTANGIBLE_ASSET_PURCHASES объединены в одну строку.
example: `| Fixed asset purchases | (98,400) | (87,100) |` → за отчётный период (98,400)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Объединённая статья денежных расходов на приобретение ОС и НМА (когда в отчёте они не разделены).

## COMMITTED_CREDIT_FACILITIES
name_ru: Неиспользованные кредитные линии
name_en: Committed credit facilities
aliases_en: Undrawn committed facilities; Available credit lines
form: Примечания
hint: Note: for narrative/text disclosures without a table header, preserve the amount and scale exactly as written in the sentence. Example: if the source says "$3 billion available under a revolving credit agreement", return value "3" (or "3.0") and unit_text "$ billion"/"billion"; do not convert it to "3,000" and do not borrow "in millions" from nearby tables. Do not create a comparative period unless the facility amount is explicitly disclosed for that date.
example: `| Committed credit facilities | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Сумма доступных, но неиспользованных кредитных линий.

## COSTS_EXPENSES
name_ru: Себестоимость
name_en: Costs and expenses
aliases_en: Cost of goods sold; Cost of sales; Cost of revenue; Cost of products; Cost of services
form: P&L
hint: P&L: прямая себестоимость обычно идёт сразу после выручки и перед gross profit. В разных отраслях может называться по деятельности компании, но должна оставаться прямыми затратами на проданные товары/услуги. Если прямые затраты раскрыты несколькими строками (например cost of services/products), суммируй только эти строки. Не бери subtotal/total, который включает amortization, SG&A, R&D или operating expenses; не рассчитывай из выручки и operating income.
example: `| Costs and expenses | (435,501) | (400,869) |` → за отчётный период (435,501)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Прямые затраты, связанные с производством проданной продукции/услуг.

## DEFERRED_TAX_ASSETS
name_ru: Отложенные налоговые активы
name_en: Deferred tax assets
aliases_en: Deferred income tax assets
form: Баланс
hint: Внеоборотные активы. Подробности в Примечании 'Налог на прибыль' / 'Отложенные налоги'.
example: `| Deferred tax assets | 12,345 | 11,200 |` → на отчётную дату 12,345
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
example: `| Deferred tax liabilities | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Налоги на прибыль, подлежащие уплате в будущих периодах из-за налогооблагаемых временных разниц.

## DEPRECIATION_AMORTIZATION
name_ru: Амортизация материальных и нематериальных активов
name_en: Depreciation and amortization
aliases_en: D&A; DD&A; Depreciation; depletion and amortization; Depreciation and amortization; Amortization of acquisition-related intangibles
form: ОДДС
hint: ОДДС/примечания: если рядом раскрыты Depreciation and amortization и отдельная amortization of acquisition-related intangibles, общая D&A — их сумма. Не путай с ROU amortization.
example: `| Depreciation and amortization | 40,100 | 37,600 |` → за отчётный период 40,100
unit_kind: money
sign: положительный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Сумма амортизации основных средств и нематериальных активов за период.

## DERIVATIVE_LIABILITIES
name_ru: Производные финансовые обязательства
name_en: Derivatives
aliases_en: Derivative financial instruments; Derivative liabilities
form: Баланс
hint: Балансовый отчёт. Также в Примечании 'Финансовые инструменты' / 'Управление рисками'.
example: `| Derivatives | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Обязательства по производным финансовым инструментам, оцениваемые по справедливой стоимости.

## DIVIDENDS
name_ru: Дивиденды
name_en: Dividends
aliases_en: Dividends paid; Cash dividends paid
form: ОДДС
hint: Cash-flow statement: all cash dividends paid during the period. If dividends paid to parent shareholders and non-controlling interests are disclosed separately, sum them. Parent-only dividends belong to DIVIDENDS_SHAREHOLDERS.
example: `| Dividends | 12,345 | 11,200 |` → за отчётный период 12,345
unit_kind: money
sign: отрицательный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

All cash dividends paid by the group during the period.

## DIVIDENDS_PAID_NONCONTROLLING_INTERESTS
name_ru: Дивиденды, выплаченные держателям неконтролирующих долей
name_en: Dividends paid to non-controlling interests
aliases_en: Dividends paid to minority interests; Dividends to non-controlling shareholders
form: ОДДС
hint: ОДДС, раздел 'Финансовая деятельность' (отдельной строкой от дивидендов акционерам).
example: `| Dividends paid to non-controlling interests | (2,400) | (2,100) |` → за отчётный период (2,400)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Денежные выплаты дивидендов держателям неконтролирующих долей дочерних компаний.

## DIVIDENDS_RECEIVED
name_ru: Дивиденды полученные
name_en: Dividends received
aliases_en: Cash dividends received
form: ОДДС
hint: ОДДС, раздел 'Инвестиционная деятельность'.
example: `| Dividends received | 1,900 | 1,350 |` → за отчётный период 1,900
unit_kind: money
sign: положительный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Денежные поступления дивидендов от ассоциированных и прочих компаний.

## DIVIDENDS_SHAREHOLDERS
name_ru: Дивиденды акционерам
name_en: Dividends paid to shareholders
aliases_en: Dividends paid to equity holders of the parent
form: ОДДС
hint: ОДДС, раздел 'Финансовая деятельность'. Бери строку выплат дивидендов акционерам и сохраняй знак/скобки как в источнике.
example: `| Dividends paid to shareholders | (30,000) | (27,500) |` → за отчётный период (30,000)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Денежные выплаты дивидендов акционерам материнской компании за период.

## DOUBTFUL_ACCOUNTS_ALLOWANCE
name_ru: Резерв под ожидаемые кредитные убытки (дебиторская задолженность)
name_en: Allowance for doubtful accounts
aliases_en: ECL allowance; Allowance for expected credit losses; Provision for credit losses
form: Примечания
hint: Примечание 'Дебиторская задолженность' или 'Управление кредитным риском'. Может быть отдельной строкой 'Изменение резерва ECL'.
example: `| Allowance for doubtful accounts | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: любой
period_type: point
standard: Любой
formula_default_abs_behavior: да

Резерв под ожидаемые кредитные убытки по дебиторской задолженности.

## EBITDA
name_ru: Прибыль до вычета процентов, налогов, износа и амортизации
name_en: EBITDA
aliases_en: Adjusted EBITDA; Segment EBITDA; EBITDA total
form: Примечания
hint: Сегментная отчётность или MD&A. Для main metric бери итог/Total по EBITDA, не отдельный сегмент, не elimination-only строку и не EBITDA before IFRS 16/adjusted subtotal, если раскрыт консолидированный total. Если строка 'EBITDA по сегменту' содержит сегменты, исключения/eliminations и затем итоговые значения в последних колонках или в конце строки, бери эти итоговые значения как main EBITDA. Возвращай null только если явной строки EBITDA/OIBDA нет.
example: `| EBITDA | (2,400) | 55,000 |` → за отчётный период (2,400)
unit_kind: money
sign: любой
period_type: duration
standard: Любой
formula_default_abs_behavior: нет

Операционная прибыль до амортизации (EBIT + D&A). Часто компании раскрывают как нефинансовую метрику.

## EBT
name_ru: Прибыль до налогообложения
name_en: EBT
aliases_en: Profit before tax; Income before income taxes; Pre-tax income
form: P&L
hint: P&L, предпоследняя строка. Если у компании убыток, в источнике обычно стоят скобки или минус; сохраняй знак/скобки как в источнике.
example: `| EBT | 86,820 | 81,298 |` → за отчётный период 86,820
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
example: `| Earnings per ordinary share | 12,47 | 11,03 |` → за отчётный период 12,47
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
aliases_en: Finance costs; Interest expense
form: P&L
hint: P&L, после операционной прибыли. Бери строку finance costs/interest expense и сохраняй знак/скобки как в источнике.
example: `| Financial expenses | (23,780) | (19,004) |` → за отчётный период (23,780)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Процентные расходы по кредитам, займам, облигациям и прочее обслуживание долга.

## FINANCIAL_INCOME
name_ru: Финансовые доходы
name_en: Financial income
aliases_en: Interest income; Finance income
form: P&L
hint: P&L, после операционной прибыли. Бери строку finance/financial income и сохраняй значение как в источнике.
example: `| Financial income | 8,402 | 6,118 |` → за отчётный период 8,402
unit_kind: money
sign: положительный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Процентные и прочие финансовые доходы (от размещения средств, депозитов, ценных бумаг).

## FUTURE_CAPEX_LIABILITIES
name_ru: Обязательства будущих периодов по капитальным затратам
name_en: Future capital liabilities
aliases_en: Capital commitments; Committed capital expenditures
form: Примечания
hint: Примечание 'Условные обязательства' / 'Капитальные обязательства' / 'Договорные обязательства'.
example: `| Future capital liabilities | 12,800 | 9,450 |` → на отчётную дату 12,800
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Договорные обязательства по будущим капитальным затратам, ещё не отражённые в балансе.

## GOODWILL
name_ru: Гудвил
name_en: Goodwill
aliases_en: Goodwill
form: Баланс
hint: Внеоборотные активы баланса. Подробнее — в Примечании 'Гудвил' / 'Объединение бизнеса'.
example: `| Goodwill | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Превышение цены приобретения дочерней компании над справедливой стоимостью её чистых идентифицируемых активов.

## GROSS_PROFIT
name_ru: Валовая прибыль
name_en: Gross profit
aliases_en: Gross margin; Gross profit; Subtotal after cost of sales
form: P&L
hint: P&L, строка после себестоимости. Если строка Gross profit не подписана, но есть очевидный subtotal после прямых costs of sales/products/services и перед operating expenses, можно брать этот subtotal. Не выводи gross profit, если есть только operating income без раскрытой себестоимости.
example: `| Gross profit | 168,900 | 151,402 |` → за отчётный период 168,900
unit_kind: money
sign: любой
period_type: duration
standard: Любой
formula_default_abs_behavior: нет

Выручка минус себестоимость продаж.

## GUARANTEES
name_ru: Гарантии, поручительства
name_en: Garantees
aliases_en: Financial guarantees; Bank guarantees; Contingent liabilities
form: Примечания
hint: Примечание 'Условные обязательства' / 'Гарантии и поручительства'. Внебалансовая статья.
example: `| Garantees | 4,300 | 4,300 |` → на отчётную дату 4,300
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Сумма выданных гарантий и поручительств за третьих лиц.

## INCOME_TAX
name_ru: Расход по текущему налогу на прибыль
name_en: Income tax expense
aliases_en: Tax expense; Provision for income taxes; Income tax provision (benefit); Current income tax expense; Current tax expense
form: P&L
hint: IFRS P&L/tax note: take the current income tax expense line for the period. Do not replace it with the total income-tax benefit/expense line that combines current tax, deferred tax and one-off tax effects.
example: `| Income tax expense | (18,400) | (16,950) |` → за отчётный период (18,400)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Current income tax expense for the period, excluding deferred tax and one-off tax effects when separately disclosed.

## INSURANCE_EXPENSE
name_ru: Расходы по страховой деятельности
name_en: Insurance service expense
aliases_en: Insurance claims and expenses; Claims and benefits paid
form: P&L
hint: P&L страховой компании. Не применимо к не-страховым — null.
example: `| Insurance service expense | (33,900) | (30,210) |` → за отчётный период (33,900)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Расходы страховщика по договорам страхования (по IFRS 17).

## INSURANCE_INCOME
name_ru: Выручка по страховой деятельности
name_en: Insurance service income
aliases_en: Insurance premium income; Net earned premiums
form: P&L
hint: P&L страховой компании. Не применимо к не-страховым организациям — null.
example: `| Insurance service income | 41,300 | 37,850 |` → за отчётный период 41,300
unit_kind: money
sign: положительный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Доходы страховщика по договорам страхования (по IFRS 17).

## INSURANCE_LIABILITIES
name_ru: Обязательства по договорам страхования
name_en: Insurance contracts liabilities
aliases_en: Insurance liabilities; Technical reserves
form: Баланс
hint: Балансовый отчёт страховой компании. Только если компания ведёт страховую деятельность.
example: `| Insurance contracts liabilities | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Обязательства страховщика по договорам страхования (резервы убытков, премии).

## INTANGIBLE_ASSETS
name_ru: Нематериальные активы
name_en: Intangible assets
aliases_en: Intangible assets; Other intangible assets; Intangibles
form: Баланс
hint: Внеоборотные активы баланса (отдельная строка после гудвила). Детализация в Примечании 'Нематериальные активы'.
example: `| Intangible assets | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Идентифицируемые неденежные активы без физической субстанции — лицензии, патенты, программное обеспечение, бренды.

## INTANGIBLE_ASSET_PURCHASES
name_ru: Приобретение нематериальных активов
name_en: Intangible asset purchases
aliases_en: Purchases of intangible assets; Acquisition of intangible assets
form: ОДДС
hint: ОДДС, раздел 'Инвестиционная деятельность' (отдельной строкой от ОС). Если в отчёте объединено с ОС — null, см. COMBINED_ASSET_PURCHASES.
example: `| Intangible asset purchases | (7,100) | (6,600) |` → за отчётный период (7,100)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Денежные расходы на приобретение нематериальных активов за период.

## INTEREST_PAID
name_ru: Уплаченные проценты
name_en: Interest paid
aliases_en: Interest payments; Cash interest paid
form: ОДДС
hint: ОДДС, раздел 'Финансовая деятельность' (или иногда 'Операционная' по выбору учётной политики).
example: `| Interest paid | (21,050) | (18,300) |` → за отчётный период (21,050)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Денежные выплаты процентов по кредитам и займам за период.

## INTEREST_PAID_LEASE_LIABILITIES
name_ru: Уплаченные проценты по лизингу
name_en: Interest paid on lease liabilities
aliases_en: Interest expense on lease liabilities; Cash paid for lease interest
form: ОДДС
hint: ОДДС, раздел 'Финансовая деятельность'. Также в Примечании 'IFRS 16' / 'Аренда'.
example: `| Interest paid on lease liabilities | (1,820) | (1,640) |` → за отчётный период (1,820)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Процентная составляющая лизинговых платежей за период.

## INTEREST_RECEIVED
name_ru: Полученные проценты
name_en: Interest received
aliases_en: Interest receipts; Cash interest received
form: ОДДС
hint: ОДДС, чаще в разделе 'Инвестиционная деятельность' (или 'Операционная').
example: `| Interest received | 4,600 | 3,900 |` → за отчётный период 4,600
unit_kind: money
sign: положительный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Денежные поступления процентов от размещений и финансовых активов.

## INVENTORIES
name_ru: Запасы
name_en: Inventories
aliases_en: Inventory; Stocks
form: Баланс
hint: Оборотные активы баланса. Детализация в Примечании 'Запасы'.
example: `| Inventories | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Сырьё, незавершённое производство и готовая продукция, удерживаемые для продажи.

## LONG_TERM_DEBT
name_ru: Долгосрочные кредиты и займы
name_en: Long-term debt
aliases_en: Long-term borrowings; Long-term loans and borrowings
form: Баланс
hint: Долгосрочные обязательства баланса. Детализация (валюта, ставка, сроки) в Примечании 'Кредиты и займы' / 'Заёмные средства'.
example: `| Long-term debt | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Кредиты и займы со сроком погашения более 12 месяцев.

## LONG_TERM_LEASE_LIABILITIES
name_ru: Долгосрочные обязательства по аренде
name_en: Long-term lease liabilities
aliases_en: Non-current lease liabilities; Long-term portion of lease liabilities
form: Баланс
hint: Долгосрочные обязательства баланса. Также в Примечании 'IFRS 16' — графа 'Свыше 1 года'.
example: `| Long-term lease liabilities | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Обязательства по аренде по IFRS 16 со сроком погашения более 12 месяцев.

## LONG_TERM_ROU
name_ru: Долгосрочные активы в форме права пользования
name_en: Long Term Right of Use Assets
aliases_en: Non-current right-of-use assets; Long-term ROU assets
form: Баланс
hint: Это активы права пользования (ROU assets), а не обязательства по аренде. Бери строку баланса/right-of-use assets; не подставляй current/non-current lease liabilities.
example: `| Long Term Right of Use Assets | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Активы ППА к признанию через более 12 месяцев.

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
example: `| Net income | (12,480) | 31,002 |` → за отчётный период (12,480)
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
aliases_en: Net cash from operating activities; Net cash provided by operating activities
form: ОДДС
hint: ОДДС (Cash Flow Statement), итоговая строка раздела 'Операционная деятельность'.
example: `| Net operating cash flow | 143,200 | 128,700 |` → за отчётный период 143,200
unit_kind: money
sign: любой
period_type: duration
standard: Любой
formula_default_abs_behavior: нет

Итоговый денежный поток от основной операционной деятельности (после уплаты налогов и процентов).

## NET_OTHER_RESERVES
name_ru: Чистое восстановление прочих резервов
name_en: Net other reserves
aliases_en: Net reversal of other provisions; Net recovery of other provisions
form: ОДДС
hint: ОДДС или P&L. Может быть в составе 'Прочие операционные доходы/расходы' или отдельной строкой.
example: `| Net other reserves | 1,240 | (3,800) |` → за отчётный период 1,240
unit_kind: money
sign: любой
period_type: duration
standard: Любой
formula_default_abs_behavior: нет

Чистый эффект от прочих резервов за период (восстановление минус начисление).

## NON_CONTROL_INTEREST
name_ru: Неконтрольные доли участия
name_en: Non-controlling interest
aliases_en: Minority interest; Non-controlling interests
form: Баланс
hint: Раздел КАПИТАЛ баланса (отдельная подстрока). Может быть как положительной, так и отрицательной.
example: `| Non-controlling interest | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: любой
period_type: point
standard: Любой
formula_default_abs_behavior: нет

Доля капитала дочерних компаний, не принадлежащая материнской организации.

## OPERATING_EXPENSES
name_ru: Операционные расходы
name_en: Operating expenses
aliases_en: SG&A; Selling; general and administrative expenses; General and administrative expense; Marketing, general and administrative; G&A; Operating expenses; Selling, general and administrative expenses; Selling, general and administrative
form: P&L
hint: P&L: expenses after gross profit and before operating profit. If there is no single operating-expenses line, sum operating expense rows in that bridge: SG&A/administrative/selling expenses, D&A shown there, LTIP, ECL and similar operating expense rows. Exclude cost of sales, other operating income and finance/tax lines.
example: `| Operating expenses | (58,300) | (51,100) |` → за отчётный период (58,300)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Расходы, связанные с операционной деятельностью, кроме себестоимости (коммерческие + общехозяйственные + админ).

## OPERATING_PROFIT
name_ru: Операционная прибыль
name_en: Operating profit
aliases_en: Operating income; EBIT; Income from operations
form: P&L
hint: P&L, строка после операционных расходов и до 'Финансовые доходы/расходы'.
example: `| Operating profit | 110,600 | 100,302 |` → за отчётный период 110,600
unit_kind: money
sign: любой
period_type: duration
standard: Любой
formula_default_abs_behavior: нет

Прибыль до финансовых доходов/расходов и налога — результат основной деятельности.

## PERPETUAL_BONDS
name_ru: Бессрочные облигации
name_en: Perpetual bonds
aliases_en: Perpetual notes; Hybrid capital notes
form: Баланс
hint: Раздел КАПИТАЛ баланса (если эмитент учитывает их как капитал) или Долгосрочные обязательства.
example: `| Perpetual bonds | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Облигации без даты погашения, классифицируемые как часть собственного капитала.

## PPE
name_ru: Основные средства
name_en: Property, plant and equipment
aliases_en: PP&E; Fixed assets; Property and equipment
form: Баланс
hint: Внеоборотные активы баланса. Также детализация в Примечании 'Основные средства'.
example: `| Property, plant and equipment | 12,345 | 11,200 |` → на отчётную дату 12,345
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
example: `| Repayment of the principle of lease liabilities | (12,400) | (11,100) |` → за отчётный период (12,400)
unit_kind: money
sign: отрицательный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Основная часть лизинговых платежей (без процентов) за период.

## RETAINED_EARNINGS
name_ru: Нераспределенная прибыль
name_en: Retained Earnings
aliases_en: Accumulated retained earnings; Accumulated deficit; Retained earnings balance
form: Баланс
hint: Раздел капитала: retained earnings / accumulated deficit на конец периода. Не путай с net income за период.
example: `| Retained Earnings | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: любой
period_type: point
standard: Любой
formula_default_abs_behavior: нет

Накопленная чистая прибыль за все периоды за вычетом распределённых дивидендов.

## RIGHT_OF_USE_ASSETS
name_ru: Активы в форме права пользования
name_en: ROU
aliases_en: Right-of-use assets; ROU assets; Right-of-use asset closing balance
form: Баланс
hint: Balance sheet or lease/right-of-use asset note. Extract every clearly disclosed date, not only balance-sheet dates; include comparative closing balances from the lease note when explicitly shown.
example: `| ROU | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Активы, признанные арендатором по МСФО 16 — это будущие платежи по аренде, дисконтированные на дату начала аренды.

## ROU_DEPRECIATION
name_ru: Амортизация активов в форме права пользования
name_en: Right of Use Assets Depreciation
aliases_en: Depreciation of right-of-use assets; ROU asset depreciation; Amortization of operating lease right-of-use assets
form: ОДДС
hint: Примечание 'Аренда' / lease note или ОДДС: amortization/depreciation of right-of-use assets. Не путай с общей depreciation and amortization.
example: `| Right of Use Assets Depreciation | 6,900 | 6,100 |` → за отчётный период 6,900
unit_kind: money
sign: положительный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Амортизация активов IFRS 16 за период.

## SALES_REVENUE
name_ru: Выручка
name_en: Sales revenue
aliases_en: Revenue; Net revenue; Net sales
form: P&L
hint: P&L: first revenue line from ordinary activities. For segment-level SALES_REVENUE, prefer revenue/sales to external customers; do not include intersegment revenue when it is disclosed separately.
example: `| Sales revenue | 604,401 | 552,271 |` → за отчётный период 604,401
unit_kind: money
sign: положительный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Доходы компании от обычной операционной деятельности (без НДС).

## SECURITIES_ASSETS
name_ru: Финансовые инструменты (Актив)
name_en: Securities
aliases_en: Financial instruments; Investment securities; Marketable securities; Short-term investments; Available-for-sale securities
form: Баланс
hint: Балансовый отчёт или примечание к инвестициям: ценные бумаги / marketable securities / short-term investments на дату баланса. Не путай с cash equivalents и bank deposits/time deposits.
example: `| Securities | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Долговые и долевые ценные бумаги, удерживаемые в качестве инвестиций или для торговли.

## SECURITIES_LIABILITIES
name_ru: Финансовые инструменты (Обязательства)
name_en: Securities
aliases_en: Financial instruments; Financial liabilities; Debt instruments issued
form: Баланс
hint: Балансовый отчёт. Детализация в Примечании 'Долговые ценные бумаги' / 'Облигационный заём'.
example: `| Securities | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Облигации и прочие долговые ценные бумаги, выпущенные компанией.

## SHARES_BUYBACK
name_ru: Выкуп собственных акций
name_en: Shares buyback
aliases_en: Share repurchase; Repurchase of common shares; Purchase of own shares
form: ОДДС
hint: ОДДС, раздел 'Финансовая деятельность'. Бери строку выкупа/покупки собственных акций и сохраняй знак/скобки как в источнике.
example: `| Shares buyback | 12,345 | 11,200 |` → за отчётный период 12,345
unit_kind: money
sign: отрицательный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Денежные расходы на выкуп собственных акций у акционеров.

## SHARE_CAPITAL
name_ru: Уставной капитал
name_en: Share capital
aliases_en: Common stock; Ordinary shares; Capital stock
form: Баланс
hint: Раздел капитала: share capital/common stock/capital stock на конец периода. Не путай с total equity.
example: `| Share capital | 12,345 | 11,200 |` → на отчётную дату 12,345
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
example: `| Share premium | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Превышение цены размещения акций над номинальной стоимостью.

## SHORT_TERM_BORROWINGS
name_ru: Краткосрочные кредиты и займы
name_en: Short-term borrowings and current portion of long-term debt
aliases_en: Current borrowings; Short-term loans and borrowings; Current portion of long-term debt
form: Баланс
hint: Краткосрочные обязательства баланса. Часто включает текущую часть долгосрочного долга.
example: `| Short-term borrowings and current portion of long-term debt | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Кредиты и займы со сроком погашения в течение 12 месяцев (включая текущую часть долгосрочных).

## SHORT_TERM_LEASE_LIABILITIES
name_ru: Краткосрочные обязательства по аренде
name_en: Short-term lease liabilities
aliases_en: Current lease liabilities; Current portion of lease liabilities
form: Баланс
hint: Краткосрочные обязательства баланса или примечание по аренде. Бери current portion/current lease liabilities, даже если в балансе она включена в accrued/other current liabilities.
example: `| Short-term lease liabilities | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Обязательства по аренде по IFRS 16 со сроком погашения в течение 12 месяцев.

## SHORT_TERM_ROU
name_ru: Краткосрочные активы в форме права пользования
name_en: Short Term Right of Use Assets
aliases_en: Current right-of-use assets; Short-term ROU assets
form: Примечания
hint: Примечание 'IFRS 16' / 'Аренда' — графа 'До 1 года'.
example: `| Short Term Right of Use Assets | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Активы ППА к признанию в течение 12 месяцев.

## SUBSIDIARY_ACQUISITIONS
name_ru: Приобретение дочерних организаций (обществ) за вычетом (минусом) денежных средств и за вычетом (минусом) полученных денежных средств
name_en: Subsidairy acquisitions
aliases_en: Acquisition of subsidiaries net of cash; Business combinations; Acquisition of businesses
form: ОДДС
hint: ОДДС, раздел 'Инвестиционная деятельность'. Включай строки денежных оттоков на приобретение бизнесов/дочерних организаций, включая приобретения у связанных сторон. Не бери покупки/выкуп неконтролирующих долей без приобретения бизнеса. Сохраняй знак/скобки как в источнике.
example: `| Subsidairy acquisitions | 12,345 | 11,200 |` → за отчётный период 12,345
unit_kind: money
sign: отрицательный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Денежные расходы на приобретение дочерних компаний (за вычетом приобретённых денежных средств).

## TOTAL_ASSETS
name_ru: Итого активы
name_en: Total assets
aliases_en: Total assets; Sum of assets
form: Баланс
hint: Балансовый отчёт (Statement of Financial Position), нижняя строка раздела АКТИВЫ. Должна равняться сумме внеоборотных и оборотных активов.
example: `| Total assets | 1,204,883 | 1,145,210 |` → на отчётную дату 1,204,883
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
example: `| Associate investments | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Совокупные инвестиции в ассоциированные компании и совместные предприятия.

## TOTAL_CURRENT_ASSETS
name_ru: Итого оборотных активов
name_en: Total current assets
aliases_en: Total current assets; Current assets
form: Баланс
hint: Балансовый отчёт, итоговая строка блока 'Оборотные активы' (запасы, дебиторка, денежные средства).
example: `| Total current assets | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Сумма ликвидных активов, ожидаемых к реализации в течение 12 месяцев.

## TOTAL_CURRENT_LIABILITIES
name_ru: Итого краткосрочных обязательств
name_en: Total current liabilities
aliases_en: Total current liabilities; Current liabilities
form: Баланс
hint: Балансовый отчёт, итоговая строка блока 'Краткосрочные обязательства'.
example: `| Total current liabilities | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Сумма обязательств с погашением в течение 12 месяцев.

## TOTAL_EQUITY
name_ru: Итого собственный капитал
name_en: Total equity
aliases_en: Total shareholders' equity; Stockholders' equity
form: Баланс
hint: Балансовый отчёт, итог раздела КАПИТАЛ. Иногда отдельно 'Капитал, приходящийся на акционеров материнской' + 'Неконтролирующие доли'.
example: `| Total equity | 512,004 | 498,771 |` → на отчётную дату 512,004
unit_kind: money
sign: любой
period_type: point
standard: Любой
formula_default_abs_behavior: нет

Совокупная стоимость собственного капитала, включая капитал акционеров и неконтролирующие доли.

## TREASURY_SHARES
name_ru: Собственные акции, выкупленные у акционеров
name_en: Treasury shares
aliases_en: Treasury stock; Own shares repurchased
form: Баланс
hint: Раздел капитала: treasury shares/treasury stock на конец периода. Обычно уменьшает капитал; не путай с buyback cash-flow за период.
example: `| Treasury shares | 12,345 | 11,200 |` → на отчётную дату 12,345
unit_kind: money
sign: отрицательный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Стоимость собственных акций, выкупленных у акционеров.
