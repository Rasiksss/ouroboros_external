# Dev-контур симбиоза: образы и compose-проект `aifa-sym`

Развёртывание dev как код (шаг 03 плана). Что именно выкатывать и когда — решает владелец
(`aifa-symbiosis/plan/PROGRESS.md` §2); порядок выкатки — ниже, проверен шагом 04.
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
`127.0.0.1:18180` → `/ready` ouroboros (ADR-0028) и, с шага 12, `127.0.0.1:13000` → фронтенд (вход
пользователей по SSH-туннелю, ниже); у Workstation и службы портов нет вовсе. Порт ouroboros
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
| `AIFA_ENGINE_TOKEN` | `python -c "import secrets; print(secrets.token_urlsafe(48))"` |

Один и тот же пароль стоит в `.env` и в адресе базы в `*.env` (и один и тот же ключ S3 — у
контейнера `s3` и у обоих продуктов); секрет Workstation — в `.env` и в `ouroboros.env`; `AIFA_ENGINE_TOKEN` —
одно значение в `ouroboros.env` и `engine.env` (пустой секрет закрывает внутренний API службы целиком, ADR-0028).
`OUROBOROS_USER_ENABLED_SKILL_IDS` в `ouroboros.env` на dev обновляется при выкатке шага 12: без допуска
`aifa-planning` и `aifa-engine` корень не прочитает навык, а `aifa_goal` отвечает `TOOL_UNAVAILABLE` (ADR-0047).

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

- Базы `workstation-svc:local` нет нигде, кроме нижних слоёв `workstation-svc-aifa:local` на dev:
  на dev её восстанавливает `workstation/restore_base.py` (порядок выкатки, п. 6). Локально слой
  комнаты проверяют на двойнике базы (UBI 8 + Python 3.12, пользователь `aef`, заглушки `ws-sandbox` и
  `busybox`) — `docker build -t workstation-svc:local` из такого `Dockerfile`. `entrypoint.sh` базы с CRLF:
  слой комнаты нормализует его (`sed`), иначе контейнер падает `no such file or directory` (P4).
- Образы собирались и контур поднимался (все сервисы `healthy`, ouroboros после `alembic upgrade head`
  из `entrypoint.sh`) в Linux-VM с Docker Engine; Docker Desktop для этого не обязателен.
- Образ ouroboros ставит зависимости не через `uv sync --frozen`, а через `uv export --frozen` и
  `uv pip install --require-hashes` из `pypi.org`: все 137 пакетов замка ссылаются на
  `nexus.genai.int` (ADR-0034, предложено). Версии и хэши — те же, что в замке.
- Образу службы нужен доступ к `github.com` при сборке (`mmar-carl` стоит в её замке из git).
- Проверка без docker: `aifa/scripts/check.sh` (F9: `test_dev_config.py`, `test_dev_images.py`).

## Порядок выкатки на dev (шаг 04, проверен 15.10.2026)

Хост `85.198.84.50`, `ssh rasul@…`, группа `docker`, `sudo` без пароля. Всё — только проект `aifa-sym`;
перед каждой `docker`-операцией в имени цели нет `prod`. Длинное — под `nohup` с логом в файл.

1. **Сигнал и предусловия** — записаны в `PROGRESS.md` §2 (сигнал «выкатывай», О2, список чистки).
2. **Снимок «до»** → `~/aifa-sym-before-<дата>.txt` на сервере: `docker ps -a`, `df -h /`, `free -m`,
   `docker system df`, образы, время старта каждого контейнера (`docker inspect … State.StartedAt`).
3. **Чистка** — строго по утверждённому списку (шаг 04: только кэш сборки, `docker builder prune`);
   после — `df -h /`. Образов `finengine-backend` и каталогов `~/aifa-*` без отдельного слова не трогаем.
4. **Код**: `git bundle create x.bundle sym/NN-…` → `scp` → `git clone -b sym/NN-… x.bundle ~/aifa-sym`;
   дальнейшие правки — инкрементальный bundle (`<база>..<ветка>`) и `git pull x.bundle <ветка>`.
   Не `tar | ssh`; `/opt/finagent*` не трогаем.
