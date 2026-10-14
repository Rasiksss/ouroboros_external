# Ячейка ifrs_bank_ru — банки, МСФО, отчётность на русском

* стандарт отчётности: **IFRS**
* единица измерения по умолчанию: **млн. руб.**
* подпись периода, когда своей в документе нет: **Текущий**

Всё ниже до первой записи `## <ENUM>` — ПРАВИЛА ЯЧЕЙКИ: как читать
отчётность этого стандарта. Они уезжают в промпт целиком. Правятся
текстом, без программиста. Правила конкретного показателя — в его
записи, поле `hint`.
### Дополнительные guardrails

1. **Проверка P&L арифметикой.** В OCR-таблицах банков иногда съезжают строки. Перед тем как вернуть P&L-метрику, мысленно проверь соседние subtotal:
   - ECL/provision flow должен объяснять разницу между ЧПД до резервов и ЧПД после резервов.
   - `OPERATING_EXPENSES_BANKS`, `FEE_AND_COMMISSION_EXPENSE` и `CHANGE_IN_EXPECTED_CREDIT_LOSSES` как расходы обычно отрицательные, если P&L показывает их в скобках или строка называется «создание/расходы/формирование резерва». Возвращай знак источника даже если OCR потерял скобку в самой ячейке.

2. **Активы с общей строкой.** Если баланс показывает объединённую строку вроде «Основные средства, нематериальные активы и активы в форме права пользования», не используй её для `PPE`, `INTANGIBLE_ASSETS` или `RIGHT_OF_USE_ASSETS`. Иди в примечание и бери отдельную строку/колонку для каждой метрики. Для `PPE` нужна балансовая/остаточная стоимость на конец периода, а не первоначальная стоимость; исключай НМА, goodwill и right-of-use assets.

3. **Относительные заголовки периодов.** Если таблица вместо года пишет «за отчетный период» / «за предшествующий отчетный период» / `current period` / `previous period`, верни периоды из задания: текущий год отчёта и предыдущий год. Не используй такой текст как period label в JSON.

4. **Промежуточная отчётность: не теряй колонки.** В квартальных/полугодовых/9М отчётах P&L часто раскрывает четыре колонки: 3M/Q за текущий и сравнительный год плюс YTD (6M/9M) за текущий и сравнительный год. Для каждой duration-метрики из P&L верни ВСЕ явно раскрытые колонки этой строки: и квартал/3M, и YTD. Не останавливайся на первой паре колонок. Для point-метрик из баланса и примечаний возвращай все даты таблицы, обычно текущую отчётную дату и предыдущий 31 декабря; не превращай такие строки в Q/6M/9M.

5. **Stage/ECL таблицы.** Если в примечании к кредитам есть таблица со стадиями 1/2/3 и колонками gross / ECL allowance / net, извлеки из этой одной таблицы все подходящие метрики: `LOANS_STAGE1_GROSS`, `LOANS_STAGE2_GROSS`, `LOANS_STAGE3_GROSS` из gross-колонок и `ECL_ALLOWANCE_STAGE1`, `ECL_ALLOWANCE_STAGE2`, `ECL_ALLOWANCE_STAGE3` из allowance/ECL-колонок. Не возвращай net carrying amount для gross или allowance. Если стадии раскрыты для нескольких дат, верни все эти даты.

## CASH_AND_CASH_BALANCES_WITH_CB
name_ru: Обязательные резервы на счетах в центральных банках
name_en: Cash and cash balances with central bank
aliases_ru: Обязательные резервы на счетах в ЦБ РФ; Обязательные резервы на счетах в Центральном банке
aliases_en: Mandatory reserves with central bank; Reserve deposits with central bank
form: Баланс
hint: Балансовый отчёт банка, актив. Часто первая строка раздела активов.
example: `| Обязательные резервы на счетах в Банке России | 188 500 | 165 200 |` → 188 500
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Обязательные резервы и средства банка на счетах в центральных банках.

Это АКТИВ банка. Привлечённое от ЦБ — обязательство FUNDS_FROM_CENTRAL_BANK, и обе строки называют Банк России.

## CASH_EQUIVALENTS
name_ru: Денежные средства и их эквиваленты
name_en: Cash and cash equivalents
aliases_ru: Денежные средства и эквиваленты денежных средств; Денежные средства и денежные эквиваленты; Денежные средства и их эквиваленты на конец периода
aliases_en: Cash and equivalents; Cash and cash equivalents at end of period
form: Баланс
hint: Оборотные активы баланса или последняя строка ОДДС. Возвращай все явно раскрытые даты cash and cash equivalents at end of period, включая сравнительные даты, которых нет в балансе. Не путай с bank deposits/time deposits и marketable securities.
example: `| Денежные средства и их эквиваленты | 1 204 883 | 1 145 210 |` → на отчётную дату 1 204 883
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Касса, расчётные счета и краткосрочные высоколиквидные инвестиции (срок ≤ 3 мес.).

У банка это не та же строка, что обязательные резервы в ЦБ (CASH_AND_CASH_BALANCES_WITH_CB): резервы обездвижены и раскрываются отдельной строкой актива.

## CET1_RATIO_DISCLOSED
name_ru: Норматив CET1 (раскрываемый)
name_en: CET1 ratio (disclosed)
aliases_ru: CET1 ratio; норматив Н 1.1; Н1.1; норматив Н 20.1
aliases_en: CET1 capital ratio; Common equity Tier 1 ratio
form: Примечания
hint: Примечание 'Достаточность капитала'. Для российских банков CET1 = норматив базового капитала H1.1 / Н1.1 (или консолидированный Н20.1). Не используй H1.0/Н1.0 — это общий норматив капитала. Единица — проценты.
example: `| Норматив достаточности базового капитала Н1.1 | 11,3% | 10,8% |` → 11,3
unit_kind: percent
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Раскрываемый банком норматив достаточности базового капитала первого уровня.

Три российских норматива стоят в одной таблице и различаются одной цифрой: Н1.0 — общий, Н1.1 — базовый капитал (это CET1), Н1.2 — основной капитал (это Tier 1).

## CHANGES_OCF_ASSETS_LIABILITIES
name_ru: Изменения в оборотном капитале
name_en: Changes in working capital
aliases_ru: Изменения в операционных активах и обязательствах
aliases_en: Changes in operating assets and liabilities; Working capital movements
form: ОДДС
hint: ОДДС, средняя часть раздела 'Операционная деятельность' (между корректировками прибыли и налоговыми платежами).
example: `| Изменение операционных активов и обязательств | (145 300) | 88 200 |` → −145 300
unit_kind: money
sign: любой
period_type: duration
standard: Любой
formula_default_abs_behavior: нет

Совокупное изменение оборотного капитала (запасы, дебиторка, кредиторка) за период.

## CHANGE_IN_EXPECTED_CREDIT_LOSSES
name_ru: Изменение резерва по ожидаемым кредитным убыткам
name_en: Change in ECL
aliases_ru: Резерв под кредитные убытки по денежным средствам; их эквивалентам; средствам в других банках и кредитам и авансам клиентам; Создание резервов под ожидаемые кредитные убытки; Расходы по кредитным убыткам; Формирование резервов под ожидаемые кредитные убытки
aliases_en: Movement in ECL allowance; Changes in ECL allowance
form: P&L
hint: P&L банка или таблица движения резервов: бери только чистый поток создания/восстановления/изменения ECL за период. Приоритетная строка: 'создание/формирование резервов', 'расходы по кредитным убыткам', 'изменение резерва по ожидаемым кредитным убыткам'. Если строка означает создание/расходы/формирование резервов, возвращай отрицательное значение; если восстановление/reversal — положительное. Не бери closing balance/остаток резерва на дату, NII before/after provisions, OCI/FVOCI reserve movement или сумму allowance в балансе. Если нет отдельной строки flow за период - null.
example: `| Создание резервов под ожидаемые кредитные убытки | (128 400) | (96 200) |` → −128 400
unit_kind: money
sign: любой
period_type: duration
standard: Любой
formula_default_abs_behavior: нет

Изменение оценочных резервов ECL за период (по всем финансовым активам, не только кредитам).

Это ПОТОК за период. Остаток резерва на дату — TOTAL_CREDIT_ALLOWANCE.

