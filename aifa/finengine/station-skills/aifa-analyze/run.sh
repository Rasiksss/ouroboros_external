#!/bin/bash
# Лаунчер навыка анализа. Тело — общее (`_runner/launch.sh`). METHOD_REL
# добавляет методику в проверку КОМПЛЕКТА: без неё скрипт дошёл бы до сборки
# промпта и упал через минуты работы и потраченный вызов модели.
SKILL="aifa-analyze"
SKILL_REL="aifa_station_skills/aifa-analyze/scripts/analyze.py"
METHOD_REL="aifa_station_skills/aifa-analyze/method/_order.md"
export SKILL SKILL_REL METHOD_REL
exec sh "${0%/*}/../_runner/launch.sh" "$@"
