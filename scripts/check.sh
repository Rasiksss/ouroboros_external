#!/usr/bin/env bash
# The single pre-merge gate. Mirrors the GitLab check / test / unit-regression jobs.
# Usage:
#   ./scripts/check.sh            # everything, stops at the first failure
#   ./scripts/check.sh --fast     # format + lint + types only, no suites
#
# Exit code is the answer: 0 means done, anything else means not done.
# On failure the gate reports WHAT failed, WHY it gates, and the FIX command to run next,
# so a red cross costs one command instead of one iteration of guessing.

set -eo pipefail

cd "$(dirname "$0")/.."

STEP=""
WHY=""
FIX=""

report() {
  status=$?
  if [ "$status" -eq 0 ]; then
    exit 0
  fi
  printf '\n\033[1;31m>> FAILED: %s\033[0m (exit %s)\n' "$STEP" "$status"
  printf '   \033[1mWHAT\033[0m  %s\n' "$STEP"
  printf '   \033[1mWHY\033[0m   %s\n' "$WHY"
  printf '   \033[1mFIX\033[0m   %s\n' "$FIX"
  exit "$status"
}
trap report EXIT

step() {
  STEP="$1"
  WHY="$2"
  FIX="$3"
  printf '\n\033[1m>> %s\033[0m\n' "$1"
}

step "ruff format" \
  "CI runs --check and rejects unformatted code; nothing below is worth reading until this passes." \
  "uv run ruff format ."
uv run ruff format --check .

step "ruff check" \
  "Lint gates the coding rules that are mechanically checkable (AGENTS.md §Coding Rules)." \
  "uv run ruff check --fix . && uv run ruff check ."
uv run ruff check .

step "mypy" \
  "Types are enforced with no disabled error codes; an untyped seam hides a contract change." \
  "uv run mypy 2>&1 | head -30   # fix the first error, later ones are usually cascades"
uv run mypy

if [ "${1:-}" = "--fast" ]; then
  printf '\n\033[1mOK\033[0m (--fast: suites skipped)\n'
  exit 0
fi

step "pytest tests_headless" \
  "Production profile: deploy env, router surface, forbidden tools, governance docs." \
  "uv run pytest tests_headless -n 8 -x -q --last-failed   # no database? docker compose -f docker/docker-compose.test.yml up -d"
uv run pytest tests_headless -n 8

step "pytest tests" \
  "Unit/regression suite; a failure here is a behavior change you did not name in a test." \
  "uv run pytest tests -n 8 -m 'not integration and not browser' -x -q --last-failed   # no database? docker compose -f docker/docker-compose.test.yml up -d"
uv run pytest tests -n 8 -m "not integration and not browser"

step "pytest skills" \
  "Distribution-owned skill and extension behavior ships read-only; breakage is a release bug." \
  "uv run pytest skills -n 8 -m 'not integration and not browser' -x -q --last-failed   # no database? docker compose -f docker/docker-compose.test.yml up -d"
uv run pytest skills -n 8 -m "not integration and not browser"

printf '\n\033[1mOK\033[0m\n'