## CLIENT_DEPOSITS
name_ru: Депозиты клиентов
name_en: Total deposits
aliases_ru: Средства клиентов; Депозиты
aliases_en: Due to customers; Customer deposits
form: Баланс
hint: Балансовый отчёт банка, ключевое обязательство. Детализация в Примечании 'Средства клиентов'.
example: `| Средства клиентов | 11 204 500 | 9 880 100 |` → 11 204 500
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Совокупный объём средств клиентов на счетах и депозитах банка.

## COMMON_EQUITY_TIER_ONE_CAPITAL
name_ru: Основной капитал первого уровня
name_en: Common equity tier 1 capital
aliases_ru: Капитал первого уровня; CET1 капитал; Базовый капитал первого уровня
aliases_en: CET1 capital; Common equity Tier 1 capital ratio
form: Примечания
hint: Примечание 'Достаточность капитала' / 'Управление капиталом'.
example: `| Базовый капитал первого уровня | 1 402 300 | 1 288 900 |` → 1 402 300
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Базовый капитал первого уровня по Базель III.

Это СУММА капитала, а не норматив в процентах: норматив — CET1_RATIO_DISCLOSED.

## CORPORATE_DEPOSITS
name_ru: Средства корпоративных клиентов
name_en: Corporate deposits
aliases_ru: Депозиты корпоративных клиентов; Средства юридических лиц
aliases_en: Corporate deposits; Business deposits; Deposits from legal entities
form: Примечания
hint: Примечание 'Средства клиентов' — бери только средства юридических лиц / корпоративных клиентов / corporate customers. Не используй total customer accounts, средства физических лиц или государственное финансирование. Если отдельной корпоративной строки нет - null.
example: `| Средства юридических лиц | 4 102 300 | 3 640 700 |` → 4 102 300
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Депозиты юридических лиц.

## CORPORATE_LOANS
name_ru: Корпоративные кредиты
name_en: Total commercial loans and leases
aliases_ru: Кредиты корпоративным клиентам; Кредиты юридическим лицам
aliases_en: Corporate loans; Commercial loans
form: Примечания
hint: Примечание 'Кредиты клиентам' — разбивка по сегментам клиентов.
example: `| Кредиты юридическим лицам | 5 902 100 | 5 118 400 |` → 5 902 100
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Кредиты, выданные юридическим лицам.

## CURRENCY_METALS_INCOME
name_ru: Чистый доход от операций с иностранными валютами и драгоценными металлами
name_en: Net income from operations with foreign currency and precious metals
aliases_ru: Чистый доход от операций с иностранной валютой
aliases_en: Net foreign exchange income; Net FX income
form: P&L
hint: P&L банка, в составе непроцентных доходов.
example: `| Чистый доход от операций с иностранной валютой и драгоценными металлами | 28 700 | (12 400) |` → 28 700
unit_kind: money
sign: любой
period_type: duration
standard: Любой
formula_default_abs_behavior: нет

Чистый результат по операциям с иностранной валютой и драгоценными металлами.

Величина чистая: в неудачный по курсу год она отрицательна.

## DEBT_SECURITIES_ISSUED
name_ru: Выпущенные долговые ценные бумаги
name_en: Debt securities issued
aliases_ru: Выпущенные облигации; Долговые ценные бумаги в обращении; Выпущенные долговые ценные бумаги; Выпущенные векселя; Депозитные сертификаты
aliases_en: Bonds issued; Notes and bonds issued
form: Баланс
hint: Выпущенные долговые ценные бумаги — обязательство. СКЛАДЫВАЙ выпущенные облигации + векселя + депозитные сертификаты + ноты. 🔴 НЕ включай субординированные обязательства: у них СВОЙ показатель, и выделенное в отдельную сущность сохраняет самостоятельность. Слагаемые назови в source_text.
example: `| Выпущенные долговые ценные бумаги | 402 800 | 355 100 |` → 402 800
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Облигации и прочие долговые ценные бумаги, эмитированные банком.

## DEFERRED_TAX_ASSETS
name_ru: Отложенные налоговые активы
name_en: Deferred tax assets
aliases_ru: Активы по отложенному налогу на прибыль
aliases_en: Deferred income tax assets
form: Баланс
hint: Внеоборотные активы. Подробности в Примечании 'Налог на прибыль' / 'Отложенные налоги'.
example: `| Отложенные налоговые активы | 12 480 | 9 902 |` → 12 480
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Сумма налогов на прибыль к уменьшению в будущих периодах за счёт вычитаемых временных разниц.

## DEFERRED_TAX_LIABILITIES
name_ru: Отложенные налоговые обязательства
name_en: Deferred tax liabilities
aliases_ru: Обязательства по отложенному налогу на прибыль
aliases_en: Deferred income tax liabilities
form: Баланс
hint: Долгосрочные обязательства баланса. Подробности в Примечании 'Налог на прибыль' / 'Отложенные налоги'.
example: `| Отложенные налоговые обязательства | 8 140 | 7 655 |` → 8 140
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Налоги на прибыль, подлежащие уплате в будущих периодах из-за налогооблагаемых временных разниц.

## DERIVATIVE_LIABILITIES
name_ru: Производные финансовые обязательства
name_en: Derivatives
aliases_ru: Производные финансовые инструменты (обязательства); Деривативы (обязательства)
aliases_en: Derivative financial instruments; Derivative liabilities
form: Баланс
hint: Балансовый отчёт. Также в Примечании 'Финансовые инструменты' / 'Управление рисками'.
example: `| Производные финансовые инструменты | 44 200 | 51 900 |` → 44 200
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Обязательства по производным финансовым инструментам, оцениваемые по справедливой стоимости.

## DIVIDENDS
name_ru: Дивиденды
name_en: Dividends
aliases_ru: Дивиденды уплаченные; Дивиденды выплаченные
aliases_en: Dividends paid
form: ОДДС
hint: ОДДС, раздел «Финансовая деятельность»: «Дивиденды выплаченные»/«Дивиденды уплаченные» за период. Обычно денежный отток — сохраняй минус/скобки из источника. Не бери объявленные дивиденды из примечания о событиях после отчётной даты и не бери дивиденды полученные — это DIVIDENDS_RECEIVED.
example: `| Дивиденды выплаченные | (210 400) | (165 000) |` → −210 400
unit_kind: money
sign: отрицательный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Денежные выплаты дивидендов акционерам за период — отток по финансовой деятельности.

Объявленные, но не выплаченные дивиденды сюда не относятся: здесь движение денег, а не решение собрания.

## DIVIDENDS_RECEIVED
name_ru: Дивиденды полученные
name_en: Dividends received
aliases_ru: Полученные дивиденды
aliases_en: Cash dividends received
form: ОДДС
hint: ОДДС, раздел 'Инвестиционная деятельность'.
example: `| Дивиденды полученные | 4 800 | 3 900 |` → 4 800
unit_kind: money
sign: положительный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Денежные поступления дивидендов от ассоциированных и прочих компаний.

## EBT
name_ru: Прибыль до налогообложения
name_en: EBT
aliases_ru: Прибыль до налога; Прибыль до налога на прибыль
aliases_en: Profit before tax; Income before income taxes; Pre-tax income
form: P&L
hint: P&L банка, предпоследняя строка перед налогом на прибыль. Если у банка убыток, в источнике обычно стоят скобки или минус; сохраняй знак/скобки как в источнике.
example: `| Прибыль до налогообложения | 633 200 | 539 900 |` → 633 200
unit_kind: money
sign: любой
period_type: duration
standard: Любой
formula_default_abs_behavior: нет

Прибыль до вычета налога на прибыль — финальная строка P&L перед налогом.

## ECL_ALLOWANCE_STAGE1
name_ru: Резерв под обесценение кредитов Этап 1
name_en: ECL allowance Stage 1
aliases_ru: Оценочный резерв под ожидаемые кредитные убытки Этап 1; ОКУ Этап 1; Оценочный резерв под кредитные убытки по кредитам; предоставленным клиентам Этап 1
aliases_en: Stage 1 ECL allowance
form: Примечания
hint: Примечание 'Кредиты клиентам' / 'Резервы под обесценение', таблица ОКУ по этапам. СКЛАДЫВАЙ колонку Этап 1 ПО ВСЕМ кредитным портфелям (юридические лица, ипотека, прочие розничные): сводной таблицы по банку в отчёте обычно нет, и её отсутствие — не повод вернуть пусто. Бери allowance/ОКУ на конец периода, не gross carrying amount. Сохраняй знак и скобки из источника. Слагаемые назови в source_text.
example: `| Оценочный резерв под ОКУ | Этап 1 (98 200) | Этап 2 (112 400) | Этап 3 (204 600) |` → −98 200
unit_kind: money
sign: отрицательный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Резерв под ожидаемые кредитные убытки на стадии 1 (12 месяцев).

