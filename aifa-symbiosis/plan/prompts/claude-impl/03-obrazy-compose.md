# Реализация · шаг 03 · Образы и compose dev

Ты — кодовый агент симбиоза AIFA и ouroboros (Claude Code, Sonnet 5.5, усилие High). Эта сессия —
ровно один шаг плана `aifa-symbiosis/plan/README.md`. Рабочая папка —
`C:\Users\magom\Desktop\ouroboros`. `Krab_ext` — только чтение.

## Прочитай до работы
1. `aifa/CLAUDE.md`.
2. `aifa-symbiosis/plan/PRINCIPLES.md`.
3. `aifa-symbiosis/plan/PROGRESS.md` — записи шагов 01–02, §4 (P3, P4).
4. ADR-0002, ADR-0007, ADR-0010, ADR-0013, ADR-0016, ADR-0017, ADR-0018, ADR-0020, ADR-0027, ADR-0028.
5. `aifa-symbiosis/architecture/ARCHITECTURE.md` §4.2 (контейнеры); `FITNESS.md` F9.
6. `aifa-symbiosis/PROJECTS_STATE.md` §2.5 (как старая копия ouroboros собиралась на публичных
   образах) и §3 (сервер).
7. Справочно, только чтение: `Krab_ext/AIFA-CLAW/deploy/Dockerfile.backend`, `Dockerfile.frontend`,
   `docker-compose.yml`, `docker-compose.dev.yml`, `workstation-aifa/Dockerfile`; корневые
   `Dockerfile*` ouroboros и `docs/SETTINGS.md` ouroboros (разделы о PostgreSQL, S3, браузерном
   входе, Workstation, инструментах и навыках).

## Постановка задачи
Описать развёртывание dev-контура симбиоза как код: образы ouroboros, службы, фронтенда и
Workstation, compose-проект `aifa-sym`, примеры окружения — и закрепить конфигурацию проверкой F9.

## Описание
- `aifa/deploy/dev/`:
  - `ouroboros.Dockerfile` — контекст корень репозитория, публичная база `python:3.12-slim`,
    `uv sync --frozen`, запуск `server.py`; исключения контекста — **`ouroboros.Dockerfile.dockerignore`
    рядом с Dockerfile** (BuildKit), а не корневой `.dockerignore` (F1): исключить `aifa/`,
    `aifa-symbiosis/`, `.venv`, кэши;
  - `finengine.Dockerfile` — контекст `aifa/finengine`, `uv sync --frozen --project …`;
  - `frontend.Dockerfile` — контекст `aifa/web`, по образцу `Dockerfile.frontend` AIFA (`BUILD_ID`);
  - `workstation/Dockerfile` — `FROM workstation-svc:local` + слой пакетов комнаты (ADR-0007),
    обход `/etc/mime.types` как в AIFA; без finengine и моста моделей;
  - `compose.yml` — проект `aifa-sym`: `ouroboros`, `ouro-postgres`, `s3` (MinIO), `workstation`;
    `engine` и `frontend` — под профилем `app` (поднимаются с шага 12); одна внутренняя сеть;
    публикация портов — только `127.0.0.1` для `/ready` ouroboros (ADR-0028); у Workstation —
    никаких; тома с именами `aifa-sym-*`. Где PostgreSQL/S3 — по О2 (если не решено — вариант
    «новые контейнеры» с пометкой «до решения О2»);
  - `ouroboros.env.example` — обязательный набор ADR-0027 (`OUROBOROS_MULTIUSER=true`,
    `BROWSER_INGRESS_ENABLED=true`, `OUROBOROS_WORKER_SLOTS=4`, `OUROBOROS_HARD_TIMEOUT_SEC=2700`,
    `OUROBOROS_MEMORY_DISTILL=false`, подтверждение выключено, `WORKSTATION_*`, модели Cloud.ru,
    PostgreSQL/S3, ключи шифрования — заглушками); списки `OUROBOROS_ENABLED_TOOLS`,
    `OUROBOROS_DISABLED_TOOLS`, `OUROBOROS_DISABLED_SKILLS` — черновые с пометкой «уточняется в шаге
    04 (P3)»; `engine.env.example` — по `Settings` службы (без OpenClaw-переменных не обойтись до
    шага 19 — оставить, пометить);
  - `README.md` — как собрать локально, как поднимать на dev (наброски; точный порядок выкатки
    допишет шаг 04).
- **F9** в `aifa/tests/architecture/test_dev_config.py` — по `FITNESS.md`: обязательные значения
  и снимок списков; нет публикации портов кроме loopback; нет `prod` в именах; нет ссылок на
  контейнеры prod как цели; адреса баз службы и ouroboros различны. С тестом-«нарушителем».
- **Локальная сборка**: `ouroboros` и `finengine` собираются локально (Docker Desktop); если
  Docker недоступен — сборка переносится в шаг 04, запись в §4. `workstation` локально не
  собирается (базы нет) — это P4.

## Образ целевого результата
- `docker compose -p aifa-sym -f aifa/deploy/dev/compose.yml config` валиден.
- Образы `ouroboros` и `finengine` собираются; контейнер ouroboros стартует локально хотя бы до
  ошибки «нет базы» (без падения импорта).
- F9 зелёная на дереве, красная на «нарушителе»; ворота зелёные.

## Границы
Не делать: ничего на стенде, ни одного подключения к серверу кроме чтения справочных файлов (не
нужно — всё есть в `Krab_ext`); код службы; секреты в git.

## Правила шага
- Dockerfile — минимальные, без «на будущее»; каждое `ENV` объяснимо ссылкой на ADR.
- Ключи шифрования ouroboros (`OUROBOROS_CREDENTIALS_ENCRYPTION_KEY` — ровно 64 байта base64,
  инвариант 8) — только заглушка в примере с указанием, как сгенерировать.

## Проверки готовности
| Что | Чем |
|---|---|
| Конфигурация по ADR-0027/0028/0018 | F9 |
| ouroboros не тронут | F1 (dockerignore рядом с Dockerfile, не в корне) |
| Сборка | `docker build` двух образов (или запись о переносе в шаг 04) |

## Конец сессии
По `PRINCIPLES.md` §Д. `PROGRESS.md` §1: шаг 03 — «готово» или «остановлено».
