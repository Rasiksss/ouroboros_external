# Образ ouroboros dev-контура `aifa-sym` (ADR-0002, ADR-0020, ADR-0034).
# Контекст — корень репозитория; исключения контекста — ouroboros.Dockerfile.dockerignore рядом
# (корневой .dockerignore — файл ouroboros, F1).
#   docker build -f aifa/deploy/dev/ouroboros.Dockerfile -t aifa-sym-ouroboros:dev .

FROM python:3.12-slim AS deps

ARG UV_VERSION=0.10.4
RUN pip install --no-cache-dir "uv==${UV_VERSION}" && python -m venv /opt/venv

WORKDIR /src
COPY pyproject.toml uv.lock ./
# ADR-0034: замок указывает на nexus.genai.int, `uv sync --frozen` из публичной сети невозможен.
# Версии и sha256 — из замка, файлы — с pypi.org.
RUN uv export --frozen --no-dev --no-emit-project --no-config --no-header -o requirements.txt \
 && uv pip install --python /opt/venv/bin/python --no-config --no-deps --require-hashes \
        --index-url https://pypi.org/simple -r requirements.txt

FROM python:3.12-slim

WORKDIR /app
COPY --from=deps /opt/venv /opt/venv
# Каталоги — те же, что копирует Dockerfile.local ouroboros: server.py импортирует их при старте.
# Перечень явный: aifa/ и aifa-symbiosis/ не попадают в образ и без dockerignore.
COPY server.py alembic.ini ./
COPY config_registry ./config_registry
COPY background_memory ./background_memory
COPY infra ./infra
COPY multiuser_config ./multiuser_config
COPY new_core ./new_core
COPY ouroboros ./ouroboros
COPY security_config ./security_config
COPY supervisor ./supervisor
COPY transport ./transport
COPY skills ./skills
COPY prompts ./prompts

RUN groupadd -r ouroboros && useradd -r -g ouroboros -u 10001 ouroboros \
 && mkdir -p /data && chown -R ouroboros:ouroboros /data /app

# ADR-0019: навыки читаются из ${OUROBOROS_REPO_DIR}/skills.
ENV PATH="/opt/venv/bin:${PATH}" \
    OUROBOROS_REPO_DIR=/app

USER ouroboros
EXPOSE 8080
CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8080"]