Стадия не раскрыта отдельно — это null, а не ноль: ноль означал бы, что банк резерв не создавал.

## ECL_ALLOWANCE_STAGE2
name_ru: Резерв под обесценение кредитов Этап 2
name_en: ECL allowance Stage 2
aliases_ru: Оценочный резерв под ожидаемые кредитные убытки Этап 2; ОКУ Этап 2; Оценочный резерв под кредитные убытки по кредитам; предоставленным клиентам Этап 2
aliases_en: Stage 2 ECL allowance
form: Примечания
hint: Примечание 'Кредиты клиентам' / 'Резервы под обесценение', таблица ОКУ по этапам. СКЛАДЫВАЙ колонку Этап 2 ПО ВСЕМ кредитным портфелям (юридические лица, ипотека, прочие розничные): сводной таблицы по банку в отчёте обычно нет, и её отсутствие — не повод вернуть пусто. Бери allowance/ОКУ на конец периода, не gross carrying amount. Сохраняй знак и скобки из источника. Слагаемые назови в source_text.
example: `| Оценочный резерв под ОКУ | Этап 1 (98 200) | Этап 2 (112 400) | Этап 3 (204 600) |` → −112 400
unit_kind: money
sign: отрицательный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Резерв под ожидаемые кредитные убытки за весь срок жизни (стадия 2).

Стадия не раскрыта отдельно — это null, а не ноль.

## ECL_ALLOWANCE_STAGE3
name_ru: Резерв под обесценение кредитов Этап 3
name_en: ECL allowance Stage 3
aliases_ru: Оценочный резерв под ожидаемые кредитные убытки Этап 3; ОКУ Этап 3; Оценочный резерв под кредитные убытки по кредитам; предоставленным клиентам Этап 3
aliases_en: Stage 3 ECL allowance
form: Примечания
hint: Примечание 'Кредиты клиентам' / 'Резервы под обесценение', таблица ОКУ по этапам. СКЛАДЫВАЙ колонку Этап 3 ПО ВСЕМ кредитным портфелям (юридические лица, ипотека, прочие розничные): сводной таблицы по банку в отчёте обычно нет, и её отсутствие — не повод вернуть пусто. Бери allowance/ОКУ на конец периода, не gross carrying amount. Сохраняй знак и скобки из источника. Слагаемые назови в source_text.
example: `| Оценочный резерв под ОКУ | Этап 1 (98 200) | Этап 2 (112 400) | Этап 3 (204 600) |` → −204 600
unit_kind: money
sign: отрицательный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Резерв под ожидаемые кредитные убытки за весь срок жизни (стадия 3, обесцененные кредиты).

Стадия не раскрыта отдельно — это null, а не ноль.

## EPS
name_ru: Прибыль на одну обыкновенную акцию
name_en: Earnings per ordinary share
aliases_ru: Прибыль на акцию; Базовая прибыль на акцию; Базовая и разводненная прибыль на акцию; Прибыль на одну обыкновенную акцию
aliases_en: Earnings per share; EPS; Basic EPS; Basic earnings per share
form: P&L
hint: P&L: строка Basic earnings per share / базовая прибыль на акцию после net income. Бери Basic, не Diluted; unit_text всегда per-share: 'руб/акция' или 'коп./акция' по подписи строки, а не масштаб отчёта ('млн руб.', 'млрд руб.').
example: `| Базовая прибыль на акцию, руб. | 23,45 | 19,87 |` → 23,45 рубля на акцию
unit_kind: money
sign: любой
period_type: duration
standard: Любой
formula_default_abs_behavior: нет

Чистая прибыль на одну обыкновенную акцию (валюта на акцию, не в миллионах).

Единица здесь — валюта НА АКЦИЮ, а не масштаб отчёта: подставить к этой строке «млн руб.» значит занизить показатель в миллион раз.

## EQUITY_SHAREHOLDERS
name_ru: Капитал, приходящийся на акционеров материнской компании
name_en: Equity attributable to owners of the parent
aliases_ru: Капитал, приходящийся на собственников материнской компании; Итого капитал акционеров материнской компании; Капитал акционеров материнской компании
form: Баланс
hint: Баланс, раздел КАПИТАЛ: «Итого капитал, приходящийся на акционеров материнской компании» — строка ДО «Неконтролирующие доли». НЕ путать с итогом капитала (тот включает неконтролирующие доли). Нет отдельной строки — значит неконтролирующих долей нет, бери итог капитала.
example: `| Итого капитал, приходящийся на акционеров материнской компании | 1 499 604 | 1 390 200 |` → 1 499 604
unit_kind: money
sign: любой
period_type: point
standard: Любой
formula_default_abs_behavior: нет

Собственный капитал за вычетом неконтролирующих долей — часть капитала, принадлежащая акционерам материнской компании.

## FEE_AND_COMMISSION_EXPENSE
name_ru: Комиссионный расход
name_en: Fee and comission expense
aliases_ru: Комиссионные расходы; Расходы по комиссиям
aliases_en: Fee expense; Commission expense
form: P&L
hint: P&L банка, после комиссионных доходов. Бери строку комиссионных расходов и сохраняй знак/скобки как в источнике; если P&L показывает расход в скобках, возвращай отрицательное значение даже если примечание показывает компоненты положительными.
example: `| Комиссионные расходы | (56 400) | (47 100) |` → −56 400
unit_kind: money
sign: отрицательный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Расходы по комиссионным операциям (в т.ч. платежи системам, контрагентам).

## FEE_AND_COMMISSION_INCOME
name_ru: Комиссионный доход
name_en: Fee and comission income
aliases_ru: Комиссионные доходы; Доход от комиссий
aliases_en: Fee income; Commission income
form: P&L
hint: P&L банка. Детализация в Примечании 'Комиссионные доходы и расходы'.
example: `| Комиссионные доходы | 244 800 | 212 300 |` → 244 800
unit_kind: money
sign: положительный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Доходы от комиссионных и расчётно-кассовых операций.

## FUNDS_FROM_CENTRAL_BANK
name_ru: Средства, предоставленные ЦБ
name_en: Funds provided by the Central Bank
aliases_ru: Кредиты ЦБ; Рефинансирование от центрального банка; Средства Банка России; Средства национальных центральных банков; Срочные кредиты и депозиты Банка России
aliases_en: Loans from central bank; Borrowings from the central bank
form: Баланс
hint: Балансовый отчёт банка, обязательство перед Банком России / центральными банками: кредиты, депозиты, репо или прочее фондирование от ЦБ. Не путай с обязательными резервами на счетах в ЦБ — это актив CASH_AND_CASH_BALANCES_WITH_CB.
example: `| Средства Банка России | 320 000 | 145 000 |` → 320 000
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Кредиты и депозиты, полученные банком от центрального банка.

## GOODWILL
name_ru: Гудвил
name_en: Goodwill
aliases_ru: Деловая репутация
aliases_en: Goodwill
form: Баланс
hint: Внеоборотные активы баланса. Подробнее — в Примечании 'Гудвил' / 'Объединение бизнеса'.
example: `| Гудвил | 21 300 | 21 300 |` → 21 300
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Превышение цены приобретения дочерней компании над справедливой стоимостью её чистых идентифицируемых активов.

## GOVERNMENTAL_DEPOSITS
name_ru: Средства государственных органов власти
name_en: Governmental deposits
aliases_ru: Депозиты государственных органов; Средства государственных и муниципальных органов
aliases_en: Government deposits; Deposits from government entities
form: Примечания
hint: Примечание 'Средства клиентов' — разбивка по сегментам.
example: `| Средства государственных органов | 222 000 | 224 000 |` → 222 000
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Депозиты госорганов и муниципальных образований.

