#!/usr/bin/env bash
# Дымовые проверки контура aifa-sym (этап 0.6). Повторяются шагом 12 и далее.
# Запуск на dev-хосте из корня клона:  aifa/deploy/dev/smoke/run.sh [1|2|3|4]...
# Контейнеры одноразовые, в сети aifa-sym-internal; имена без `prod`; порты не публикуются.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DEV="$(dirname "$HERE")"
PROJECT=aifa-sym
NETWORK=aifa-sym-internal
IMAGE=aifa-sym-ouroboros:dev
READY_URL=${AIFA_SYM_READY_URL:-http://127.0.0.1:18180/ready}
CHECKS=("$@")
[ ${#CHECKS[@]} -eq 0 ] && CHECKS=(1 2 3 4)
FAILED=0

# Секреты из .env рядом с compose.yml: попадают только в окружение одноразового контейнера.
set -a
# shellcheck disable=SC1091
[ -f "$DEV/.env" ] && . "$DEV/.env"
set +a

inside() { # скрипт аргументы... — одноразовый контейнер из образа ouroboros
  local script=$1
  shift
  docker run --rm --network "$NETWORK" --name "$PROJECT-smoke-$$-$RANDOM" \
    -e PYTHONIOENCODING=utf-8 -e AIFA_SYM_WORKSTATION_CREATE_SECRET \
    -v "$HERE:/smoke:ro" --entrypoint python "$IMAGE" "/smoke/$script" "$@"
}

result() { # номер описание код
  if [ "$3" -eq 0 ]; then echo "PASS $1 $2"; else echo "FAIL $1 $2"; FAILED=1; fi
}

for check in "${CHECKS[@]}"; do
  case "$check" in
    1)
      code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$READY_URL")
      echo "ready: HTTP $code"
      [ "$code" = 200 ]
      result 1 "/ready ouroboros = 200" $?
      ;;
    2)
      inside ws_chat.py --no-owner
      result 2a "рукопожатие без X-Ouroboros-Owner-Id отклонено" $?
      inside ws_chat.py --prompt "привет"
      result 2b "рукопожатие с заголовками, ответ модели на «привет»" $?
      ;;
    3)
      since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
      inside ws_chat.py --chat smoke-code --expect 4 --timeout 420 --prompt \
        "Выполни в Workstation код print(2+2) через инструмент code_exec и пришли его вывод."
      rc=$?
      calls=$(docker logs --since "$since" "${PROJECT}-ouroboros-1" 2>&1 | grep -c -i "code_exec")
      echo "упоминаний code_exec в логе ouroboros: $calls"
      [ "$rc" -eq 0 ] && [ "$calls" -gt 0 ]
      result 3 "code_exec выполнен в Workstation контура" $?
      ;;
    4)
      inside room.py --base-url http://workstation:8080
      result 4 "C5.2: разовая комната sealed_room" $?
      ;;
    *) echo "неизвестная проверка: $check"; FAILED=1 ;;
  esac
done
exit "$FAILED"
