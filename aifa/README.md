# AIFA — движок

Каталог содержит только движок Finengine. Фронт перенесён в
`../deferred/aifa-web/`; его сборка и развёртывание отложены.

## Корпоративный CI/CD

Контекст сборки — `aifa/`, как `workstation/` для Workstation:

| Файл | Назначение |
|---|---|
| `Dockerfile` | Два этапа сборки, корпоративные Python-образы и индексы пакетов |
| `desc.yaml` | Метаданные pipeline, Sonar, baseImage/runImage, ресурсы и несекретное окружение |
| `requirements.txt` | Версии production-зависимостей из `finengine/uv.lock` |
| `entrypoint.sh` | Запуск API на FINENGINE_HOST/FINENGINE_PORT, по умолчанию 0.0.0.0:8080 |
| `.dockerignore` | В сборку входят только код и необходимые ресурсы движка |

В `desc.yaml` скопированы wrapper/domain, владелец и закреплённый digest
корпоративного Python 3.12 из `workstation/desc.yaml`. Sonar key отдельный:
`aif-agents-aef-aifa`; проект с этим ключом должен быть доступен pipeline.
Отдельный `.gitlab-ci.yml` не добавляется: у эталонного Workstation его нет,
сборку запускает корпоративная платформа по дескриптору.

Pipeline передаёт `app.baseImage` как `BUILD_IMAGE`, `app.runImage` как `RUN_IMAGE`,
а также `TOKEN_OSC` и `NEXUS_TOKEN_USERNAME`/`NEXUS_TOKEN_PASSWORD`
(резервный вариант — `NEXUS_USERNAME`/`NEXUS_PASSWORD`). Эти данные объявлены
только на этапе builder. Не сохраняйте вывод Compose с подставленными секретами.
Оба образа должны использовать совместимые Python 3.12 и системные библиотеки.

В runtime из внутренних RPM-репозиториев устанавливаются `tesseract`,
`tesseract-langpack-rus`, `tesseract-langpack-eng`. При других именах пакетов
в образе передайте `DNF_PACKAGES`. Chromium отдельно не устанавливается.

### Обновление зависимостей

Production-зависимости перечислены в `finengine/pyproject.toml`;
`finengine/uv.lock` фиксирует их версии. В движке нет Git-зависимостей,
отдельных wheel из внешних репозиториев и зависимостей старых notebook/CoT-цепочек.
Библиотеки для кода, исполняемого в Workstation, задаются отдельно в Workstation.

После изменения списка зависимостей из корня репозитория:

```bash
uv lock --project aifa/finengine
uv export --offline --frozen --no-dev --no-emit-project --no-hashes --no-header --no-annotate --project aifa/finengine --output-file aifa/requirements.txt
```

`requirements.txt` — экспорт lock без ручных подмен пакетов.
Корпоративный Dockerfile устанавливает его через SberOSC/Nexus.

## Развёртывание

Jenkins, подключения платформы, ConfigMap и SecMan описаны в `deploy/README.md`.
Несекретные параметры заданы в `desc.yaml`; секреты читаются из файла
`SECMAN_SECRETS_FILE`. Для `/app/data` платформа предоставляет записываемое хранилище.

Прежний публичный рецепт оставлен отдельно как
`finengine/Dockerfile.public` с контекстом `aifa/finengine/`; корпоративный
CI и действующие Compose-файлы его не используют.
