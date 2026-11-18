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

RUN if command -v python >/dev/null 2>&1; then \
        python -m venv /opt/venv; \
    else \
        python3 -m venv /opt/venv; \
    fi
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt ./

# Resolve SberChat and runtime dependencies together. Any installation failure
# stops the build; credentials are only declared in the builder stage.
RUN pip install --upgrade "pip<=24.0" \
        --index-url "https://token:${TOKEN_OSC}@sberosc.sigma.sbrf.ru/repo/pypi/simple" \
        --extra-index-url "https://${NEXUS_TOKEN_USERNAME:-${NEXUS_USERNAME}}:${NEXUS_TOKEN_PASSWORD:-${NEXUS_PASSWORD}}@nexus-ci.delta.sbrf.ru/repository/pypi-sberchat/simple" \
        --extra-index-url "https://${NEXUS_TOKEN_USERNAME:-${NEXUS_USERNAME}}:${NEXUS_TOKEN_PASSWORD:-${NEXUS_PASSWORD}}@nexus-ci.delta.sbrf.ru/repository/pypi-release/simple" \
        --trusted-host sberosc.sigma.sbrf.ru \
        --trusted-host nexus-ci.delta.sbrf.ru \
        --default-timeout 120 --no-cache-dir --progress-bar off \
        cachetools==7.1.8 sber-sberchat-bot-sdk==4.17.2 -r requirements.txt

FROM ${RUN_IMAGE}
USER root
WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY server.py entrypoint.sh ./
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

RUN groupadd -g 10001 aef && useradd -u 10001 -g aef aef \
    && mkdir -p /tmp/data /tmp/repo /var/log/app \
    && chown -R aef:aef /app /tmp/data /tmp/repo /var/log/app \
    && sed -i 's/\r$//' /app/entrypoint.sh \
    && chmod +x /app/entrypoint.sh

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OUROBOROS_ENV=production \
    OUROBOROS_REPO_DIR=/tmp/repo \
    OUROBOROS_APP_ROOT=/tmp \
    OUROBOROS_DATA_DIR=/tmp/data \
    OUROBOROS_SERVER_HOST=0.0.0.0 \
    OUROBOROS_SERVER_PORT=8080
EXPOSE 8080
USER aef
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "-m", "ouroboros.app"]
