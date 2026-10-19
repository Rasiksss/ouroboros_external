#!/usr/bin/env bash
# Дымовые проверки контура aifa-sym (этап 0.6; 6–9 — шаг 12, профиль app).
# Запуск на dev-хосте из корня клона:  aifa/deploy/dev/smoke/run.sh [1|2|3|4|5|6|7|8|9]...
# Контейнеры одноразовые, в сети aifa-sym-internal; имена без `prod`; порты не публикуются.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DEV="$(dirname "$HERE")"
PROJECT=aifa-sym
NETWORK=aifa-sym-internal
IMAGE=aifa-sym-ouroboros:dev
READY_URL=${AIFA_SYM_READY_URL:-http://127.0.0.1:18180/ready}
FRONTEND_URL=${AIFA_SYM_FRONTEND_URL:-http://127.0.0.1:13000}
CHECKS=("$@")
[ ${#CHECKS[@]} -eq 0 ] && CHECKS=(1 2 3 4 5 6 7 8 9)
FAILED=0

# Секреты из .env рядом с compose.yml: попадают только в окружение одноразового контейнера.
set -a
# shellcheck disable=SC1091
[ -f "$DEV/.env" ] && . "$DEV/.env"
# Пароли тестовых учёток службы (шаг 12): файл с правами 600 на сервере, не в git.
[ -f "${AIFA_SYM_QA_ENV:-$HOME/aifa-sym-qa.env}" ] && . "${AIFA_SYM_QA_ENV:-$HOME/aifa-sym-qa.env}"
set +a

inside() { # скрипт аргументы... — одноразовый контейнер из образа ouroboros
  local script=$1
  shift
  docker run --rm --network "$NETWORK" --name "$PROJECT-smoke-$$-$RANDOM" \
    -e PYTHONIOENCODING=utf-8 -e AIFA_SYM_WORKSTATION_CREATE_SECRET \
    -v "$HERE:/smoke:ro" --entrypoint python "$IMAGE" "/smoke/$script" "$@"
}

api() { # команда аргументы... — клиент службы с хоста, через loopback-порт фронтенда
  python3 "$HERE/api.py" "$@" --base "$FRONTEND_URL"
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
      # Имя инструмента ouroboros в лог не пишет: вызов виден как exec-запрос к Workstation контура.
      calls=$(docker logs --since "$since" "${PROJECT}-ouroboros-1" 2>&1 |
        grep -c -E 'workstation:8080/v1/sessions/[^ ]*/exec:stream')
      echo "exec-запросов к Workstation в логе ouroboros: $calls"
      [ "$rc" -eq 0 ] && [ "$calls" -gt 0 ]
      result 3 "code_exec выполнен в Workstation контура" $?
      ;;
    4)
      inside room.py --base-url http://workstation:8080
      result 4 "C5.2: разовая комната sealed_room" $?
      ;;
    5)
      inside room.py --base-url http://workstation:8080 --limits
      rc=$?
      limits=$(docker inspect -f '{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}} {{.HostConfig.NanoCpus}} {{.HostConfig.PidsLimit}}' \
        "${PROJECT}-workstation-1")
      echo "пределы контейнера workstation (память, память+swap, наноCPU, процессы): $limits"
      [ "$rc" -eq 0 ] && [ "$limits" = "1610612736 1610612736 1500000000 256" ]
      result 5 "ADR-0039: пределы Workstation (exec и контейнер)" $?
      ;;
    6)
      api health
      result 6 "служба отвечает через фронтенд (/api/health, P31)" $?
      ;;
    7)
      api build-id --expect "${AIFA_SYM_BUILD_ID:-dev}"
      result 7 "/build-id фронтенда = AIFA_SYM_BUILD_ID" $?
      ;;
    8)
      AIFA_SYM_QA_PASSWORD=$AIFA_SYM_QA_A_PASSWORD api login --user qa-a
      result 8 "вход qa-a" $?
      ;;
    9)
      AIFA_SYM_QA_PASSWORD=$AIFA_SYM_QA_A_PASSWORD api hello --user qa-a --prompt "привет"
      result 9 "ход «привет» доходит до ouroboros и обратно (UI-ручки)" $?
      ;;
    *) echo "неизвестная проверка: $check"; FAILED=1 ;;
  esac
done
exit "$FAILED"
