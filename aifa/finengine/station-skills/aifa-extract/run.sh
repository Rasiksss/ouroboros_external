#!/bin/bash
# Лаунчер навыка извлечения. Тело — общее (`_runner/launch.sh`): полка вместо
# запечённой копии (#51), громкий отказ вместо подмены версии (#181), отпечаток
# исполненного файла (#155).
SKILL="aifa-extract"
SKILL_REL="aifa_station_skills/aifa-extract/scripts/extract.py"
export SKILL SKILL_REL
exec sh "${0%/*}/../_runner/launch.sh" "$@"
