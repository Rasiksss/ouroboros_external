# Образ службы движка `engine` (ADR-0003, ADR-0020, ADR-0034). Контекст — aifa/finengine.
#   docker build -f aifa/deploy/dev/finengine.Dockerfile -t aifa-sym-engine:dev aifa/finengine

FROM python:3.12-slim AS builder

ARG UV_VERSION=0.10.4
# git — зависимость `mmar-carl` замка службы берётся из git.
RUN apt-get update && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir "uv==${UV_VERSION}"

# Сборка не должна молча подтягивать чужой Python вместо python:3.12-slim.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.12-slim

# OCR-запасной путь разбора PDF: `ingestion/pdf_parser.py` вызывает tesseract через pytesseract.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app/src /app/src
# Код читает `station-skills/` рядом с `src/` (ADR-0032, до шага 19), а `deploy/tools_whitelist.yaml`
# и `deploy/certs/` — рядом с `src/` (ADR-0034, P15): без них все инструменты открыты, Росстат закрыт.
COPY station-skills /app/station-skills
COPY deploy/tools_whitelist.yaml /app/deploy/tools_whitelist.yaml
COPY deploy/certs/ /app/deploy/certs/

RUN groupadd -r finengine && useradd -r -g finengine -u 10002 finengine \
 && mkdir -p /app/data/documents && chown -R finengine:finengine /app/data

ENV PATH="/opt/venv/bin:${PATH}"

USER finengine
EXPOSE 8000
CMD ["uvicorn", "finengine.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