## INCOME_TAX
name_ru: Расход по текущему налогу на прибыль
name_en: Income tax expense
aliases_ru: Текущий налог на прибыль
aliases_en: Tax expense; Provision for income taxes; Income tax provision (benefit)
form: P&L
hint: P&L: итоговая строка income tax provision/benefit перед net income. Не бери cash paid for taxes и не заменяй итоговую строку отдельными current/deferred tax components.
example: `| Расход по налогу на прибыль | (124 300) | (108 900) |` → −124 300
unit_kind: money
sign: отрицательный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Сумма налога на прибыль за период (текущий + отложенный).

## INDIVIDUALS_DEPOSITS
name_ru: Средства Физических лиц
name_en: Consumer deposits
aliases_ru: Депозиты физических лиц; Вклады физических лиц
aliases_en: Retail deposits; Deposits from individuals
form: Примечания
hint: Примечание 'Средства клиентов' — бери только средства физических лиц / розничных клиентов. Не используй total customer accounts, корпоративные средства или госфинансирование. Если отдельной строки физических лиц нет - null.
example: `| Средства физических лиц | 6 880 200 | 6 015 400 |` → 6 880 200
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Депозиты и вклады физических лиц.

## INDIVIDUALS_LOANS
name_ru: Розничные кредиты
name_en: Total consumer loans and leases
aliases_ru: Кредиты физическим лицам; Потребительские кредиты
aliases_en: Retail loans; Consumer loans; Loans to individuals
form: Примечания
hint: Примечание 'Кредиты клиентам' — разбивка по сегментам.
example: `| Кредиты физическим лицам | 3 938 100 | 3 297 300 |` → 3 938 100
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Кредиты, выданные физическим лицам (ипотека, потребкредиты, автокредиты).

## INSURANCE_EXPENSE
name_ru: Расходы по страховой деятельности
name_en: Insurance service expense
aliases_ru: Страховые выплаты; Расходы по страховым случаям
aliases_en: Insurance claims and expenses; Claims and benefits paid
form: P&L
hint: P&L страховой компании. Не применимо к не-страховым — null.
example: `| Расходы по страховой деятельности | (58 100) | (52 300) |` → −58 100
unit_kind: money
sign: отрицательный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Расходы страховщика по договорам страхования (по IFRS 17).

## INSURANCE_INCOME
name_ru: Выручка по страховой деятельности
name_en: Insurance service income
aliases_ru: Страховая премия; Заработанные страховые премии
aliases_en: Insurance premium income; Net earned premiums
form: P&L
hint: P&L страховой компании. Не применимо к не-страховым организациям — null.
example: `| Выручка по страховой деятельности | 74 200 | 66 800 |` → 74 200
unit_kind: money
sign: положительный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Доходы страховщика по договорам страхования (по IFRS 17).

## INSURANCE_LIABILITIES
name_ru: Обязательства по договорам страхования
name_en: Insurance contracts liabilities
aliases_ru: Страховые резервы; Страховые обязательства
aliases_en: Insurance liabilities; Technical reserves
form: Баланс
hint: Балансовый отчёт страховой компании. Только если компания ведёт страховую деятельность.
example: `| Обязательства по договорам страхования | 96 300 | 88 700 |` → 96 300
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Обязательства страховщика по договорам страхования (резервы убытков, премии).

## INTANGIBLE_ASSETS
name_ru: Нематериальные активы
name_en: Intangible assets
aliases_ru: Нематериальные активы; НМА; Прочие нематериальные активы; Нематериальные активы за вычетом гудвила
aliases_en: Intangible assets; Other intangible assets; Intangibles
form: Баланс
hint: Внеоборотные активы баланса (отдельная строка после гудвила) или отдельная строка в Примечании 'Нематериальные активы'. Не используй объединённую строку с PPE/ROU; если гудвил раскрыт отдельно, не включай его в INTANGIBLE_ASSETS.
example: `| Нематериальные активы | 54 200 | 48 900 |` → 54 200; гудвил в эту сумму не входит
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Идентифицируемые неденежные активы без физической субстанции — лицензии, патенты, программное обеспечение, бренды.

## INTERBANK_DEPOSITS
name_ru: Средства в банках
name_en: Interbank deposits
aliases_ru: Депозиты в банках; Межбанковские депозиты
aliases_en: Due from banks; Interbank placements; Deposits with banks
form: Баланс
hint: Балансовый отчёт банка, актив.
example: `| Средства в других банках | 402 100 | 355 600 |` → 402 100
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Денежные средства, размещённые в других банках.

Это АКТИВ — то, что банк разместил у других. Привлечённое от банков лежит в INTERBANK_LOANS, и русские названия этих двух строк различаются одной буквой.

## INTERBANK_LOANS
name_ru: Средства банков
name_en: Interbank loans
aliases_ru: Депозиты банков; Межбанковские привлечения
aliases_en: Due to banks; Interbank borrowings; Deposits from banks
form: Баланс
hint: Балансовый отчёт банка. Может быть отдельной строкой от кредитов клиентам.
example: `| Средства других банков | 288 700 | 310 400 |` → 288 700
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Кредиты, выданные другим банкам.

Это ОБЯЗАТЕЛЬСТВО — привлечённое от других банков. Размещённое в других банках — INTERBANK_DEPOSITS.

## INTEREST_EXPENSE_ER
name_ru: Процентные расходы, рассчитываемые по методу эффективной процентной ставки
name_en: Interest expense, calculated on the basis of effective interest rate
aliases_ru: Процентные расходы (по методу ЭПС)
aliases_en: Interest expense calculated using the effective interest method; Interest expense (EIR basis)
form: P&L
hint: P&L банка или примечание 'Процентные доходы и расходы'. Бери только процентные расходы, явно рассчитанные по методу эффективной процентной ставки / ЭПС / EIR. Обычную строку 'процентные расходы' используй только если раздел/примечание подтверждает ЭПС; иначе возвращай null.
example: `| Процентные расходы, рассчитанные по методу ЭПС | (689 900) | (515 600) |` → −689 900
unit_kind: money
sign: отрицательный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Процентные расходы, рассчитанные по методу эффективной процентной ставки.

## INTEREST_INCOME_ER
name_ru: Процентные доходы, рассчитываемые по методу эффективной процентной ставки
name_en: Interest income, calculated on the basis of effective interest rate
aliases_ru: Процентные доходы (по методу ЭПС)
aliases_en: Interest income calculated using the effective interest method; Interest income (EIR basis)
form: P&L
hint: P&L банка, верхняя строка раздела процентных доходов. Детализация в Примечании 'Процентные доходы и расходы'.
example: `| Процентные доходы, рассчитанные по методу ЭПС | 1 402 300 | 1 155 700 |` → 1 402 300
unit_kind: money
sign: положительный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Процентные доходы, рассчитанные по методу эффективной процентной ставки.

## INTEREST_PAID
name_ru: Уплаченные проценты
name_en: Interest paid
aliases_ru: Проценты уплаченные; Проценты по кредитам уплаченные
aliases_en: Interest payments; Cash interest paid
form: ОДДС
hint: ОДДС, строка уплаченных процентов за период: 'Проценты уплаченные', 'Уплаченные проценты'. Обычно денежный отток, сохраняй минус/скобки из источника. Если заголовок колонки только 'за отчетный период' / 'за предшествующий отчетный период', используй текущий/предыдущий год отчёта, а не этот текст как период.
example: `| Проценты уплаченные | (312 700) | (241 550) |` → −312 700
unit_kind: money
sign: отрицательный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Денежные выплаты процентов по кредитам и займам за период.

## INTEREST_RECEIVED
name_ru: Полученные проценты
name_en: Interest received
aliases_ru: Проценты полученные
aliases_en: Interest receipts; Cash interest received
form: ОДДС
hint: ОДДС, чаще в разделе 'Инвестиционная деятельность' (или 'Операционная').
example: `| Проценты полученные | 884 300 | 712 900 |` → 884 300
unit_kind: money
sign: положительный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Денежные поступления процентов от размещений и финансовых активов.

## JUNIOR_DEBT
name_ru: Субординированная задолженность
name_en: Junior debt
aliases_ru: Субординированные займы; Субординированный долг
aliases_en: Subordinated debt; Subordinated loans
form: Баланс
hint: Балансовый отчёт банка. Часто учитывается в составе Tier 2 капитала.
example: `| Субординированные займы | 210 000 | 210 000 |` → 210 000
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Долг, имеющий более низкий приоритет погашения по сравнению с обычным долгом.

