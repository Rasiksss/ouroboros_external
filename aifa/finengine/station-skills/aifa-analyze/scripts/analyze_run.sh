#!/bin/bash
# AIFA CLAW — обёртка разбора: личная методика + запуск навыка ОДНОЙ командой.
#
# ЗАЧЕМ ОНА ЕСТЬ (#123). Пользователь правит методику словами, правка ложится в
# его личный слой на бэкенде, агент отвечает «записал» — а разбор идёт в
# запечатанной комнате, куда эта правка не попадала никак. Команду разбора
# собирает МОДЕЛЬ, и про личный слой она не знает; флаг `--methodology` помечен
# ВНУТРЕННИМ и в теле навыка не назван намеренно — файл должен появиться в
# комнате сам. Появляется он здесь.
#
# ПОЧЕМУ ОБЁРТКА, А НЕ САМ `analyze.py`. Заказ файла через демона — это
# доставка, а не разбор: ровно тем же приёмом и в таком же скрипте
# (`aifa-extract/scripts/extract_run.sh`) в комнату приезжает текст документа.
# Второй способ спросить демона разъехался бы с первым при первой же правке
# протокола, а `analyze.py` пришлось бы учить отличать «не заказывал» от
# «заказал и не приехало».
#
# ЧТО ПРОИСХОДИТ (идемпотентно, безопасно ре-запускать):
#   1) нет ./methodology.json → записка .model-rpc/<rid>.method.json {id, nonce};
#      демон station-bridge (сеть есть у него, у комнаты нет) берёт правки на
#      бэкенде ПО НОНСУ и кладёт файл. Нонс — из FE_PROGRESS_NONCE, который
#      модель и так выставляет для прогресса: второго способа его назвать нет;
#   2) файл приехал → добавляем `--methodology`; не приехал → НЕ добавляем.
#
# 🔴 «РОВНО ОДИН ХОД», А НЕ «РОВНО ОДИН АНАЛИЗ» — ОСОЗНАННАЯ ГРАНИЦА. Второй
# запуск обёртки в ТОЙ ЖЕ комнате видит готовый `methodology.json` и добавляет
# флаг, ни о чём не спрашивая: ни нонса, ни моста. Значит разовая правка «в этот
# раз» уедет в ОБА разбора хода, если модель запустит их два. Так и задумано:
# «в этот раз» — это указание пользователя на ХОД, и применить его к первому
# документу, а второй посчитать по базовой было бы худшей из двух неточностей —
# молчаливо разными правилами в одном ответе. Комната живёт один ход
# (`main.py` минтит ключ гейтвея на каждый `/chat/turns`), поэтому дальше хода
# файл не переживает.
#
# 🔴 ОТСУТСТВИЕ МЕТОДИКИ — НЕ ОТКАЗ. У пользователя может не быть ни одной
# правки, мост может быть старой выкладки, нонса может не быть вовсе. Во всех
# трёх случаях разбор обязан пойти по БАЗОВОЙ методике, а не умереть: паспорт
# отчёта назовёт её базовой, и это честный ответ. Уронить готовый к работе
# разбор из-за необязательной доставки было бы хуже дефекта, который она чинит.
#
# 🔴 ТОЛЬКО POSIX: `bash` в комнате станции — это BUSYBOX (тот же бинарь, что
# /bin/sh). Массивов нет, внешних утилит (`env`, `dirname`) в exec-allowlist
# нет — отсюда `${0%/*}` и накопление аргументов через `set --`.
set -eu

METH="./methodology.json"

# Весь цикл ожидания — в python: у busybox-ash нет арифметики, `$((n+1))` валит
# скрипт синтаксисом ещё на парсинге. Выходим ВСЕГДА нулём — решение принимает
# шелл ниже, по наличию файла.
python3 - "${FE_PROGRESS_NONCE:-}" "$METH" <<'PYEOF'
import json, os, sys, time, uuid

nonce, dst = sys.argv[1], sys.argv[2]


def delivered():
    return os.path.exists(dst) and os.path.getsize(dst) > 0


if delivered():
    sys.exit(0)  # уже доставлена — перезапуск обёртки ничего не переспрашивает
if not nonce:
    print("методика: нонса хода нет — разбор пойдёт по БАЗОВОЙ", file=sys.stderr)
    sys.exit(0)
rid = uuid.uuid4().hex
os.makedirs(".model-rpc", exist_ok=True)
tmp = f".model-rpc/{rid}.method.json.tmp"
with open(tmp, "w", encoding="utf-8") as f:
    f.write(json.dumps({"id": rid, "nonce": nonce}))
