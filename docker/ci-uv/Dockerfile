# Базовый CI-образ: ghcr.io/astral-sh/uv:python3.12-bookworm-slim + системные пакеты для приложений.
# Собирать в среде с доступом к apt (не в air-gapped build приложения), затем пушить в Nexus:
#   docker build -t registry.genai.int/ci/uv:python3.12-bookworm-slim .
#   docker push registry.genai.int/ci/uv:python3.12-bookworm-slim
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*
