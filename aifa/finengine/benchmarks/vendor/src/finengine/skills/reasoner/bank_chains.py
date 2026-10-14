"""Bank-specific reasoning chains for AIFA DEV.

The upstream `financier-reasoner` library (`Репа в репе/financier-reasoner/cot/prompts.py`)
ships only NF-company chains — EBITDA, ROA, CapEx, Cash Conversion Cycle,
DuPont, ICR — semantically nonsensical when applied to bank reports.

This module mirrors the upstream style (sequential `StepDescription` lists
with the same fields and prose register) but covers the canonical
bank-analysis dimensions:

  - NIM_ANALYSIS:        Net Interest Income, NIM trend, Net Interest Spread.
  - CREDIT_QUALITY:      Stage 1/2/3 loans, ECL coverage, NPL ratio, cost of risk.
  - CAPITAL_ADEQUACY:    CET1 / Tier1 ratios, RWA growth, capital buffers.
  - DEPOSITS_FUNDING:    Loan-to-deposit, retail vs corporate deposit mix.
  - BANK_PROFITABILITY:  ROE / ROA computed bank-style (no DuPont turnover).

Metric enums match the registry in `bulk_extractor.EXTRACTABLE_BANK`.

`STEP_CONFIG_BANK` mirrors the shape of upstream `STEP_CONFIG`
(`(key, chain, title)` triples) so `runner.run_full_synthesis` can take
it via the `chains=` argument without further refactoring.
"""
from __future__ import annotations

from mmar_carl import StepDescription


# ─── Цепочка 1 ────────────────────────────────────────────────────────────
# NIM (Net Interest Margin) — главный индикатор маржинальности банка.
# Заменяет EBITDA для нефинансовых компаний.
NIM_ANALYSIS = [
    StepDescription(
        number=1,
        title="Динамика чистого процентного дохода",
        aim="Выяснить, как изменился NET_INTEREST_INCOME (NII) год к году и достаточен ли темп роста",
        reasoning_questions="Насколько изменился NII за период? Это органический рост (выше инфляции) или сокращение?",
        dependencies=[],
        step_context_queries=["NET_INTEREST_INCOME", "INTEREST_INCOME", "INTEREST_EXPENSE"],
        stage_action="Рассчитать темп прироста NII год-к-году и сравнить с темпом инфляции (~5-8%)",
        example_reasoning="""
        1) NII растёт быстрее инфляции — банк увеличивает реальный процентный доход (положительный сигнал);
        2) NII растёт ниже инфляции — реальный доход падает (негативный сигнал);
        3) NII упал — критический сигнал, дальше разобрать INTEREST_INCOME vs INTEREST_EXPENSE: что именно изменилось.
           Если процентный доход падает — банк потерял часть портфеля или ставки;
           если процентные расходы выросли быстрее доходов — рост стоимости фондирования.
        """,
    ),
    StepDescription(
        number=2,
        title="Net Interest Margin (NIM)",
        aim="Понять маржинальность банка по процентному доходу относительно работающих активов",
        reasoning_questions="Какая Net Interest Margin = NII / средние работающие активы? Растёт или падает?",
        dependencies=[1],
        step_context_queries=["NET_INTEREST_INCOME", "TOTAL_ASSETS", "LOANS_TO_CUSTOMERS_NET"],
        stage_action="NIM = NET_INTEREST_INCOME / средние активы (упрощённо: ((TOTAL_ASSETS_t + TOTAL_ASSETS_{t-1}) / 2)). Для банков нормальный диапазон 2-6%",
        example_reasoning="""
        1) NIM 4-6% — высокомаржинальный банк, скорее всего розничный фокус (более дорогие кредиты населению);
        2) NIM 2-4% — стандарт для крупных универсальных банков;
        3) NIM <2% — низкая маржа, банк под давлением (растущая стоимость фондирования или ценовая конкуренция);
        4) Сравнить NIM с прошлым периодом — расширение/сужение спреда — главный фактор прибыли.
        """,
    ),
]


