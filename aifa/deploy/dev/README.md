# Dev-контур симбиоза: образы и compose-проект `aifa-sym`

Развёртывание dev как код (шаг 03 плана). Что именно выкатывать и когда — решает владелец
(`aifa-symbiosis/plan/PROGRESS.md` §2); порядок выкатки наброском ниже, точный допишет шаг 04.
Правила стенда — `aifa/CLAUDE.md` §10: только compose-проект `aifa-sym`, **prod не трогать**,
старый AIFA на dev (`finengine-*`, `openclaw-dev`, `workstation-svc`) работает рядом до шага 21.

## Состав

| Файл | Что |
|---|---|
| `compose.yml` | проект `aifa-sym`: `ouroboros`, `ouro-postgres`, `s3` (+ разовый `s3-init`), `workstation`; профиль `app` (с шага 12): `engine`, `engine-postgres`, `frontend` |
| `ouroboros.Dockerfile` (+ `.dockerignore` рядом) | образ ouroboros; контекст — корень репозитория |
| `finengine.Dockerfile` (+ `.dockerignore`) | образ службы движка; контекст — `aifa/finengine` |
| `frontend.Dockerfile` (+ `.dockerignore`) | образ фронтенда; контекст — `aifa/web` |
| `workstation/Dockerfile` | Workstation контура: `workstation-svc:local` + слой пакетов комнаты |
| `ouroboros.env.example` | окружение ouroboros — обязательный набор ADR-0027 (проверяет F9) |
| `engine.env.example` | окружение службы по её `Settings` |
| `infra.env.example` | пароли контейнеров инфраструктуры → `.env` рядом с `compose.yml` |

Исключения контекста сборки лежат рядом с Dockerfile (`<имя>.Dockerfile.dockerignore`, BuildKit):
корневой `.dockerignore` — файл ouroboros, его не правим (F1).

## Сеть и доверие

Одна сеть `aifa-sym-internal`, тома `aifa-sym-*`. Единственный опубликованный порт —
`127.0.0.1:18180` → `/ready` ouroboros (ADR-0028); у Workstation портов нет вовсе. Порт ouroboros
не должен стать доступным снаружи: с доверенными заголовками любой, кто до него дотянется, станет
любым владельцем. Всё это держит F9.

## Подготовка окружения

```bash
cd aifa/deploy/dev
cp ouroboros.env.example ouroboros.env
cp engine.env.example engine.env
cp infra.env.example .env          # compose читает его сам
```

Заглушки `CHANGE_ME` заменить; файлы `*.env` и `.env` в git не попадают. Генерация:

| Что | Команда |
|---|---|
| `OUROBOROS_CREDENTIALS_ENCRYPTION_KEY` (ровно 64 байта base64, инвариант 8) | `openssl rand -base64 64 \| tr -d '\n'` |
| пароли баз, ключи S3, `WORKSTATION_CREATE_SECRET` | `openssl rand -hex 24` |
| `FINENGINE_AUTH_SECRET` | `python -c "import secrets; print(secrets.token_urlsafe(64))"` |

Один и тот же пароль стоит в `.env` и в адресе базы в `*.env` (и один и тот же ключ S3 — у
контейнера `s3` и у обоих продуктов); секрет Workstation — в `.env` и в `ouroboros.env`.

## Проверка и сборка

```bash
# конфигурация compose (docker daemon не нужен)
docker compose -p aifa-sym -f aifa/deploy/dev/compose.yml config
docker compose -p aifa-sym -f aifa/deploy/dev/compose.yml --profile app config --services

# образы
docker compose -p aifa-sym -f aifa/deploy/dev/compose.yml build ouroboros          # локально и на dev
docker compose -p aifa-sym -f aifa/deploy/dev/compose.yml --profile app build engine frontend
docker compose -p aifa-sym -f aifa/deploy/dev/compose.yml build workstation        # только на dev
```