5. **Секреты** — `aifa/deploy/dev/.env` и `ouroboros.env` из `*.example`, права 600, вне git; пароли и
   ключ шифрования генерируются на сервере; ключ Cloud.ru вставляется владельцем (в шаге 04 — из `.env`
   старого стенда ouroboros).
6. **База Workstation** (на dev нет тега `workstation-svc:local`, только `workstation-svc-aifa:local` с
   кодом finengine и мостом — как база он не годится):
   ```bash
   docker save workstation-svc-aifa:local -o ~/aifa-sym-room.tar
   python3 aifa/deploy/dev/workstation/restore_base.py ~/aifa-sym-room.tar ~/aifa-sym-base.tar
   docker load -i ~/aifa-sym-base.tar            # Loaded image ID: sha256:<id>
   docker tag sha256:<id> workstation-svc:local
   rm -f ~/aifa-sym-room.tar ~/aifa-sym-base.tar
   ```
   Образ комнаты старого AIFA не меняется: создаётся только новый тег на его нижние слои.
7. **Сборка**: `docker compose -p aifa-sym -f aifa/deploy/dev/compose.yml build workstation ouroboros`
   и `pull s3 s3-init ouro-postgres` (≈1,7 ГБ новых образов: SeaweedFS 724 МБ, aws-cli 649 МБ).
8. **Подъём без профиля `app`**: `docker compose -p aifa-sym -f aifa/deploy/dev/compose.yml up -d --wait`.
9. **Дымовые проверки**: `aifa/deploy/dev/smoke/run.sh` (ниже).
10. **После**: `docker ps` prod-контейнеров и время их старта равны снимку «до»; `/health` старого AIFA.

Профиль `app` (`engine`, `engine-postgres`, `frontend`) — с шага 12, порядок — раздел «Выкатка шага 12».

### Дымовые проверки (`smoke/`)

`smoke/run.sh [1…9]` — 1–5 одноразовые контейнеры из образа ouroboros в сети `aifa-sym-internal`;
секрет Workstation берётся из `.env` и не печатается. 6–9 (шаг 12) — `python3 smoke/api.py` на хосте через
loopback-порт фронтенда; пароль `qa-a` — из `~/aifa-sym-qa.env` (`AIFA_SYM_QA_A_PASSWORD`).

| № | Что | Критерий |
|---|---|---|
| 1 | `/ready` ouroboros через `127.0.0.1:18180` | HTTP 200 |
| 2 | WS `ws://ouroboros:8080/ws` (`ws_chat.py`) | без `X-Ouroboros-Owner-Id` — рукопожатие отклонено; с `X-Ouroboros-Owner-Id: smoke`, `X-Ouroboros-Chat-Id: smoke-1` — ответ модели на «привет» |
| 3 | `code_exec` в Workstation контура | ответ содержит «4», в логе ouroboros — `POST …/v1/sessions/<id>/exec:stream` к `workstation:8080` |
| 4 | C5.2: разовая комната по протоколу `sealed_room` (`room.py`) | `numpy`, `pandas`, `openpyxl`, `pptx`, `docx` импортируются; сокет заблокирован; переменных окружения службы нет |
| 5 | Пределы Workstation, ADR-0039 (`room.py --limits`, `docker inspect`) | у exec `RLIMIT_CPU` 300 с и `RLIMIT_AS` 1 ГиБ, 1,5 ГБ не выделяются, `timeoutSec` 301 отклонён; у контейнера память 1,5 ГБ без swap, 1,5 CPU, 256 процессов |
| 6 | `/api/health` через фронтенд (P31) | HTTP 200: перезапись `/api/*` дошла до службы |
| 7 | `/build-id` фронтенда | равен `AIFA_SYM_BUILD_ID` из `.env` |
| 8 | вход `qa-a` | `/auth/login` и `/auth/me` — 200 |
| 9 | ход «привет» (`POST /chat/turns` → поток событий) | `completed` и непустой ответ ouroboros |

