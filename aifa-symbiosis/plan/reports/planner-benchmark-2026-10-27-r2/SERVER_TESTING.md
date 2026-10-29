# Как самому тестировать планировщик на dev

Есть два разных замера:

1. **Генерация предложения:** один и тот же сохранённый контекст отправляется разным моделям.
   Это быстро и позволяет сравнить ответы. Не запускает supervisor, извлечение показателей или Excel.
2. **Полный прогон UI:** включает открытие цикла, чтение инструментов, планировщика, приёмку корнем
   и исполнение. Он отвечает на вопрос «работает ли продукт целиком», но не изолирует скорость модели.

На 27.10 исправленный контекст подготовлен и протестирован изолированным процессом; рабочие
сервисы ещё не обновлены. Готовый case.json ниже уже содержит исправленный реестр и периоды.
Чтобы оценивать их получение через живой UI, сначала нужна выкатка согласованных изменений engine
и навыков Ouroboros, включая исправление дедупликации. Сам тест генерации этого не требует.

## 1. Подготовленные файлы на сервере

Вход:

```bash
ssh rasul@85.198.84.50
B="$HOME/planner-bench-20261027-r2"
ls "$B"
```

Там лежат `planner_benchmark.py`, `case.json` и эта инструкция. Исходник скрипта в репозитории:
`aifa/scripts/planner_benchmark.py`. case.json содержит только два сообщения и схему предложения,
без ключей. Документ — тот же отчёт ЛУКОЙЛ, запрос — тот же, что в предыдущих UI-прогонах.

Один раз скопируй клиент и case в контейнеры:

```bash
docker exec aifa-sym-engine-1 mkdir -p /tmp/planner-bench/results
docker exec aifa-sym-ouroboros-1 mkdir -p /tmp/planner-bench/results
docker cp "$B/planner_benchmark.py" aifa-sym-engine-1:/tmp/planner-bench/planner_benchmark.py
docker cp "$B/case.json" aifa-sym-engine-1:/tmp/planner-bench/case.json
docker cp "$B/planner_benchmark.py" aifa-sym-ouroboros-1:/tmp/planner-bench/planner_benchmark.py
```

Ключ и адрес берутся из уже работающего окружения engine. Файлы окружения читать или печатать не
нужно. Контейнеры не перезапускаются. После пересоздания контейнера скопируй файлы заново.

## 2. Запустить одну модель три раза

Например, GLM-5.3 / low:

```bash
LABEL="glm53-low-$(date -u +%Y%m%dT%H%M%SZ)"
docker exec -e AIFA_LIVE_MODEL=1 aifa-sym-engine-1 \
  python /tmp/planner-bench/planner_benchmark.py \
  --case /tmp/planner-bench/case.json \
  --out "/tmp/planner-bench/results/$LABEL" \
  --models z-ai/glm-5.3 \
  --reasoning low --repeat 3 --concurrency 1 \
  --timeout 180 --max-tokens 14000
```

Без `AIFA_LIVE_MODEL=1` скрипт откажется выполнять платные вызовы. Timeout — абсолютный срок одного
вызова, включая streaming; max-tokens включает и рассуждения, и ответ.

Для нескольких моделей за один запуск:

```bash
LABEL="fast-models-$(date -u +%Y%m%dT%H%M%SZ)"
docker exec -e AIFA_LIVE_MODEL=1 aifa-sym-engine-1 \
  python /tmp/planner-bench/planner_benchmark.py \
  --case /tmp/planner-bench/case.json \
  --out "/tmp/planner-bench/results/$LABEL" \
  --models deepseek/deepseek-v4-pro deepseek/deepseek-v4-flash z-ai/glm-5.2 \
  --reasoning none --repeat 3 --concurrency 1
```

Здесь нужны OpenRouter ID **без** префикса `cloudru::`.

