#!/usr/bin/env bash
# Ворота симбиоза AIFA и ouroboros (ADR-0020, FITNESS.md). Запускается из любого каталога, в Git Bash
# на Windows и в Linux.
#   aifa/scripts/check.sh          # всё
#   aifa/scripts/check.sh --fast   # без наборов тестов службы и фронтенда; проверки симбиоза идут
#
# Стадии идут все подряд — красная не прерывает остальные. Код возврата — первый ненулевой,
# в конце сводка.
#   1. ouroboros: ./scripts/check.sh как есть (с --fast, если передан)
#   2. служба aifa/finengine: ruff, mypy, pytest (pytest — без --fast)
#   3. проверки симбиоза aifa/tests: ruff, format, mypy --strict, pytest (F1, F2, F11, F13)
#   4. фронтенд aifa/web: npm test (без --fast)
#
# Окружение службы — её замок (`uv.lock`), без конфигурации корня: корневой [tool.uv] ведёт на
# корпоративный индекс. В контуре с индексом: AIFA_UV_FLAGS="" aifa/scripts/check.sh.
#
# Известное красное (PROGRESS.md §3, записи шага 01 и 02) — не новое: сравнивать надо с базовой
# линией, а не с нулём.

set -u

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO" || exit 2

FAST=0
if [ "${1:-}" = "--fast" ]; then
  FAST=1
elif [ $# -gt 0 ]; then
  echo "usage: aifa/scripts/check.sh [--fast]" >&2
  exit 2
fi

UV_FLAGS="${AIFA_UV_FLAGS---no-config}"
MYPY_CONFIG="$REPO/aifa/scripts/mypy.ini"

if [ -t 1 ]; then BOLD=$'\033[1m'; RED=$'\033[1;31m'; GREEN=$'\033[1;32m'; END=$'\033[0m'; else BOLD=""; RED=""; GREEN=""; END=""; fi

FIRST_RC=0
SUMMARY=()

record() { # имя код секунды
  local label=$1 rc=$2 secs=$3
  if [ "$rc" -eq 0 ]; then
    SUMMARY+=("${GREEN}PASS${END}  $label (${secs}s)")
  else
    SUMMARY+=("${RED}FAIL${END}  $label (exit $rc, ${secs}s)")
    [ "$FIRST_RC" -eq 0 ] && FIRST_RC=$rc
  fi
}

stage() { # имя команда...
  local label=$1
  shift
  printf '\n%s>> %s%s\n' "$BOLD" "$label" "$END"
  local started=$SECONDS
  "$@"
  record "$label" $? $((SECONDS - started))
}

skip() { # имя причина
  SUMMARY+=("SKIP  $1 ($2)")
}

service() { # команда... — в окружении службы, из aifa/finengine
  # shellcheck disable=SC2086  # UV_FLAGS — список флагов
  uv run $UV_FLAGS --frozen --directory aifa/finengine --extra dev "$@"
}

service_mypy() {
  # shellcheck disable=SC2086
  uv run $UV_FLAGS --frozen --directory aifa/finengine --extra dev --with mypy "$@"
}

in_web() {
  (cd aifa/web && "$@")
}

ouroboros_gate() {
  if [ "$FAST" -eq 1 ]; then ./scripts/check.sh --fast; else ./scripts/check.sh; fi
}

# 1. ouroboros как есть. aifa/ruff.toml исключает aifa/ из его ruff — иначе формат-ворота
#    ouroboros краснели бы на коде AIFA, у которого своё форматирование.
stage "1 ouroboros: scripts/check.sh$([ "$FAST" -eq 1 ] && echo ' --fast')" ouroboros_gate

# 2. служба
stage "2a служба: ruff check" service ruff check .
stage "2b служба: mypy src/finengine" service_mypy mypy --config-file "$MYPY_CONFIG" src/finengine
if [ "$FAST" -eq 1 ]; then
  skip "2c служба: pytest tests" "--fast"
else
  # --continue-on-collection-errors: сборка тестов, читающих deploy/, падает до шага 03 (P16)
  stage "2c служба: pytest tests" service pytest tests -q --continue-on-collection-errors
fi

# 3. проверки симбиоза (окружение службы, только стандартная библиотека + pytest)
stage "3a симбиоз: ruff check" service ruff check ../tests
stage "3b симбиоз: ruff format --check" service ruff format --check ../tests
stage "3c симбиоз: mypy --strict" service_mypy mypy --config-file "$MYPY_CONFIG" --strict ../tests/architecture ../tests/golden
stage "3d симбиоз: pytest aifa/tests" service pytest ../tests -q

# 4. фронтенд
if [ "$FAST" -eq 1 ]; then
  skip "4 фронтенд: npm test" "--fast"
elif [ ! -d aifa/web/node_modules ]; then
  printf '\n%s>> 4 фронтенд: npm test%s\n' "$BOLD" "$END"
  echo "нет aifa/web/node_modules: выполните (cd aifa/web && npm ci)" >&2
  record "4 фронтенд: npm test (нет node_modules)" 1 0
else
  stage "4 фронтенд: npm test" in_web npm test
fi

printf '\n%s== Сводка ==%s\n' "$BOLD" "$END"
for line in "${SUMMARY[@]}"; do
  printf '  %s\n' "$line"
done
if [ "$FIRST_RC" -eq 0 ]; then
  printf '\n%sOK%s\n' "$GREEN" "$END"
else
  printf '\n%sНе зелёно%s (первый ненулевой код: %s). Сверьте с базовой линией в PROGRESS.md.\n' "$RED" "$END" "$FIRST_RC"
fi
exit "$FIRST_RC"