Пределы Workstation (ADR-0039) держит контейнер, а не песочница: её cgroup-настройки
(`SESSION_MEMORY_MAX` и др.) в контейнере не действуют, действуют только rlimit одного exec и
потолок `EXEC_TIMEOUT_SEC`.

### Откат

```bash
cd ~/aifa-sym
docker compose -p aifa-sym -f aifa/deploy/dev/compose.yml down        # контейнеры и сеть; тома остаются
```

Тома `aifa-sym-*` (`ouro-data`, `ouro-pgdata`, `s3-data`) **без команды владельца не удаляются**
(`down -v` — нельзя). Тег `workstation-svc:local` — только новая ссылка на слои старой комнаты, его
удаление (`docker rmi workstation-svc:local`) старый AIFA не затрагивает. Чистка образов
`aifa-sym-*`, `chrislusf/seaweedfs`, `amazon/aws-cli` — отдельным решением.

## Выкатка шага 12: служба и фронтенд (профиль `app`)

Сигнал владельца — `PROGRESS.md` §2 (19.10.2026). Порядок — как в «Порядке выкатки» выше, плюс:

1. **Окружение на сервере** (секреты не печатаются, права 600): `.env` — `AIFA_SYM_ENGINE_PG_PASSWORD`,
   `AIFA_SYM_BUILD_ID=<коммит>`; `ouroboros.env` — значения списков и допуска навыков из примера,
   `AIFA_ENGINE_URL`, `AIFA_ENGINE_TOKEN` (общий со службой); `engine.env` — из `engine.env.example`:
   секреты генерируются на сервере, пароль базы = `AIFA_SYM_ENGINE_PG_PASSWORD`, ключи S3 и секрет Workstation = как в
   `.env`. Ключа Tavily нет — строка не пишется, поиск в сети выключен.
2. **Шлюз и модель службы** — Cloud.ru напрямую (ADR-0049), модель DeepSeek-V4-Pro (ADR-0050; ADR-0048 про GLM-5.2 заменена): в `engine.env` на сервере `FINENGINE_OPENROUTER_BASE_URL=https://foundation-models.api.cloud.ru/v1` и `OPENROUTER_API_KEY` — тот же ключ, что `CLOUDRU_FOUNDATION_MODELS_API_KEY` в `ouroboros.env` (копируется на сервере без вывода). Через `infproxy` не ходим: он отдавал 500 (обрыв между ним и Cloud.ru) и ответы по 5–25 мин. Замер 19.10.2026 прямого DeepSeek на запросе классификатора: 30 / 105 / 37 с (при пределе разбора 120 с); в тот же день он рвал соединение на 40-й и 104-й секунде. Лимит ключа — 200 запросов в минуту на модель, его делят ouroboros, служба и старый dev.
3. **Сборка и подъём**: `docker compose -p aifa-sym -f aifa/deploy/dev/compose.yml --profile app build engine frontend
   ouroboros` (с `AIFA_SYM_BUILD_ID` из `.env`; `set -a; . aifa/deploy/dev/.env; set +a`), затем `--profile app up -d
   --wait`. Миграций нет: таблицы службы создаёт её старт (`create_all`), схему ouroboros — `alembic upgrade head` из его
   `entrypoint.sh`. Сбой `apt-get` при сборке (connection reset) — повторить.
4. **Учётки `qa-a`, `qa-b`** — штатным путём AIFA: вход admin (`FINENGINE_ADMIN_*` из `engine.env`) → `POST /auth/invites`
   → `POST /auth/register`. Пароли генерируются на сервере и лежат в `~/aifa-sym-qa.env` (600); владельцу их передают
   вне git и вне чата.
5. **Дымовые**: `aifa/deploy/dev/smoke/run.sh` (1–9). Сценарии S0–S5 — `smoke/scenarios/*.json` через
   `aifa/tests/golden/golden_cli.py record` с `AIFA_GOLDEN_BASE_URL=http://127.0.0.1:13000/api`; документ ЛУКОЙЛ
   («Бухгалтерская отчётность на 30.06.2026», 24 стр., скан) в git не хранится — кладётся рядом со сценарием как
   `lukoil-6m2026.pdf`.