# ─── Цепочка 2 ────────────────────────────────────────────────────────────
# Качество кредитного портфеля — Stage 1/2/3 (МСФО 9), ECL coverage, NPL.
CREDIT_QUALITY = [
    StepDescription(
        number=1,
        title="Структура портфеля по стадиям обесценения (МСФО 9)",
        aim="Оценить долю проблемных кредитов и динамику миграции стадий",
        reasoning_questions="Какова структура валовых кредитов по Stage 1/2/3? Как она изменилась?",
        dependencies=[],
        step_context_queries=[
            "LOANS_STAGE1_GROSS", "LOANS_STAGE2_GROSS", "LOANS_STAGE3_GROSS",
            "LOANS_TO_CUSTOMERS_GROSS",
        ],
        stage_action="Долю Stage 3 = LOANS_STAGE3_GROSS / LOANS_TO_CUSTOMERS_GROSS. Аналогично Stage 1 и 2. Сравнить с прошлым периодом",
        example_reasoning="""
        1) Stage 1 (performing) обычно 85-95% — нормально;
        2) Рост доли Stage 2 — раннее предупреждение, банк отметил «значительное увеличение кредитного риска» (SICR);
        3) Рост доли Stage 3 (credit-impaired) — реализованный credit risk, прямой сигнал ухудшения качества активов;
        4) Миграция Stage 2 → Stage 3 за период — показатель скорости ухудшения: высокая миграция = негативный тренд.
        """,
    ),
    StepDescription(
        number=2,
        title="Покрытие резервами по стадиям (ECL coverage)",
        aim="Понять, насколько банк зарезервировал ожидаемые кредитные потери",
        reasoning_questions="Каков coverage ratio по Stage 3? Достаточно ли резервов для покрытия «плохих» кредитов?",
        dependencies=[1],
        step_context_queries=[
            "LOANS_STAGE1_GROSS", "LOANS_STAGE2_GROSS", "LOANS_STAGE3_GROSS",
            "ECL_ALLOWANCE_STAGE1", "ECL_ALLOWANCE_STAGE2", "ECL_ALLOWANCE_STAGE3",
        ],
        stage_action="Coverage Stage_i = ECL_ALLOWANCE_STAGE_i / LOANS_STAGE_i_GROSS. Особенно важен Stage 3",
        example_reasoning="""
        1) Coverage Stage 3 50-70% — стандартный уровень для розницы (40-60% для корпоративных, обеспеченных кредитов);
        2) Coverage Stage 3 < 30% — недостаточный резерв, банк может столкнуться с дополнительным provisioning charge;
        3) Coverage Stage 3 > 80% — может быть консервативной политикой ИЛИ признаком, что банк ожидает массовых дефолтов;
        4) Рост ECL_ALLOWANCE_STAGE3 быстрее, чем LOANS_STAGE3_GROSS — банк стал консервативнее (положительный сигнал доверия к управлению).
        """,
    ),
    StepDescription(
        number=3,
        title="NPL ratio и cost of risk",
        aim="Сравнить раскрытый NPL ratio с расчётной долей Stage 3 и оценить cost of risk",
        reasoning_questions="Какой NPL_RATIO раскрыл банк? Соответствует ли он Stage 3? Какова стоимость риска?",
        dependencies=[1, 2],
        step_context_queries=[
            "NPL_RATIO", "NPL_COVERAGE_RATIO",
            "CHANGE_IN_EXPECTED_CREDIT_LOSSES",
            "LOANS_TO_CUSTOMERS_GROSS",
        ],
        stage_action="""
        1) Сравнить раскрытый NPL_RATIO со Stage 3 ratio (расхождение — норма, NPL обычно учитывает только 90+ days overdue);
        2) Cost of risk = CHANGE_IN_EXPECTED_CREDIT_LOSSES / средний кредитный портфель — годовая «плата» за риск.
        """,
        example_reasoning="""
        1) NPL_RATIO 2-4% — нормально для крупных универсальных банков; > 5% — повышенный риск;
        2) Cost of risk 0.5-1.5% — стандартный уровень; > 2% — банк активно списывает; < 0.3% — может быть «retraso» в признании потерь;
        3) Если CHANGE_IN_EXPECTED_CREDIT_LOSSES вырос двузначно за период — банк ожидает ухудшения экономической среды.
        """,
    ),
]


