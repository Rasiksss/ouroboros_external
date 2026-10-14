#!/bin/bash
# AIFA CLAW — воркер-обёртка извлечения (#84 Фаза C): ОДНА команда для суб-агента.
#
# Запускается В КОМНАТЕ воркера (exec суб-агента, cwd = workspace комнаты; пути
# ТОЛЬКО относительные — /workspace в комнате не абсолютный, грабля exec-станции).
# Едет на ПОЛКЕ (files:upload плагина): вход = .shelf/aifa_station_skills/... после
# распаковки .shelf.zip. Шаги (идемпотентно, безопасно ре-запускать):
#   1) нет ./ocr-<doc>.txt → пишем записку .model-rpc/<rid>.fetch.json
#      {id, doc_id, nonce, dst}; демон station-bridge доставляет текст с бэка
#      (/internal/doc-text) и кладёт его под именем из `dst` (комната
#      запечатана — сеть у демона). Ждём до 90с. Старый мост про `dst` не знает
#      и кладёт в ./ocr.txt — на этот случай ниже фолбэк.
#   2) лаунчер навыка aifa-extract (shelf-aware run.sh) с FE_PROGRESS_NONCE
#      (прогресс релеем #83) и FE_RPC_DIR (модель через мост);
#   3) при `--out-json` — СРАЗУ ЖЕ лаунчер aifa-calc с `--final`: терминал хода
#      ставит он (см. большой комментарий про эстафету ниже). Модель запускает
#      ОДНУ команду — эту.
#
# Использование (полный список флагов — в `_usage` ниже; непонятный флаг = отказ):
#   bash extract_run.sh --doc-id <id> [--nonce <nonce>] [--role <role>]
#                       [--cell <ячейка>] [--metrics <список>] [--basis-full]
#                       [--segments auto|on|off] [--out-json <файл>]
set -euo pipefail

# 🔴 ВСЕ переменные разбора аргументов ОБЪЯВЛЕНЫ ЗДЕСЬ, включая те, что
# ставятся только флагом. При `set -u` (строка выше) необъявленная
# переменная — не пустая строка, а СМЕРТЬ скрипта: `XCALC: unbound
# variable`. Так и вышло 26.08 на прогоне БСПБ: `XCALC` завели вместе с
# `--extra-calculated` (2c2aa19), а в эту строку не добавили — и КАЖДЫЙ
# прогон без этого флага падал на строке 218, ещё до запуска движка.
# Модель отчёт об ошибке прочитала верно и полезла править наш скрипт.
DOC=""; NONCE=""; ROLE="other"; CT=""; CELL=""; MET=""; SEG=""; OUTX=""; OUTJ=""
XTRA=""; XCALC=""
BASIS=0
_usage() {
  echo "usage: extract_run.sh --doc-id <id> [--nonce n] [--role r]" >&2
  echo "       [--company-type bank|non_financial] [--cell <ячейка>]" >&2
  echo "       [--metrics <список>] [--extra-metrics <файл>]" >&2
  echo "       [--extra-calculated <файл>] [--basis-full]" >&2
  echo "       [--segments auto|on|off]" >&2
  echo "       [--out-xlsx <файл>] [--out-json <файл>]" >&2
}
while [ $# -gt 0 ]; do case "$1" in
  --doc-id) DOC="$2"; shift 2;;
  --nonce) NONCE="$2"; shift 2;;
  --role) ROLE="$2"; shift 2;;
  --company-type) CT="$2"; shift 2;;
  --cell) CELL="$2"; shift 2;;
  --metrics) MET="$2"; shift 2;;
  --extra-metrics) XTRA="$2"; shift 2;;
  --extra-calculated) XCALC="$2"; shift 2;;
  --segments) SEG="$2"; shift 2;;
  --out-xlsx) OUTX="$2"; shift 2;;
  --out-json) OUTJ="$2"; shift 2;;
  --basis-full) BASIS=1; shift;;
  # НЕПОНЯТЫЙ ФЛАГ — ОТКАЗ, А НЕ `shift`. Прежняя ветка `*) shift;;` глотала
  # опечатку молча: `--basis_full` просто не срабатывал, и анализ оставался без
  # полного базиса при внешне удачном прогоне.
  *) echo "🔴 ОТКАЗ: непонятный аргумент '$1'" >&2; _usage; exit 2;;
