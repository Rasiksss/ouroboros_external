"""Per-metric enrichment: synonyms, description, search hint, period type.

Merged on top of the Excel registry by build_metric_registry.py — the Excel
data takes priority (any non-empty cell wins). Use this file to provide
defaults for fields the human reviewer hasn't filled yet.

Schema for each entry:
    {
        "aliases_ru": [str, ...],   # Russian phrasings the metric may appear under
        "aliases_en": [str, ...],   # English phrasings (for IFRS reports printed bilingually)
        "description": str,         # 1 sentence — what the metric is
        "hint": str,                # 1 sentence — which section / note of the report to scan
        "period_type": str,         # "point" (на дату) or "duration" (за период)
    }
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_default_metric_period_types() -> dict[str, str]:
    path = Path(__file__).with_name("metric_period_types.py")
    spec = importlib.util.spec_from_file_location("metric_period_types", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return dict(mod.DEFAULT_METRIC_PERIOD_TYPES)


PERIOD_TYPE_ENRICHMENT = _load_default_metric_period_types()


ENRICHMENT: dict[str, dict[str, list[str] | str]] = {
    # ─── BALANCE SHEET — assets ──────────────────────────────────────────
    "TOTAL_ASSETS": {
        "aliases_ru": ["ИТОГО АКТИВЫ", "Итого по активам", "Активы всего", "Всего активов", "Итого активов"],
        "aliases_en": ["Total assets", "Sum of assets"],
        "description": "Совокупная балансовая стоимость всех активов компании на отчётную дату.",
        "hint": "Балансовый отчёт (Statement of Financial Position), нижняя строка раздела АКТИВЫ. Должна равняться сумме внеоборотных и оборотных активов.",
    },
    "TOTAL_CURRENT_ASSETS": {
        "aliases_ru": ["Итого оборотные активы", "Оборотные активы всего", "Текущие активы", "Краткосрочные активы"],
        "aliases_en": ["Total current assets", "Current assets"],
        "description": "Сумма ликвидных активов, ожидаемых к реализации в течение 12 месяцев.",
        "hint": "Балансовый отчёт, итоговая строка блока 'Оборотные активы' (запасы, дебиторка, денежные средства).",
    },
    "PPE": {
        "aliases_ru": ["Основные средства", "ОС", "Основные средства и оборудование", "Здания, сооружения и оборудование"],
        "aliases_en": ["Property, plant and equipment", "PP&E", "Fixed assets", "Tangible fixed assets"],
        "description": "Долгосрочные материальные активы — здания, оборудование, транспорт.",
        "hint": "Внеоборотные активы баланса. Также детализация в Примечании 'Основные средства'.",
    },
    "RIGHT_OF_USE_ASSETS": {
        "aliases_ru": ["Активы в форме права пользования", "Активы ППА", "Право пользования активами", "ROU активы"],
        "aliases_en": ["Right-of-use assets", "ROU assets", "Lease right-of-use assets"],
        "description": "Активы, признанные арендатором по МСФО 16 — это будущие платежи по аренде, дисконтированные на дату начала аренды.",
        "hint": "Внеоборотные активы баланса (отдельная строка). Детализация в Примечании 'Аренда' / 'IFRS 16'.",
    },
    "LONG_TERM_ROU": {
        "aliases_ru": ["Долгосрочные активы ППА", "Долгосрочная часть прав пользования"],
        "aliases_en": ["Non-current right-of-use assets"],
        "description": "Активы ППА к признанию через более 12 месяцев.",
        "hint": "Это активы права пользования (ROU assets), а не обязательства по аренде. Бери строку баланса/right-of-use assets; не подставляй current/non-current lease liabilities.",
    },
    "SHORT_TERM_ROU": {
        "aliases_ru": ["Краткосрочные активы ППА"],
        "aliases_en": ["Current right-of-use assets"],
        "description": "Активы ППА к признанию в течение 12 месяцев.",
        "hint": "Примечание 'IFRS 16' / 'Аренда' — графа 'До 1 года'.",
    },
    "GOODWILL": {
        "aliases_ru": ["Гудвил", "Гудвилл", "Деловая репутация"],
        "aliases_en": ["Goodwill"],
        "description": "Превышение цены приобретения дочерней компании над справедливой стоимостью её чистых идентифицируемых активов.",
        "hint": "Внеоборотные активы баланса. Подробнее — в Примечании 'Гудвил' / 'Объединение бизнеса'.",
    },
    "INTANGIBLE_ASSETS": {
        "aliases_ru": ["Нематериальные активы", "НМА", "Прочие нематериальные активы", "Нематериальные активы за вычетом гудвила"],
        "aliases_en": ["Intangible assets", "Other intangible assets", "Intangibles"],
        "description": "Идентифицируемые неденежные активы без физической субстанции — лицензии, патенты, программное обеспечение, бренды.",
        "hint": "Внеоборотные активы баланса (отдельная строка после гудвила). Детализация в Примечании 'Нематериальные активы'.",
    },
    "DEFERRED_TAX_ASSETS": {
        "aliases_ru": ["Отложенные налоговые активы", "ОНА"],
        "aliases_en": ["Deferred tax assets", "DTA"],
        "description": "Сумма налогов на прибыль к уменьшению в будущих периодах за счёт вычитаемых временных разниц.",
        "hint": "Внеоборотные активы. Подробности в Примечании 'Налог на прибыль' / 'Отложенные налоги'.",
    },
    "ASSOCIATE_INVESTMENTS": {
        "aliases_ru": ["Инвестиции в ассоциированные компании", "Вложения в ассоциированные компании", "Инвестиции в АК"],
        "aliases_en": ["Investments in associates", "Equity-accounted investments"],
        "description": "Доли в компаниях, на которые компания оказывает существенное влияние (обычно 20-50% долей).",
        "hint": "Внеоборотные активы баланса. Учёт по методу долевого участия. Примечание 'Ассоциированные компании и совместные предприятия'.",
    },
    "TOTAL_ASSOCIATE_INVESTMENTS": {
        "aliases_ru": ["Инвестиции в ассоциированные и СП", "Инвестиции в АК и СП"],
        "aliases_en": ["Investments in associates and JVs", "Investments: equity method", "Equity method investments"],
        "description": "Совокупные инвестиции в ассоциированные компании и совместные предприятия.",
        "hint": "Внеоборотные активы баланса. Объединённая строка для АК + СП / equity method investments, если отчёт не разделяет их.",
    },
    "INVENTORIES": {
        "aliases_ru": ["Запасы", "Товарно-материальные запасы", "ТМЦ", "Сырьё и материалы"],
        "aliases_en": ["Inventories", "Stocks", "Inventory"],
        "description": "Сырьё, незавершённое производство и готовая продукция, удерживаемые для продажи.",
        "hint": "Оборотные активы баланса. Детализация в Примечании 'Запасы'.",
    },
    "ACCOUNTS_RECEIVABLE": {
        "aliases_ru": ["Дебиторская задолженность", "Торговая дебиторская задолженность", "Дебиторская задолженность покупателей и заказчиков", "Дебиторка"],
        "aliases_en": ["Trade and other receivables", "Accounts receivable", "Trade receivables"],
        "description": "Суммы, причитающиеся компании от покупателей и прочих контрагентов.",
        "hint": "Оборотные активы баланса. Бери торговую дебиторскую задолженность / accounts receivable, net. Не включай предоплаты, авансы выданные, НДС к возмещению и прочую дебиторскую задолженность, если они раскрыты отдельно.",
    },
    "BANK_DEPOSITS": {
        "aliases_ru": ["Банковские депозиты", "Срочные депозиты", "Депозиты в банках"],
        "aliases_en": ["Bank deposits", "Short-term deposits", "Term deposits"],
        "description": "Денежные средства, размещённые в банках на срочной основе (обычно < 12 месяцев).",
        "hint": "Оборотные активы баланса или примечание к денежным средствам/краткосрочным инвестициям. Если строка баланса 'краткосрочные финансовые вложения' раскрыта в примечании как банковские/time deposits, бери эту строку. Не путай с cash equivalents и marketable securities.",
    },
    "CASH_EQUIVALENTS": {
        "aliases_ru": ["Денежные средства и их эквиваленты", "Денежные средства", "Денежные средства и эквиваленты"],
        "aliases_en": ["Cash and cash equivalents", "Cash"],
        "description": "Касса, расчётные счета и краткосрочные высоколиквидные инвестиции (срок ≤ 3 мес.).",
        "hint": "Оборотные активы баланса или последняя строка ОДДС. Возвращай все явно раскрытые даты cash and cash equivalents at end of period, включая сравнительные даты, которых нет в балансе. Не путай с bank deposits/time deposits и marketable securities.",
    },
    "OTHER_ASSETS": {
        "aliases_ru": ["Прочие активы", "Прочие внеоборотные активы", "Иные активы"],
        "aliases_en": ["Other assets"],
        "description": "Активы, не отнесённые к стандартным категориям баланса.",
        "hint": "Балансовый отчёт: широкая метрика other assets включает current/prepaid and other assets плюс other non-current assets, если они раскрыты отдельно. Не бери изменение этой строки из ОДДС.",
    },

    # ─── BALANCE SHEET — equity & liabilities ────────────────────────────
    "TOTAL_EQUITY": {
        "aliases_ru": ["Итого собственный капитал", "ИТОГО КАПИТАЛ", "Капитал и резервы", "Итого капитал, относящийся к акционерам"],
        "aliases_en": ["Total equity", "Shareholders' equity", "Stockholders' equity"],
        "description": "Совокупная стоимость собственного капитала, включая капитал акционеров и неконтролирующие доли.",
        "hint": "Балансовый отчёт, итог раздела КАПИТАЛ. Иногда отдельно 'Капитал, приходящийся на акционеров материнской' + 'Неконтролирующие доли'.",
    },
    "SHARE_CAPITAL": {
        "aliases_ru": ["Уставный капитал", "Акционерный капитал", "Объявленный капитал"],
        "aliases_en": ["Share capital", "Common stock", "Issued capital"],
        "description": "Номинальная стоимость выпущенных акций.",
        "hint": "Раздел КАПИТАЛ баланса. Детализация в Примечании 'Акционерный капитал'.",
    },
    "SHARE_PREMIUM": {
        "aliases_ru": ["Эмиссионный доход", "Добавочный капитал", "Премия от эмиссии акций"],
        "aliases_en": ["Share premium", "Additional paid-in capital"],
        "description": "Превышение цены размещения акций над номинальной стоимостью.",
        "hint": "Раздел КАПИТАЛ баланса.",
    },
    "TREASURY_SHARES": {
        "aliases_ru": ["Собственные акции, выкупленные у акционеров", "Собственные выкупленные акции", "Казначейские акции"],
        "aliases_en": ["Treasury shares", "Treasury stock"],
        "description": "Стоимость собственных акций, выкупленных у акционеров.",
        "hint": "Раздел КАПИТАЛ баланса (со знаком минус). Для buyback — также см. ОДВК.",
    },
    "RETAINED_EARNINGS": {
        "aliases_ru": ["Нераспределённая прибыль", "Накопленная прибыль", "Нераспределённая прибыль (непокрытый убыток)"],
        "aliases_en": ["Retained earnings", "Accumulated earnings"],
        "description": "Накопленная чистая прибыль за все периоды за вычетом распределённых дивидендов.",
        "hint": "Раздел КАПИТАЛ баланса. Изменения отслеживаются в ОДВК (Statement of Changes in Equity).",
    },
    "NON_CONTROL_INTEREST": {
        "aliases_ru": ["Неконтролирующие доли участия", "Доля неконтролирующих акционеров", "Неконтролирующая доля", "Доля меньшинства"],
        "aliases_en": ["Non-controlling interests", "NCI", "Minority interest"],
        "description": "Доля капитала дочерних компаний, не принадлежащая материнской организации.",
        "hint": "Раздел КАПИТАЛ баланса (отдельная подстрока). Может быть как положительной, так и отрицательной.",
    },
    "PERPETUAL_BONDS": {
        "aliases_ru": ["Бессрочные облигации", "Вечные облигации", "Бессрочный субординированный долг"],
        "aliases_en": ["Perpetual bonds", "Perpetual notes"],
        "description": "Облигации без даты погашения, классифицируемые как часть собственного капитала.",
        "hint": "Раздел КАПИТАЛ баланса (если эмитент учитывает их как капитал) или Долгосрочные обязательства.",
    },
    "LONG_TERM_DEBT": {
        "aliases_ru": ["Долгосрочные кредиты и займы", "Долговые обязательства долгосрочные", "Долгосрочная задолженность по кредитам", "Заёмные средства долгосрочные"],
        "aliases_en": ["Long-term debt", "Long-term borrowings", "Non-current debt", "Long-term loans and borrowings"],
        "description": "Кредиты и займы со сроком погашения более 12 месяцев.",
        "hint": "Долгосрочные обязательства баланса. Детализация (валюта, ставка, сроки) в Примечании 'Кредиты и займы' / 'Заёмные средства'.",
    },
    "LONG_TERM_LEASE_LIABILITIES": {
        "aliases_ru": ["Долгосрочные обязательства по аренде", "Долгосрочная часть обязательств по аренде", "Долгосрочные арендные обязательства"],
        "aliases_en": ["Long-term lease liabilities", "Non-current lease liabilities"],
        "description": "Обязательства по аренде по IFRS 16 со сроком погашения более 12 месяцев.",
        "hint": "Долгосрочные обязательства баланса. Также в Примечании 'IFRS 16' — графа 'Свыше 1 года'.",
    },
    "DEFERRED_TAX_LIABILITIES": {
        "aliases_ru": ["Отложенные налоговые обязательства", "ОНО"],
        "aliases_en": ["Deferred tax liabilities", "DTL"],
        "description": "Налоги на прибыль, подлежащие уплате в будущих периодах из-за налогооблагаемых временных разниц.",
        "hint": "Долгосрочные обязательства баланса. Подробности в Примечании 'Налог на прибыль' / 'Отложенные налоги'.",
    },
    "SHORT_TERM_BORROWINGS": {
        "aliases_ru": ["Краткосрочные кредиты и займы", "Краткосрочная задолженность по кредитам", "Текущая часть долгосрочной задолженности по кредитам", "Заёмные средства краткосрочные"],
        "aliases_en": ["Short-term borrowings", "Current portion of long-term debt", "Short-term loans"],
        "description": "Кредиты и займы со сроком погашения в течение 12 месяцев (включая текущую часть долгосрочных).",
        "hint": "Краткосрочные обязательства баланса. Часто включает текущую часть долгосрочного долга.",
    },
    "SHORT_TERM_LEASE_LIABILITIES": {
        "aliases_ru": ["Краткосрочные обязательства по аренде", "Текущая часть обязательств по аренде", "Краткосрочные арендные обязательства"],
        "aliases_en": ["Short-term lease liabilities", "Current lease liabilities"],
        "description": "Обязательства по аренде по IFRS 16 со сроком погашения в течение 12 месяцев.",
        "hint": "Краткосрочные обязательства баланса или примечание по аренде. Бери current portion/current lease liabilities, даже если в балансе она включена в accrued/other current liabilities.",
    },
    "ACCOUNTS_PAYABLE": {
        "aliases_ru": ["Кредиторская задолженность", "Торговая кредиторская задолженность", "Задолженность поставщикам и подрядчикам", "Кредиторка"],
        "aliases_en": ["Trade and other payables", "Accounts payable", "Trade payables"],
        "description": "Суммы, причитающиеся поставщикам, подрядчикам и прочим кредиторам.",
        "hint": "Краткосрочные обязательства баланса. Детализация в Примечании 'Кредиторская задолженность'.",
    },
    "TOTAL_CURRENT_LIABILITIES": {
        "aliases_ru": ["Итого краткосрочные обязательства", "Текущие обязательства", "Краткосрочные обязательства всего", "Итого текущих обязательств"],
        "aliases_en": ["Total current liabilities", "Current liabilities"],
        "description": "Сумма обязательств с погашением в течение 12 месяцев.",
        "hint": "Балансовый отчёт, итоговая строка блока 'Краткосрочные обязательства'.",
    },
    "OTHER_LIABILITIES": {
        "aliases_ru": ["Прочие обязательства", "Прочие долгосрочные обязательства", "Прочие краткосрочные обязательства"],
        "aliases_en": ["Other liabilities"],
        "description": "Обязательства, не отнесённые к стандартным категориям баланса.",
        "hint": "Балансовый отчёт: широкая метрика other liabilities включает accrued/other current liabilities плюс other long-term liabilities, если они раскрыты отдельно. Не бери cash-flow change in accrued liabilities.",
    },
    "DERIVATIVE_LIABILITIES": {
        "aliases_ru": ["Производные финансовые обязательства", "Деривативы (обязательства)", "Производные финансовые инструменты — обязательства"],
        "aliases_en": ["Derivative liabilities", "Derivative financial instruments"],
        "description": "Обязательства по производным финансовым инструментам, оцениваемые по справедливой стоимости.",
        "hint": "Балансовый отчёт. Также в Примечании 'Финансовые инструменты' / 'Управление рисками'.",
    },
    "INSURANCE_LIABILITIES": {
        "aliases_ru": ["Обязательства по договорам страхования", "Страховые резервы", "Обязательства по страховым договорам"],
        "aliases_en": ["Insurance contract liabilities", "Insurance liabilities"],
        "description": "Обязательства страховщика по договорам страхования (резервы убытков, премии).",
        "hint": "Балансовый отчёт страховой компании. Только если компания ведёт страховую деятельность.",
    },
    "SECURITIES_ASSETS": {
        "aliases_ru": ["Финансовые инструменты (актив)", "Ценные бумаги", "Инвестиционные ценные бумаги", "Финансовые активы"],
        "aliases_en": ["Financial assets", "Securities", "Investment securities"],
        "description": "Долговые и долевые ценные бумаги, удерживаемые в качестве инвестиций или для торговли.",
        "hint": "Внеоборотные или оборотные активы баланса (зависит от срока). Детализация в Примечании 'Финансовые инструменты'.",
    },
    "SECURITIES_LIABILITIES": {
        "aliases_ru": ["Финансовые инструменты (обязательства)", "Эмитированные ценные бумаги", "Облигационный заём"],
        "aliases_en": ["Financial liabilities", "Issued securities", "Bonds issued"],
        "description": "Облигации и прочие долговые ценные бумаги, выпущенные компанией.",
        "hint": "Балансовый отчёт. Детализация в Примечании 'Долговые ценные бумаги' / 'Облигационный заём'.",
    },

    # ─── INCOME STATEMENT (P&L) ──────────────────────────────────────────
    "SALES_REVENUE": {
        "aliases_ru": ["Выручка", "Выручка от продаж", "Выручка от реализации", "Доходы от обычных видов деятельности", "Выручка от реализации продукции"],
        "aliases_en": ["Revenue", "Sales", "Net sales", "Total revenue"],
        "description": "Доходы компании от обычной операционной деятельности (без НДС).",
        "hint": "Первая строка отчёта о прибылях и убытках (P&L). Детализация по сегментам в одноимённой ноте.",
    },
    "COSTS_EXPENSES": {
        "aliases_ru": ["Себестоимость", "Себестоимость реализации", "Себестоимость продаж", "Себестоимость продукции"],
        "aliases_en": ["Cost of sales", "Cost of goods sold", "COGS", "Cost of revenue", "Cost of products", "Cost of services"],
        "description": "Прямые затраты, связанные с производством проданной продукции/услуг.",
        "hint": "P&L: прямая себестоимость обычно идёт сразу после выручки и перед gross profit. В разных отраслях может называться по деятельности компании, но должна оставаться прямыми затратами на проданные товары/услуги. Если прямые затраты раскрыты несколькими строками (например cost of services/products), суммируй только эти строки. Не бери subtotal/total, который включает amortization, SG&A, R&D или operating expenses; не рассчитывай из выручки и operating income.",
    },
    "GROSS_PROFIT": {
        "aliases_ru": ["Валовая прибыль", "Валовая маржа"],
        "aliases_en": ["Gross profit", "Gross margin", "Subtotal after cost of sales"],
        "description": "Выручка минус себестоимость продаж.",
        "hint": "P&L, строка после себестоимости. Если строка Gross profit не подписана, но есть очевидный subtotal после прямых costs of sales/products/services и перед operating expenses, можно брать этот subtotal.",
    },
    "OPERATING_EXPENSES": {
        "aliases_ru": ["Операционные расходы", "Коммерческие, общехозяйственные и административные расходы", "Коммерческие и административные расходы", "SG&A"],
        "aliases_en": ["Operating expenses", "SG&A", "Selling, general and administrative expenses", "General and administrative expense", "Marketing, general, and administrative"],
        "description": "Расходы, связанные с операционной деятельностью, кроме себестоимости (коммерческие + общехозяйственные + админ).",
        "hint": "P&L, после валовой прибыли. Приоритет: total operating expenses / operating, selling, general and administrative expenses. Если отдельными строками раскрыты Operating expenses и SG&A/G&A без общего subtotal, суммируй эти operating-expense строки. Не бери себестоимость и total costs and expenses.",
    },
    "OPERATING_PROFIT": {
        "aliases_ru": ["Операционная прибыль", "Прибыль от операционной деятельности", "Прибыль от продаж", "EBIT"],
        "aliases_en": ["Operating profit", "EBIT", "Operating income"],
        "description": "Прибыль до финансовых доходов/расходов и налога — результат основной деятельности.",
        "hint": "P&L, строка после операционных расходов и до 'Финансовые доходы/расходы'.",
    },
    "DEPRECIATION_AMORTIZATION": {
        "aliases_ru": ["Амортизация", "Амортизация основных средств и НМА", "Износ и амортизация", "Амортизация и износ"],
        "aliases_en": ["Depreciation and amortization", "D&A", "Depreciation and amortisation"],
        "description": "Сумма амортизации основных средств и нематериальных активов за период.",
        "hint": "ОДДС/примечания: если рядом раскрыты Depreciation and amortization и отдельная amortization of acquisition-related intangibles, общая D&A — их сумма. ROU amortization — отдельная метрика.",
    },
    "ROU_DEPRECIATION": {
        "aliases_ru": ["Амортизация активов ППА", "Амортизация активов в форме права пользования", "Амортизация ROU"],
        "aliases_en": ["Depreciation of right-of-use assets"],
        "description": "Амортизация активов IFRS 16 за период.",
        "hint": "Примечание 'IFRS 16' / 'Аренда'. Также раскрывается в P&L отдельной строкой.",
    },
    "FINANCIAL_INCOME": {
        "aliases_ru": ["Финансовые доходы", "Процентные доходы", "Доходы от финансовой деятельности"],
        "aliases_en": ["Finance income", "Financial income", "Interest income"],
        "description": "Процентные и прочие финансовые доходы (от размещения средств, депозитов, ценных бумаг).",
        "hint": "P&L, после операционной прибыли. Бери строку finance/financial income и сохраняй значение как в источнике.",
    },
    "FINANCIAL_EXPENSES": {
        "aliases_ru": ["Финансовые расходы", "Расходы на обслуживание заёмных средств", "Процентные расходы"],
        "aliases_en": ["Finance costs", "Financial expenses", "Interest expense"],
        "description": "Процентные расходы по кредитам, займам, облигациям и прочее обслуживание долга.",
        "hint": "P&L, после операционной прибыли. Бери строку finance costs/interest expense и сохраняй знак/скобки как в источнике.",
    },
    "EBT": {
        "aliases_ru": ["Прибыль до налогообложения", "Прибыль до налога на прибыль", "Прибыль (убыток) до налога на прибыль", "Прибыль до налогов"],
        "aliases_en": ["Profit before tax", "Pre-tax profit", "Earnings before tax", "Profit/(loss) before tax"],
        "description": "Прибыль до вычета налога на прибыль — финальная строка P&L перед налогом.",
        "hint": "P&L, предпоследняя строка. Если у компании убыток, в источнике обычно стоят скобки или минус; сохраняй знак/скобки как в источнике.",
    },
    "INCOME_TAX": {
        "aliases_ru": ["Расход по налогу на прибыль", "Налог на прибыль", "Расходы по налогу на прибыль", "Налог на прибыль (расход/доход)"],
        "aliases_en": ["Income tax", "Tax expense", "Income tax expense", "Income tax provision (benefit)"],
        "description": "Сумма налога на прибыль за период (текущий + отложенный).",
        "hint": "P&L, предпоследняя строка перед чистой прибылью. Если налог положительный — расход; если отрицательный — доход (возврат).",
    },
    "NET_INCOME": {
        "aliases_ru": ["Чистая прибыль", "Чистая прибыль (убыток)", "Прибыль за год", "Прибыль/(убыток) за период", "Прибыль/(убыток) за год"],
        "aliases_en": ["Net income", "Net profit", "Profit for the year", "Profit for the period", "Net earnings"],
        "description": "Финальный финансовый результат компании за период после налогов.",
        "hint": "P&L: бери общий Net income/Profit for the period после налогов, до распределения на attributable to parent и non-controlling interests. В interim-отчетах с колонками 3M/Q и 6M/YTD возвращай обе группы периодов. Если убыток — отрицательное число.",
    },
    "EPS": {
        "aliases_ru": ["Прибыль на одну обыкновенную акцию", "Базовая прибыль на акцию", "EPS", "Прибыль на акцию (рубли на акцию)"],
        "aliases_en": ["EPS", "Earnings per share", "Basic earnings per share"],
        "description": "Чистая прибыль на одну обыкновенную акцию (валюта на акцию, не в миллионах).",
        "hint": "P&L: строка Basic earnings per share после net income. Бери Basic, не Diluted; единица — валюта на акцию.",
    },
    "EBITDA": {
        "aliases_ru": ["EBITDA", "EBITDA по сегменту", "Прибыль до вычета процентов, налогов, износа и амортизации", "Скорректированная EBITDA", "Adjusted EBITDA"],
        "aliases_en": ["EBITDA", "Adjusted EBITDA", "Segment EBITDA", "EBITDA total"],
        "description": "Операционная прибыль до амортизации (EBIT + D&A). Часто компании раскрывают как нефинансовую метрику.",
        "hint": "Сегментная отчётность или MD&A (Management Discussion). Иногда в Примечании 'Сегменты' строка называется 'EBITDA по сегменту'; для main metric бери колонку Итого/Total, не отдельные сегменты, не elimination-only строку и не EBITDA before IFRS 16/adjusted subtotal, если раскрыт консолидированный total. Если строка содержит сегменты, исключения/eliminations и затем итоговые значения в последних колонках или в конце строки, бери эти итоговые значения как main EBITDA. Возвращай null только если явной строки EBITDA/OIBDA нет.",
    },
    "DOUBTFUL_ACCOUNTS_ALLOWANCE": {
        "aliases_ru": ["Резерв под кредитные убытки (ECL)", "Резерв под обесценение дебиторской задолженности", "Резерв под сомнительные долги", "Резерв ECL"],
        "aliases_en": ["Allowance for doubtful accounts", "ECL allowance", "Bad debt provision"],
        "description": "Резерв под ожидаемые кредитные убытки по дебиторской задолженности.",
        "hint": "Примечание 'Дебиторская задолженность' или 'Управление кредитным риском'. Может быть отдельной строкой 'Изменение резерва ECL'.",
    },
    "INSURANCE_INCOME": {
        "aliases_ru": ["Выручка по страховой деятельности", "Доходы по страховой деятельности", "Страховые премии"],
        "aliases_en": ["Insurance revenue", "Insurance contract revenue"],
        "description": "Доходы страховщика по договорам страхования (по IFRS 17).",
        "hint": "P&L страховой компании. Не применимо к не-страховым организациям — null.",
    },
    "INSURANCE_EXPENSE": {
        "aliases_ru": ["Расходы по страховой деятельности", "Страховые расходы", "Страховые выплаты"],
        "aliases_en": ["Insurance service expenses"],
        "description": "Расходы страховщика по договорам страхования (по IFRS 17).",
        "hint": "P&L страховой компании. Не применимо к не-страховым — null.",
    },
    "NET_OTHER_RESERVES": {
        "aliases_ru": ["Чистое восстановление прочих резервов", "Прочие изменения в резервах", "Восстановление/начисление прочих резервов"],
        "aliases_en": ["Net change in other reserves", "Net other provisions"],
        "description": "Чистый эффект от прочих резервов за период (восстановление минус начисление).",
        "hint": "ОДДС или P&L. Может быть в составе 'Прочие операционные доходы/расходы' или отдельной строкой.",
    },

    # ─── CASH FLOW STATEMENT ─────────────────────────────────────────────
    "NET_OPERATING_CASH_FLOW": {
        "aliases_ru": ["Чистые денежные средства от операционной деятельности", "Денежные средства от операционной деятельности", "Операционный денежный поток", "ЧДПОД", "Денежные средства, полученные от операционной деятельности"],
        "aliases_en": ["Net cash from operating activities", "Operating cash flow", "Cash flow from operations"],
        "description": "Итоговый денежный поток от основной операционной деятельности (после уплаты налогов и процентов).",
        "hint": "ОДДС (Cash Flow Statement), итоговая строка раздела 'Операционная деятельность'.",
    },
    "ADJUSTMENTS_OPERATING_CASH_FLOWS": {
        "aliases_ru": ["Корректировки операционного ДДС", "Неденежные корректировки", "Корректировки прибыли до налогообложения"],
        "aliases_en": ["Adjustments for non-cash items", "Operating cash flow adjustments"],
        "description": "Сумма всех неденежных корректировок прибыли при формировании операционного ДДС (D&A, изменения резервов и пр.).",
        "hint": "ОДДС, верхняя часть раздела 'Операционная деятельность' между прибылью и движением оборотного капитала.",
    },
    "CHANGES_OCF_ASSETS_LIABILITIES": {
        "aliases_ru": ["Изменения в оборотном капитале", "Изменение оборотных активов и обязательств", "Изменения в оборотных активах и обязательствах"],
        "aliases_en": ["Changes in working capital", "Changes in operating assets and liabilities"],
        "description": "Совокупное изменение оборотного капитала (запасы, дебиторка, кредиторка) за период.",
        "hint": "ОДДС, средняя часть раздела 'Операционная деятельность' (между корректировками прибыли и налоговыми платежами).",
    },
    "ASSET_PURCHASES": {
        "aliases_ru": ["Приобретение основных средств", "Капитальные вложения в ОС", "Покупка основных средств"],
        "aliases_en": ["Purchases of property, plant and equipment", "Additions to properties, plant and equipment", "Additions to PP&E", "PP&E acquisitions"],
        "description": "Денежные расходы на приобретение основных средств за период.",
        "hint": "ОДДС/примечание к investing activities: покупки/добавления PPE. Если отдельно есть Additions to properties, plant and equipment, бери эту строку; не подменяй total capital expenditures, если PPE раскрыто отдельно. Если сумма с НМА вместе — COMBINED_ASSET_PURCHASES.",
    },
    "INTANGIBLE_ASSET_PURCHASES": {
        "aliases_ru": ["Приобретение нематериальных активов", "Покупка НМА"],
        "aliases_en": ["Purchases of intangible assets", "Acquisitions of intangibles"],
        "description": "Денежные расходы на приобретение нематериальных активов за период.",
        "hint": "ОДДС, раздел 'Инвестиционная деятельность' (отдельной строкой от ОС). Если в отчёте объединено с ОС — null, см. COMBINED_ASSET_PURCHASES.",
    },
    "COMBINED_ASSET_PURCHASES": {
        "aliases_ru": ["Приобретение основных средств и НМА", "Покупка ОС и НМА", "Приобретение основных средств и нематериальных активов"],
        "aliases_en": ["Purchases of PP&E and intangible assets"],
        "description": "Объединённая статья денежных расходов на приобретение ОС и НМА (когда в отчёте они не разделены).",
        "hint": "ОДДС, раздел 'Инвестиционная деятельность'. Заполнять только если ASSET_PURCHASES и INTANGIBLE_ASSET_PURCHASES объединены в одну строку.",
    },
    "ASSET_SALES_PROCEEDS": {
        "aliases_ru": ["Поступления от продажи ОС и НМА", "Поступления от выбытия основных средств", "Поступления от продажи внеоборотных активов"],
        "aliases_en": ["Proceeds from sale of PP&E", "Disposal of fixed assets", "Proceeds from divestitures, net", "Proceeds from dispositions"],
        "description": "Денежные поступления от продажи внеоборотных активов за период.",
        "hint": "ОДДС, раздел 'Инвестиционная деятельность'. В US GAAP также может называться Proceeds from divestitures, net или Proceeds from dispositions.",
    },
    "CAPEX": {
        "aliases_ru": ["Капитальные затраты", "CAPEX", "Капвложения"],
        "aliases_en": ["Capital expenditure", "CAPEX", "Capex", "Capital expenditures", "Payments to acquire property, plant and equipment"],
        "description": "Совокупные капитальные затраты — приобретение ОС + НМА за вычетом продажи.",
        "hint": "ОДДС или MD&A, раздел инвестиционной деятельности / capital expenditures. Если компания раскрывает итоговый CapEx/capital expenditures напрямую, бери раскрытый итог и все его колонки периодов, включая 3M/Q и 6M/YTD. Иначе используй приобретение ОС плюс НМА. Сохраняй знак/скобки как в источнике; не заменяй CapEx одной строкой покупки ОС, если есть отдельный итоговый CapEx или отдельная покупка НМА.",
    },
    "CAPITALIZED_INTEREST": {
        "aliases_ru": ["Капитализированные проценты", "Капитализированные затраты по займам", "Капитализированные и уплаченные проценты"],
        "aliases_en": ["Capitalized interest", "Capitalized borrowing costs"],
        "description": "Проценты по займам, капитализированные в стоимость квалифицируемых активов.",
        "hint": "Примечание 'Кредиты и займы' или 'Затраты по займам'. Также может быть в Примечании 'Основные средства'.",
    },
    "INTEREST_PAID": {
        "aliases_ru": ["Уплаченные проценты", "Проценты уплаченные", "Проценты по кредитам уплаченные", "Проценты, уплаченные по кредитам и займам"],
        "aliases_en": ["Interest paid", "Interest payments"],
        "description": "Денежные выплаты процентов по кредитам и займам за период.",
        "hint": "ОДДС, раздел 'Финансовая деятельность' (или иногда 'Операционная' по выбору учётной политики).",
    },
    "INTEREST_PAID_LEASE_LIABILITIES": {
        "aliases_ru": ["Уплаченные проценты по лизингу", "Проценты уплаченные по обязательствам по аренде", "Процентные расходы по обязательствам по аренде"],
        "aliases_en": ["Interest paid on lease liabilities"],
        "description": "Процентная составляющая лизинговых платежей за период.",
        "hint": "ОДДС, раздел 'Финансовая деятельность'. Также в Примечании 'IFRS 16' / 'Аренда'.",
    },
    "INTEREST_RECEIVED": {
        "aliases_ru": ["Полученные проценты", "Проценты полученные"],
        "aliases_en": ["Interest received"],
        "description": "Денежные поступления процентов от размещений и финансовых активов.",
        "hint": "ОДДС, чаще в разделе 'Инвестиционная деятельность' (или 'Операционная').",
    },
    "PRINCIPAL_REPAYMENT_OF_LEASE_LIABILITIES": {
        "aliases_ru": ["Погашение обязательств по аренде", "Погашение основной части обязательств по аренде", "Платежи в погашение арендных обязательств"],
        "aliases_en": ["Principal repayment of lease liabilities", "Lease principal payments"],
        "description": "Основная часть лизинговых платежей (без процентов) за период.",
        "hint": "ОДДС, раздел 'Финансовая деятельность'. Не путать с INTEREST_PAID_LEASE_LIABILITIES.",
    },
    "SUBSIDIARY_ACQUISITIONS": {
        "aliases_ru": ["Приобретение дочерних организаций", "Приобретение дочерних компаний", "Приобретение бизнеса"],
        "aliases_en": ["Acquisitions of subsidiaries", "Business combinations"],
        "description": "Денежные расходы на приобретение дочерних компаний (за вычетом приобретённых денежных средств).",
        "hint": "ОДДС, раздел 'Инвестиционная деятельность'. Включай строки денежных оттоков на приобретение бизнесов/дочерних организаций, включая приобретения у связанных сторон. Не бери покупки/выкуп неконтролирующих долей без приобретения бизнеса. Сохраняй знак/скобки как в источнике.",
    },
    "ASSOCIATE_INCOMES": {
        "aliases_ru": ["Доходы от ассоциированных компаний", "Доля в прибыли ассоциированных компаний"],
        "aliases_en": ["Equity income in investee", "Equity income from investees", "Share of profit of associates"],
        "description": "Доля компании в прибыли/убытке ассоциированных компаний или инвестиций по методу долевого участия.",
        "hint": "P&L или примечания equity method/investee. Не путать с proceeds from sale/disposal of subsidiaries.",
    },
    "DIVIDENDS_SHAREHOLDERS": {
        "aliases_ru": ["Дивиденды акционерам", "Дивиденды, выплаченные акционерам Компании", "Дивиденды акционерам материнской компании"],
        "aliases_en": ["Dividends paid to shareholders", "Dividends to equity holders"],
        "description": "Денежные выплаты дивидендов акционерам материнской компании за период.",
        "hint": "ОДДС, раздел 'Финансовая деятельность'. Бери строку выплат дивидендов акционерам и сохраняй знак/скобки как в источнике.",
    },
    "DIVIDENDS_PAID_NONCONTROLLING_INTERESTS": {
        "aliases_ru": ["Дивиденды держателям НКД", "Дивиденды, выплаченные неконтролирующим акционерам", "Дивиденды держателям неконтролирующих долей"],
        "aliases_en": ["Dividends paid to non-controlling interests", "Dividends to NCI"],
        "description": "Денежные выплаты дивидендов держателям неконтролирующих долей дочерних компаний.",
        "hint": "ОДДС, раздел 'Финансовая деятельность' (отдельной строкой от дивидендов акционерам).",
    },
    "DIVIDENDS_RECEIVED": {
        "aliases_ru": ["Дивиденды полученные", "Полученные дивиденды", "Дивиденды от ассоциированных компаний"],
        "aliases_en": ["Dividends received"],
        "description": "Денежные поступления дивидендов от ассоциированных и прочих компаний.",
        "hint": "ОДДС, раздел 'Инвестиционная деятельность'.",
    },
    "SHARES_BUYBACK": {
        "aliases_ru": ["Выкуп собственных акций", "Buyback акций", "Приобретение собственных акций"],
        "aliases_en": ["Share buyback", "Treasury share purchases", "Repurchase of treasury shares"],
        "description": "Денежные расходы на выкуп собственных акций у акционеров.",
        "hint": "ОДДС, раздел 'Финансовая деятельность'. Бери строку выкупа/покупки собственных акций и сохраняй знак/скобки как в источнике.",
    },

    # ─── OFF-BALANCE / CONTINGENT ────────────────────────────────────────
    "COMMITTED_CREDIT_FACILITIES": {
        "aliases_ru": ["Неиспользованные кредитные линии", "Открытые кредитные линии", "Резервные обязательства банков"],
        "aliases_en": ["Committed credit facilities", "Undrawn credit lines"],
        "description": "Сумма доступных, но неиспользованных кредитных линий.",
        "hint": "Note: for narrative/text disclosures without a table header, preserve the amount and scale exactly as written in the sentence. Example: if the source says \"$3 billion available under a revolving credit agreement\", return value \"3\" (or \"3.0\") and unit_text \"$ billion\"/\"billion\"; do not convert it to \"3,000\" and do not borrow \"in millions\" from nearby tables. Do not create a comparative period unless the facility amount is explicitly disclosed for that date.",
    },
    "FUTURE_CAPEX_LIABILITIES": {
        "aliases_ru": ["Обязательства по капвложениям", "Договорные обязательства по капитальному строительству", "Заключённые контракты по капитальным затратам"],
        "aliases_en": ["Capital commitments", "Future capex commitments"],
        "description": "Договорные обязательства по будущим капитальным затратам, ещё не отражённые в балансе.",
        "hint": "Примечание 'Условные обязательства' / 'Капитальные обязательства' / 'Договорные обязательства'.",
    },
    "GUARANTEES": {
        "aliases_ru": ["Гарантии и поручительства", "Выданные гарантии", "Поручительства"],
        "aliases_en": ["Guarantees", "Guarantees issued", "Financial guarantees"],
        "description": "Сумма выданных гарантий и поручительств за третьих лиц.",
        "hint": "Примечание 'Условные обязательства' / 'Гарантии и поручительства'. Внебалансовая статья.",
    },
    "MODIFIED_AUDITORS_OPINION": {
        "aliases_ru": ["Модифицированное мнение аудитора", "Аудиторское заключение с оговоркой", "Отказ от выражения мнения"],
        "aliases_en": ["Modified auditor's opinion", "Qualified opinion"],
        "description": "Текстовая отметка: было ли в аудиторском заключении модифицированное мнение (да / нет).",
        "hint": "Аудиторское заключение независимого аудитора (обычно в начале отчёта). Если 'Мнение не модифицировано' — значение = 0 (нет). Если есть оговорка / отказ / отрицательное мнение — 1 (да).",
    },

    # ─── BANK-SPECIFIC ───────────────────────────────────────────────────
    "TOTAL_LOANS_TO_CLIENTS": {
        "aliases_ru": ["Кредиты клиентам", "Кредиты и авансы клиентам", "Кредитный портфель", "Ссудная задолженность"],
        "aliases_en": ["Loans to customers", "Total loans to clients", "Loan book", "Customer loans"],
        "description": "Совокупный объём кредитов, выданных клиентам банка (за вычетом резервов ECL).",
        "hint": "Балансовый отчёт банка, ключевой актив. Детализация в Примечании 'Кредиты клиентам'.",
    },
    "CORPORATE_LOANS": {
        "aliases_ru": ["Корпоративные кредиты", "Кредиты юридическим лицам", "Кредиты корпоративным клиентам"],
        "aliases_en": ["Corporate loans", "Loans to corporate clients"],
        "description": "Кредиты, выданные юридическим лицам.",
        "hint": "Примечание 'Кредиты клиентам' — разбивка по сегментам клиентов.",
    },
    "INDIVIDUALS_LOANS": {
        "aliases_ru": ["Розничные кредиты", "Кредиты физическим лицам", "Кредиты розничным клиентам"],
        "aliases_en": ["Retail loans", "Loans to individuals", "Consumer loans"],
        "description": "Кредиты, выданные физическим лицам (ипотека, потребкредиты, автокредиты).",
        "hint": "Примечание 'Кредиты клиентам' — разбивка по сегментам.",
    },
    "INTERBANK_LOANS": {
        "aliases_ru": ["Межбанковские кредиты", "Средства, выданные банкам", "Кредиты банкам"],
        "aliases_en": ["Interbank loans", "Loans to banks", "Due from banks"],
        "description": "Кредиты, выданные другим банкам.",
        "hint": "Балансовый отчёт банка. Может быть отдельной строкой от кредитов клиентам.",
    },
    "INTERBANK_DEPOSITS": {
        "aliases_ru": ["Средства в банках", "Депозиты в банках", "Размещения в других банках"],
        "aliases_en": ["Interbank deposits", "Due from banks", "Placements with banks"],
        "description": "Денежные средства, размещённые в других банках.",
        "hint": "Балансовый отчёт банка, актив.",
    },
    "CASH_AND_CASH_BALANCES_WITH_CB": {
        "aliases_ru": ["Обязательные резервы на счетах в центральных банках", "Денежные средства и счета в Банке России", "Остатки на счетах в ЦБ"],
        "aliases_en": ["Cash and balances with central bank", "Cash and balances with the CBR"],
        "description": "Обязательные резервы и средства банка на счетах в центральных банках.",
        "hint": "Балансовый отчёт банка, актив. Часто первая строка раздела активов.",
    },
    "FUNDS_FROM_CENTRAL_BANK": {
        "aliases_ru": ["Средства, предоставленные ЦБ", "Кредиты Центрального Банка", "Финансирование от ЦБ"],
        "aliases_en": ["Funds from central bank", "Borrowings from central bank"],
        "description": "Кредиты и депозиты, полученные банком от центрального банка.",
        "hint": "Балансовый отчёт банка, обязательство. Также в Примечании 'Привлечённые средства'.",
    },
    "LOANS_STAGE1_GROSS": {
        "aliases_ru": ["Кредиты Stage 1 (валовые)", "Кредиты на этапе 1", "Кредиты без признаков кредитного риска"],
        "aliases_en": ["Stage 1 loans gross", "Stage 1 loans"],
        "description": "Валовая балансовая стоимость кредитов на стадии 1 (12-month ECL) по IFRS 9.",
        "hint": "Примечание 'Кредиты клиентам' — таблица распределения кредитов по стадиям ECL.",
    },
    "LOANS_STAGE2_GROSS": {
        "aliases_ru": ["Кредиты Stage 2 (валовые)", "Кредиты на этапе 2"],
        "aliases_en": ["Stage 2 loans gross", "Stage 2 loans"],
        "description": "Валовая балансовая стоимость кредитов на стадии 2 (lifetime ECL без обесценения) по IFRS 9.",
        "hint": "Примечание 'Кредиты клиентам' — таблица распределения по стадиям.",
    },
    "LOANS_STAGE3_GROSS": {
        "aliases_ru": ["Кредиты Stage 3 (валовые)", "Кредиты на этапе 3", "Обесцененные кредиты", "NPL"],
        "aliases_en": ["Stage 3 loans gross", "Stage 3 loans", "Credit-impaired loans"],
        "description": "Валовая балансовая стоимость обесцененных кредитов (стадия 3, lifetime ECL) по IFRS 9.",
        "hint": "Примечание 'Кредиты клиентам' — таблица распределения по стадиям.",
    },
    "ECL_ALLOWANCE_STAGE1": {
        "aliases_ru": ["Резерв ECL Stage 1", "Резерв на 12-месячные ожидаемые убытки", "Оценочный резерв под ожидаемые кредитные убытки Stage 1"],
        "aliases_en": ["ECL allowance Stage 1", "12-month ECL"],
        "description": "Резерв под ожидаемые кредитные убытки на стадии 1 (12 месяцев).",
        "hint": "Примечание 'Кредиты клиентам' / 'Резервы под обесценение'.",
    },
    "ECL_ALLOWANCE_STAGE2": {
        "aliases_ru": ["Резерв ECL Stage 2", "Резерв на ожидаемые кредитные убытки за весь срок Stage 2"],
        "aliases_en": ["ECL allowance Stage 2", "Lifetime ECL Stage 2"],
        "description": "Резерв под ожидаемые кредитные убытки за весь срок жизни (стадия 2).",
        "hint": "Примечание 'Кредиты клиентам' / 'Резервы под обесценение'.",
    },
    "ECL_ALLOWANCE_STAGE3": {
        "aliases_ru": ["Резерв ECL Stage 3", "Резерв на обесценение кредитов Stage 3", "Резерв под обесценение проблемных кредитов"],
        "aliases_en": ["ECL allowance Stage 3", "Lifetime ECL Stage 3", "Impairment allowance"],
        "description": "Резерв под ожидаемые кредитные убытки за весь срок жизни (стадия 3, обесцененные кредиты).",
        "hint": "Примечание 'Кредиты клиентам' / 'Резервы под обесценение'.",
    },
    "TOTAL_CREDIT_ALLOWANCE": {
        "aliases_ru": [
            "Резерв под обесценение кредитов",
            "Оценочный резерв под кредитные убытки",
            "ОКУ под обесценение кредитов и авансов клиентам",
        ],
        "aliases_en": ["Allowance for credit losses", "ECL allowance on loans", "Impairment allowance on loans"],
        "description": "Балансовый остаток оценочного резерва / allowance под кредитные убытки по кредитам и авансам клиентам на отчётную дату.",
        "hint": "Примечание 'Кредиты и авансы клиентам' / 'Резервы под обесценение': бери closing balance/остаток оценочного резерва по кредитам на конец периода. Для этой метрики нужен allowance на дату, а не P&L-поток создания/восстановления ECL; поток — это CHANGE_IN_EXPECTED_CREDIT_LOSSES. Если строка называется 'резерв/оценочный резерв/allowance' и относится к кредитам клиентам, возвращай её как отрицательную contra-asset величину, даже если OCR потерял скобки.",
    },
    "CHANGE_IN_EXPECTED_CREDIT_LOSSES": {
        "aliases_ru": ["Изменение резервов под ожидаемые кредитные убытки", "Изменение ECL по ценным бумагам", "Изменение оценочных резервов ECL"],
        "aliases_en": ["Change in expected credit losses", "ECL change"],
        "description": "Изменение оценочных резервов ECL за период (по всем финансовым активам, не только кредитам).",
        "hint": "P&L банка или таблица движения резервов. Нужен чистый поток создания/восстановления ECL за период, не остаток allowance на дату. Если строка означает создание/расходы/формирование резервов, возвращай отрицательное значение; если восстановление/reversal — положительное. В interim-отчетах с колонками 3M/Q и 6M/YTD возвращай обе группы периодов, не только YTD.",
    },
    "CLIENT_DEPOSITS": {
        "aliases_ru": ["Средства клиентов", "Депозиты клиентов", "Счета и депозиты клиентов", "Привлечённые средства клиентов"],
        "aliases_en": ["Customer deposits", "Client deposits", "Deposits from customers"],
        "description": "Совокупный объём средств клиентов на счетах и депозитах банка.",
        "hint": "Балансовый отчёт банка, ключевое обязательство. Детализация в Примечании 'Средства клиентов'.",
    },
    "CORPORATE_DEPOSITS": {
        "aliases_ru": ["Корпоративные депозиты", "Депозиты юридических лиц", "Средства корпоративных клиентов"],
        "aliases_en": ["Corporate deposits", "Deposits from corporate clients"],
        "description": "Депозиты юридических лиц.",
        "hint": "Примечание 'Средства клиентов' — разбивка по сегментам.",
    },
    "INDIVIDUALS_DEPOSITS": {
        "aliases_ru": ["Депозиты физических лиц", "Розничные депозиты", "Средства физических лиц", "Вклады физических лиц"],
        "aliases_en": ["Retail deposits", "Individual deposits", "Deposits from individuals"],
        "description": "Депозиты и вклады физических лиц.",
        "hint": "Примечание 'Средства клиентов' — разбивка по сегментам.",
    },
    "GOVERNMENTAL_DEPOSITS": {
        "aliases_ru": ["Депозиты госорганов", "Средства бюджетных организаций"],
        "aliases_en": ["Governmental deposits"],
        "description": "Депозиты госорганов и муниципальных образований.",
        "hint": "Примечание 'Средства клиентов' — разбивка по сегментам.",
    },
    "DEBT_SECURITIES_ISSUED": {
        "aliases_ru": ["Выпущенные долговые ценные бумаги", "Облигации, выпущенные банком", "Векселя, выпущенные банком"],
        "aliases_en": ["Debt securities issued", "Issued bonds", "Notes issued"],
        "description": "Облигации и прочие долговые ценные бумаги, эмитированные банком.",
        "hint": "Балансовый отчёт банка, обязательство. Детализация в Примечании 'Долговые ценные бумаги'.",
    },
    "JUNIOR_DEBT": {
        "aliases_ru": ["Субординированный долг", "Субординированные обязательства", "Субординированные облигации"],
        "aliases_en": ["Subordinated debt", "Junior debt", "Subordinated liabilities"],
        "description": "Долг, имеющий более низкий приоритет погашения по сравнению с обычным долгом.",
        "hint": "Балансовый отчёт банка. Часто учитывается в составе Tier 2 капитала.",
    },
    "REPO_PLEDGED_SECURITIES": {
        "aliases_ru": ["Ценные бумаги, заложенные по сделкам репо", "Ценные бумаги в репо"],
        "aliases_en": ["Securities pledged under repurchase agreements", "Repo pledged securities"],
        "description": "Ценные бумаги, переданные в залог по сделкам репо.",
        "hint": "Балансовый отчёт или Примечание 'Финансовые инструменты'. Внебалансовая или балансовая статья в зависимости от учётной политики.",
    },
    "SECURITIES_AMORTISED_COST": {
        "aliases_ru": ["Финансовые активы, оцениваемые по амортизированной стоимости", "Долговые инструменты по амортизированной стоимости"],
        "aliases_en": ["Securities at amortised cost", "Financial assets at amortised cost"],
        "description": "Долговые ценные бумаги, удерживаемые до погашения, по амортизированной стоимости.",
        "hint": "Балансовый отчёт банка. Категория измерения по IFRS 9.",
    },
    "SECURITIES_FVOCI": {
        "aliases_ru": ["Финансовые активы, оцениваемые по справедливой стоимости через прочий совокупный доход", "Финансовые активы FVOCI"],
        "aliases_en": ["Securities FVOCI", "Financial assets at fair value through OCI"],
        "description": "Финансовые активы, оцениваемые по справедливой стоимости через ПСД (по IFRS 9).",
        "hint": "Балансовый отчёт банка. Категория измерения по IFRS 9.",
    },
    "SECURITIES_FVPL": {
        "aliases_ru": ["Финансовые активы, оцениваемые по справедливой стоимости через прибыль или убыток", "Финансовые активы FVPL", "Финансовые активы, оцениваемые по СС через прибыль и убытки"],
        "aliases_en": ["Securities FVPL", "Financial assets at fair value through P&L"],
        "description": "Финансовые активы, оцениваемые по справедливой стоимости через ОПУ.",
        "hint": "Балансовый отчёт банка. Категория измерения по IFRS 9.",
    },
    "LEASE_LIABILITIES_BANKS": {
        "aliases_ru": ["Обязательства по аренде (банк)", "Лизинговые обязательства банка"],
        "aliases_en": ["Lease liabilities"],
        "description": "Обязательства по аренде по IFRS 16 у банка (общая сумма, без разбивки на долго/краткосрочные).",
        "hint": "Балансовый отчёт банка, обязательство. Также в Примечании 'IFRS 16'.",
    },
    "INTEREST_INCOME_ER": {
        "aliases_ru": ["Процентные доходы по эффективной ставке", "Процентные доходы, рассчитанные по методу эффективной процентной ставки"],
        "aliases_en": ["Interest income at effective rate", "Interest income calculated using EIR"],
        "description": "Процентные доходы, рассчитанные по методу эффективной процентной ставки.",
        "hint": "P&L банка, верхняя строка раздела процентных доходов. Детализация в Примечании 'Процентные доходы и расходы'.",
    },
    "INTEREST_EXPENSE_ER": {
        "aliases_ru": ["Процентные расходы по эффективной ставке", "Процентные расходы, рассчитанные по методу эффективной процентной ставки"],
        "aliases_en": ["Interest expense at effective rate"],
        "description": "Процентные расходы, рассчитанные по методу эффективной процентной ставки.",
        "hint": "P&L банка, после процентных доходов. Бери строку процентных расходов и сохраняй знак/скобки как в источнике.",
    },
    "NET_INTEREST_INCOME": {
        "aliases_ru": ["Чистый процентный доход", "ЧПД", "Чистый процентный доход после резерва"],
        "aliases_en": ["Net interest income", "NII"],
        "description": "Процентные доходы минус процентные расходы.",
        "hint": "P&L банка, ключевая строка после процентных доходов и расходов. В interim-отчетах с колонками 3M/Q и 6M/YTD возвращай обе группы периодов, не только YTD.",
    },
    "FEE_AND_COMMISSION_INCOME": {
        "aliases_ru": ["Комиссионные доходы", "Доходы по комиссиям", "Доходы от операций по комиссионному вознаграждению"],
        "aliases_en": ["Fee and commission income", "Commission income"],
        "description": "Доходы от комиссионных и расчётно-кассовых операций.",
        "hint": "P&L банка. Детализация в Примечании 'Комиссионные доходы и расходы'.",
    },
    "FEE_AND_COMMISSION_EXPENSE": {
        "aliases_ru": ["Комиссионные расходы", "Расходы по комиссиям"],
        "aliases_en": ["Fee and commission expense", "Commission expense"],
        "description": "Расходы по комиссионным операциям (в т.ч. платежи системам, контрагентам).",
        "hint": "P&L банка, после комиссионных доходов. Бери строку комиссионных расходов и сохраняй знак/скобки как в источнике.",
    },
    "NET_COMMISSION_INCOME": {
        "aliases_ru": ["Чистый комиссионный доход", "ЧКД"],
        "aliases_en": ["Net fee and commission income", "Net commission income"],
        "description": "Комиссионные доходы минус комиссионные расходы.",
        "hint": "P&L банка. Если в отчёте отдельно не раскрыт — null (мы досчитаем). В interim-отчетах с колонками 3M/Q и 6M/YTD возвращай обе группы периодов, не только YTD.",
    },
    "CURRENCY_METALS_INCOME": {
        "aliases_ru": ["Доходы от валютных и торговых операций", "Чистая прибыль от операций с иностранной валютой и драгметаллами"],
        "aliases_en": ["FX and trading income", "Net gains from currency and trading operations"],
        "description": "Чистый результат по операциям с иностранной валютой и драгоценными металлами.",
        "hint": "P&L банка, в составе непроцентных доходов.",
    },
    "OTHER_OPERATING_INCOME": {
        "aliases_ru": ["Прочие операционные доходы", "Прочие чистые операционные доходы"],
        "aliases_en": ["Other operating income"],
        "description": "Прочие доходы от операционной деятельности банка.",
        "hint": "P&L банка, в составе непроцентных доходов.",
    },
    "NON_INTEREST_INCOME_BANKS": {
        "aliases_ru": ["Непроцентный доход", "Непроцентные доходы (банк)"],
        "aliases_en": ["Non-interest income"],
        "description": "Совокупные непроцентные доходы (комиссии + торговые + прочие).",
        "hint": "P&L банка, в части после ЧПД. Если отдельной строкой нет — null.",
    },
    "OPERATING_INCOME_BANKS": {
        "aliases_ru": ["Операционные доходы (банк)", "Операционная прибыль до резервов"],
        "aliases_en": ["Operating income (bank)", "Total operating income"],
        "description": "Совокупные операционные доходы банка (ЧПД + комиссии + прочие, до создания резервов).",
        "hint": "P&L банка, после ЧПД и непроцентных доходов.",
    },
    "OPERATING_EXPENSES_BANKS": {
        "aliases_ru": ["Операционные расходы (банк)", "Прочие операционные расходы"],
        "aliases_en": ["Operating expenses (bank)", "Total operating expenses"],
        "description": "Совокупные операционные расходы банка (зарплаты, IT, аренда, прочее).",
        "hint": "P&L банка, после операционных доходов и резервов.",
    },
    "TOTAL_INVESTMENT_BANKING_FEES": {
        "aliases_ru": ["Доходы инвестиционного банкинга", "Комиссии инвестиционного банка"],
        "aliases_en": ["Investment banking fees", "IB fees"],
        "description": "Доходы от инвестиционно-банковской деятельности (M&A, размещения, andewriting).",
        "hint": "P&L банка с инвестбанковской деятельностью или Примечание 'Сегментный анализ'.",
    },
    "RISK_WEIGHTED_ASSETS": {
        "aliases_ru": ["Активы, взвешенные с учётом риска", "RWA"],
        "aliases_en": ["Risk-weighted assets", "RWA"],
        "description": "Активы, взвешенные по уровню риска для расчёта нормативов достаточности капитала.",
        "hint": "Примечание 'Достаточность капитала' / 'Управление капиталом'.",
    },
    "COMMON_EQUITY_TIER_ONE_CAPITAL": {
        "aliases_ru": ["Капитал первого уровня (CET1)", "Базовый капитал первого уровня"],
        "aliases_en": ["Common equity tier 1 capital", "CET1 capital"],
        "description": "Базовый капитал первого уровня по Базель III.",
        "hint": "Примечание 'Достаточность капитала' / 'Управление капиталом'.",
    },
    "TIER1_RATIO_DISCLOSED": {
        "aliases_ru": ["Норматив Tier 1 (раскрываемый)", "Достаточность капитала первого уровня", "Норматив достаточности капитала первого уровня"],
        "aliases_en": ["Tier 1 ratio", "Tier 1 capital ratio"],
        "description": "Раскрываемый банком норматив достаточности капитала первого уровня (в процентах).",
        "hint": "Примечание 'Достаточность капитала'. Единица — проценты.",
    },
    "CET1_RATIO_DISCLOSED": {
        "aliases_ru": ["Норматив CET1 (раскрываемый)", "Достаточность базового капитала", "Норматив достаточности базового капитала"],
        "aliases_en": ["CET1 ratio", "Common equity tier 1 ratio"],
        "description": "Раскрываемый банком норматив достаточности базового капитала первого уровня.",
        "hint": "Примечание 'Достаточность капитала'. Единица — проценты.",
    },
}

for _enum, _period_type in PERIOD_TYPE_ENRICHMENT.items():
    ENRICHMENT.setdefault(_enum, {})["period_type"] = _period_type


# Runtime `metrics.json` is the prompt source of truth, but it is generated
# from Excel + this enrichment file. Keep post-benchmark prompt learnings here
# too, so a future registry rebuild does not silently drop them.
_BENCHMARK_ENRICHMENT_UPDATES: dict[str, dict[str, list[str] | str]] = {
    "ACCOUNTS_RECEIVABLE": {
        "aliases_en": ["Trade accounts receivable", "Trade accounts receivable, net", "Accounts receivable, net"],
        "hint": "Оборотные активы баланса. Бери торговую дебиторскую задолженность / accounts receivable, net. Не включай предоплаты, НДС к возмещению и прочую дебиторскую задолженность, если они раскрыты отдельно.",
    },
    "ASSET_SALES_PROCEEDS": {
        "aliases_en": ["Proceeds from divestitures, net", "Proceeds from dispositions"],
        "hint": "ОДДС, раздел 'Инвестиционная деятельность'. Также может называться proceeds from divestitures/dispositions; это денежные поступления от продажи активов/бизнесов, не выручка.",
    },
    "BANK_DEPOSITS": {
        "aliases_ru": ["Краткосрочные финансовые вложения депозиты"],
        "aliases_en": ["Bank deposits", "Short-term deposits", "Time deposits"],
        "hint": "Оборотные активы баланса или примечание к денежным средствам/краткосрочным инвестициям. Если строка баланса 'краткосрочные финансовые вложения' раскрыта в примечании как банковские/time deposits, бери эту строку. Не путай с cash equivalents и marketable securities.",
    },
    "CAPEX": {
        "aliases_en": ["Capital expenditures", "Payments to acquire property, plant and equipment", "Capital expenditures total"],
        "hint": "ОДДС или MD&A, раздел инвестиционной деятельности / capital expenditures. Если компания раскрывает итоговый CapEx/capital expenditures напрямую, бери раскрытый итог и все его колонки периодов, включая 3M/Q и 6M/YTD. Иначе используй приобретение ОС плюс НМА. Сохраняй знак/скобки как в источнике; не заменяй CapEx одной строкой покупки ОС, если есть отдельный итоговый CapEx или отдельная покупка НМА.",
    },
    "CASH_EQUIVALENTS": {
        "aliases_ru": ["Денежные средства и их эквиваленты на конец периода"],
        "aliases_en": ["Cash and cash equivalents at end of period"],
        "hint": "Оборотные активы баланса или последняя строка ОДДС. Возвращай все явно раскрытые даты cash and cash equivalents at end of period, включая сравнительные даты, которых нет в балансе. Не путай с bank deposits/time deposits и marketable securities.",
    },
    "COSTS_EXPENSES": {
        "aliases_en": ["Cost of products", "Cost of services"],
        "hint": "P&L: прямая себестоимость обычно идёт сразу после выручки и перед gross profit. В разных отраслях может называться по деятельности компании, но должна оставаться прямыми затратами на проданные товары/услуги. Если прямые затраты раскрыты несколькими строками (например cost of services/products), суммируй только эти строки. Не бери subtotal/total, который включает amortization, SG&A, R&D или operating expenses; не рассчитывай из выручки и operating income.",
    },
    "DEPRECIATION_AMORTIZATION": {
        "aliases_en": ["Depreciation and amortization", "Amortization of acquisition-related intangibles"],
        "hint": "ОДДС/примечания: если рядом раскрыты Depreciation and amortization и отдельная amortization of acquisition-related intangibles, общая D&A — их сумма. Не путай с ROU amortization.",
    },
    "EBITDA": {
        "aliases_ru": ["EBITDA по сегменту"],
        "aliases_en": ["Segment EBITDA", "EBITDA total"],
        "hint": "Сегментная отчётность или MD&A. Для main metric бери итог/Total по EBITDA, не отдельный сегмент, не elimination-only строку и не EBITDA before IFRS 16/adjusted subtotal, если раскрыт консолидированный total. Если строка 'EBITDA по сегменту' содержит сегменты, исключения/eliminations и затем итоговые значения в последних колонках или в конце строки, бери эти итоговые значения как main EBITDA. Возвращай null только если явной строки EBITDA/OIBDA нет.",
    },
    "EPS": {
        "aliases_en": ["Basic earnings per share"],
        "hint": "P&L: строка Basic earnings per share после net income. Бери Basic, не Diluted; значение может быть дробным и обычно имеет единицу валюта/share, не millions.",
    },
    "GROSS_PROFIT": {
        "aliases_en": ["Gross profit", "Subtotal after cost of sales"],
        "hint": "P&L, строка после себестоимости. Если строка Gross profit не подписана, но есть очевидный subtotal после прямых costs of sales/products/services и перед operating expenses, можно брать этот subtotal. Не выводи gross profit, если есть только operating income без раскрытой себестоимости.",
    },
    "INCOME_TAX": {
        "aliases_en": ["Income tax provision (benefit)"],
        "hint": "P&L: итоговая строка income tax provision/benefit перед net income. Не бери cash paid for taxes и не заменяй итоговую строку отдельными current/deferred tax components.",
    },
    "ASSOCIATE_INCOMES": {
        "name_ru": "Доходы от ассоциированных компаний",
        "aliases_ru": ["Доходы от ассоциированных компаний", "Доля в прибыли ассоциированных компаний"],
        "aliases_en": ["Equity income in investee", "Equity income from investees", "Share of profit of associates"],
        "hint": "P&L или примечания equity method/investee. Не путать с proceeds from sale/disposal of subsidiaries.",
    },
    "OPERATING_PROFIT": {
        "aliases_en": ["Income from operations"],
    },
    "OPERATING_EXPENSES": {
        "aliases_en": ["General and administrative expense", "Marketing, general and administrative", "G&A"],
        "hint": "P&L, после валовой прибыли. Приоритет: total operating expenses / operating, selling, general and administrative expenses. Если отдельными строками раскрыты Operating expenses и SG&A/G&A без общего subtotal, суммируй эти operating-expense строки. Не бери себестоимость и total costs and expenses.",
    },
    "OTHER_ASSETS": {
        "aliases_en": ["Prepaid expenses and other assets", "Other current assets", "Other long-term assets"],
        "hint": "Балансовый отчёт: широкая метрика other assets включает current/prepaid and other assets плюс other non-current assets, если они раскрыты отдельно. Не бери изменение этой строки из ОДДС.",
    },
    "OTHER_LIABILITIES": {
        "aliases_en": ["Accrued and other liabilities", "Other current liabilities", "Other long-term liabilities"],
        "hint": "Балансовый отчёт: широкая метрика other liabilities включает accrued/other current liabilities плюс other long-term liabilities, если они раскрыты отдельно. Не бери cash-flow change in accrued liabilities.",
    },
    "RETAINED_EARNINGS": {
        "aliases_en": ["Accumulated deficit", "Retained earnings balance"],
        "hint": "Раздел капитала: retained earnings / accumulated deficit на конец периода. Не путай с net income за период.",
    },
    "ROU_DEPRECIATION": {
        "aliases_en": ["Amortization of operating lease right-of-use assets"],
        "hint": "Примечание 'Аренда' / lease note или ОДДС: amortization/depreciation of right-of-use assets. Не путай с общей depreciation and amortization.",
    },
    "SECURITIES_ASSETS": {
        "aliases_en": ["Marketable securities", "Short-term investments", "Available-for-sale securities"],
        "hint": "Балансовый отчёт или примечание к инвестициям: ценные бумаги / marketable securities / short-term investments на дату баланса. Не путай с cash equivalents и bank deposits/time deposits.",
    },
    "SHARE_CAPITAL": {
        "aliases_en": ["Common stock", "Capital stock"],
        "hint": "Раздел капитала: share capital/common stock/capital stock на конец периода. Не путай с total equity.",
    },
    "SHARE_PREMIUM": {
        "aliases_en": ["Additional paid-in capital", "APIC"],
        "hint": "Раздел капитала: share premium/additional paid-in capital на конец периода.",
    },
    "TOTAL_ASSOCIATE_INVESTMENTS": {
        "aliases_en": ["Equity method investment", "Investments in associates", "Investments: equity method", "Equity method investments"],
        "hint": "Внеоборотные активы баланса: investments in associates/JVs/equity method investments на дату баланса. Не путай с purchases of strategic investments в ОДДС.",
    },
    "TREASURY_SHARES": {
        "aliases_en": ["Treasury stock"],
        "hint": "Раздел капитала: treasury shares/treasury stock на конец периода. Обычно уменьшает капитал; не путай с buyback cash-flow за период.",
    },
}

# `tests/build_metric_registry.py` uses this explicit allow-list to reapply
# benchmark-derived prompt fields even when the Excel registry already has an
# older non-empty hint/alias cell. Other enrichment entries keep Excel priority.
FORCE_ENRICHMENT_FIELDS = _BENCHMARK_ENRICHMENT_UPDATES


def _merge_enrichment_updates() -> None:
    def merge_unique(existing: list[str], additions: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for value in [*(existing or []), *(additions or [])]:
            if not value:
                continue
            key = str(value).strip().lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(value)
        return out

    for enum, update in _BENCHMARK_ENRICHMENT_UPDATES.items():
        target = ENRICHMENT.setdefault(enum, {})
        for key in ("aliases_ru", "aliases_en"):
            values = update.get(key)
            if isinstance(values, list):
                target[key] = merge_unique(target.get(key, []), values)
        hint = update.get("hint")
        if isinstance(hint, str):
            target["hint"] = hint
        for key in ("name_ru", "name_en"):
            value = update.get(key)
            if isinstance(value, str):
                target[key] = value


_merge_enrichment_updates()