# ─── Цепочка 3 ────────────────────────────────────────────────────────────
# Достаточность капитала — Базель III: CET1, Tier 1, RWA.
CAPITAL_ADEQUACY = [
    StepDescription(
        number=1,
        title="Структура капитала и RWA",
        aim="Оценить уровень регуляторного капитала и его соотношение к взвешенным по риску активам",
        reasoning_questions="Каков CET1 ratio? Изменился ли он? Как растёт RWA относительно баланса?",
        dependencies=[],
        step_context_queries=[
            "COMMON_EQUITY_TIER_ONE_CAPITAL", "RISK_WEIGHTED_ASSETS",
            "CET1_RATIO_DISCLOSED", "TOTAL_ASSETS",
        ],
        stage_action="""
        1) Если CET1_RATIO_DISCLOSED раскрыт — взять напрямую;
        2) Иначе: CET1 ratio = COMMON_EQUITY_TIER_ONE_CAPITAL / RISK_WEIGHTED_ASSETS;
        3) Density of risk = RWA / TOTAL_ASSETS — оценить рискованность баланса.
        """,
        example_reasoning="""
        1) CET1 ratio 11-13% — стандарт для российских банков (минимум по Базелю III + buffers ~10.5%);
        2) CET1 < 10% — банк близок к минимальным требованиям, ограниченные возможности дивидендов и роста;
        3) CET1 > 15% — избыток капитала, может говорить о консервативной политике ИЛИ слабом росте бизнеса;
        4) Density RWA / TOTAL_ASSETS:
           - 30-50% — корпоративный банк со значительной долей надёжных кредитов;
           - 50-70% — розничный банк или с большой долей рисковых кредитов;
           - > 70% — высокий уровень риска на балансе.
        """,
    ),
    StepDescription(
        number=2,
        title="Tier 1 и общая достаточность капитала",
        aim="Сравнить раскрытые регуляторные показатели достаточности с минимальными требованиями ЦБ",
        reasoning_questions="Какие у банка Tier 1 и Total Capital ratios? Есть ли запас над минимумом?",
        dependencies=[1],
        step_context_queries=[
            "TIER1_RATIO_DISCLOSED", "COMMON_EQUITY_TIER_ONE_CAPITAL",
            "RISK_WEIGHTED_ASSETS",
        ],
        stage_action="Сравнить TIER1_RATIO_DISCLOSED с минимумом ЦБ для системно значимых банков (8.5% Tier1, 11.5% Total с буферами)",
        example_reasoning="""
        1) Tier1 близок к CET1 — банк не использует AT1-инструменты, простая структура капитала;
        2) Большой gap между Tier1 и CET1 — есть бессрочные субординированные облигации, которые могут быть конвертированы при ухудшении CET1;
        3) Запас над минимумом > 3pp — банк может расти без допэмиссии и платить дивиденды;
        4) Запас < 1pp — банк под давлением, ограниченные возможности роста, риск ограничения дивидендов регулятором.
        """,
    ),
]