## LEASE_LIABILITIES_BANKS
name_ru: Обязательства по аренде
name_en: Lease liabilities
aliases_ru: Арендные обязательства
aliases_en: Lease liabilities
form: Баланс
hint: Балансовый отчёт или примечание IFRS 16: остаток обязательств по аренде на отчётную дату. Не используй денежный поток погашения аренды за период - это PRINCIPAL_REPAYMENT_OF_LEASE_LIABILITIES. Если отдельного lease liabilities balance нет - null.
example: `| Обязательства по аренде | 35 100 | 30 800 |` → 35 100
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Обязательства по аренде по IFRS 16 у банка (общая сумма, без разбивки на долго/краткосрочные).

Остаток на дату. Погашение основной суммы за период — PRINCIPAL_REPAYMENT_OF_LEASE_LIABILITIES.

## LOANS_STAGE1_GROSS
name_ru: Кредиты Этап 1 (валовые)
name_en: Loans Stage 1 (gross)
aliases_ru: Валовая балансовая стоимость Этап 1; Кредиты и авансы клиентам Этап 1 Валовая стоимость; Кредиты клиентам Этап 1 Валовая стоимость
aliases_en: Stage 1 loans; Performing loans (Stage 1)
form: Примечания
hint: Примечание 'Кредиты клиентам' — таблица распределения кредитов по стадиям ECL. Бери gross carrying amount / валовую балансовую стоимость по Stage 1, обычно subtotal по всем кредитам клиентов; не бери net carrying amount после резерва, allowance/ECL reserve или total loans без стадий. Если Stage 1 не раскрыт отдельно - null.
example: `| Кредиты клиентам, валовая стоимость | Этап 1 8 902 400 | Этап 2 640 100 | Этап 3 297 700 |` → 8 902 400
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Валовая балансовая стоимость кредитов на стадии 1 (12-month ECL) по IFRS 9.

## LOANS_STAGE2_GROSS
name_ru: Кредиты Этап 2 (валовые)
name_en: Loans Stage 2 (gross)
aliases_ru: Валовая балансовая стоимость Этап 2; Кредиты и авансы клиентам Этап 2 Валовая стоимость; Кредиты клиентам Этап 2 Валовая стоимость
aliases_en: Stage 2 loans; Under-performing loans (Stage 2)
form: Примечания
hint: Примечание 'Кредиты клиентам' — таблица распределения по стадиям ECL. Бери gross carrying amount / валовую балансовую стоимость по Stage 2 (lifetime ECL not credit-impaired); не бери allowance, net carrying amount, Stage 1/3 или total loans. Если Stage 2 не раскрыт отдельно - null.
example: `| Кредиты клиентам, валовая стоимость | Этап 1 8 902 400 | Этап 2 640 100 | Этап 3 297 700 |` → 640 100
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Валовая балансовая стоимость кредитов на стадии 2 (lifetime ECL без обесценения) по IFRS 9.

## LOANS_STAGE3_GROSS
name_ru: Кредиты Этап 3 (валовые)
name_en: Loans Stage 3 (gross)
aliases_ru: Валовая балансовая стоимость Этап 3; Кредиты и авансы клиентам Этап 3 Валовая стоимость; Кредиты клиентам Этап 3 Валовая стоимость
aliases_en: Stage 3 loans; Non-performing loans (Stage 3)
form: Примечания
hint: Примечание 'Кредиты клиентам' — таблица распределения по стадиям ECL. Бери gross carrying amount / валовую балансовую стоимость по Stage 3 / credit-impaired loans; не бери allowance, net carrying amount, NPL ratio или total overdue loans без stage table. Если Stage 3 не раскрыт отдельно - null.
example: `| Кредиты клиентам, валовая стоимость | Этап 1 8 902 400 | Этап 2 640 100 | Этап 3 297 700 |` → 297 700
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Валовая балансовая стоимость обесцененных кредитов (стадия 3, lifetime ECL) по IFRS 9.

## MODIFIED_AUDITORS_OPINION
name_ru: Наличие модифицированного мнения аудитора
name_en: Modified auditor's opinion
aliases_ru: Модифицированное мнение аудитора; Аудиторское заключение с оговоркой; Отказ от выражения мнения
aliases_en: Modified auditor's opinion; Qualified opinion
form: Примечания
hint: Аудиторское заключение независимого аудитора (обычно в начале отчёта). Возвращай числовой флаг только за год текущего аудиторского заключения: 0 для немодифицированного мнения, 1 для оговорки / отказа / отрицательного мнения. Не создавай сравнительный период, если отдельное аудиторское мнение за него не раскрыто.
example: «Мнение с оговоркой» в заключении аудитора → 1; «Мнение не модифицировано» → 0
unit_kind: count
sign: любой
period_type: point
standard: Любой
formula_default_abs_behavior: нет

Текстовая отметка: было ли в аудиторском заключении модифицированное мнение (да / нет).

Значение здесь — не сумма, а флаг: 0 или 1. Аудиторское заключение печатается перед формами отчётности, и страница с ним ни одной формой не является — ищется по заголовку «Аудиторское заключение независимого аудитора».

## NET_COMMISSION_INCOME
name_ru: Чистый комиссионный доход
name_en: Net comission and fee income
aliases_ru: Чистые комиссионные доходы
aliases_en: Net fee and commission income
form: P&L
hint: P&L банка. Если в отчёте отдельно не раскрыт — null (мы досчитаем). В interim-отчетах с колонками 3M/Q и 6M/YTD возвращай обе группы периодов, не только YTD.
example: `| Чистые комиссионные доходы | 188 400 | 165 200 |` → 188 400
unit_kind: money
sign: любой
period_type: duration
standard: Любой
formula_default_abs_behavior: нет

Комиссионные доходы минус комиссионные расходы.

## NET_INCOME
name_ru: Чистая прибыль
name_en: Net income
aliases_ru: Чистая прибыль/(убыток); Прибыль за период/год/квартал; Чистая прибыль собственников
aliases_en: Net earnings; Net profit; Profit for the period
form: P&L
hint: P&L: бери общий Net income/Profit for the period после налогов, до распределения на attributable to parent и non-controlling interests. В interim-отчетах с колонками 3M/Q и 6M/YTD возвращай обе группы периодов. Если убыток — отрицательное число.
example: `| Чистая прибыль за год | 508 900 | 431 002 |` → за отчётный период 508 900
unit_kind: money
sign: любой
period_type: duration
standard: Любой
formula_default_abs_behavior: нет

Финальный финансовый результат компании за период после налогов.

Скобки в отчёте означают минус: снять их — значит превратить убыточный год в прибыльный.

## NET_INCOME_SHAREHOLDERS
name_ru: Прибыль, приходящаяся на акционеров материнской компании
name_en: Profit attributable to owners of the parent
aliases_ru: Прибыль, приходящаяся на собственников материнской компании; Прибыль акционеров материнской компании; Прибыль, относящаяся к акционерам материнской компании; Прибыль, причитающаяся акционерам материнской компании
form: P&L
hint: ОПУ, блок распределения прибыли в самом низу: «Прибыль, приходящаяся на: акционеров материнской компании / неконтролирующие доли» — бери ПЕРВУЮ строку. Если распределения нет (у группы нет дочерних с неконтролирующей долей), прибыль совпадает с чистой прибылью — бери её.
example: `| Прибыль, приходящаяся на: акционеров материнской компании | 496 500 | 425 100 |` → 496 500 (первая строка блока распределения)
unit_kind: money
sign: любой
period_type: duration
standard: Любой
formula_default_abs_behavior: нет

Часть чистой прибыли за период, приходящаяся на акционеров материнской компании, БЕЗ неконтролирующих долей.

## NET_INTEREST_INCOME
name_ru: Чистый процентный доход
name_en: Net interest income
aliases_ru: Чистый процентный доход; ЧПД; Чистый процентный доход после резерва; Чистый процентный доход после создания резервов; Чистый процентный доход после формирования резервов
aliases_en: Net interest income; NII
form: P&L
hint: P&L банка, ключевая строка после процентных доходов и расходов. Если рядом есть строки 'до формирования резервов' и финальный чистый процентный доход после резервов/ECL, для NET_INTEREST_INCOME бери строку ПОСЛЕ резервов (решение владельца 19.08.2026: у банка содержательный ЧПД — с поправкой на кредитный риск). В interim-отчетах с колонками 3M/Q и 6M/YTD возвращай обе группы периодов.
example: `| Чистые процентные доходы после расходов по кредитным убыткам | 32 049 | 36 636 |` → 32 049 (строка ДО резервов — другой показатель, не брать)
unit_kind: money
sign: любой
period_type: duration
standard: Любой
formula_default_abs_behavior: нет

