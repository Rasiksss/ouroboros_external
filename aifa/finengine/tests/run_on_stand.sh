#!/usr/bin/env bash
# Прогнать тесты ПРОТИВ ЖИВОГО Postgres — на dev-стенде, в контейнере бэкенда.
#
#   bash tests/run_on_stand.sh                 # весь набор
#   bash tests/run_on_stand.sh tests/test_x.py # выборочно
#
# 🔴 ЗАЧЕМ. Правило Алексея 6.2 (🔴 MUST, D23): «нужна БД — реальный Postgres».
# С 26.08.2026 хранилища тестируются только так: SQLite-подложка снесена вместе
# с файловыми запасными путями. Без этой команды тесты хранилищ ПРОПУСКАЮТСЯ —
# внятно, с причиной, а не молча (то же правило 6.2 это разрешает).
#
# Форма — из 6.1: репозиторий монтируется в контейнер, pytest ставится на месте,
# ruff — тоже: сторожа мёртвого кода зовут его, и без него они врут находками,
# адрес базы отдаётся переменной. Отличие от их команды одно: у нас Postgres не
# на `host.docker.internal`, а свой контейнер в сети `finengine_internal`.
#
# ⚠️ Каждый тест берёт СВОЮ схему (`бд` в conftest) и сносит её за собой, так
# что прогон не задевает данные стенда. Схема `public` не трогается.
#
# 🔴 ДВА ПРОГОНА ДЕЛЯТ РАБОТУ, И НИ ОДИН НЕ ПОЛНЫЙ САМ ПО СЕБЕ:
#   локально  — всё, чему не нужна база (тесты хранилищ там ПРОПУСКАЮТСЯ);
#   здесь     — всё, чему она нужна.
# Вендорский снимок реестра (`benchmarks/vendor`, 337 МБ на LFS) сюда НЕ едет:
# зависящие от него сторожа расхождения с апстримом гоняются локально, где он
# и лежит. Гнать его по ssh ради двух тестов — минуты на каждый прогон.
set -euo pipefail

VPS="${VPS:-85.198.84.50}"
KEY="${KEY:-$HOME/.ssh/finengine_deploy}"
TARGETS="${*:-tests/}"
IMAGE="$(ssh -i "$KEY" root@"$VPS" 'docker inspect finengine-backend-1 --format "{{.Config.Image}}"')"

echo "▶ образ: $IMAGE; цели: $TARGETS"

# На стенд едет ТОЛЬКО то, что нужно прогону: в образе бэкенда каталога tests
# нет, а гнать репозиторий целиком — 12 ГБ (сборки фронта, бенчмарки, данные).
# Список явный, а не «всё кроме»: «кроме» молча растёт с каждым новым каталогом.
# Каталог НЕ сносим: rsync докидывает разницу, и повторный прогон идёт секунды,
# а не переливает вендорский снимок реестра (337 МБ) заново.
ssh -i "$KEY" root@"$VPS" 'mkdir -p /tmp/fa-tests'
rsync -azR --delete -e "ssh -i $KEY" \
  --exclude '__pycache__' --exclude '*.pyc' \
  --exclude 'benchmarks/vendor' \
  --exclude 'tests/Нефинансовые excel' --exclude 'tests/Банки excel' \
  --exclude 'tests/Нефинансовые JSON' --exclude 'tests/Валидация 10 нефинансовых отчетов' \
  src tests station-skills deploy benchmarks web/src agent-workspace CLAUDE.md pyproject.toml \
  root@"$VPS":/tmp/fa-tests/

# 🔴 АДРЕС С ПАРОЛЕМ НЕ ПОЯВЛЯЕТСЯ НИ В ОДНОМ ARGV. Прежняя редакция везла его
# аргументом ssh и `docker run -e VAR=значение` — он светился в `ps` на VPS и
# в истории. Теперь: скрипт уезжает по stdin (argv ssh — только `bash -s`),
# адрес добывается У ЖИВОГО БЭКЕНДА уже НА VPS и передаётся в контейнер формой
# `-e VAR` без значения — docker берёт его из окружения своего процесса, argv
# несёт одно имя переменной. Заодно адрес не может разойтись с тем, на чём
# стенд работает на самом деле.
ssh -i "$KEY" root@"$VPS" bash -s <<УДАЛЁННО
set -euo pipefail
export FINENGINE_TEST_DATABASE_URL="\$(docker exec finengine-backend-1 printenv FINENGINE_JOURNALS_DATABASE_URL)"
docker run --rm \
  --network finengine_internal \
  -v /tmp/fa-tests:/repo -w /repo \
  -e PYTHONPATH=/repo/src \
  -e FINENGINE_TEST_DATABASE_URL \
  '$IMAGE' sh -c 'pip install -q pytest pytest-asyncio ruff && python -m pytest $TARGETS -q'
УДАЛЁННО