# ─── Цепочка 4 ────────────────────────────────────────────────────────────
# Структура фондирования и Loan-to-Deposit.
DEPOSITS_FUNDING = [
    StepDescription(
        number=1,
        title="Структура клиентских средств",
        aim="Понять, на чьи деньги опирается банк — корпоративные клиенты vs население",
        reasoning_questions="Каково соотношение средств физлиц и корпоратов? Как изменилось за период?",
        dependencies=[],
        step_context_queries=[
            "INDIVIDUALS_DEPOSITS", "CORPORATE_DEPOSITS", "CLIENT_DEPOSITS",
            "INTERBANK_DEPOSITS",
        ],
        stage_action="""
        1) Доля физлиц = INDIVIDUALS_DEPOSITS / CLIENT_DEPOSITS;
        2) Доля корпоратов = CORPORATE_DEPOSITS / CLIENT_DEPOSITS;
        3) Доля межбанка = INTERBANK_DEPOSITS / (CLIENT_DEPOSITS + INTERBANK_DEPOSITS).
        Сравнить с прошлым периодом — направление сдвига.
        """,
        example_reasoning="""
        1) Высокая доля физлиц (60-80%) — стабильное розничное фондирование, обычно дешевле и менее волатильно;
        2) Высокая доля корпоратов (>50%) — банк зависим от крупных клиентов, риск концентрации;
        3) Высокая доля межбанка (>20%) — рискованно: ликвидность в кризис высыхает первой;
        4) Сдвиг физлица→корпораты — может означать утрату доверия населения ИЛИ агрессивный набор корпоративного бизнеса (нужен контекст).
        """,
    ),
    StepDescription(
        number=2,
        title="Loan-to-Deposit ratio (LDR)",
        aim="Оценить, в какой степени банк финансирует кредиты за счёт клиентских средств",
        reasoning_questions="Каково соотношение LOANS_TO_CUSTOMERS_NET / CLIENT_DEPOSITS? Растёт ли разрыв?",
        dependencies=[1],
        step_context_queries=[
            "LOANS_TO_CUSTOMERS_NET", "LOANS_TO_CUSTOMERS_GROSS",
            "CLIENT_DEPOSITS",
        ],
        stage_action="LDR = LOANS_TO_CUSTOMERS_NET / CLIENT_DEPOSITS. Норма для российских банков — 70-95%",
        example_reasoning="""
        1) LDR < 70% — у банка избыток ликвидности, есть «free cash» для роста кредитования (положительный сигнал, если есть планы роста);
        2) LDR 70-95% — здоровое соотношение, кредиты в основном фондируются депозитами;
        3) LDR > 100% — банк финансирует кредиты не только депозитами, но и оптовым фондированием (interbank, bonds);
           риск рефинансирования вырастает в стрессовых условиях;
        4) Растущий LDR при стагнации депозитной базы — признак агрессивного роста за счёт более дорогих источников фондирования.
        """,
    ),
    StepDescription(
        number=3,
        title="Долговые ценные бумаги и оптовое фондирование",
        aim="Понять долю заимствований на финансовых рынках в общей структуре пассивов",
        reasoning_questions="Какая доля DEBT_SECURITIES_ISSUED + INTERBANK_DEPOSITS в общих обязательствах?",
        dependencies=[1, 2],
        step_context_queries=[
            "DEBT_SECURITIES_ISSUED", "INTERBANK_DEPOSITS", "TOTAL_LIABILITIES",
            "SUBORDINATED_DEBT",
        ],
        stage_action="Wholesale funding share = (DEBT_SECURITIES_ISSUED + INTERBANK_DEPOSITS + SUBORDINATED_DEBT) / TOTAL_LIABILITIES",
        example_reasoning="""
        1) Wholesale share < 15% — банк опирается на стабильную депозитную базу;
        2) Wholesale share 15-30% — диверсифицированная структура, нормально для крупных банков;
        3) Wholesale share > 30% — повышенный риск рефинансирования, особенно если в этой части короткие межбанковские;
        4) Рост wholesale при росте LDR — банк растёт быстрее депозитной базы и пользуется more expensive funding.
        """,
    ),
]


