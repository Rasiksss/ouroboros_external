"""Путь к полке: exec — относительный, файловый API — /workspace (#185).

ЗАЧЕМ ЭТОТ ФАЙЛ. Про полку знают ТРИ независимые стороны, и до 16.08.2026 они
договаривались только на словах:

  1. плагин гейтвея (`deploy/workstation-ext/index.ts`) кладёт архив ЧЕРЕЗ
     ФАЙЛОВЫЙ API по `/workspace/.shelf.zip`;
  2. тела навыков (`station-skills/*/SKILL.md`) говорят модели, что распаковать
     и что запустить;
  3. общий лаунчер (`station-skills/_runner/launch.sh`) распаковывает архив,
     если каталога ещё нет.

🔴 ГЛАВНОЕ, ЧТО ЗДЕСЬ ЗАФИКСИРОВАНО: `/workspace` — ЭТО КОРЕНЬ ФАЙЛОВОГО API
ГЕЙТВЕЯ, А НЕ ПУТЬ В ФАЙЛОВОЙ СИСТЕМЕ КОМНАТЫ. Рабочий каталог exec —
`/tmp/ws/<id>/workspace`, и `/workspace` оттуда недостижим вовсе.

ЦЕНА МОЕЙ ОШИБКИ, ОПЛАЧЕННАЯ ПРОГОНОМ ВЛАДЕЛЬЦА. 16.08 я решил, что причина
ENOENT — относительный путь, и сделал пути абсолютными: `/workspace/.shelf`.
Выкатил. Следующий же прогон показал в чате:

    «он ищет по пути /workspace/.shelf, а рабочая директория —
     /tmp/ws/.../workspace … Нужно задать FE_SHELF_DIR»

Модель дочинила прогон руками через переменную окружения. То есть правка,
задуманная как страховка, стала поломкой, а спас её тот, кого она «страховала».

ПОЧЕМУ Я ОШИБСЯ. Плагин кладёт архив по `/workspace/.shelf.zip`, и я принял
этот путь за путь в комнате. Он — путь в ФАЙЛОВОМ API. У exec своя система
координат. Проверить это можно было одной командой `pwd` в живой комнате, и я
её не сделал: перепутал два пространства имён, глядя в код обоих.

⚠️ ОТСЮДА ПРАВИЛО ЭТОГО ФАЙЛА: пути для EXEC — только относительные (cwd и
есть workspace), пути для файлового API — только `/workspace/...`. Смешивать
их нельзя, и «унифицировать» тоже.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: поставить в `launch.sh` дефолт `/workspace/.shelf` —
красным станет `test_launcher_defaults_are_relative_to_cwd`; поставить в любой
SKILL.md абсолютный путь в exec-команде — `test_skill_bodies_use_cwd_relative_paths`.

⚠️ ЧЕГО ЭТИ СТОРОЖА НЕ ЛОВЯТ (дописано ревизией 21.08.2026). Три из четырёх
проверяют ТЕКСТ, который читает не наш код, а чужой исполнитель: плагин на
TypeScript и модель гейтвея. Исполнить их нечем — здесь нет ни одной функции
нашего репозитория, которая приняла бы этот текст и что-то по нему сделала.
Значит: совпадение путей они докажут, а РАБОТУ полки в живой комнате — нет.
Полку, доехавшую пустой, полку, распакованную не туда, и модель, которая
прочитала правильный путь и всё равно пошла своей дорогой, — не увидит ни
один из них. Единственный исполняемый здесь — сторож дефолтов лаунчера: он
запускает `run.sh` по-настоящему.
"""
from __future__ import annotations

import re
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = sorted((ROOT / "station-skills").glob("*/SKILL.md"))
LAUNCHER = ROOT / "station-skills" / "_runner" / "launch.sh"
PLUGIN = ROOT / "deploy" / "workstation-ext" / "index.ts"

# Куда плагин кладёт архив — в координатах ФАЙЛОВОГО API. Это НЕ путь в комнате
# и подставлять его в exec-команды нельзя (проверено поломкой 16.08).
SHELF_ZIP_IN_FILE_API = "/workspace/.shelf.zip"


def test_the_plugin_still_uploads_where_we_think():
    """Опора всей проверки: убеждаемся, что плагин не переехал."""
    assert PLUGIN.is_file(), f"плагина нет: {PLUGIN}"
    assert SHELF_ZIP_IN_FILE_API in PLUGIN.read_text(encoding="utf-8"), (
        f"плагин больше не кладёт полку в {SHELF_ZIP_IN_FILE_API} — значит "
        "изменилось само место, и относительные пути в комнате тоже надо "
        "перепроверить живым прогоном, а не рассуждением")


_REL = "aifa_station_skills/aifa-extract/scripts/extract.py"