os.replace(tmp, f".model-rpc/{rid}.method.json")
print(f"methodology note: {rid}", file=sys.stderr)
resp_p = f".model-rpc/{rid}.methodresp.json"
# 20 с потолка: ответ демона — это чтение маленького JSON по внутренней сети.
# Дольше ждать нечего, а на СТАРОМ мосту (записки он не знает) ожидание — это
# чистая задержка перед каждым разбором.
for _ in range(40):
    time.sleep(0.5)
    if delivered():
        sys.exit(0)
    if os.path.exists(resp_p):
        try:
            resp = json.load(open(resp_p))
        except Exception:
            continue  # пишется в этот момент
        if resp.get("error"):
            print(f"методика не доставлена ({resp['error']}) — разбор по БАЗОВОЙ",
                  file=sys.stderr)
        sys.exit(0)
print("методика не доставлена за 20с (старый мост?) — разбор по БАЗОВОЙ", file=sys.stderr)
PYEOF

if [ -s "$METH" ]; then
  set -- "$@" --methodology "$METH"
fi

# ── ВНЕШНИЙ СРЕЗ (первый эшелон, п.2) ───────────────────────────────────────
#
# Сборщик новостей и макро написан давно и выверен, а вызывающих на живом пути
# было НОЛЬ: раздел «Контекст извне» не появлялся никогда, и пустота читалась
# как «снаружи тихо». Худший вид молчания — отсутствие ответа неотличимо от
# ответа «ничего не происходило».
#
# Ищет БЭКЕНД: комната запечатана, и попросить составителя анализа «загляни в
# новости» физически нельзя — он напишет по памяти, датированной обучением.
# Компанию берём из `ocr.meta.json` (его кладёт демон рядом с текстом): её знает
# документ, а не модель, и угадывать её здесь нечем.
EXT="./external.json"
python3 - "${FE_PROGRESS_NONCE:-}" "$EXT" <<'PYEOF'
import json, os, sys, time, uuid

nonce, dst = sys.argv[1], sys.argv[2]


def delivered():
    return os.path.exists(dst) and os.path.getsize(dst) > 0


if delivered() or not nonce:
    sys.exit(0)
company = period = ""
for name in os.listdir("."):
    if name.endswith(".meta.json"):
        try:
            meta = json.load(open(name, encoding="utf-8"))
        except Exception:
            continue
        company = str(meta.get("company_name") or "").strip()
        period = str(meta.get("period") or "").strip()
        if company:
            break
if not company:
    print("внешний срез: компания не названа — раздела не будет", file=sys.stderr)
    sys.exit(0)
rid = uuid.uuid4().hex
os.makedirs(".model-rpc", exist_ok=True)
tmp = f".model-rpc/{rid}.external.json.tmp"
with open(tmp, "w", encoding="utf-8") as f:
    f.write(json.dumps({"id": rid, "nonce": nonce, "company": company,
                        "period": period, "language": "ru"}, ensure_ascii=False))
os.replace(tmp, f".model-rpc/{rid}.external.json")
print(f"external note: {rid} ({company})", file=sys.stderr)
resp_p = f".model-rpc/{rid}.externalresp.json"
# 45 с: поиск ходит в интернет, это дольше чтения методики. Дольше ждать нечем —
# разбор без раздела выходит полным, а стоящий разбор не выходит вовсе.
for _ in range(90):
    time.sleep(0.5)
    if delivered():
        sys.exit(0)
    if os.path.exists(resp_p):
        try:
            resp = json.load(open(resp_p))
        except Exception:
            continue
        if resp.get("error"):
            print(f"внешний срез не собран ({resp['error']}) — раздела не будет",
                  file=sys.stderr)
        sys.exit(0)
print("внешний срез не собран за 45с (старый мост?) — раздела не будет", file=sys.stderr)
PYEOF

if [ -s "$EXT" ]; then
  set -- "$@" --external "$EXT"
fi

# ⚠️ Лаунчер навыка — С ПОЛКИ, соседний `../run.sh`, а не запечённая в образ
# копия: смешивать источники значит считать по разным редакциям навыка.
#
# ⚠️ `exec` ЗДЕСЬ ВЕРЕН, в отличие от обёртки извлечения. Там ниже стоит вторая
# половина эстафеты, и `exec` сделал бы её мёртвым кодом; здесь ниже нет ничего.
# Заодно процесс остаётся ОДИН на всю цепочку (обёртка → лаунчер → python), и
# сторож дубля по живым процессам комнаты считает ровно столько же, сколько
# считал до появления обёртки.
exec sh "${0%/*}/../run.sh" "$@"