# ─── Цепочка 5 ────────────────────────────────────────────────────────────
# Bank ROE / ROA — без NF-овской Дюпон-логики.
BANK_PROFITABILITY = [
    StepDescription(
        number=1,
        title="ROE банка",
        aim="Оценить рентабельность собственного капитала банка относительно отраслевых ориентиров",
        reasoning_questions="Какова ROE = NET_INCOME / средний TOTAL_EQUITY? Растёт или падает?",
        dependencies=[],
        step_context_queries=[
            "NET_INCOME", "TOTAL_EQUITY", "NET_INCOME_ATTRIBUTABLE_TO_SHAREHOLDERS",
        ],
        stage_action="ROE = NET_INCOME / средний TOTAL_EQUITY (упрощённо: (TOTAL_EQUITY_t + TOTAL_EQUITY_{t-1}) / 2)",
        example_reasoning="""
        1) ROE 18-25% — отличная рентабельность для крупного российского банка (Сбер уровень);
        2) ROE 12-18% — здоровый средний уровень;
        3) ROE 5-12% — слабая рентабельность, банк под давлением (низкая маржа, рост резервов, или избыток капитала);
        4) ROE < 5% или отрицательный — критический сигнал: убытки или близко к убыткам;
        5) Сравнить с прошлым периодом — направление более важно, чем абсолютное значение.
        """,
    ),
    StepDescription(
        number=2,
        title="ROA банка",
        aim="Оценить рентабельность активов — насколько эффективно банк использует свой баланс",
        reasoning_questions="Какова ROA = NET_INCOME / средний TOTAL_ASSETS? Какой equity multiplier?",
        dependencies=[1],
        step_context_queries=[
            "NET_INCOME", "TOTAL_ASSETS", "TOTAL_EQUITY",
        ],
        stage_action="""
        1) ROA = NET_INCOME / средний TOTAL_ASSETS;
        2) Equity multiplier (леверидж) = TOTAL_ASSETS / TOTAL_EQUITY;
        3) Проверка: ROE ≈ ROA × Equity multiplier.
        """,
        example_reasoning="""
        1) ROA 1.5-2.5% — отличный уровень для российского банка;
        2) ROA 0.8-1.5% — нормальный средний уровень;
        3) ROA < 0.5% — банк зарабатывает копейки на свои активы, проблемная маржинальность;
        4) Equity multiplier 8-12× — стандарт для банков; > 15× — агрессивный леверидж, повышенный риск;
        5) Если ROE высокий, но ROA низкий — банк зарабатывает прибыль за счёт высокого левериджа, не за счёт операционной эффективности.
        """,
    ),
    StepDescription(
        number=3,
        title="Cost-to-Income ratio",
        aim="Оценить операционную эффективность — какую долю операционных доходов съедают расходы",
        reasoning_questions="Каков CIR = OPERATING_EXPENSES / OPERATING_INCOME? Снижается ли он?",
        dependencies=[2],
        step_context_queries=[
            "OPERATING_EXPENSES", "NET_INTEREST_INCOME",
            "FEE_AND_COMMISSION_INCOME",
        ],
        stage_action="CIR = OPERATING_EXPENSES / (NET_INTEREST_INCOME + FEE_AND_COMMISSION_INCOME). Норма 30-50%",
        example_reasoning="""
        1) CIR 30-40% — отличная операционная эффективность (топ-банки);
        2) CIR 40-55% — нормальный уровень;
        3) CIR > 60% — банк тратит слишком много на операционку (зарплаты, IT, ветви), низкая операционная левеража;
        4) Снижающийся CIR — банк масштабируется, операционные расходы растут медленнее доходов (положительный сигнал);
        5) Растущий CIR — давление на маржинальность ИЛИ инвестиции в рост, нужно смотреть на динамику доходов.
        """,
    ),
]