def _launch_without_env(cwd: Path) -> subprocess.CompletedProcess:
    """Запуск ровно как в комнате: рабочий каталог задан, путей к полке — нет.

    `FE_SHELF_DIR`/`FE_SHELF_ZIP` НЕ передаются намеренно — проверяются именно
    умолчания. `FE_BAKED_DIR` уводится в несуществующий каталог, чтобы
    молчаливый уход на запечённую копию не сошёл за успех.
    """
    return subprocess.run(
        ["bash", str(ROOT / "station-skills" / "aifa-extract" / "run.sh")],
        capture_output=True, text=True, timeout=60, cwd=str(cwd),
        env={"PATH": "/usr/bin:/bin:/usr/local/bin",
             "FE_BAKED_DIR": str(cwd / "запечённой-копии-нет")},
    )


@pytest.mark.parametrize("shape", ["каталог", "архив"])
def test_launcher_defaults_are_relative_to_cwd(tmp_path, shape):
    """Лаунчер исполняется внутри exec, значит ищет полку ОТНОСИТЕЛЬНО cwd.

    🔴 ПЕРЕПИСАН 21.08.2026 С ПОДСТРОКИ НА ЗАПУСК. Прежняя редакция искала в
    теле `launch.sh` строки `FE_SHELF_DIR:-./.shelf` и `FE_SHELF_ZIP:-./.shelf.zip`.
    Она охраняла НАПИСАНИЕ умолчания, а не место поиска: `SHELF="$PWD/.shelf"`,
    `cd /workspace` строкой выше или `SHELF="${FE_SHELF_DIR:-$FE_ROOT/.shelf}"`
    ломают ровно тот же прогон и оставляют обе подстроки на месте.

    Теперь `run.sh` запускается по-настоящему из временного каталога, полка
    лежит рядом относительным путём, а переменные окружения НЕ заданы. Дошло
    исполнение до скрипта полки — умолчания относительные; ушло в отказ
    кодом 3 — лаунчер снова ищет там, где в комнате ничего нет.

    Абсолютный `/workspace/...` здесь недостижим: это корень файлового API
    гейтвея, а не путь в комнате. Проверено живым прогоном 16.08 — см. шапку.
    """
    marker = "ЗАПУЩЕНО-С-ПОЛКИ-ПО-УМОЛЧАНИЮ"
    body = f"print({marker!r})\n"
    if shape == "каталог":
        script = tmp_path / ".shelf" / _REL
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(body, encoding="utf-8")
    else:
        with zipfile.ZipFile(tmp_path / ".shelf.zip", "w") as zf:
            zf.writestr(_REL, body)

    r = _launch_without_env(tmp_path)
    assert r.returncode == 0, (
        f"лаунчер без FE_SHELF_* не нашёл полку ({shape}), код {r.returncode}. "
        f"В комнате нет /workspace — умолчания обязаны быть относительными:\n"
        f"{r.stderr[-800:]}")
    assert marker in r.stdout, (
        f"исполнилось не то, что лежит на полке ({shape}):\n{r.stdout}\n{r.stderr[-800:]}")
    assert "./.shelf" in r.stderr, (
        "отпечаток называет полку не относительным путём — значит лаунчер "
        f"нашёл её где-то ещё:\n{r.stderr[-800:]}")


@pytest.mark.parametrize("skill_md", SKILLS, ids=lambda p: p.parent.name)
def test_skill_bodies_use_cwd_relative_paths(skill_md):
    """Команды в теле навыка исполняет EXEC — значит пути относительные.

    Модель выполняет написанное буквально. Дашь ей `/workspace/.shelf` — она
    получит ENOENT, потому что в комнате такого пути нет, и полезет чинить
    окружение вручную (так и было 16.08).
    """
    body = skill_md.read_text(encoding="utf-8")
    bad = [m.group(0) for m in re.finditer(r"/workspace/\.shelf", body)]
    assert not bad, (
        f"{skill_md.parent.name}/SKILL.md даёт модели абсолютный путь к полке "
        f"({len(bad)} шт.), а в комнате рабочий каталог /tmp/ws/<id>/workspace "
        "и /workspace недостижим. Нужен относительный `.shelf/`")


@pytest.mark.parametrize("skill_md", SKILLS, ids=lambda p: p.parent.name)
def test_what_the_body_tells_to_run_exists_in_the_repo(skill_md):
    """Что тело велит запустить — то в репозитории и лежит.

    Ловит опечатку в имени скрипта: модель получит ENOENT и уйдёт в тот же
    цикл перезапусков. Путь в теле — внутри полки, а полка собирается из
    `station-skills/`, поэтому корень отображается один в один.
    """
    body = skill_md.read_text(encoding="utf-8")
    prefix = ".shelf/aifa_station_skills/"
    for m in re.finditer(re.escape(prefix) + r"([\w./-]+\.(?:sh|py))", body):
        rel = m.group(1)
        target = ROOT / "station-skills" / rel
        assert target.is_file(), (
            f"{skill_md.parent.name}/SKILL.md велит запустить {prefix}{rel}, "
            f"а в репозитории такого файла нет ({target}). В комнате это даст "
            "ENOENT, а модель ответит перезапуском — см. #185")