esac; done
if [ -z "$DOC" ]; then
  _usage
  exit 2
fi

# ИМЯ ДОКУМЕНТА СТАНОВИТСЯ ИМЕНЕМ ФАЙЛА — значит его форму надо проверить ЗДЕСЬ.
# Слэш или `..` в идентификаторе увели бы запись за рабочую папку комнаты. Демон
# такое имя всё равно отобьёт (`_room_doc_name`), но тогда он молча положит
# текст в `ocr.txt`, а обёртка будет ждать другой файл — и прогон умрёт по
# таймауту вместо внятного отказа.
case "$DOC" in
  *[!A-Za-z0-9._-]*|*..*|.*)
    echo "🔴 ОТКАЗ: --doc-id содержит недопустимые символы: '$DOC'" >&2
    exit 2;;
esac

# ПРОВЕРКА ФОРМЫ СПИСКА ПОКАЗАТЕЛЕЙ — ЗДЕСЬ, А НЕ ТОЛЬКО В ПИТОНЕ. Раньше её
# держал сносимый драйвер бэкенда (`station_driver.py`, `[A-Z0-9_]{1,64}`),
# потому что интерполяция в командную строку шла там. Интерполяция переехала
# сюда (`--metrics "$MET"`) — и проверка обязана переехать следом.
if [ -n "$MET" ]; then
  case "$MET" in
    *\'*|*\"*|*\`*|*\$*|*\;*|*\|*|*\&*|*'<'*|*'>'*|*'('*|*')'*|*$'\n'*)
      echo "🔴 ОТКАЗ: --metrics содержит служебные символы — это список имён" >&2
      exit 2;;
  esac
fi

# 1) Доставка текста (пропускаем, если уже доставлен — идемпотентность).
#
#    🔴 ТЕКСТ ИМЕНУЕТСЯ ДОКУМЕНТОМ (19.08.2026). Раньше и записка, и ожидание
#    были про один `ocr.txt`. Два документа в одном ходе — и второй прогон
#    видел готовый `ocr.txt` первого, выходил из этого блока с кодом 0 и
#    докладывал ЧУЖИЕ числа под своим нонсом. Молчаливый неверный ответ.
#    Теперь записка просит `ocr-<doc>.txt`, и ждём мы именно его.
#
#    ⚠️ ФОЛБЭК НА `ocr.txt` — НЕ ПЕРЕСТРАХОВКА. Мост станции деплоится ОТДЕЛЬНО
#    от полки навыков: новая полка со старым мостом получит текст в `ocr.txt`,
#    потому что про поле `dst` старый мост не знает. Без фолбэка первый же
#    прогон после выкладки полки умер бы по FETCH-TIMEOUT.
#
#    ⚠️ Весь цикл ожидания — в python: шелл комнаты (busybox-ash) собран БЕЗ
#    арифметики, `$((n+1))` валит скрипт синтаксисом ещё на парсинге.
python3 - "$DOC" "$NONCE" <<'PYEOF'
import json, os, sys, time, uuid

DST = f"ocr-{sys.argv[1]}.txt"


def delivered(path):
    return os.path.exists(path) and os.path.getsize(path) > 0


if delivered(DST):
    sys.exit(0)  # уже доставлен
rid = uuid.uuid4().hex
os.makedirs(".model-rpc", exist_ok=True)
tmp = f".model-rpc/{rid}.fetch.json.tmp"
# Нонс в записке (#84 nonce-скоуп): бэк отдаёт документ только по валидной
# паре (nonce, doc_id) — чужой doc_id из комнаты не заказать.
# `dst` — просьба назвать файл документом; старый мост поле игнорирует.
with open(tmp, "w", encoding="utf-8") as f:
    f.write(json.dumps({"id": rid, "doc_id": sys.argv[1], "nonce": sys.argv[2],
                        "dst": DST}))