| Модель | Что передавать |
|---|---|
| `z-ai/glm-5.3` | `--reasoning low` или `high`; low не означает выключенные рассуждения |
| `z-ai/glm-5.2` | `--reasoning none` или `high` |
| `deepseek/deepseek-v4-pro` | `--reasoning none` или `high` |
| `deepseek/deepseek-v4-flash` | `--reasoning none` или `high` |
| `moonshotai/kimi-k2.5` | `--reasoning none` либо `on` |
| `minimax/minimax-m3` | `--reasoning on` для сравнения с выполненной серией |

Для чистого сравнения используй один case, одинаковые timeout/max-tokens, минимум три повтора
и concurrency=1. Concurrency=2 ускоряет серию, но добавляет конкуренцию за общие ресурсы.
Не запускай одновременно другую серию или UI-кейс, если измеряешь задержку.

## 3. Закрепить провайдера

OpenRouter может выбрать разные площадки для одной модели. Сохраняй поле provider каждого ответа.
Для контроля можно добавить:

```bash
--provider fireworks
```

Для DeepSeek Pro в предыдущей серии использовался `--provider azure`. Это меняет только конкретный
запрос: скрипт отправляет `provider.only` и выключает fallback, конфигурацию Infproxy не меняет.
Если площадка недоступна для модели, получишь ошибку вместо незаметного перехода на другую.
См. [маршрутизацию провайдеров OpenRouter](https://openrouter.ai/docs/guides/routing/provider-selection).

HTTP 400 «model not allowed» означает запрет allowlist Infproxy. Сам по себе `/v1/models` показывает
более широкий каталог и не доказывает возможность вызова через этот прокси.

## 4. Забрать результаты и проверить настоящий контракт

В engine обычно нет пакета Ouroboros, поэтому `schema_valid=true` — ещё не приёмка плана,
а `accepted=null` означает «полная проверка здесь недоступна». Сделай её в контейнере Ouroboros:

```bash
mkdir -p "$B/results"
docker cp "aifa-sym-engine-1:/tmp/planner-bench/results/$LABEL" "$B/results/$LABEL"
docker cp "$B/results/$LABEL" "aifa-sym-ouroboros-1:/tmp/planner-bench/results/$LABEL"
docker exec -u 0 -e PYTHONPATH=/app aifa-sym-ouroboros-1 \
  python /tmp/planner-bench/planner_benchmark.py \
  --validate "/tmp/planner-bench/results/$LABEL"
docker cp "aifa-sym-ouroboros-1:/tmp/planner-bench/results/$LABEL/." "$B/results/$LABEL/"
```

`-u 0` нужен только этому офлайн-валидатору: скопированные с хоста каталоги могут иметь другого
владельца и права 700. Он читает и обновляет JSON в `/tmp/planner-bench`, не управляет приложением.
`--validate` не делает сетевых вызовов. Использует `PlanProposal` и `PlanState.accept`: проверяет
зависимости, входы, покрытие требований и неразрешённые вопросы при новой ревизии 0. Если есть
отклонённые ответы, exit code = 1; это результат проверки, а не повод удалять ответы.
Проверка строгая: текст должен быть JSON без Markdown-ограждения, как запрашивается в этой серии.
Это не воспроизводит чтение завершённой подзадачи и проверку прав живого корня.

Каждый JSON содержит:

- `elapsed_sec` — до конца ответа или ошибки, включая неуспешные;
- `first_delta_sec` — первый reasoning или текст, `first_content_sec` — начало самого плана;
- `provider`, `http`, `finish_reason`, `usage`, `error`;
- `text` — полный ответ; `schema_valid` и `accepted` — две разные проверки.

`finish_reason=length` — исчерпан выходной лимит; даже если API ответил 200, готового плана может
не быть. Пустой текст с большим reasoning_tokens — это не быстрый успешный ответ.

Краткая сводка на сервере:

```bash
python3 - "$B/results/$LABEL" <<'PY'
import json, pathlib, statistics, sys
groups = {}
for path in pathlib.Path(sys.argv[1]).glob('*.json'):
    if path.name == 'case.json':
        continue
    r = json.loads(path.read_text())
    groups.setdefault((r['model'], r['reasoning'], r['provider']), []).append(r)
for key, rows in groups.items():
    seconds = [r['elapsed_sec'] for r in rows]
    accepted = sum(r.get('accepted') is True for r in rows)
    unknown = sum(r.get('accepted') is None for r in rows)
    print(key, 'n=', len(rows), 'median=', round(statistics.median(seconds), 2),
          'range=', (min(seconds), max(seconds)), 'accepted=', accepted,
          'not_validated=', unknown)
PY
```

## 5. Проверить качество вручную

Открой `text` у каждого формально принятого ответа и проверь:

1. Все требования запроса сохранены, годы и финансовые значения не придуманы.
2. Показатели и формулы берутся из полного реестра, атомы — из карточек каталога.
3. Независимые действия объединены в ход; поиск и зависимая загрузка — разные ходы.
4. После загрузки страниц один автор может разобрать текст, посчитать статистики и собрать Excel;
   дополнительные авторы нужны только при другом исполнителе между ними.
5. Автор не получает сеть; корень не получает задание самому считать или пересчитывать числа.
6. Есть три нужных листа, формулы Excel над исходными ячейками, источники, страницы и даты.
7. Не хватает наблюдений для корреляции, есть пропуски или нулевой знаменатель — запланирована
   явная недоступность результата, а не выдуманный показатель.

Формальная приёмка не проверяет смысл этих пунктов. Не выбирай модель только по двум секундам
до неправильного JSON или по первому токену.

## 6. Полный UI-прогон

После выкатки исправлений открой новый чат для каждого кейса, прикрепи тот же PDF, дождись загрузки,
разреши поиск и отправь тот же запрос. Не переиспользуй накопленные артефакты старого чата.
Запиши отдельно время «сообщение → запуск планировщика», длительность подзадачи, число возвратов
плана и время «сообщение → Excel». Это другие метрики, их нельзя смешивать с elapsed_sec выше.

Текущую настройку моделей можно посмотреть без ключей:

```bash
docker exec aifa-sym-ouroboros-1 python -c \
  'import os; print(os.environ.get("OUROBOROS_MODEL_BY_SKILL", ""))'
```

Для смены только планировщика редактируется `aifa/deploy/dev/ouroboros.env`, переменная
`OUROBOROS_MODEL_BY_SKILL`. Например, сохраняя автора на Flash/high:

```text
OUROBOROS_MODEL_BY_SKILL=distribution:aifa-plan-author=cloudru::z-ai/glm-5.3@low,distribution:aifa-atom-author=cloudru::deepseek/deepseek-v4-flash@high
```

Перед пересозданием — останови предыдущий прогон, дождись терминального статуса и проверь отсутствие
активных задач:

```bash
docker exec -i aifa-sym-ouro-postgres-1 sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At' <<'SQL'
select task_id,status from tasks where status in ('requested','running','queued','scheduled','interrupted','waiting_confirmation');
SQL
```

Когда список пуст и выбран момент выкатки:

```bash
cd "$HOME/aifa-sym"
set -a
. aifa/deploy/dev/.env
set +a
docker compose -p aifa-sym -f aifa/deploy/dev/compose.yml --profile app \
  up -d --no-deps --wait ouroboros
```

Эта команда применяет изменение окружения **уже собранного образа**. Она не добавляет новый код
или навыки в образ: исходную выкатку исправлений надо выполнить отдельно по dev README.
После смены модели ещё раз проверь фактический `OUROBOROS_MODEL_BY_SKILL` и используй новый чат.
Конфигурация общего Infproxy для этих шагов не меняется.

Для другого документа нужен новый case: свежий вопрос, профиль и периоды его отчёта, все страницы
его реестра и актуального каталога. Не заменяй только вопрос, оставляя метаданные старого PDF.