- Базы `workstation-svc:local` нет нигде, кроме dev (P4). Слой комнаты проверен на локальном
  двойнике базы (UBI 8 + Python 3.12, пользователь `aef`, заглушки `ws-sandbox` и `busybox`):
  `docker build -t workstation-svc:local` из такого `Dockerfile`, затем `build workstation`.
  Двойник только для сборки слоя; настоящий Workstation — образ dev.
- Образы собирались и контур поднимался (все сервисы `healthy`, ouroboros после `alembic upgrade head`
  из `entrypoint.sh`) в Linux-VM с Docker Engine; Docker Desktop для этого не обязателен.
- Образ ouroboros ставит зависимости не через `uv sync --frozen`, а через `uv export --frozen` и
  `uv pip install --require-hashes` из `pypi.org`: все 137 пакетов замка ссылаются на
  `nexus.genai.int` (ADR-0034, предложено). Версии и хэши — те же, что в замке.
- Образу службы нужен доступ к `github.com` при сборке (`mmar-carl` стоит в её замке из git).
- Проверка без docker: `aifa/scripts/check.sh` (F9: `test_dev_config.py`, `test_dev_images.py`).

## Запуск (набросок; порядок выкатки допишет шаг 04)

1. Сигнал владельца «выкатывай» записан в `PROGRESS.md` §2; цель — compose-проект `aifa-sym`, в
   имени нет `prod`.
2. На хосте свободно ≥ 3 ГБ (P17: сборка упёрлась в диск у 97 %) и памяти хватает рядом со
   старым AIFA (P5: ~3,3 ГБ; потолки — `mem_limit` в `compose.yml`).
3. Доставка исходников на dev — `git bundle` или `scp` нескольких файлов, не `tar | ssh`; серверное
   дерево старого AIFA (`/opt/finagent`) не трогается — у `aifa-sym` своя копия.
4. `build` → `up -d` → `curl -fsS http://127.0.0.1:18180/ready` на хосте; ouroboros стартует
   только с базой и корзиной (без них падает первым — это ожидаемо).
5. Профиль `app` — с шага 12.

Старый AIFA на dev (сторож живого хода, бэкап `~/p1-backup-*`, накладывание поверх
серверного состояния) выкатывается по разделу 7а `CLAUDE.md` AIFA-CLAW (в этот репозиторий не
перенесён, ADR-0032 п. 3); для `aifa-sym` он не нужен: проект новый, чужого состояния в нём нет.

## Чем образы отличаются от образов AIFA

| Не перенесено | Почему |
|---|---|
| Chromium Playwright | только новостные коннекторы; ≈0,5 ГБ при 1,6 ГБ свободного диска (P17) |
| NLTK-данные, poppler, ghostscript | в `src/` нет `import nltk`, `pdf2image`, вызовов `gs` |
| `deploy/demo/` (ручка `/demo/load`) | демо-снимки не нужны контуру симбиоза |
| мост моделей, код службы в Workstation | ADR-0007: слой комнаты — только пакеты |
| Redis | `FINENGINE_REDIS_URL` пуст — шина в процессе; один воркер |
| MinIO | заменён SeaweedFS: образов `minio/minio` нет ни на Docker Hub, ни на quay.io (ADR-0035) |
| Caddy, Dozzle | общий `finengine-caddy-1` не трогаем (ADR-0028) |

## Открытое

- О2 (где PostgreSQL и S3): здесь «новые контейнеры» — до решения владельца.
- P3: списки инструментов и навыков черновые — уточняются по живому экземпляру в шаге 04.
- P4: имя переменной секрета создания сессии на стороне сервиса Workstation и `ENTRYPOINT`/`CMD`
  базы `workstation-svc:local` проверить на dev первой сборкой.
- Версии пакетов комнаты (`numpy`, `pandas`, …) не закреплены, как и в слое AIFA; снять
  `pip freeze` со старой комнаты и закрепить (P28).
- Адрес шлюза моделей службы (`FINENGINE_OPENROUTER_BASE_URL`) берётся из окружения старого dev.
