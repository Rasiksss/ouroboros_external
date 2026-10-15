#!/bin/bash
# Лаунчер навыка расчёта коэффициентов. Тело — общее (`_runner/launch.sh`).
SKILL="aifa-calc"
SKILL_REL="aifa_station_skills/aifa-calc/scripts/calc_run.py"
export SKILL SKILL_REL
exec sh "${0%/*}/../_runner/launch.sh" "$@"