Процентные доходы минус процентные расходы.

## NET_OTHER_RESERVES
name_ru: Чистое восстановление прочих резервов
name_en: Net other reserves
aliases_ru: (Создание)/восстановление резерва под судебные иски и прочие обязательства; Восстановление резерва под кредитные убытки по прочим финансовым активам
aliases_en: Net reversal of other provisions; Net recovery of other provisions
form: P&L
hint: Чистое изменение прочих резервов по судебным искам/прочим обязательствам (создание минус восстановление). Это не ECL по кредитам и не комиссионные/операционные расходы. Не включай credit loss reserves/ECL, loan provisions, deferred tax, broad other operating income/expense или изменение резервов по кредитам. Если отдельной строки прочих резервов нет - null.
example: `| (Создание)/восстановление резерва под судебные иски | (4 200) | 1 800 |` → −4 200
unit_kind: money
sign: любой
period_type: duration
standard: Любой
formula_default_abs_behavior: нет

Чистый эффект от прочих резервов за период (восстановление минус начисление).

Знак несёт смысл: создание резерва — минус, восстановление — плюс.

## NON_CONTROL_INTEREST
name_ru: Неконтрольные доли участия
name_en: Non-controlling interest
aliases_ru: Неконтролирующие доли; Доля меньшинства
aliases_en: Minority interest; Non-controlling interests
form: Баланс
hint: Раздел КАПИТАЛ баланса (отдельная подстрока). Может быть как положительной, так и отрицательной.
example: `| Неконтролирующие доли | 12 400 | (3 100) |` → 12 400
unit_kind: money
sign: любой
period_type: point
standard: Любой
formula_default_abs_behavior: нет

Доля капитала дочерних компаний, не принадлежащая материнской организации.

## NON_INTEREST_INCOME_BANKS
name_ru: Непроцентный доход
name_en: Non-interest income
aliases_ru: Комиссионные и прочие доходы; Непроцентные доходы; Прочие непроцентные доходы; Прочие операционные доходы от финансовой деятельности
aliases_en: Non-interest revenue
form: P&L
hint: P&L банка, в части после ЧПД. Используй явную итоговую строку непроцентных/прочих операционных доходов; не конструируй широкий итог из разрозненных строк, если отчёт не раскрывает такой subtotal. Не используй отдельную строку 'прочие доходы', если она является только компонентом, а не subtotal непроцентных доходов.
example: `| Итого непроцентные доходы | 288 300 | 254 700 |` → 288 300
unit_kind: money
sign: любой
period_type: duration
standard: Любой
formula_default_abs_behavior: нет

Совокупные непроцентные доходы (комиссии + торговые + прочие).

## OPERATING_EXPENSES_BANKS
name_ru: Операционные расходы банков
name_en: Total non-interest expense
aliases_ru: Непроцентные расходы; Операционные и административные расходы
aliases_en: Total operating expenses; Non-interest expenses
form: P&L
hint: P&L банка, после операционных доходов и резервов. Обычно это total non-interest/operating/admin expenses; расходы возвращай со знаком минус, если в P&L они показаны в скобках. Если расшифровка примечания показывает компоненты положительными, сохраняй знак итоговой P&L-строки.
example: `| Итого операционные расходы | (452 800) | (401 300) |` → −452 800
unit_kind: money
sign: отрицательный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Совокупные операционные расходы банка (зарплаты, IT, аренда, прочее).

## OPERATING_INCOME_BANKS
name_ru: Операционный доход банков
name_en: Total revenue, net of interest expense
aliases_ru: Операционные доходы; Чистый операционный доход
aliases_en: Operating income; Net revenue
form: P&L
hint: P&L банка, после ЧПД и непроцентных доходов.
example: `| Операционные доходы до резервов | 1 004 700 | 902 300 |` → 1 004 700
unit_kind: money
sign: любой
period_type: duration
standard: Любой
formula_default_abs_behavior: нет

Совокупные операционные доходы банка (ЧПД + комиссии + прочие, до создания резервов).

## OTHER_ASSETS
name_ru: Прочие активы
name_en: Other assets
aliases_ru: Прочие активы; Прочие внеоборотные активы; Иные активы
aliases_en: Other assets; Prepaid expenses and other assets; Other current assets; Other long-term assets
form: Баланс
hint: Балансовый отчёт: широкая метрика other assets включает current/prepaid and other assets плюс other non-current assets, если они раскрыты отдельно. Не бери изменение этой строки из ОДДС.
example: `| Прочие активы | 145 900 | 132 400 |` → 145 900
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Активы, не отнесённые к стандартным категориям баланса.

## OTHER_LIABILITIES
name_ru: Прочие обязательства
name_en: Other liabilities
aliases_ru: Прочие обязательства; Прочие долгосрочные обязательства; Прочие краткосрочные обязательства
aliases_en: Other liabilities; Accrued and other liabilities; Other current liabilities; Other long-term liabilities
form: Баланс
hint: Балансовый отчёт: широкая метрика other liabilities включает accrued/other current liabilities плюс other long-term liabilities, если они раскрыты отдельно. Не бери cash-flow change in accrued liabilities.
example: `| Прочие обязательства | 188 300 | 170 200 |` → 188 300
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Обязательства, не отнесённые к стандартным категориям баланса.

## OTHER_OPERATING_INCOME
name_ru: Прочие Операционные Доходы
name_en: Other operating income
aliases_ru: Прочие доходы
aliases_en: Other income; Other revenues
form: P&L
hint: P&L банка: только явная строка 'прочие операционные доходы' / 'other operating income'. Не включай FX, trading result, insurance income, fee income, recovery of ECL or broad non-interest income subtotal. Если такой строки нет - null.
example: `| Прочие операционные доходы | 14 500 | 11 800 |` → 14 500
unit_kind: money
sign: любой
period_type: duration
standard: Любой
formula_default_abs_behavior: нет

Прочие доходы от операционной деятельности банка.

## PERPETUAL_BONDS
name_ru: Бессрочные облигации
name_en: Perpetual bonds
aliases_ru: Облигации без срока погашения; Выпуск бессрочных облигаций; бессрочный субординированный займ
aliases_en: Perpetual notes; Hybrid capital notes
form: Баланс
hint: Только бессрочные/перпетуальные облигации или AT1-инструменты без срока погашения, раскрытые в капитале или субординированном долге. Не используй обычные выпущенные долговые бумаги, срочный субординированный долг, уставный капитал, эмиссионный доход или нераспределённую прибыль. Если отдельной строки perpetual/бессрочных инструментов нет - null.
example: `| Бессрочные субординированные облигации | 45 000 | 45 000 |` → 45 000
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Облигации без даты погашения, классифицируемые как часть собственного капитала.

Срочный субординированный долг сюда не относится — он в JUNIOR_DEBT. Признак бессрочного инструмента один: даты погашения нет.

## PPE
name_ru: Основные средства
name_en: Property, plant and equipment
aliases_ru: Основные средства; ОС; Основные средства и оборудование; Здания, сооружения и оборудование
aliases_en: PP&E; Fixed assets; Property and equipment
form: Примечания
hint: Примечание 'Основные средства': бери балансовую/остаточную стоимость PPE на конец периода, не первоначальную стоимость. Если баланс даёт объединённую строку 'основные средства, нематериальные активы и активы в форме права пользования', не используй её для PPE — иди в примечание и бери только основные средства, исключая НМА, goodwill и активы в форме права пользования.
example: `| Основные средства (остаточная стоимость) | 312 400 | 298 700 |` → 312 400, а не первоначальную стоимость
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Долгосрочные материальные активы — здания, оборудование, транспорт.

