from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="FINENGINE_", extra="ignore")

    # No prefix — reads ANTHROPIC_API_KEY directly
    anthropic_api_key: str = Field("", alias="ANTHROPIC_API_KEY", validation_alias="ANTHROPIC_API_KEY")
    data_dir: Path = Path("./data")
    docs_dir: Path = Path("./data/documents")
    host: str = "0.0.0.0"
    port: int = 8000

    chunk_size: int = 800
    chunk_overlap: int = 100

    claude_model: str = "claude-opus-4-6"

    # OpenClaw gateway — primary inference path for FA-CLAW.
    # Backend container reaches the host gateway (loopback-bound on the
    # VPS) via host.docker.internal, mapped through extra_hosts in
    # deploy/docker-compose.yml. Token comes from /root/.openclaw/openclaw.json
    # `gateway.auth.token` on the dev VPS.
    openclaw_gateway_url: str = Field(
        "http://host.docker.internal:18789",
        alias="FINENGINE_OPENCLAW_GATEWAY_URL",
        validation_alias="FINENGINE_OPENCLAW_GATEWAY_URL",
    )
    openclaw_gateway_token: str = Field(
        "",
        alias="FINENGINE_OPENCLAW_GATEWAY_TOKEN",
        validation_alias="FINENGINE_OPENCLAW_GATEWAY_TOKEN",
    )

    # Сервис Workstation (комнаты станции): здесь исполняется код временных
    # атомов гибридного планировщика. Пусто — код не исполняется (честный отказ).
    workstation_url: str = Field(
        "", alias="FINENGINE_WORKSTATION_URL", validation_alias="FINENGINE_WORKSTATION_URL",
    )
    # Секрет создания сессии Workstation контура (ADR-0007, ADR-0028): тот же, что у ouroboros
    # (WORKSTATION_CREATE_SECRET). Пусто — заголовок не ставится.
    workstation_create_secret: str = Field(default="", validation_alias="FINENGINE_WORKSTATION_CREATE_SECRET")

    # ouroboros (CONTRACTS.md §2): служба ходит к нему по WS хода и за историей. Адрес — внутренняя
    # сеть compose-проекта (ADR-0028), порт наружу не публикуется. Имена окружения — по префиксу
    # FINENGINE_ (`FINENGINE_OUROBOROS_URL` и т. д.).
    ouroboros_url: str = "http://ouroboros:8080"
    ouroboros_connect_timeout_sec: float = 10.0
    # Загрузка и чтение файла каталога (CONTRACTS.md §6): дольше обычного запроса — до 50 МБ и проверка
    # содержимого на стороне ouroboros.
    ouroboros_file_timeout_sec: float = 120.0
    # Копия OUROBOROS_HARD_TIMEOUT_SEC и OUROBOROS_MAX_INBOUND_MESSAGE_CHARS из окружения ouroboros
    # (служба его окружения не читает): держать равными. Крайний срок хода — первое плюс 60 с (ADR-0021).
    ouroboros_hard_timeout_sec: float = 2700.0
    ouroboros_max_inbound_chars: int = 50_000
    # Тишина на соединении хода дольше этого — обрыв. Биения ouroboros идут раз в 30 с.
    ouroboros_silence_sec: float = 120.0
    ouroboros_stop_wait_sec: float = 30.0
    # Пауза между попытками переподключиться и между опросами истории после обрыва.
    ouroboros_retry_pause_sec: float = 2.0
    ouroboros_reconnect_attempts: int = 3

    # Внутренний API инструментов (CONTRACTS.md §3, ADR-0028): ouroboros ходит с этим токеном; имя
    # окружения общее с ouroboros и плагином. Пусто — API закрыто целиком (503), а не открыто.
    aifa_engine_token: str = Field(default="", validation_alias="AIFA_ENGINE_TOKEN")
    # Предел ответа инструмента в знаках (§3.0); длинное отдаётся частями.
    ouroboros_result_limit: int = 12_000
    # Сколько `aifa_goal` держит один HTTP-вызов до ответа `ЖДИ job=<id>` (§3.1, P61): весь срок инструмента
    # держит служба, плагин ждёт один запрос и между опросами проверяет остановку (ADR-0022 п. 3).
    ouroboros_goal_wait_sec: float = 240.0
    # Как часто задание сверяется с ходом: ход закончен или отменён — задание снимается (§3.1, C3.4).
    ouroboros_run_watch_sec: float = 1.0
    # ВРЕМЕННО (ADR-0029, PRINCIPLES А.7), удаляется в шаге 19: кто ведёт цикл при сбое стандартного пути.
    # `engine` — служба гонит `DynamicRun` (как с шага 09); `ouroboros` — `aifa_goal` отвечает `НУЖЕН_ЦИКЛ`,
    # а ходы приходят вызовами `aifa_move`/`aifa_finish`. Окружение: FINENGINE_LOOP_DRIVER.
    loop_driver: Literal["engine", "ouroboros"] = "engine"

    # OpenRouter settings (legacy fallback / non-chat call sites still
    # using openrouter directly: extract per-metric, AIFA reranker, etc.)
    openrouter_api_key: str = Field("", alias="OPENROUTER_API_KEY", validation_alias="OPENROUTER_API_KEY")

    # FRED API (St. Louis Fed) — free, registered account required.
    # Powers the macro-indicators block at the top of the Daily News digest:
    # CPI, PPI, GDP, NFP, unemployment, retail sales, industrial production,
    # consumer sentiment, EIA crude inventory. Eurozone series available too;
    # China is partial; RU data on FRED was largely discontinued in 2022.
    # Get a key at https://fred.stlouisfed.org/docs/api/api_key.html
    fred_api_key: str = Field(
        "", alias="FINENGINE_FRED_API_KEY",
        validation_alias="FINENGINE_FRED_API_KEY",
    )

    # EIA API (U.S. Energy Information Administration) — free, registered
    # account required. Used for the weekly crude-oil stocks indicator
    # (WCESTUS1 — ending stocks excluding SPR, in thousand barrels).
    # Register at https://www.eia.gov/opendata/register.php
    eia_api_key: str = Field(
        "", alias="FINENGINE_EIA_API_KEY",
        validation_alias="FINENGINE_EIA_API_KEY",
    )

    # Finnhub API — free tier (60 req/min), registered account required.
    # Tier-2 structured source for the IR Calendar: upcoming earnings dates
    # of FOREIGN companies (US/EU/China ADRs). Does NOT cover MOEX/RU names.
    # Unset = the Finnhub calendar source is silently skipped (graceful).
    # Register at https://finnhub.io/register
    finnhub_api_key: str = Field(
        "", alias="FINENGINE_FINNHUB_API_KEY",
        validation_alias="FINENGINE_FINNHUB_API_KEY",
    )

    # Outbound proxy for "foreign" data sources that geo-block the prod
    # VPS's RU egress IP — currently EIA (US .gov WAF) and NBS China.
    # Format: same as HTTPS_PROXY, e.g. "http://user:pass@host:port".
    # Unset = EIA + NBS rows silently dropped from the macro dashboard.
    macro_foreign_proxy: str = Field(
        "", alias="FINENGINE_MACRO_FOREIGN_PROXY",
        validation_alias="FINENGINE_MACRO_FOREIGN_PROXY",
    )
    # Enable role-specific table layouts (build_formatted_table): dgr/cib/die/…
    role_formatters_enabled: bool = Field(
        True,
        alias="FINENGINE_ROLE_FORMATTERS",
        validation_alias="FINENGINE_ROLE_FORMATTERS",
    )
    # ТАБЛИЦЫ В ПРОМПТЕ СЖАТИЯ ЕДУТ МОДЕЛИ ДВАЖДЫ (21.08.2026, третий эшелон).
    #
    # Первая стадия получает ВЕСЬ текст отчёта И каталог таблиц, а тела таблиц
    # в каталоге — буквальный повтор того, что уже есть в тексте. Замер на трёх
    # настоящих отчётах:
    #
    #     ab-probe   368 238 знаков текста | каталог 269 141 (73 %) | повтор 238 190
    #     3f7092b7   206 114               | каталог  95 941 (47 %) | повтор  64 632
    #     megafon     76 928               | каталог  54 538 (71 %) | повтор  46 267
    #
    # То есть промпт больше нужного в полтора-два раза, а сжатие — 216 секунд из
    # 318. Каталог при этом НУЖЕН: он несёт адресацию (`table_id`, страницу,
    # заголовок, соседний текст), по которой модель называет улики. Дублируется
    # только ТЕЛО.
    #
    # ⚠️ ВЫКЛЮЧЕНО ПО УМОЛЧАНИЮ, И ЭТО НЕ ОСТОРОЖНИЧАНЬЕ. Промпт сжатия — сердце
    # извлечения, а `docs/rules/architecture.md` называет регрессию качества
    # блокером мержа. Ускорение, не проверенное замером качества, — это ставка,
    # а не улучшение. Включать после прогона A/B:
    #
    #     PYTHONPATH=. .venv/bin/python benchmarks/ab_extract.py --env dev
    #
    # Включение — одна переменная и перезапуск бэкенда:
    #     FINENGINE_COMPRESSION_TABLE_BODIES=short
    compression_table_bodies: str = Field(
        "full",
        alias="FINENGINE_COMPRESSION_TABLE_BODIES",
        validation_alias="FINENGINE_COMPRESSION_TABLE_BODIES",
    )

    # Сколько батчей второй стадии идут ОДНОВРЕМЕННО (17.08.2026).
    #
    # У двухстадийной семафор батчей стоял на 1: они шли строго по одному, и
    # каждый — это отдельный вызов модели. Наша схема (`full_context`) пускает
    # свои шарды все сразу (`asyncio.create_task` на каждый), поэтому её время
    # — это самый долгий шард, а у двухстадийной — СУММА всех батчей.
    # На стенде 17.08 прогон «Детского мира» не уложился в комнату станции
    # (~21 минута, `exec` 900 с) и модель перезапускала его трижды.
    #
    # Число вызовов модели правка НЕ меняет — меняет только темп.
    #
    # ⚠️ ЗНАЧЕНИЕ СНИЖЕНО С 6 ДО 4 (20.08.2026), И ЭТО НЕ ОСТОРОЖНОСТЬ РАДИ
    # ОСТОРОЖНОСТИ. Шестёрка стояла здесь с 17.08 и была МЁРТВОЙ: до
    # `extract_batch_size` состав ячейки уходил одним батчем, шесть мест
    # семафора стояли над одной задачей — сдерживать было нечего. Теперь она
    # оживает сразу, и оживает на 50 процентов выше темпа, на котором батчи
    # реально гоняет upstream: у Finengine умолчание `two_stage_batch_jobs` — 4
    # (`src/finengine/skills/extractors/_base.py`). Заводить первый живой
    # прогон выше проверенного темпа не за что: 16.08 шесть ОДНОВРЕМЕННЫХ
    # ПРОГОНОВ выбрали лимит ключа OpenRouter, а упёршись в лимит, мы получим
    # не ускорение, а ретраи — и сорвавшиеся батчи, каждый из которых стоит
    # десять пустых показателей.
    #
    # Поднять обратно — одна переменная окружения, без пересборки:
    # `FINENGINE_EXTRACT_BATCH_JOBS=6`. Упереться в лимит — `=1`, вернётся
    # прежнее последовательное поведение.
    extract_batch_jobs: int = Field(
        4,
        alias="FINENGINE_EXTRACT_BATCH_JOBS",
        validation_alias="FINENGINE_EXTRACT_BATCH_JOBS",
    )

    # Сколько показателей уходит в ОДИН вызов второй стадии (20.08.2026).
    #
    # Темп выше (`extract_batch_jobs`) сдерживает одновременность батчей — но
    # сдерживать ему было нечего: без этого размера состав ячейки уходил одним
    # батчем, и шесть мест семафора стояли над одной задачей. Замер на стенде:
    # `main_extract_1` — 178 с одним вызовом на 80 показателей банка (имя стадии
    # с 21.08 несёт попытку: в свежем разрезе это `main_extract_1_attempt_01`).
    #
    # Значения — боевые у Finengine (`extractors/_base.py`): 10 показателей в
    # батче, 4 попытки на батч, 5 в сегментном. Вынесены настройками, а не
    # константами, чтобы владелец правил темп без пересборки.
    #
    # ⚠️ Размер меняет НЕ ТОЛЬКО темп: при заданном размере в промпт уходит
    # только состав батча (`all_metrics=target_metrics`), и показатели без
    # улик первой стадии не запрашиваются вовсе. Это правка качества —
    # сверять по golden.
    extract_batch_size: int = Field(
        10,
        alias="FINENGINE_EXTRACT_BATCH_SIZE",
        validation_alias="FINENGINE_EXTRACT_BATCH_SIZE",
    )
    # Сколько показателей уходит в ОДИН вызов первой стадии — атома `locate` (08.10.2026).
    #
    # Замер на dev (ЛУКОЙЛ, форма на 58 показателей): один вызов на всё считался ~372 с при
    # пределе клиента 360 с и не завершился ни разу. Шарды идут одновременно под общим темпом
    # `extract_batch_jobs`.
    #
    # ⚠️ Цена: каждый шард читает ВЕСЬ отчёт, то есть входные токены растут в число шардов раз.
    # Вернуть один вызов — значение не меньше числа показателей формы (например `1000`).
    locate_batch_size: int = Field(
        15,
        alias="FINENGINE_LOCATE_BATCH_SIZE",
        validation_alias="FINENGINE_LOCATE_BATCH_SIZE",
    )
    # Батч возвращает JSON целиком; невалидный ответ переспрашивается с другим
    # порядком показателей. Меньше попыток — больше пустых клеток на батч.
    extract_batch_attempts: int = Field(
        4,
        alias="FINENGINE_EXTRACT_BATCH_ATTEMPTS",
        validation_alias="FINENGINE_EXTRACT_BATCH_ATTEMPTS",
    )
    # ЧЕМ батчи набираются: `balanced` — по ожидаемому весу, `spread` — через
    # один по реестру (прежнее поведение), `contiguous` — подряд.
    #
    # Батчи расходятся по времени на порядок: замер стенда 20.08 (ПСБ,
    # `ifrs_bank_ru`, пять батчей) — 22,2 с у лёгкого против 148,3 с у тяжёлого
    # при сумме 369,5 с.
    #
    # 🔴 СКОЛЬКО ДАЁТ БАЛАНСИРОВКА — ЗАВИСИТ ОТ ЧИСЛА БАТЧЕЙ, И ЗНАК ТОЖЕ.
    # Волн выходит `ceil(N/темп)`, ровные батчи дают `ceil(N/темп) × S/N`, а
    # предел любого деления — `S/темп`: ровные оптимальны только при числе
    # батчей, кратном темпу, или не больше темпа. Восемь батчей при темпе 4 —
    # около 6 процентов; ПЯТЬ при темпе 4 — ЗАМЕДЛЕНИЕ около 6 процентов.
    # ⚠️ Числа пересчитаны 21.08 под нынешнюю меру веса (ожидаемый выход,
    # CV ≈ 0,3–0,45). Стоявшие здесь «8–9 процентов» откалиброваны на разбросе
    # снятой меры (длина карточки, CV ≈ 2,1) и новой не принадлежат — разбор в
    # докстроке `_balanced_batches`.
    # ⚠️ Стоявшее здесь до 21.08 «при темпе 4 ровные батчи дали бы
    # 92,4 с» брало предел `S/темп` за то, что дают ровные батчи: ровные пятеро
    # дают 2×S/5 = 147,8 с против измеренных 148,3. Пересчёт и симуляция — в
    # докстроке `tests/test_the_heaviest_batch_sets_the_pace.py`; перед тем как
    # судить замер, смотреть `stage2_batch_count` в диагностике прогона.
    #
    # ⚠️ ЭТО ПРАВКА КАЧЕСТВА, А НЕ ТОЛЬКО ТЕМПА: меняется СОСТАВ батчей, а
    # состав батча — это состав промпта. Поэтому значение настройкой, а не
    # константой: `FINENGINE_EXTRACT_BATCH_STRATEGY=spread` возвращает прежнее
    # деление одной переменной окружения, без пересборки, если замер по golden
    # покажет ухудшение.
    #
    # ⚠️ ЗНАЧЕНИЯ ПЕРЕЧИСЛЕНЫ, А НЕ СВОБОДНАЯ СТРОКА, И ЭТО НЕ ПЕДАНТИЗМ.
    # Вся история отката держится на том, что владелец наберёт эту переменную
    # руками на стенде. Строкой `FINENGINE_EXTRACT_BATCH_STRATEGY=balnced`
    # принималась молча, а `ValueError: Unknown batch strategy` прилетал уже на
    # делении — то есть ПОСЛЕ первой стадии, сжёгшей минуты и токены на сжатие
    # документа. Перечислением опечатка падает при чтении настройки, до вызова
    # модели. Держит `test_a_misspelled_strategy_is_refused_before_the_run`.
    extract_batch_strategy: Literal["balanced", "spread", "contiguous"] = Field(
        "balanced",
        alias="FINENGINE_EXTRACT_BATCH_STRATEGY",
        validation_alias="FINENGINE_EXTRACT_BATCH_STRATEGY",
    )
    # Сегментных показателей самое большее пять, поэтому размер свой и меньший.
    extract_segment_batch_size: int = Field(
        5,
        alias="FINENGINE_EXTRACT_SEGMENT_BATCH_SIZE",
        validation_alias="FINENGINE_EXTRACT_SEGMENT_BATCH_SIZE",
    )

    # Спрашивать ли у второй стадии показатели, которым первая стадия не нашла
    # улик (20.08.2026). УМОЛЧАНИЕ ЗДЕСЬ НАШЕ, А НЕ UPSTREAM'ОВСКОЕ, И КУПЛЕНО
    # ЗАМЕРОМ, А НЕ РАССУЖДЕНИЕМ.
    #
    # Upstream при заданном размере батча выбрасывает безулиочные из состава.
    # У нас первая стадия отбирает улики хуже, и цена этого измерена: два
    # прогона ПСБ (`ifrs_bank_ru`), отличавшиеся только шардированием — тот же
    # код, тот же реестр, тот же документ, — дали 38 показателей без шардов
    # против 34 с шардами. Пять потеряны поимённо:
    # EXTREMELY_LIQUID_ASSETS, INTERBANK_DEPOSITS, INTEREST_EARNING_ASSETS,
    # NET_INTEREST_MARGIN, NET_OTHER_RESERVES — среди них работающие активы и
    # чистая процентная маржа, то есть не мелочь на краю реестра.
    #
    # ⚠️ РАЗБРОС МОДЕЛИ БОЛЬШОЙ (между прогонами число найденных гуляло от 34
    # до 50), поэтому «пять» — не константа обиды, а порядок величины потери.
    # Выключить — `FINENGINE_EXTRACT_ASK_WITHOUT_EVIDENCE=0`, без пересборки:
    # если замер покажет, что безулиочные только жгут токены и ничего не
    # находят (или, хуже, находят выдуманное), поведение вернётся к upstream.
    extract_ask_without_evidence: bool = Field(
        True,
        alias="FINENGINE_EXTRACT_ASK_WITHOUT_EVIDENCE",
        validation_alias="FINENGINE_EXTRACT_ASK_WITHOUT_EVIDENCE",
    )
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = Field("qwen/qwen3.6-plus", alias="FINENGINE_OPENROUTER_MODEL", validation_alias="FINENGINE_OPENROUTER_MODEL")
    # Короткий управляющий JSON-вызов (intent) не должен
    # наследовать 360 секунд × 3 попытки тяжёлого извлечения отчёта.
    routing_model_timeout_seconds: float = Field(
        120.0,
        alias="FINENGINE_ROUTING_MODEL_TIMEOUT_SECONDS",
        validation_alias="FINENGINE_ROUTING_MODEL_TIMEOUT_SECONDS",
    )
    # Планировщик цикла (DynamicRun) читает всё состояние хода — десятки тысяч знаков, растущие с
    # каждым поиском и показателем: в 120 с он на dev не укладывался (08.10.2026). Предел отдельный,
    # чтобы зависший intent не держал пользователя 5 минут.
    planner_model_timeout_seconds: float = Field(
        300.0,
        alias="FINENGINE_PLANNER_MODEL_TIMEOUT_SECONDS",
        validation_alias="FINENGINE_PLANNER_MODEL_TIMEOUT_SECONDS",
    )
    # Модели планировщика цикла и автора временных атомов (решение владельца 08.10.2026): задачи
    # разные, и модель им ставят порознь. Пусто — модель бэкенда (`openrouter_model`).
    planner_model: str = Field(
        "", alias="FINENGINE_PLANNER_MODEL", validation_alias="FINENGINE_PLANNER_MODEL",
    )
    # Глубина размышлений планировщика цикла. На dev (09.10.2026) при `high` он отвечал от 17 до 345 с:
    # размышления идут в тот же бюджет вывода, ответ — короткий JSON.
    planner_reasoning_effort: str = Field(
        "high", alias="FINENGINE_PLANNER_REASONING_EFFORT", validation_alias="FINENGINE_PLANNER_REASONING_EFFORT",
    )
    atom_author_model: str = Field(
        "", alias="FINENGINE_ATOM_AUTHOR_MODEL", validation_alias="FINENGINE_ATOM_AUTHOR_MODEL",
    )
    # Модели двух стадий извлечения (решение владельца 09.10.2026): `locate` ищет кандидатов,
    # `read.value` достаёт значения. Пусто — модель бэкенда (`openrouter_model`).
    locate_model: str = Field(
        "", alias="FINENGINE_LOCATE_MODEL", validation_alias="FINENGINE_LOCATE_MODEL",
    )
    read_value_model: str = Field(
        "", alias="FINENGINE_READ_VALUE_MODEL", validation_alias="FINENGINE_READ_VALUE_MODEL",
    )
    # Автор временных атомов пишет код целиком и в 120 с на dev не укладывался (08.10.2026). Предел
    # отдельный от планировщика: у автора до трёх попыток на атом, и его крутят независимо.
    atom_author_model_timeout_seconds: float = Field(
        300.0,
        alias="FINENGINE_ATOM_AUTHOR_MODEL_TIMEOUT_SECONDS",
        validation_alias="FINENGINE_ATOM_AUTHOR_MODEL_TIMEOUT_SECONDS",
    )
    # Атом `web.search` цикла гибридного планировщика: сколько находок просить у поисковика и сколько
    # знаков фрагмента хранить в артефакте.
    web_search_max_results: int = Field(
        10, alias="FINENGINE_WEB_SEARCH_MAX_RESULTS", validation_alias="FINENGINE_WEB_SEARCH_MAX_RESULTS",
    )
    web_search_snippet_chars: int = Field(
        1000, alias="FINENGINE_WEB_SEARCH_SNIPPET_CHARS", validation_alias="FINENGINE_WEB_SEARCH_SNIPPET_CHARS",
    )
    # Предел размера списка находок одного поиска в состоянии хода планировщика: адреса целиком,
    # фрагменты ужимаются.
    web_search_state_chars: int = Field(
        4000, alias="FINENGINE_WEB_SEARCH_STATE_CHARS", validation_alias="FINENGINE_WEB_SEARCH_STATE_CHARS",
    )
    # Атом-загрузка: предел текста HTML-страницы после снятия разметки (файлы данных не режутся).
    download_page_text_chars: int = Field(
        60_000, alias="FINENGINE_DOWNLOAD_PAGE_TEXT_CHARS", validation_alias="FINENGINE_DOWNLOAD_PAGE_TEXT_CHARS",
    )
    # Pin OpenRouter to a single provider (e.g. "DashScope" for Qwen) so
    # the same call always hits the same backend. Without this, OpenRouter
    # routes across multiple providers and we observed micro-variance in
    # outputs even at temperature=0. Empty = no pinning (default routing).
    openrouter_provider: str = Field("", alias="FINENGINE_OPENROUTER_PROVIDER", validation_alias="FINENGINE_OPENROUTER_PROVIDER")

    # Модель дайджеста: и классификация новостей, и перевод заголовков
    # регуляторов. Классификация — сотни вызовов за прогон и основная статья
    # расхода, поэтому цена здесь решает.
    #
    # ⚠️ Модель РАССУЖДАЮЩАЯ, и токены размышлений идут в тот же счёт, что и
    # ответ. Меняя её, проверяй max_output обеих стадий: на бюджете 2000
    # перевод молчал (finish_reason=length, 6861 знак размышлений), на 9000
    # молчала классификация — и пустой выпуск записывался успешным.
    #
    # Откат без выкатки: FINENGINE_NEWS_CLASSIFY_MODEL=deepseek/deepseek-chat-v3-0324
    news_classify_model: str = Field(
        "deepseek/deepseek-v4-flash",
        alias="FINENGINE_NEWS_CLASSIFY_MODEL",
        validation_alias="FINENGINE_NEWS_CLASSIFY_MODEL",
    )

    # Сколько отобранных новостей уходит к модели за прогон. Было 60 литералом,
    # и это оказалось главным ограничителем полноты выпуска: сбор даёт ~800
    # новостей, правила оставляют ~200, а модель видела только 60 — остальные
    # 140 не попадали в выпуск НИКОГДА, каким бы важным ни было событие.
    #
    # Цена замерена по 12 прогонам: $0.00073 за кандидата. 60 → $0.044,
    # 100 → $0.073 за прогон (около 6 ₽). Бюджет вывода считается от этого
    # числа автоматически (см. classify.classify_batch), поэтому менять можно
    # одной ручкой без выкатки: FINENGINE_NEWS_TOP_CANDIDATES.
    news_top_candidates: int = Field(
        100,
        alias="FINENGINE_NEWS_TOP_CANDIDATES",
        validation_alias="FINENGINE_NEWS_TOP_CANDIDATES",
    )

    # GigaChat — Sber's LLM via the official `gigachat` SDK. Authenticates
    # with a base64-encoded `client_id:secret` issued by Sber's developer
    # portal. Same value as `authorization_key` in AIFA's llm_config.json.
    # `verify_ssl_certs=False` is hardcoded in the wrapper because Sber's
    # endpoint uses their corporate CA chain.
    gigachat_credentials: str = Field("", alias="GIGACHAT_CREDENTIALS", validation_alias="GIGACHAT_CREDENTIALS")
    gigachat_model: str = Field("GigaChat-Max", alias="GIGACHAT_MODEL", validation_alias="GIGACHAT_MODEL")
    # OAuth scope for the GigaChat token. Sber issues different keys per
    # tier — default `GIGACHAT_API_PERS` is for individual sandbox use,
    # `GIGACHAT_API_CORP` for corporate, `GIGACHAT_API_B2B` for legacy
    # legal-entity contracts. AIFA's keys are corporate, so we default
    # to CORP. Override via env if you swap to a personal sandbox key.
    gigachat_scope: str = Field("GIGACHAT_API_CORP", alias="GIGACHAT_SCOPE", validation_alias="GIGACHAT_SCOPE")

    # AIFA baseline calls the IDP-OCR cloud service to convert PDF → markdown
    # tables. Default is the address from the upstream "instruction notebook"
    # in `Репа в репе/document-extractor-idp-ocr/`. Override per environment
    # if the team rotates the host.
    idp_ocr_url: str = Field(
        "https://idp-ocr.ds.idp-team.ru/predict",
        alias="FINENGINE_IDP_OCR_URL",
        validation_alias="FINENGINE_IDP_OCR_URL",
    )

    # Concurrent per-page POSTs to IDP-OCR (I/O-bound, bounded by the service
    # capacity, not local CPU). Default 10; tune via FINENGINE_IDP_OCR_PARALLEL.
    idp_ocr_parallel: int = Field(
        10,
        alias="FINENGINE_IDP_OCR_PARALLEL",
        validation_alias="FINENGINE_IDP_OCR_PARALLEL",
    )

    # Внешний каталог реестра показателей (#153). База реестра — ДАННЫЕ, а не
    # код: методолог правит состав показателей и формулы, и правка не должна
    # требовать сборки образа и рестарта. Встроенные versioned layers в
    # `src/finengine/data/registry_layers/` остаются поставкой по умолчанию.
    # Внешний каталог обязан содержать тот же versioned manifest/layer set;
    # полный `<profile>.json` compatibility path удалён в Срезе 14.
    # Каталог задан, но его нет = встроенные + ГРОМКАЯ запись в лог: молчаливая
    # подмена здесь неотличима от «моя правка не сработала».
    registry_dir: str = Field(
        "", alias="FINENGINE_REGISTRY_DIR",
        validation_alias="FINENGINE_REGISTRY_DIR",
    )

    # Sentry — error tracking. Empty DSN disables the SDK.
    sentry_dsn_backend: str = Field("", alias="SENTRY_DSN_BACKEND", validation_alias="SENTRY_DSN_BACKEND")
    sentry_environment: str = Field("production", alias="SENTRY_ENVIRONMENT", validation_alias="SENTRY_ENVIRONMENT")
    sentry_traces_sample_rate: float = Field(0.1, alias="SENTRY_TRACES_SAMPLE_RATE", validation_alias="SENTRY_TRACES_SAMPLE_RATE")

    # Диалоги: строка на диалог в PostgreSQL вместо одного json-файла на
    # пользователя (см. finengine/store/dialogs.py — там же история дефекта).
    # ПУСТО = файлы, как раньше. Момент переезда живых пользователей выбирает
    # владелец, поэтому дефолт ничего не переключает.
    # Формат: postgresql+psycopg://finengine:ПАРОЛЬ@postgres:5432/finengine
    # Пароль приезжает сюда из окружения (deploy/.env) — в коде его нет и
    # печатать эту строку нельзя.
    dialogs_database_url: str = Field(
        "", alias="FINENGINE_DIALOGS_DATABASE_URL",
        validation_alias="FINENGINE_DIALOGS_DATABASE_URL",
    )

    # Исходы прогонов станции: строка на прогон (см. finengine/store/runs.py —
    # там же история потери двух прогонов 18.08). ПУСТО = как раньше: исход
    # живёт только в потоке открытой вкладки, подключения к базе нет.
    # Адрес СВОЙ, а не общий с диалогами: иначе один тумблер владельца включал
    # бы две миграции разом.
    runs_database_url: str = Field(
        "", alias="FINENGINE_RUNS_DATABASE_URL",
        validation_alias="FINENGINE_RUNS_DATABASE_URL",
    )

    # Auth — invite-only signup, JWT in HTTP-only cookie.
    # Generate the secret with `python -c "import secrets; print(secrets.token_urlsafe(64))"`.
    auth_secret_key: str = Field("CHANGE-ME-DEV-ONLY-DO-NOT-SHIP", alias="FINENGINE_AUTH_SECRET", validation_alias="FINENGINE_AUTH_SECRET")
    auth_token_ttl_days: int = 30
    auth_cookie_name: str = "finengine_session"
    auth_db_path: Path = Path("./data/users.db")
    # ПЕРЕКЛЮЧАТЕЛЬ ХРАНИЛИЩА УЧЁТНЫХ ЗАПИСЕЙ. Пусто (по умолчанию) — SQLite в
    # томе данных, как было. Непустой адрес — база; момент переезда живых
    # пользователей выбирает владелец, код ничего не включает сам. Тот же
    # приём, что у диалогов (`dialogs_database_url`) и прогонов.
    # Общий кэш координации (architecture.md 3.2). Пусто = память процесса,
    # непустой адрес — Redis, и тогда реплик может быть больше одной.
    redis_url: str = Field("", alias="FINENGINE_REDIS_URL",
                           validation_alias="FINENGINE_REDIS_URL")

    # Приставка КАЖДОГО ключа и КАЖДОГО канала в Redis. Она и только она
    # разводит контуры: dev и prod, поставленные на один Redis без разных
    # приставок, делят пропуска нонсов и каналы прогонов — чужой ход виден
    # и перебиваем. Значение — литерал, вместе с разделителем.
    redis_prefix: str = Field("fe:", alias="FINENGINE_REDIS_PREFIX",
                              validation_alias="FINENGINE_REDIS_PREFIX")

    @field_validator("redis_prefix")
    @classmethod
    def _приставка_кончается_двоеточием(cls, v: str) -> str:
        # Без разделителя приставка одного контура становится началом другого:
        # `keys("")` контура `fe:` вычерпывает и `fe:dev:...`. Изоляция как раз
        # там, где её считают уже сделанной.
        v = v.strip()
        return v if not v or v.endswith(":") else v + ":"

    # Схема контура в Postgres. Пусто = как было, общий `public`. Ставится
    # ПОДКЛЮЧЕНИЮ (`search_path`) в `store/engine.py`, потому что таблицы
    # объявлены на уровне модуля, а SQLite схем не знает вовсе.
    # ⚠️ Завести схему — отдельный шаг: `python -m finengine.store.ensure_schema`.
    pg_schema: str = Field("", alias="FINENGINE_PG_SCHEMA",
                           validation_alias="FINENGINE_PG_SCHEMA")

    # Приставка контура к КАЖДОМУ ключу в объектном хранилище. Пусто = как
    # было, объекты в корне бакета. Бакет один на хост — как Redis и Postgres.
    s3_prefix: str = Field("", alias="FINENGINE_S3_PREFIX",
                           validation_alias="FINENGINE_S3_PREFIX")

    @field_validator("s3_prefix")
    @classmethod
    def _приставка_объектов_кончается_косой(cls, v: str) -> str:
        # Та же дыра, что у Redis без двоеточия: `dev` — начало `dev2`, и
        # перечисление одного контура вычерпывает объекты другого.
        v = v.strip().lstrip("/")
        return v if not v or v.endswith("/") else v + "/"

    # Новостной конвейер. Адрес ОБЯЗАТЕЛЕН: файловый фолбэк news.db снесён
    # 26.08.2026, пустое значение — громкий отказ на старте.
    news_database_url: str = Field(
        "", alias="FINENGINE_NEWS_DATABASE_URL",
        validation_alias="FINENGINE_NEWS_DATABASE_URL",
    )

    # Сценарии (автоматизации). Адрес ОБЯЗАТЕЛЕН: фолбэк scenarios.db снесён
    # 26.08.2026, пустое значение — громкий отказ на старте.
    scenarios_database_url: str = Field(
        "", alias="FINENGINE_SCENARIOS_DATABASE_URL",
        validation_alias="FINENGINE_SCENARIOS_DATABASE_URL",
    )

    # Именованное состояние (сессии, списки наблюдения, личная методика,
    # карточки источников): пусто = файлы на томе, непустой — база.
    # ⚠️ Сюда же переведены сессии: адрес ОДИН, а не свой у каждого вида —
    # иначе тумблеров стало бы пять на одно и то же хранилище.
    state_database_url: str = Field(
        "", alias="FINENGINE_STATE_DATABASE_URL",
        validation_alias="FINENGINE_STATE_DATABASE_URL",
    )

    # Журналы (следы хода, аудит тулов, расход модели): пусто = файлы
    # data/{traces,audit,llm_traces}/<owner>/<key>.jsonl, непустой — база.
    journals_database_url: str = Field(
        "", alias="FINENGINE_JOURNALS_DATABASE_URL",
        validation_alias="FINENGINE_JOURNALS_DATABASE_URL",
    )

    # Артефакты проектов (исходы прогонов навыков): пусто = файлы
    # data/artifacts/<owner>/<project>/<run>/, непустой — база.
    artifacts_database_url: str = Field(
        "", alias="FINENGINE_ARTIFACTS_DATABASE_URL",
        validation_alias="FINENGINE_ARTIFACTS_DATABASE_URL",
    )

    # Реестр документов. Адрес ОБЯЗАТЕЛЕН: пикловый фолбэк meta.pkl снесён
    # 26.08.2026 (последний рудимент эпохи FAISS), пусто = громкий отказ.
    documents_database_url: str = Field(
        "", alias="FINENGINE_DOCUMENTS_DATABASE_URL",
        validation_alias="FINENGINE_DOCUMENTS_DATABASE_URL",
    )

    # ── Объектное хранилище (architecture.md 3.1) ────────────────────────
    # Пустой endpoint = файлы на томе, как было. Непустой — S3/MinIO.
    # Ключи держим здесь же, чтобы не расползались по модулям; в лог они не
    # попадают ни в каком виде (см. store/blobs.py).
    s3_endpoint: str = Field("", alias="FINENGINE_S3_ENDPOINT",
                             validation_alias="FINENGINE_S3_ENDPOINT")
    s3_bucket: str = Field("finengine", alias="FINENGINE_S3_BUCKET",
                           validation_alias="FINENGINE_S3_BUCKET")
    s3_access_key: str = Field("", alias="FINENGINE_S3_ACCESS_KEY",
                               validation_alias="FINENGINE_S3_ACCESS_KEY")
    s3_secret_key: str = Field("", alias="FINENGINE_S3_SECRET_KEY",
                               validation_alias="FINENGINE_S3_SECRET_KEY")
    s3_region: str = Field("us-east-1", alias="FINENGINE_S3_REGION",
                           validation_alias="FINENGINE_S3_REGION")

    auth_database_url: str = Field(
        "", alias="FINENGINE_AUTH_DATABASE_URL",
        validation_alias="FINENGINE_AUTH_DATABASE_URL",
    )
    # If the users table is empty at startup, create this admin user.
    # Empty FINENGINE_ADMIN_PASSWORD = generate a random one and print to stdout
    # (visible in Dozzle on the very first run).
    admin_username: str = Field("admin", alias="FINENGINE_ADMIN_USERNAME", validation_alias="FINENGINE_ADMIN_USERNAME")
    admin_password: str = Field("", alias="FINENGINE_ADMIN_PASSWORD", validation_alias="FINENGINE_ADMIN_PASSWORD")

    def model_post_init(self, __context):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.docs_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
