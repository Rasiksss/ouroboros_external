ARG BUILD_IMAGE
ARG RUN_IMAGE

FROM ${BUILD_IMAGE} AS builder
USER root
WORKDIR /app

ARG NEXUS_USERNAME
ARG NEXUS_PASSWORD
ARG TOKEN_OSC
ARG NEXUS_TOKEN_USERNAME
ARG NEXUS_TOKEN_PASSWORD

COPY requirements.txt ./
RUN if command -v python >/dev/null 2>&1; then \
        python -m venv /opt/venv; \
    else \
        python3 -m venv /opt/venv; \
    fi
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --upgrade pip \
        --index-url "https://token:${TOKEN_OSC}@sberosc.sigma.sbrf.ru/repo/pypi/simple" \
        --extra-index-url "https://${NEXUS_TOKEN_USERNAME:-${NEXUS_USERNAME}}:${NEXUS_TOKEN_PASSWORD:-${NEXUS_PASSWORD}}@nexus-ci.delta.sbrf.ru/repository/pypi-release/simple" \
        --trusted-host sberosc.sigma.sbrf.ru \
        --trusted-host nexus-ci.delta.sbrf.ru \
        --default-timeout 120 --no-cache-dir --progress-bar off \
        -r requirements.txt

# PyInstaller нужен обёртке AEF (wrapper 1.0.0.aef.fastapi): она собирает бандл из main.py.
# Отдельного extra у проекта нет — ставим только имя.
RUN pip install --upgrade pip \
        --index-url "https://token:${TOKEN_OSC}@sberosc.sigma.sbrf.ru/repo/pypi/simple" \
        --extra-index-url "https://${NEXUS_TOKEN_USERNAME:-${NEXUS_USERNAME}}:${NEXUS_TOKEN_PASSWORD:-${NEXUS_PASSWORD}}@nexus-ci.delta.sbrf.ru/repository/pypi-release/simple" \
        --trusted-host sberosc.sigma.sbrf.ru \
        --trusted-host nexus-ci.delta.sbrf.ru \
        --default-timeout 120 --no-cache-dir --progress-bar off \
        pyinstaller

# 🔴 FINENGINE — УСТАНОВЛЕННЫЙ ПАКЕТ, А НЕ КАТАЛОГ В PYTHONPATH (15.11.2026).
# Пакет нужен как минимум для двух вещей: `main.py` импортирует `finengine.api.main`,
# а каталог с данными (`POLICY.md`, `.yaml`-справочники) копируется в образ из
# site-packages. Сборка из исходников, зависимости не тянутся (`--no-deps`): они
# уже стоят выше по requirements.txt. `--no-build-isolation` — hatchling не полезет
# в сеть за собой. `import` в конце — проверка, что пакет собран целиком: без неё
# ошибка `No module named 'finengine.api'` всплывёт только в поде.
COPY finengine /tmp/finengine
RUN pip install --no-build-isolation --no-deps --no-cache-dir /tmp/finengine \
    && rm -rf /tmp/finengine \
    && python -c "import finengine.api.main; print('finengine import OK')"

FROM ${RUN_IMAGE}
USER root
WORKDIR /app

# Corporate Python images use RPM repositories, not Debian apt repositories.
# OCR is used by finengine.ingestion.pdf_parser as a fallback for scanned PDFs.
ARG DNF_PACKAGES="tesseract tesseract-langpack-rus tesseract-langpack-eng"
RUN if command -v dnf >/dev/null 2>&1; then \
        dnf install -y ${DNF_PACKAGES} && dnf clean all; \
    elif command -v microdnf >/dev/null 2>&1; then \
        microdnf install -y ${DNF_PACKAGES} && microdnf clean all; \
    else \
        yum install -y ${DNF_PACKAGES} && yum clean all; \
    fi

COPY --from=builder /opt/venv /opt/venv

# Путь к hooks внутри venv узнаём, а не угадываем: маска `python*` в COPY не
# раскрывается в `python3.12`, и файл молча уезжает не туда — ровно так хук и
# потерялся. `hdir` печатает сам PyInstaller; `test -f` после копирования валит
# сборку, если хук не лёг.
#
# НАШЕЙ СБОРКИ ЭТО НЕ КАСАЕТСЯ — ЗАДЕЛ НА БУДУЩЕЕ. Аргумент `--additional-hooks-dir`
# ниже передаёт наш собственный `pyinstaller`, и своего хука из `/app` ей хватает.
# Читает этот каталог PyInstaller обёртки — но лишь при условии, что обёртка
# запускается интерпретатором из `/opt/venv` (`PATH` и venv активен). Что она
# делает на самом деле — из лога не видно ни разу, поэтому основная защита — не
# здесь, а в `entrypoint.sh`: без корректно собранного бандла под поднимается из
# исходников (`/app/finengine-source`), а не падает.
RUN set -eu; \
    hdir="$(python -c 'import pathlib, PyInstaller; print(pathlib.Path(PyInstaller.__file__).parent / "hooks")')"; \
    mkdir -p "$hdir"; \
    cp /app/hook-finengine.py "$hdir/hook-finengine.py"; \
    test -f "$hdir/hook-finengine.py"; \
    echo "[build] хук установлен в $hdir"