# ─── Цепочка 6 ────────────────────────────────────────────────────────────
# Ликвидность — высоколиквидные активы и устойчивость в стрессе.
LIQUIDITY_ANALYSIS = [
    StepDescription(
        number=1,
        title="Доля высоколиквидных активов",
        aim="Оценить запас ликвидности банка относительно баланса",
        reasoning_questions="Какова доля EXTREMELY_LIQUID_ASSETS (касса, ЦБ, размещения у ЦБ) в TOTAL_ASSETS?",
        dependencies=[],
        step_context_queries=[
            "EXTREMELY_LIQUID_ASSETS", "CASH_AND_EQUIVALENTS",
            "INTERBANK_LOANS", "TOTAL_ASSETS",
        ],
        stage_action="""
        1) Доля высоколиквидных = EXTREMELY_LIQUID_ASSETS / TOTAL_ASSETS;
        2) Запас + межбанк = (EXTREMELY_LIQUID_ASSETS + INTERBANK_LOANS) / TOTAL_ASSETS — консервативный «buffer» с учётом межбанковских размещений (обычно 1-7 дней).
        """,
        example_reasoning="""
        1) Доля 8-15% — стандарт для устойчивого банка;
        2) Доля > 20% — избыток ликвидности, банк не может разместить деньги в кредиты (отрицательно для маржинальности, но плюс для устойчивости);
        3) Доля < 5% — низкий запас, повышенный риск ликвидного шока;
        4) Сравнение с прошлым периодом — направление миграции ликвидности.
        """,
    ),
    StepDescription(
        number=2,
        title="Securities portfolio и обремененные активы",
        aim="Оценить структуру портфеля ценных бумаг и уровень их обременения",
        reasoning_questions="Какова доля AFS/FVOCI и AmC ценных бумаг? Какая часть репо-обременена?",
        dependencies=[1],
        step_context_queries=[
            "SECURITIES_FVPL", "SECURITIES_FVOCI", "SECURITIES_AMORTISED_COST",
            "REPO_PLEDGED_SECURITIES", "TOTAL_ASSETS",
        ],
        stage_action="""
        1) Доля каждой категории в общей сумме ценных бумаг;
        2) Доля обременения = REPO_PLEDGED_SECURITIES / (SECURITIES_FVPL + SECURITIES_FVOCI + SECURITIES_AMORTISED_COST).
        """,
        example_reasoning="""
        1) Доля FVPL (held for trading) > 30% — банк имеет значительный торговый риск, прибыль волатильна;
        2) Доминирование FVOCI/AmC — консервативный портфель, ставка на купонный доход и устойчивость;
        3) Высокая доля REPO_PLEDGED (>30% портфеля) — банк активно использует репо для funding, при росте ставок ЦБ или закрытии лимитов это создаёт проблему;
        4) При раскрытии: мониторить рост обременения квартал-к-кварталу — это ранний индикатор давления на ликвидность.
        """,
    ),
    StepDescription(
        number=3,
        title="LCR / NSFR (если раскрыты)",
        aim="Сверить нормативы ликвидности с требованиями ЦБ",
        reasoning_questions="Раскрыты ли LCR (Liquidity Coverage Ratio) и NSFR (Net Stable Funding Ratio)? Запас над минимумом?",
        dependencies=[1, 2],
        step_context_queries=[
            "LCR_RATIO", "NSFR_RATIO",
        ],
        stage_action="""
        1) Если LCR_RATIO раскрыт: сравнить с минимумом 100% (с буферами для системно значимых до 130-140%);
        2) Если NSFR_RATIO раскрыт: сравнить с минимумом 100%;
        3) Если не раскрыты — отметить и отметить отсутствие данных (МСФО не обязывает раскрывать, но для системно значимых банков это норматив ЦБ).
        """,
        example_reasoning="""
        1) LCR > 130% — сильный запас, банк выдержит 30 дней stress-сценария по Базелю;
        2) LCR 100-120% — нормативно достаточно, но запаса немного;
        3) LCR < 100% — нарушение норматива, серьёзный регуляторный риск (если такое раскрыто, это вообще-то ЧП);
        4) NSFR > 110% — структура баланса устойчива на горизонте 1 года;
        5) NSFR < 100% — банк имеет дисбаланс по срокам активов и обязательств.
        """,
    ),
]


# Реестр цепочек для AIFA DEV банковского режима. Та же форма, что и
# upstream STEP_CONFIG в chains.py — `(key, chain, title)`.
STEP_CONFIG_BANK: list[tuple[str, list[StepDescription], str]] = [
    ("NIM_ANALYSIS", NIM_ANALYSIS, "Анализ чистой процентной маржи (NIM)"),
    ("CREDIT_QUALITY", CREDIT_QUALITY, "Качество кредитного портфеля (Stage 1/2/3, NPL)"),
    ("CAPITAL_ADEQUACY", CAPITAL_ADEQUACY, "Достаточность капитала (CET1, Tier 1, RWA)"),
    ("DEPOSITS_FUNDING", DEPOSITS_FUNDING, "Структура фондирования (LDR, депозитная база)"),
    ("BANK_PROFITABILITY", BANK_PROFITABILITY, "Рентабельность банка (ROE, ROA, CIR)"),
    ("LIQUIDITY_ANALYSIS", LIQUIDITY_ANALYSIS, "Ликвидность и портфель ценных бумаг"),
]
