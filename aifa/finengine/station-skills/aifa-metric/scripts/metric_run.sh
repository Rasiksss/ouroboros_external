#!/bin/bash
# AIFA CLAW — воркер-обёртка единого financial-goal ingress: ОДНА команда для
# суб-агента. Зеркало `aifa-extract/scripts/extract_run.sh` (тот же протокол
# доставки OCR-текста через `.model-rpc`), но вместо `--role`/`--metrics`
# несёт буквальный запрос пользователя. Все семь поддержанных GoalType
# классифицирует настоящий `intent.parse` внутри `metric_run.py`.
#
# Запускается В КОМНАТЕ воркера (exec суб-агента, cwd = workspace комнаты; пути
# ТОЛЬКО относительные). Едет на ПОЛКЕ (files:upload плагина).
#
# Использование:
#   bash metric_run.sh --doc-id <id> --query "<запрос пользователя>" [--nonce <nonce>]
#                       [--company-type bank|non_financial] [--cell <ячейка>]
#                       [--out-json <файл>]
set -euo pipefail

DOC=""; NONCE=""; QUERY=""; CT=""; CELL=""; OUTJ=""
_usage() {
  echo "usage: metric_run.sh --doc-id <id> --query <текст> [--nonce n]" >&2
  echo "       [--company-type bank|non_financial] [--cell <ячейка>]" >&2
  echo "       [--out-json <файл>]" >&2
}
while [ $# -gt 0 ]; do case "$1" in
  --doc-id) DOC="$2"; shift 2;;
  --nonce) NONCE="$2"; shift 2;;
  --query) QUERY="$2"; shift 2;;
  --company-type) CT="$2"; shift 2;;
  --cell) CELL="$2"; shift 2;;
  --out-json) OUTJ="$2"; shift 2;;
  *) echo "🔴 ОТКАЗ: непонятный аргумент '$1'" >&2; _usage; exit 2;;
esac; done
if [ -z "$DOC" ] || [ -z "$QUERY" ]; then
  _usage
  exit 2
fi

# ИМЯ ДОКУМЕНТА СТАНОВИТСЯ ИМЕНЕМ ФАЙЛА — та же проверка, что extract_run.sh:
# слэш или `..` в идентификаторе увели бы запись за рабочую папку комнаты.
case "$DOC" in
  *[!A-Za-z0-9._-]*|*..*|.*)
    echo "🔴 ОТКАЗ: --doc-id содержит недопустимые символы: '$DOC'" >&2
    exit 2;;
esac

# `--query` — свободный текст на естественном языке, не список enum'ов: здесь
# намеренно нет блок-листа спецсимволов `--metrics`. Аргументы передаются
# позиционно (`set --`/`"$@"`) без повторного разбора шеллом ни на одном шаге
# до Python's argparse — интерполяции в исполняемую команду не происходит,
# значит и риска инъекции через пунктуацию свободного текста нет.
if [ -z "${QUERY## }" ]; then
  echo "🔴 ОТКАЗ: --query пустой" >&2
  exit 2
fi

# 1) Доставка текста (пропускаем, если уже доставлен — идемпотентность).
#    Тот же протокол, что extract_run.sh — см. комментарии там.
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
        if resp.get("error"):
            print(f"FETCH-DENIED: {resp['error']}", file=sys.stderr)
            sys.exit(3)
        if delivered("ocr.txt"):
            print("fetch: старый мост, текст в ocr.txt", file=sys.stderr)
            sys.exit(0)
print("FETCH-TIMEOUT: документ не доставлен за 90с", file=sys.stderr)
sys.exit(3)
PYEOF

TXT="./ocr-$DOC.txt"
[ -s "$TXT" ] || TXT="./ocr.txt"

# 2) Точечный показатель/расчёт движком с полки (запечённый shelf-aware лаунчер).
set -- --text "$TXT" --document-id "$DOC" --query "$QUERY"
[ -n "$CT" ]   && set -- "$@" --company-type "$CT"
[ -n "$CELL" ] && set -- "$@" --cell "$CELL"
[ -n "$OUTJ" ] && set -- "$@" --out-json "$OUTJ"
export FE_PROGRESS_NONCE="$NONCE" FE_RPC_DIR=./.model-rpc
exec sh "${0%/*}/../run.sh" "$@"