# Источник для полки станции. `skills/station_driver.py` зипует `finengine` и
# `station-skills` и отправляет архив в Workstation — станция исполняет ровно ту
# версию движка, что у бэкенда. Пакету эти исходники не нужны: импорт идёт из
# venv (см. builder), а данные — из /app/finengine-data ниже.
COPY finengine/src /app/src
COPY finengine/station-skills /app/station-skills
COPY finengine/deploy/tools_whitelist.yaml /app/deploy/tools_whitelist.yaml
COPY finengine/deploy/certs/ /app/deploy/certs/
COPY main.py /app/main.py
COPY entrypoint.sh /app/entrypoint.sh

# Данные finengine (.md-промпты, .yaml/.json-справочники, .pem-сертификаты в
# deploy/) лежат ВНЕ бандла: hook читает только наш PyInstaller, а обёртка собирает
# `app` своя. Каталог переживает распаковку `--onefile` и правится без пересборки;
# main.py дописывает его в конец `finengine.__path__`, поэтому модули берутся из
# бандла, а данные — отсюда. Каталог берём из venv, а не из `/app/src`: venv — это
# ровно то, что проверено импортом выше, и он не зависит от раскладки исходников.
# Путь вычисляем питоном, а не маской `python*`: такая маска в COPY не раскрывается.
RUN set -eu; \
    srcdir="$(python -c 'import pathlib, finengine; print(pathlib.Path(finengine.__file__).parent)')"; \
    cp -a "$srcdir" /app/finengine-data; \
    test -f /app/finengine-data/policy/POLICY.md; \
    test -f /app/finengine-data/data/metrics.json

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FINENGINE_HOST=0.0.0.0 \
    FINENGINE_PORT=8080 \
    FINENGINE_DATA_PACKAGE_DIR=/app/finengine-data \
    FINENGINE_DATA_ROOT=/app \
    FINENGINE_SOURCE_ROOT=/app/finengine-source

# СБОРКА БАНДЛА — ЗДЕСЬ, А НЕ В ПАЙПЛАЙНЕ ОБЁРТКИ (14.11.2026).
# Свой бандл собираем сами: так хук гарантированно применяется, а не зависит от
# того, как обёртка AEF вызывает PyInstaller. Исходников для импорта в образе нет в
# том смысле, в котором их видит PyInstaller: `finengine` установлен в venv, а
# `/app/src` лежит рядом с `main.py` — значит, `--paths` не нужен.
#
# ЛОГ СБОРКИ ЭТОГО ОБРАЗА ОБЯЗАН СОДЕРЖАТЬ ДВЕ СТРОКИ:
#   [build] хук установлен в ...
#   [build] finengine.* в бандле: N из M
# Если в логе пода позже окажется `No module named 'finengine.api'` — значит, под
# запущен не с этого бандла, а `entrypoint.sh` подстрахует запасным путём.
#
# ПРОВЕРКА СОСТАВА — ДО, А НЕ ПОСЛЕ ПУБЛИКАЦИИ: читаем оглавление `dist/app` и валим
# сборочную задачу, если `finengine.api` там нет. `test -f` — то же самое для данных:
# без `policy/POLICY.md` сервис падает на импорте `finengine.policy`.
RUN set -eu; \
    pyinstaller --noconfirm --clean --onefile --name app \
        --additional-hooks-dir /app \
        /app/main.py; \
    python -c "import sys; from PyInstaller.archive.readers import CArchiveReader as R; n = list(R('/app/dist/app').toc); print('[build] finengine.* в бандле:', sum(x.startswith('finengine') for x in n), 'из', len(n)); sys.exit(0 if any(x.startswith('finengine.api') for x in n) else 1)"; \
    mv /app/dist/app /app/app; \
    rm -rf /tmp/build /tmp/dist /app/dist; \
    mkdir -p /app/_internal; \
    test -x /app/app; \
    test -f /app/finengine-data/policy/POLICY.md; \
    test -f /app/finengine-data/data/metrics.json

# Те же модули — снаружи бандла, для подстраховки из main.py: если хук не сработает
# при сборке обёрткой, сервис поднимется с исходников, а не упадёт на импорте.
RUN set -eu; \
    cp -a /app/src/finengine /app/finengine-source; \
    test -f /app/finengine-source/api/main.py

RUN groupadd -g 10002 aef && useradd -u 10002 -g aef aef \
    && mkdir -p /app/data/documents \
    && chown -R aef:aef /app \
    && sed -i 's/\r$//' /app/entrypoint.sh \
    && chmod +x /app/entrypoint.sh

EXPOSE 8080
USER aef
ENTRYPOINT ["/app/entrypoint.sh"]