## PRINCIPAL_REPAYMENT_OF_LEASE_LIABILITIES
name_ru: Погашение обязательств по аренде
name_en: Repayment of the principle of lease liabilities
aliases_ru: Погашение основного долга по аренде; Выплаты основной суммы долга по аренде
aliases_en: Principal payments on lease liabilities; Repayment of lease liabilities
form: ОДДС
hint: ОДДС, раздел 'Финансовая деятельность': погашение основной суммы обязательств по аренде за период, без процентной части. Не используй остаток lease liabilities на дату - это LEASE_LIABILITIES_BANKS. Обычно денежный отток, сохраняй минус/скобки. Если заголовок колонки только 'за отчетный период' / 'за предшествующий отчетный период', используй текущий/предыдущий год отчёта, а не этот текст как период.
example: `| Погашение основной суммы обязательств по аренде | (9 120) | (8 340) |` → −9 120
unit_kind: money
sign: отрицательный
period_type: duration
standard: Любой
formula_default_abs_behavior: да

Основная часть лизинговых платежей (без процентов) за период.

Это поток за период. Остаток обязательства на дату — LEASE_LIABILITIES_BANKS, другой показатель и другая форма.

## REPO_PLEDGED_SECURITIES
name_ru: Ценные бумаги, заложенные по договорам репо
name_en: Securities pledged under repo
aliases_ru: Ценные бумаги; заложенные по РЕПО; Переданные в обеспечение ценные бумаги
aliases_en: Securities sold under repurchase agreements; Pledged securities
form: Примечания
hint: Только ценные бумаги, переданные в залог/обеспечение по сделкам repo. Не включай требования по обратному repo, кредиты в банках, все securities assets или repo liabilities. Если банк не раскрывает pledged/transferred securities separately - null, не 0.
example: `| Ценные бумаги, переданные в залог по договорам репо | 155 400 | 98 200 |` → 155 400
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Ценные бумаги, переданные в залог по сделкам репо.

## RETAINED_EARNINGS
name_ru: Нераспределенная прибыль
name_en: Retained Earnings
aliases_ru: Нераспределенная прибыль (непокрытый убыток); Накопленная прибыль
aliases_en: Accumulated retained earnings; Accumulated deficit; Retained earnings balance
form: Баланс
hint: Раздел капитала: retained earnings / accumulated deficit на конец периода. Не путай с net income за период.
example: `| Нераспределённая прибыль | 1 188 400 | 1 002 900 |` → 1 188 400
unit_kind: money
sign: любой
period_type: point
standard: Любой
formula_default_abs_behavior: нет

Накопленная чистая прибыль за все периоды за вычетом распределённых дивидендов.

Накопленный убыток даёт отрицательное значение, и это факт отчётности, а не ошибка извлечения.

## RIGHT_OF_USE_ASSETS
name_ru: Активы в форме права пользования
name_en: ROU
aliases_ru: Право пользования активами; Активы с правом пользования
aliases_en: Right-of-use assets; ROU assets
form: Баланс
hint: Внеоборотные активы баланса (отдельная строка) или отдельная строка в Примечании 'Аренда' / 'IFRS 16'. Не используй объединённую строку с PPE и нематериальными активами; бери только активы в форме права пользования.
example: `| Активы в форме права пользования | 33 900 | 29 450 |` → 33 900
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Активы, признанные арендатором по МСФО 16 — это будущие платежи по аренде, дисконтированные на дату начала аренды.

## RISK_WEIGHTED_ASSETS
name_ru: Активы, взвешенные по риску (RWA)
name_en: Risk-weighted assets (RWA)
aliases_ru: RWA
aliases_en: Risk-weighted assets; RWA
form: Примечания
hint: Примечание 'Достаточность капитала' / 'Управление капиталом': бери именно активы, взвешенные по риску (RWA), абсолютную сумму, не норматив H1.0/H1.1/H1.2 в процентах и не капитал. Если таблица раскрывает кредитный/рыночный/операционный риск, используй total RWA.
example: `| Активы, взвешенные по риску, итого | 12 405 700 | 11 208 300 |` → 12 405 700
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Активы, взвешенные по уровню риска для расчёта нормативов достаточности капитала.

## SECURITIES_AMORTISED_COST
name_ru: Ценные бумаги по амортизированной стоимости (AC)
name_en: Securities at amortised cost
aliases_ru: Финансовые активы по амортизированной стоимости; AC securities
aliases_en: Financial assets at amortised cost; HTM securities
form: Баланс
hint: Только долговые ценные бумаги по амортизированной стоимости / held to collect debt securities. Не включай кредиты клиентам, средства в банках, repo receivables или все финансовые активы. Если отдельной строки securities at amortised cost нет - null, не 0.
example: `| Долговые ценные бумаги по амортизированной стоимости | 498 100 | 436 000 |` → 498 100
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Долговые ценные бумаги, удерживаемые до погашения, по амортизированной стоимости.

## SECURITIES_ASSETS
name_ru: Финансовые инструменты (Актив)
name_en: Securities
aliases_ru: Финансовые активы; Инвестиционные ценные бумаги
aliases_en: Financial instruments; Investment securities; Marketable securities; Short-term investments; Available-for-sale securities
form: Баланс
hint: Ценные бумаги в активе. СКЛАДЫВАЙ категории, если отдельной строки «итого ценные бумаги» в отчёте нет: торговые + инвестиционные + прочие портфели ценных бумаг. 🔴 НЕ включай то, для чего в реестре ЕСТЬ СВОЙ показатель — заложенные по договорам репо идут в REPO_PLEDGED_SECURITIES и в этот агрегат НЕ входят. Не включай кредиты, средства в банках, обратное репо и денежные эквиваленты. Слагаемые назови в source_text.
example: `| Инвестиционные ценные бумаги | 2 104 500 | 1 880 300 |` → 2 104 500
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Долговые и долевые ценные бумаги, удерживаемые в качестве инвестиций или для торговли.

## SECURITIES_FVOCI
name_ru: Ценные бумаги по справедливой стоимости через прочий совокупный доход (FVOCI)
name_en: Securities at FVOCI
aliases_ru: Финансовые активы, оцениваемые по справедливой стоимости через прочий совокупный доход; Финансовые активы, оцениваемые по ПСД; Ценные бумаги, оцениваемые по справедливой стоимости через прочий совокупный доход; FVOCI; ССПСД
aliases_en: Financial assets at FVOCI; AFS securities
form: Баланс
hint: Только FVOCI/ССПСД ценные бумаги/долговые securities. Не включай FVTPL/ССЧПУ, amortised cost, repo или общий securities total. Если такой категории нет отдельной строкой - null, не 0.
example: `| Финансовые активы, оцениваемые по ССПСД | 1 204 300 | 1 088 500 |` → 1 204 300
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Финансовые активы, оцениваемые по справедливой стоимости через ПСД (по IFRS 9).

## SECURITIES_FVPL
name_ru: Ценные бумаги по справедливой стоимости через прибыль или убыток
name_en: Securities at FVPL
aliases_ru: Финансовые активы; оцениваемые по ССЧПУ; Ценные бумаги; оцениваемые по справедливой стоимости через прибыль или убыток; Финансовые активы; оцениваемые по справедливой стоимости через прибыль или убыток
aliases_en: Financial assets at FVTPL; Securities at fair value through profit or loss
form: Баланс
hint: Ценные бумаги, оцениваемые по справедливой стоимости через прибыль или убыток (FVTPL/ССЧПУ). СКЛАДЫВАЙ все строки этой категории, даже если они раскрыты в разных местах: торговые ценные бумаги (по определению ССЧПУ) + инвестиционные, оцениваемые по ССЧПУ. Отдельной строкой категория бывает не раскрыта — это НЕ повод вернуть пусто. Не включай ССЧПСД/FVOCI и амортизированную стоимость.
example: `| Финансовые активы, оцениваемые по ССЧПУ | 402 100 | 355 900 |` → 402 100
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Финансовые активы, оцениваемые по справедливой стоимости через ОПУ.

## SECURITIES_LIABILITIES
name_ru: Финансовые инструменты (Обязательства)
name_en: Securities
aliases_ru: Финансовые обязательства, оцениваемые по справедливой стоимости через прибыль или убыток; Финансовые обязательства по справедливой стоимости через прибыль или убыток; Торговые финансовые обязательства; Обязательства по ценным бумагам
aliases_en: Financial instruments; Financial liabilities; Debt instruments issued
form: Баланс
hint: Балансовый отчёт или примечание по финансовым инструментам. Не используй выпущенные облигации, векселя, депозитные сертификаты или иные долговые бумаги банка — это отдельная метрика DEBT_SECURITIES_ISSUED.
example: `| Финансовые обязательства, оцениваемые по справедливой стоимости через прибыль или убыток | 95 400 | 88 100 |` → 95 400
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Финансовые обязательства банка по справедливой стоимости через прибыль или убыток / торговые финансовые обязательства.

