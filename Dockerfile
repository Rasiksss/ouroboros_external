ARG BUILD_IMAGE
ARG RUN_IMAGE

# Build stage
FROM $BUILD_IMAGE as builder
USER root

WORKDIR /app
COPY . /app

ARG NEXUS_USERNAME
ARG NEXUS_PASSWORD
ARG TOKEN_OSC
ARG NEXUS_TOKEN_USERNAME
ARG NEXUS_TOKEN_PASSWORD
ARG DNF_PACKAGES
ARG INSTALL_PLAYWRIGHT_BROWSER=0
ARG PLAYWRIGHT_DOWNLOAD_HOST="https://token:${TOKEN_OSC}@sberosc.sigma.sbrf.ru/repo/playwright"


# Create and activate virtual environment
RUN if command -v python &> /dev/null; then \
        exec python -m venv /opt/venv; \
    else \
        exec python3 -m venv /opt/venv; \
    fi
ENV PATH="/opt/venv/bin:$PATH" \
    PLAYWRIGHT_BROWSERS_PATH=0

# Install sber-sberchat-bot-sdk from pypi-sberchat and other dependencies from pypi-release
RUN  pip install uv \
    --index-url "https://token:${TOKEN_OSC}@sberosc.sigma.sbrf.ru/repo/pypi/simple" \
    --trusted-host sberosc.sigma.sbrf.ru \
    --no-cache-dir \
    --progress-bar off; UV_HTTP_TIMEOUT=120 \    
uv pip install --upgrade "pip<=24.0" \
    --index-url "https://token:${TOKEN_OSC}@sberosc.sigma.sbrf.ru/repo/pypi/simple" \
    --extra-index-url "https://${NEXUS_TOKEN_USERNAME:-${NEXUS_USERNAME}}:${NEXUS_TOKEN_PASSWORD:-${NEXUS_PASSWORD}}@nexus-ci.delta.sbrf.ru/repository/pypi-sberchat/simple" \
    --trusted-host sberosc.sigma.sbrf.ru \
    --trusted-host nexus-ci.delta.sbrf.ru \
    --no-cache-dir \
    --no-progress \
    cachetools==7.1.8 \
    sber-sberchat-bot-sdk==4.17.2; \
uv pip install --upgrade "pip<=24.0" \
    --index-url "https://token:${TOKEN_OSC}@sberosc.sigma.sbrf.ru/repo/pypi/simple" \
    --extra-index-url "https://${NEXUS_TOKEN_USERNAME:-${NEXUS_USERNAME}}:${NEXUS_TOKEN_PASSWORD:-${NEXUS_PASSWORD}}@nexus-ci.delta.sbrf.ru/repository/pypi-release/simple" \
    --trusted-host sberosc.sigma.sbrf.ru \
    --trusted-host nexus-ci.delta.sbrf.ru \
    --no-cache-dir \
    --no-progress \
    -r requirements.txt && \
    if [ "$INSTALL_PLAYWRIGHT_BROWSER" = "1" ]; then \
        /opt/venv/bin/python -m playwright install chromium; \
    fi

# Runtime stage
FROM $RUN_IMAGE
ARG INSTALL_PLAYWRIGHT_BROWSER=0
USER root
ENV PLAYWRIGHT_BROWSERS_PATH=0

# Copy virtual environment with proper permissions
COPY --from=builder /opt/venv /opt/venv

RUN if [ "$INSTALL_PLAYWRIGHT_BROWSER" = "1" ]; then \
        /opt/venv/bin/python -m playwright install-deps chromium; \
    fi

# Copy application code with proper permissions
COPY --from=builder /app /app

ENV OUROBOROS_ENV=production \
    OUROBOROS_DATA_DIR=/data \
    PORT=18800
EXPOSE 18800

# alembic.ini is copied with the application code and used by runtime migrations.

# create username\group
RUN groupadd ouroboros && useradd -g ouroboros ouroboros && mkdir -p /data /var/log/app && chown -R ouroboros:ouroboros /app /data /var/log/app && chmod +x /app/entrypoint.sh
# Set Working Directory
WORKDIR /app

# Switch to non-root user
USER ouroboros

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["/opt/venv/bin/python","-m","ouroboros.app"]