### Вход пользователей: SSH-туннель

```bash
ssh -N -L 13000:127.0.0.1:13000 rasul@85.198.84.50      # затем http://127.0.0.1:13000/v2
```

В `finengine-caddy-1` ничего не добавляется. Пароли `qa-a`, `qa-b` — `~/aifa-sym-qa.env` на сервере.

### Что развёрнуто (19.10.2026)

Контур `aifa-sym`: `ouroboros`, `ouro-postgres`, `s3`, `workstation`, `engine`, `engine-postgres`, `frontend`; код — ветка
`sym/12-vykatka-aifa-sym`, сборка фронтенда — `AIFA_SYM_BUILD_ID` из `.env`. Образы `engine`/`ouroboros` собраны с
коммита `044ce6a` (позже менялись только compose, тесты и документы). По просьбе владельца на время проверок
остановлены (`docker stop`, не удалены) приложения старого AIFA: `finengine-backend-1`, `finengine-frontend-1`,
`workstation-svc`, `openclaw-dev`; вернуть — `docker start` этих четырёх. Базы, Caddy, `infproxy` и prod не трогались.

### Известные ограничения

- Диск dev: 2,9–5,3 ГБ свободно; полная пересборка трёх образов занимает до 4 ГБ кэша — после неё `docker builder prune -f`
  (единственная разрешённая чистка).
- Модели Cloud.ru работают неустойчиво (19.10.2026): вызовы извлечения (`read.value`) занимают до 6 мин, первый вызов бывает без ответа
  до клиентского таймаута 360 с, на длинных вызовах 500 и 502. Поэтому S0–S5 шага 12 не пройдены (`PROGRESS.md` §3, P83, P85).
  Для следующих шагов это не блокер; повтор сценариев — когда Cloud.ru стабилен (`smoke/scenarios`, `~/aifa-sym-s12/seq3.sh`).
  Служба ходит в Cloud.ru напрямую, без `infproxy` (ADR-0049: он отдавал 500 из-за обрыва до Cloud.ru); модели — как у старого AIFA (ADR-0050).
- Поиск в сети (Tavily) выключен. Внутренние ручки старого пути OpenClaw (`FINENGINE_OPENCLAW_GATEWAY_TOKEN`) без
  токена отвечают отказом — путь уходит шагом 19.
- Документ, разбор которого оборвался сбоем хранилища, остаётся «в обработке» до следующего хода, который снова просит
  разбор (P79-класс; описан в шаге 11).

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

## Контрактный профиль (шаг 07)

`compose.contract.yml` — отдельный локальный проект `aifa-sym-contract`: настоящий ouroboros с
настройками `ouroboros.env.example`, PostgreSQL, S3 и подставная модель (`aifa/tests/stub_model`);
без Workstation и службы. Нужен контрактным тестам (`aifa/tests/contract/README.md`); отдельным
файлом, а не профилем в `compose.yml`, потому что F9 держит в нём ровно сервисы dev-контура и одну
сеть. На общий dev не выкатывается, контейнеры `aifa-sym` не затрагивает.

## Открытое

- **ADR-0037 (решение владельца)**: в `OUROBOROS_MULTIUSER=true` исполняемые навыки, в том числе
  расширение `aifa-engine`, скрыты от задач. Контур годится для проверок 0.6, но не для хода через
  `aifa-engine`, пока вариант не выбран.
- ADR-0036 (предложено): допуск навыка `workstation` (`OUROBOROS_USER_ENABLED_SKILL_IDS`), подтверждённые
  списки инструментов и навыков. Пока она не принята, значение стоит в `ouroboros.env` на dev.
- Секрет создания сессии этим Workstation не проверяется (в `app/` нет такой настройки, P4);
  защита контура — сеть без опубликованных портов (ADR-0028).
- На dev свободно ≈2 ГБ (критерий 0.5 «≥ 8 ГБ» не достигнут: чистка ограничена кэшем сборки).
- Адрес шлюза моделей службы (`FINENGINE_OPENROUTER_BASE_URL`) берётся из окружения старого dev.