Собственные выпущенные бумаги банка сюда НЕ входят — они в DEBT_SECURITIES_ISSUED.

## SHARE_CAPITAL
name_ru: Уставной капитал
name_en: Share capital
aliases_ru: Акционерный капитал; Обыкновенные акции (номинал)
aliases_en: Common stock; Ordinary shares; Capital stock
form: Баланс
hint: Раздел капитала: share capital/common stock/capital stock на конец периода. Не путай с total equity.
example: `| Уставный капитал | 67 760 | 67 760 |` → 67 760
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Номинальная стоимость выпущенных акций.

## SHARE_PREMIUM
name_ru: Эмиссионный доход
name_en: Share premium
aliases_ru: Добавочный капитал; Дополнительный оплаченный капитал
aliases_en: Additional paid-in capital; Capital surplus; APIC
form: Баланс
hint: Раздел капитала: только явная строка 'эмиссионный доход' / share premium / additional paid-in capital на конец периода. Не используй уставный капитал, переоценку, нераспределённую прибыль или прочие резервы. Если отдельной строки нет - null.
example: `| Эмиссионный доход | 228 100 | 228 100 |` → 228 100
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Превышение цены размещения акций над номинальной стоимостью.

## TIER1_RATIO_DISCLOSED
name_ru: Норматив Tier 1 (раскрываемый)
name_en: Tier 1 ratio (disclosed)
aliases_ru: Tier 1 ratio; норматив Н 1.2; Н1.2; норматив Н 20.2
aliases_en: Tier 1 capital ratio
form: Примечания
hint: Примечание 'Достаточность капитала'. Для российских банков Tier 1 = норматив основного капитала H1.2 / Н1.2 (или консолидированный Н20.2). Не используй H1.1/Н1.1 — это CET1. Единица — проценты.
example: `| Норматив достаточности основного капитала Н1.2 | 12,1% | 11,4% |` → 12,1
unit_kind: percent
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Раскрываемый банком норматив достаточности капитала первого уровня (в процентах).

Н1.1 — соседняя строка той же таблицы и относится к CET1, а не сюда.

## TOTAL_ASSETS
name_ru: Итого активы
name_en: Total assets
aliases_ru: ИТОГО АКТИВЫ; Итого по активам; Активы всего; Всего активов; Итого активов
aliases_en: Total assets; Sum of assets
form: Баланс
hint: Балансовый отчёт (Statement of Financial Position), нижняя строка раздела АКТИВЫ. Должна равняться сумме внеоборотных и оборотных активов.
example: `| ИТОГО АКТИВЫ | 15 204 883 | 13 145 210 |` → на отчётную дату 15 204 883
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Совокупная балансовая стоимость всех активов компании на отчётную дату.

## TOTAL_ASSOCIATE_INVESTMENTS
name_ru: Инвестиции в ассоциированные и совместные предприятия
name_en: Associate investments
aliases_ru: Вложения в ассоциированные и совместные предприятия; Инвестиции в компании по методу долевого участия
aliases_en: Investments in associates and joint ventures; Equity-accounted investments; Equity method investment; Investments in associates; Investments: equity method; Equity method investments
form: Баланс
hint: Внеоборотные активы баланса: investments in associates/JVs/equity method investments на дату баланса. Не путай с purchases of strategic investments в ОДДС.
example: `| Инвестиции в ассоциированные компании | 38 200 | 34 100 |` → 38 200
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Совокупные инвестиции в ассоциированные компании и совместные предприятия.

## TOTAL_CREDIT_ALLOWANCE
name_ru: Резервы под обесценение кредитов и авансов клиентам
name_en: Provision for credit losses
aliases_ru: Резервы под обесценение кредитов; ОКУ под обесценение кредитов и авансов клиентам; Оценочный резерв под кредитные убытки; Резерв под ожидаемые кредитные убытки по кредитам
aliases_en: Allowance for credit losses; ECL allowance on loans; Impairment allowance on loans
form: Примечания
hint: Примечание 'Кредиты и авансы клиентам' / 'Резервы под обесценение': бери closing balance/остаток оценочного резерва по кредитам на конец периода. Для этой метрики нужен allowance на дату, а не P&L-поток создания/восстановления ECL; поток — это CHANGE_IN_EXPECTED_CREDIT_LOSSES. Если строка называется 'резерв/оценочный резерв/allowance' и относится к кредитам клиентам, возвращай её как отрицательную contra-asset величину, даже если OCR потерял скобки.
example: `| Оценочный резерв под кредитные убытки на конец периода | (415 200) | (388 100) |` → −415 200
unit_kind: money
sign: отрицательный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Балансовый остаток оценочного резерва / allowance под кредитные убытки по кредитам и авансам клиентам на отчётную дату.

Это ОСТАТОК резерва на дату. Поток создания и восстановления за период — CHANGE_IN_EXPECTED_CREDIT_LOSSES; перепутать их значит выдать запас за расход.

## TOTAL_EQUITY
name_ru: Итого собственный капитал
name_en: Total equity
aliases_ru: Итого капитал; Собственный капитал акционеров
aliases_en: Total shareholders' equity; Stockholders' equity
form: Баланс
hint: Балансовый отчёт, итог раздела КАПИТАЛ. Иногда отдельно 'Капитал, приходящийся на акционеров материнской' + 'Неконтролирующие доли'.
example: `| Итого капитал | 1 512 004 | 1 398 771 |` → на отчётную дату 1 512 004
unit_kind: money
sign: любой
period_type: point
standard: Любой
formula_default_abs_behavior: нет

Совокупная стоимость собственного капитала, включая капитал акционеров и неконтролирующие доли.

Итог капитала включает неконтролирующие доли. Часть, принадлежащая акционерам материнской компании, — это EQUITY_SHAREHOLDERS, отдельный показатель.

## TOTAL_INVESTMENT_BANKING_FEES
name_ru: Доход от инвестиционно-банковского бизнеса
name_en: Total investment banking fees
aliases_ru: Доходы инвестиционного банкинга; Комиссии инвестиционного банка
aliases_en: Investment banking revenue; IB fees
form: P&L
hint: P&L банка с инвестбанковской деятельностью или Примечание 'Сегментный анализ'.
example: `| Investment banking fees | 12 400 | 10 900 |` → 12 400
unit_kind: money
sign: положительный
period_type: duration
standard: US GAAP
formula_default_abs_behavior: да

Доходы от инвестиционно-банковской деятельности (M&A, размещения, andewriting).

## TOTAL_LOANS_TO_CLIENTS
name_ru: Кредиты клиентам
name_en: Total loans and leases
aliases_ru: Кредиты и авансы клиентам; Кредитный портфель (валовой)
aliases_en: Loans and advances to customers; Gross loans
form: Баланс
hint: Балансовый отчёт банка, ключевой актив. Детализация в Примечании 'Кредиты клиентам'.
example: `| Кредиты и авансы клиентам | 9 840 200 | 8 415 700 |` → 9 840 200
unit_kind: money
sign: положительный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Совокупный объём кредитов, выданных клиентам банка (за вычетом резервов ECL).

## TREASURY_SHARES
name_ru: Собственные акции, выкупленные у акционеров
name_en: Treasury shares
aliases_ru: Собственные акции
aliases_en: Treasury stock; Own shares repurchased
form: Баланс
hint: Раздел капитала: treasury shares/treasury stock на конец периода. Обычно уменьшает капитал; не путай с buyback cash-flow за период.
example: `| Собственные акции, выкупленные у акционеров | (7 900) | (4 300) |` → −7 900
unit_kind: money
sign: отрицательный
period_type: point
standard: Любой
formula_default_abs_behavior: да

Стоимость собственных акций, выкупленных у акционеров.

Строка уменьшает капитал, поэтому в балансе она в скобках. Знак источника сохраняется: положительное значение здесь означает, что скобки потерялись при распознавании.
