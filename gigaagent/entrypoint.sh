#!/bin/sh
set -eu

# /app is the immutable image source; /tmp may be a fresh mount on every pod.
# Seed the writable runtime repository before loading prompts and skills.
if [ "${OUROBOROS_REPO_DIR:-/app}" = /tmp/repo ]; then
    mkdir -p /tmp/repo
    cp -R /app/. /tmp/repo/
fi

# Database structure is deployed exclusively by AIFA's Liquibase pipeline.
# Retired OUROBOROS_MIGRATE_ON_START values never trigger DDL.
exec "$@"
