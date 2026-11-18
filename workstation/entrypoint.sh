#!/bin/sh
set -e

# if [ -n "${SECMAN_SECRETS_FILE:-}" ]; then
#     while [ ! -s "$SECMAN_SECRETS_FILE" ]; do
#         echo "[entrypoint] Waiting for SecMan secrets file: $SECMAN_SECRETS_FILE"
#         sleep 1
#     done

#     echo "[entrypoint] SecMan secrets file is ready"
# fi

exec python app.py