os.replace(tmp, f".model-rpc/{rid}.fetch.json")
print(f"fetch note: {rid} doc={sys.argv[1]} dst={DST}", file=sys.stderr)
resp_p = f".model-rpc/{rid}.fetchresp.json"
for _ in range(45):  # до 90с
    time.sleep(2)
    if delivered(DST):
        sys.exit(0)
    if os.path.exists(resp_p):
        try:
            resp = json.load(open(resp_p))
        except Exception:
            continue  # пишется в этот момент
        # Fail-fast: демон уже ответил отказом (403 скоупа/404) — не ждать 90с.
        if resp.get("error"):
            print(f"FETCH-DENIED: {resp['error']}", file=sys.stderr)
            sys.exit(3)
        # Успех у СТАРОГО моста: имени он не называет и кладёт в `ocr.txt`.
        # Проверяем это ТОЛЬКО после его ответа — иначе подхватили бы `ocr.txt`
        # прошлого документа, то есть ровно тот дефект, который чиним.
        if delivered("ocr.txt"):
            print("fetch: старый мост, текст в ocr.txt", file=sys.stderr)
            sys.exit(0)
print("FETCH-TIMEOUT: документ не доставлен за 90с", file=sys.stderr)
sys.exit(3)
PYEOF

# Какой файл в итоге лежит — решает наличие, а не догадка: см. фолбэк выше.
TXT="./ocr-$DOC.txt"
[ -s "$TXT" ] || TXT="./ocr.txt"

# 2) Извлечение движком с полки (запечённый shelf-aware лаунчер).
#    ⚠️ БЕЗ бинаря `env`: его нет в exec-allowlist комнаты (Permission denied,
#    exit 126). Переменные — шелл-builtin export (как драйверный путь VAR=val).
#
#    🔴 ТОЛЬКО POSIX: НИКАКИХ МАССИВОВ. `bash` в комнате станции — это BUSYBOX
#    (один и тот же бинарь 190 КБ на /bin/sh и /bin/bash), а не GNU bash.
#    Массив `ARGS=(...)` там — синтаксическая ошибка, и падала она ДО первой
#    полезной строки. Пока комнату поднимал снесённый драйвер, скрипт исполнялся
#    настоящим bash сервиса станции, и дефект был невидим; на пути по навыкам
#    команду запускает МОДЕЛЬ в песочнице — и она получила отказ на первом же
#    шаге, после чего полчаса щупала версии шелла вместо работы (живой ход
#    15.08). Аргументы копим позиционными (`set --`): переразбора по пробелам
#    нет, имя файла с пробелом остаётся одним аргументом — ровно то, ради чего
#    брался массив.
set -- --text "$TXT" --document-id "$DOC" --role "$ROLE"
[ -n "$CT" ]   && set -- "$@" --company-type "$CT"
[ -n "$CELL" ] && set -- "$@" --cell "$CELL"
[ -n "$MET" ]  && set -- "$@" --metrics "$MET"
[ -n "$XTRA" ] && set -- "$@" --extra-metrics "$XTRA"
[ -n "$SEG" ]  && set -- "$@" --segments "$SEG"
[ -n "$OUTX" ] && set -- "$@" --out-xlsx "$OUTX"
[ -n "$OUTJ" ] && set -- "$@" --out-json "$OUTJ"
[ "$BASIS" = "1" ] && set -- "$@" --basis-full

# The old extract → aifa-calc relay is deliberately gone.  Calculation is
# already part of the resolved PlanGraph; a second process would be a parallel
# legacy production route.  Legacy ad-hoc definition files are still passed to
# the shell so it can return an explicit typed refusal rather than fall back.
if [ -n "$XCALC" ]; then
  set -- "$@" --extra-calculated "$XCALC"
fi
export FE_PROGRESS_NONCE="$NONCE" FE_RPC_DIR=./.model-rpc
# ⚠️ БЕЗ `dirname`: внешних бинарей в exec-allowlist комнаты нет (та же грабля,
# что с `env`). `${0%/*}` — раскрытие параметра самим шеллом, без вызова.
exec sh "${0%/*}/../run.sh" "$@"
