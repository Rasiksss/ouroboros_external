"""Выкатка не оставляет демона станции вчерашним (#123, 21.08.2026).

ЦЕНА, КОТОРУЮ ЭТОТ ФАЙЛ ЗАКРЫВАЕТ. `deploy/station-bridge/bridge.py` живёт
ТОЛЬКО в образе станции (`deploy/workstation-aifa/Dockerfile.shelf`) и
пересобирается ТОЛЬКО по флагу `--station`. Полка, тело навыка и бэкенд едут без
него. Значит обычная выкатка после правки демона даёт «новая полка + новый
бэкенд + СТАРЫЙ демон», и это состояние НЕ ПАДАЕТ, а молчит: записку комнаты
старый демон не разбирает, обёртка разбора ждёт своё окно (замер состязательной
проверки — 20,3 с) и уходит считать по БАЗОВОЙ методике. Пользователь получает
отчёт, который выглядит нормальным, посчитанный не по его правилам.

ПОЧЕМУ СТОРОЖ, А НЕ СТРОЧКА В ИНСТРУКЦИИ. Флаг зависел от памяти человека, и
это ровно та болезнь, которой в этом репозитории посвящён отдельный список
(«обновляется одно из двух»): плагин гейтвея, лаунчеры станции, Caddyfile — три
случая, каждый найден постфактум.

ЧТО ЗДЕСЬ ПРОГОНЯЕТСЯ. Блок сверки БЕРЁТСЯ ИЗ `ship.sh` и исполняется с
поддельным `ssh`: своя копия условия доказывала бы работоспособность текста,
которого в выкатке может уже не быть. Три исхода — совпало, разошлось, станция
молчит — проверяются кодом возврата и словами, которые видит оператор.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SHIP = Path(__file__).resolve().parents[1] / "deploy" / "skill-hub" / "ship.sh"


def _station_check_block() -> str:
    """Кусок ship.sh, сверяющий демона. Из скрипта, а не пересказом."""
    body = SHIP.read_text(encoding="utf-8")
    start = body.index('if [ "$WEB_ONLY" = 0 ] && [ "$WITH_STATION" = 0 ]; then')
    return body[start:body.index("\nfi\n", start) + len("\nfi\n")]


def _run(tmp_path: Path, *, in_image: str, local: str = "aaa") -> tuple[int, str]:
    """Прогнать блок с поддельным ssh: `in_image` — что «ответил» образ станции."""
    fake_ssh = tmp_path / "fake-ssh"
    fake_ssh.write_text(f"#!/bin/sh\nprintf '%s' {in_image!r}\n", encoding="utf-8")
    fake_ssh.chmod(0o755)
    script = tmp_path / "check.sh"
    script.write_text(
        "set -eu\nWEB_ONLY=0\nWITH_STATION=0\n"
        f'SSH="{fake_ssh}"\n_md5() {{ printf "%s" "{local}"; }}\n'
        + _station_check_block(), encoding="utf-8")
    done = subprocess.run(["bash", str(script)], capture_output=True, text=True, check=False)
    return done.returncode, done.stdout + done.stderr


def test_the_deploy_refuses_when_the_daemon_in_the_image_is_older(tmp_path):
    """Главное: забыть `--station` больше нельзя — выкатка останавливается."""
    rc, out = _run(tmp_path, in_image="ставший-вчерашним")
    assert rc == 8, f"выкатка со старым демоном не остановлена: rc={rc}\n{out}"
    assert "--station" in out, "отказ не назвал команду, которой чинится"


def test_the_deploy_goes_on_when_the_daemon_matches(tmp_path):
    """Ложный отказ хуже отсутствия проверки: катать надо каждый день."""
    rc, out = _run(tmp_path, in_image="aaa")
    assert rc == 0 and "✅" in out, out


def test_a_station_that_does_not_answer_is_a_loud_warning_not_a_refusal(tmp_path):
    """Лежащая станция — не повод отказать в выкатке, но и не повод молчать."""
    rc, out = _run(tmp_path, in_image="")
    assert rc == 0, "станция не ответила, а выкатку остановили"
    assert "⚠️" in out and "--station" in out, out


@pytest.mark.parametrize("flag", ["WITH_STATION", "WEB_ONLY"])
def test_the_check_steps_aside_when_it_has_nothing_to_guard(tmp_path, flag):
    """`--station` пересоберёт демона сам, `--web` не трогает ни полку, ни бэк."""
    fake_ssh = tmp_path / "fake-ssh"
    fake_ssh.write_text("#!/bin/sh\nprintf '%s' старьё\n", encoding="utf-8")
    fake_ssh.chmod(0o755)
    script = tmp_path / "check.sh"
    script.write_text(
        "set -eu\nWEB_ONLY=0\nWITH_STATION=0\n" + f"{flag}=1\n"
        f'SSH="{fake_ssh}"\n_md5() {{ printf "%s" "aaa"; }}\n'
        + _station_check_block(), encoding="utf-8")
    done = subprocess.run(["bash", str(script)], capture_output=True, text=True, check=False)
    assert done.returncode == 0 and "сверка демона" not in done.stdout, done.stdout
