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

# Create and activate virtual environment
RUN if command -v python &> /dev/null; then \
        exec python -m venv /opt/venv; \
    else \
        exec python3 -m venv /opt/venv; \
    fi
ENV PATH="/opt/venv/bin:$PATH"

# Install dependencies
RUN if [ -n "$NEXUS_TOKEN_USERNAME" ] && [ -n "$NEXUS_TOKEN_PASSWORD" ]; then \
        pip install --upgrade pip \
            --index-url "https://token:${TOKEN_OSC}@sberosc.sigma.sbrf.ru/repo/pypi/simple" \
            --extra-index-url "https://${NEXUS_TOKEN_USERNAME}:${NEXUS_TOKEN_PASSWORD}@nexus-ci.delta.sbrf.ru/repository/pypi-release/simple" \
            --trusted-host sberosc.sigma.sbrf.ru \
            --trusted-host nexus-ci.delta.sbrf.ru \
            --default-timeout 120 \
            --no-cache-dir \
            --progress-bar off \
            -r requirements.txt; \
    else \
        pip install --upgrade pip \
            --index-url "https://token:${TOKEN_OSC}@sberosc.sigma.sbrf.ru/repo/pypi/simple" \
            --extra-index-url "https://${NEXUS_USERNAME}:${NEXUS_PASSWORD}@nexus-ci.delta.sbrf.ru/repository/pypi-release/simple" \
            --trusted-host sberosc.sigma.sbrf.ru \
            --trusted-host nexus-ci.delta.sbrf.ru \
            --default-timeout 120 \
            --no-cache-dir \
            --progress-bar off \
            -r requirements.txt; \
    fi

# Sandbox-side python libraries. Installed via the same Sber indexes into a
# dedicated --target so they can be COPY'd into the runtime image — keeps the
# Nexus credentials out of the runtime stage's layer/metadata history.
RUN if [ -n "$NEXUS_TOKEN_USERNAME" ] && [ -n "$NEXUS_TOKEN_PASSWORD" ]; then \
        pip install --target /opt/sandbox-libs \
            --index-url "https://token:${TOKEN_OSC}@sberosc.sigma.sbrf.ru/repo/pypi/simple" \
            --extra-index-url "https://${NEXUS_TOKEN_USERNAME}:${NEXUS_TOKEN_PASSWORD}@nexus-ci.delta.sbrf.ru/repository/pypi-release/simple" \
            --trusted-host sberosc.sigma.sbrf.ru \
            --trusted-host nexus-ci.delta.sbrf.ru \
            --default-timeout 120 \
            --no-cache-dir \
            --progress-bar off \
            -r sandbox_requirements.txt; \
    else \
        pip install --target /opt/sandbox-libs \
            --index-url "https://token:${TOKEN_OSC}@sberosc.sigma.sbrf.ru/repo/pypi/simple" \
            --extra-index-url "https://${NEXUS_USERNAME}:${NEXUS_PASSWORD}@nexus-ci.delta.sbrf.ru/repository/pypi-release/simple" \
            --trusted-host sberosc.sigma.sbrf.ru \
            --trusted-host nexus-ci.delta.sbrf.ru \
            --default-timeout 120 \
            --no-cache-dir \
            --progress-bar off \
            -r sandbox_requirements.txt; \
    fi

# Runtime stage
FROM $RUN_IMAGE
USER root

RUN dnf install -y unrar p7zip && dnf clean all && rm -rf /var/cache/dnf

# Copy virtual environment with proper permissions
COPY --from=builder /opt/venv /opt/venv

# Copy application code with proper permissions
COPY --from=builder /app /app

# Pre-built sandbox binaries (built once via existing artifact pipeline, see vendor/bin)
COPY --from=builder /app/vendor/bin/ws-sandbox /usr/local/bin/ws-sandbox
COPY --from=builder /app/vendor/bin/busybox /usr/local/libexec/workstation/busybox

RUN chmod 0755 /usr/local/bin/ws-sandbox \
    /usr/local/libexec/workstation/busybox \
    && test -x /usr/local/bin/ws-sandbox \
    && test -x /usr/local/libexec/workstation/busybox

ENV PATH="/opt/venv/bin:$PATH"

# Shared, read-only sandbox /bin. Lives outside the per-session rootfs so
# Landlock can grant the sandbox rx-only access — sessions cannot tamper with
# their own toolchain (no in-session binary substitution between exec_command
# calls). One directory per pod, not per session.
RUN mkdir -p /opt/workstation/bin \
    && cp /usr/local/libexec/workstation/busybox /opt/workstation/bin/bash \
    && for applet in sh cat ls pwd grep mkdir touch echo; \
       do ln /opt/workstation/bin/bash "/opt/workstation/bin/${applet}"; done \
    && install -m 0755 "$(command -v unrar)" /opt/workstation/bin/unrar \
    # p7zip ships the binary as 7za or 7z depending on the repo build
    && install -m 0755 "$(command -v 7za || command -v 7z)" /opt/workstation/bin/7za \
    && cp /usr/bin/python3.12 /opt/workstation/bin/python3 \
    && ln /opt/workstation/bin/python3 /opt/workstation/bin/python \
    && chmod -R 0755 /opt/workstation/bin \
    && test -x /opt/workstation/bin/unrar \
    && test -x /opt/workstation/bin/7za

# Empty rootfs template for per-session copies. Sessions get only working
# directories; the toolchain itself is in /opt/workstation/bin (above).
RUN mkdir -p /opt/workstation/template \
    && chmod 0755 /opt/workstation/template

# Bring sandbox-side python libraries into the system site-packages so
# /bin/python3 in sessions can import them — /usr/lib is rx-allowlisted by
# Landlock. Installed in the builder stage (sandbox_requirements.txt) and
# carried over without re-introducing Nexus credentials to this stage.
COPY --from=builder /opt/sandbox-libs /usr/lib/python3.12/site-packages

# create username/group
RUN groupadd aef
RUN useradd -g aef aef

# Set permissions
RUN chown -R aef:aef /app && chmod +x /app/entrypoint.sh

# Set Working Directory
WORKDIR /app

EXPOSE 8080

# Switch to non-root user
USER aef
ENTRYPOINT ["./entrypoint.sh"]
