# ADR-0034. Образы dev: установка ouroboros по замку без `uv sync`, состав образов

Статус: принято (владелец, 15.10.2026; предложено сессией шага 03)
Уточняет: ADR-0020 (раздел «Образы»)

## Контекст
- Корневой `uv.lock` (ouroboros, 137 пакетов) ссылается только на `nexus.genai.int`: у каждого
  пакета `source = { registry = "https://nexus.genai.int/…" }`, у каждого колеса и sdist — URL в
  nexus. Проверено 15.10.2026: `uv sync --frozen --no-dev --no-install-project --no-config
  --default-index https://pypi.org/simple` идёт за `…/python_multipart-0.0.32-…whl` на
  `nexus.genai.int` и падает; `--default-index` URL замка не подменяет. Замок править нельзя
  (F11, ADR-0020, ADR-0030). Старая копия ouroboros на dev по той же причине собиралась из
  `requirements-public.txt` (`PROJECTS_STATE.md` §2.5).
- `uv export --frozen --no-dev --no-emit-project --no-config` читает замок без сети и даёт 113
  пакетов с точными версиями и `sha256`; `uv pip install --require-hashes --index-url
  https://pypi.org/simple` установил все 113 (≈30 с). Хэши — файлов PyPI, nexus их только
  проксирует, поэтому состав и целостность те же, что у замка.
- ADR-0020 предполагал `.dockerignore`, исключающий `aifa/`; корневой `.dockerignore` — файл
  ouroboros (F1). BuildKit читает `<Dockerfile>.dockerignore` рядом с Dockerfile.
- Служба ищет `deploy/tools_whitelist.yaml` и `deploy/certs/` рядом с `src/`
  (`aifa/finengine/src/finengine/llm/tools.py:2028`, `net/ru_gov_tls.py:38`; в образе — `/app/deploy/`)
  и `station-skills/` рядом с `src/` (`skills/analysis/methodology.py:142`,
  `skills/extraction/catalogue.py:239`; в образе — `/app/station-skills/`). Без белого списка
  служба открывает все инструменты (`tools.py:2032`), без сертификатов Росстат закрыт (P15).

## Решение
1. **Образ `ouroboros`** ставит зависимости из замка командами `uv export --frozen` и
   `uv pip install --require-hashes` из `pypi.org` в `/opt/venv`; `uv sync --frozen` в нём не
   используется (из публичной сети он невозможен). Образ `finengine` — `uv sync --frozen
   --project` по замку службы (он разрешён по PyPI, шаг 01).
2. Исключения контекста — `<имя>.Dockerfile.dockerignore` рядом с Dockerfile; корневой
   `.dockerignore` не правится.
3. `tools_whitelist.yaml` и `certs/` AIFA лежат в `aifa/finengine/deploy/` — там, где их ищет
   код (`parents[3]/deploy`), и в чекауте, и в образе (`/app/deploy/`). Контекст образа остаётся
   `aifa/finengine` (ADR-0020), дополнительный контекст не нужен. Раскладка `aifa/finengine/` в
   `ARCHITECTURE.md` §3 дополнен каталогом `deploy/` (принятие записи). Остальные
   части `deploy/` AIFA (шлюз OpenClaw, станция, мост моделей, `infproxy`, prod) не переносятся:
   они умирают шагом 19 (ADR-0009).
4. Образ службы собирается из того, что код действительно использует: без Chromium Playwright
   (нужен только новостным коннекторам, ≈0,5 ГБ при 1,6 ГБ свободного диска dev, P17), без
   NLTK-данных, poppler и ghostscript (в `src/` нет `import nltk`, `pdf2image`, вызовов `gs`);
   tesseract с `rus` и `eng` — есть (`ingestion/pdf_parser.py`). `station-skills/` копируется,
   пока код службы его читает (ADR-0032, до шага 19). Сводка демо-снимков (`deploy/demo/`, ручка
   `/demo/load`) не переносится.
5. Снимок F9 — `aifa/contracts/dev_config.snapshot.json`: изменение набора настроек ouroboros
   идёт через F13, то есть вместе с ADR (ADR-0027).
6. Образ ouroboros запускается через `entrypoint.sh` ouroboros (`alembic upgrade head`, затем
   команда): без актуальной схемы сервер отказывается стартовать (`assert_schema_at_head`),
   `Dockerfile.local` этого не делает — проверено на живой базе.
7. S3 контура — SeaweedFS (`chrislusf/seaweedfs`), бакеты создаёт `amazon/aws-cli`: образов
   `minio/minio` в публичных реестрах больше нет (Docker Hub, quay.io — проверено 15.10.2026,
   ADR-0035). Ouroboros и служба говорят с ним по S3 API, обе проверены на старте.

## Следствия
- Установка ouroboros в образ зависит от доступности `pypi.org` в момент сборки, а не nexus;
  версии и содержимое пакетов фиксирует замок (`--require-hashes`).
- Обновление снимка ouroboros (ADR-0030) новым `uv.lock` ничего в Dockerfile не меняет.
- Если у владельца будет доступ к nexus с dev, пункт 1 можно заменить на `uv sync --frozen`
  новой записью.

## Чем проверяется
F9 (`test_dev_images.py`: публичные базы, доставка белого списка и сертификатов, dockerignore
рядом с Dockerfile), F11 (замок ouroboros не менялся), F1; тесты службы
`test_ru_gov_trust_is_scoped.py`, `test_llm_tool_wiring.py` (ходят к `aifa/finengine/deploy/`).
