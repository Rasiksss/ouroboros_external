#!/bin/bash
# Лаунчер единого natural-language ingress для семи GoalType.
# полка вместо запечённой копии (#51), громкий отказ вместо подмены версии (#181),
# отпечаток исполненного файла (#155).
SKILL="aifa-metric"
SKILL_REL="aifa_station_skills/aifa-metric/scripts/metric_run.py"
export SKILL SKILL_REL
exec sh "${0%/*}/../_runner/launch.sh" "$@"
