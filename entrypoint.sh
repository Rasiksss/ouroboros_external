#!/bin/sh
set -e
echo "[entrypoint] applying schema migrations..."
PYTHONPATH=. /opt/venv/bin/python -m alembic upgrade head
exec "$@"
