"""Второй экземпляр станционного скрипта в комнате отбивается по ЖИВЫМ процессам.

ИСТОРИЯ ТРЁХ ПОПЫТОК (17.08.2026, прогоны Дойчбанка) — чтобы четвёртой не было:

  1. текст в теле навыка («опрашивай, не перезапускай») — прочитан и
     проигнорирован: три лишних копии;
  2. файловый замок в лаунчере — прожил один прогон: `rm -rf .aifa-extract.lock`
     и самодельная обёртка мимо лаунчера;
  3. проверка по /proc В САМОМ скрипте навыка. Стирать нечего, обход прямым
     `python3 …/extract.py` попадает под ту же проверку — НО на стенде она
     ОСЛЕПЛА: `ws-sandbox` подменяет /proc, и процесс не видит ни соседей, ни
     себя (прогон 9f0afd6e: два извлечения рядом, ноль отказов в логе).
     Поэтому здесь же проверка №4 — страж обязан ЗАЯВИТЬ о своей слепоте.
     Настоящий запрет переезжает туда, где процессы видны (по нонсу).

⚠️ ЦЕНА ДУБЛЯ ПЕРЕОЦЕНЕНА 17.08. Считали, что копии рушат качество; замер
против движка Finengine показал, что низкая заполняемость Дойчбанка — свойство
ДОКУМЕНТА (их движок даёт столько же). Дубль — это впустую сожжённые время и
токены, а не испорченная таблица.

Проверки идут на ПОДДЕЛЬНОМ /proc (каталог с cmdline/cwd/stat), поэтому
работают и на macOS: настоящий /proc есть только в станции.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from finengine.skills.station_guard import refuse_if_duplicate

ROOT = Path(__file__).resolve().parents[1]
MARKER = "aifa-extract/scripts/extract.py"


def _fake_proc(tmp_path, pid: int, cmdline: str, cwd: Path) -> Path:
    d = tmp_path / "proc" / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(cmdline.replace(" ", "\0").encode())
    os.symlink(cwd, d / "cwd")
    return d


def _self_visible(tmp_path) -> None:
    """Поддельный /proc обязан показывать СЕБЯ — как настоящий.

    Без этого страж (справедливо) считает такой /proc слепым и выходит, а
    проверка дубля молча превращается в проверку слепоты. Настоящая песочница
    ведёт себя именно так, и отличать эти два случая — смысл всей затеи.
    """
    me = tmp_path / "proc" / str(os.getpid())
    me.mkdir(parents=True, exist_ok=True)
    (me / "cmdline").write_bytes(b"pytest\0")
    (me / "stat").write_text(f"{os.getpid()} (python) S 1 0 0")


def test_duplicate_in_same_cwd_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _self_visible(tmp_path)
    _fake_proc(tmp_path, 4242, f"python3 .shelf/{MARKER} --text ./ocr.txt", tmp_path)
    with pytest.raises(SystemExit) as e:
        refuse_if_duplicate(MARKER, proc_root=str(tmp_path / "proc"))
    assert e.value.code == 5
    err = capsys.readouterr().err
    assert "УЖЕ РАБОТАЕТ" in err and "kill 4242" in err, f"отказ неполный:\n{err}"


def test_other_cwd_is_not_our_business(tmp_path, monkeypatch):
    """Тот же скрипт в ДРУГОЙ комнате — законная параллель, не дубль."""
    monkeypatch.chdir(tmp_path)
    other = tmp_path / "other-room"
    other.mkdir()
    _self_visible(tmp_path)
    _fake_proc(tmp_path, 4243, f"python3 .shelf/{MARKER}", other)
    refuse_if_duplicate(MARKER, proc_root=str(tmp_path / "proc"))  # не падает


def test_ancestor_shell_is_not_a_duplicate(tmp_path, monkeypatch):
    """Родительский `bash -lc "… extract.py …"` несёт маркер в своей командной
    строке — принять его за дубль значило бы отбивать КАЖДЫЙ запуск."""
    monkeypatch.chdir(tmp_path)
    me = os.getpid()
    d = tmp_path / "proc" / str(me)
    d.mkdir(parents=True)
    (d / "stat").write_text(f"{me} (python) S 4244 0 0")
    _fake_proc(tmp_path, 4244, f"bash -lc bash extract_run.sh {MARKER}", tmp_path)
    refuse_if_duplicate(MARKER, proc_root=str(tmp_path / "proc"))  # не падает


def test_no_proc_means_no_room_means_no_refusal(tmp_path, monkeypatch):
    """На машине разработчика /proc нет — движок как библиотека (ab_sweep,
    тесты) обязан работать без оглядки на стража."""
    monkeypatch.chdir(tmp_path)
    refuse_if_duplicate(MARKER, proc_root=str(tmp_path / "нет-такого"))


def test_blindness_is_announced_not_swallowed(tmp_path, monkeypatch, capsys):
    """🔴 Стенд 17.08: в песочнице `ws-sandbox` каталог /proc ЕСТЬ, а записей
    в нём НЕТ — процесс не видит ни соседей, ни себя. Первая версия стража
    молча пропускала, и два извлечения шли рядом без единой строки в логе.

    Пустой, но существующий /proc — это НЕ «дублей нет», это «я слепой», и
    сказать об этом обязан сам страж: иначе следующий разбор снова начнётся с
    вопроса «почему отказа не было».
    """
    monkeypatch.chdir(tmp_path)
    blind = tmp_path / "proc"
    blind.mkdir()  # каталог есть, процессов в нём нет — как в песочнице
    refuse_if_duplicate(MARKER, proc_root=str(blind))  # не падает: работу не рвём

    # В stderr — ТИШИНА. Первая версия писала туда предупреждение, и модель,
    # которая читает stderr, пересказала его пользователю как ошибку прогона
    # («скрипт пытается читать /proc, но в sandbox он недоступен»). Наша
    # внутренняя диагностика не должна попадать пользователю на экран.
    assert not capsys.readouterr().err, "страж шумит в stderr — это увидит пользователь"
    diag = (tmp_path / ".station-guard.diag").read_text(encoding="utf-8")
    assert "проверка дубля не работает" in diag, "слепота нигде не зафиксирована"


def _calls_in(script: Path) -> set[str]:
    tree = ast.parse(script.read_text(encoding="utf-8"))
    return {
        node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        for node in ast.walk(tree) if isinstance(node, ast.Call)
    }


@pytest.mark.parametrize("script,marker", [
    (ROOT / "station-skills" / "aifa-extract" / "scripts" / "extract.py",
     "aifa-extract/scripts/extract.py"),
    (ROOT / "station-skills" / "aifa-analyze" / "scripts" / "analyze.py",
     "aifa-analyze/scripts/analyze.py"),
])
def test_station_scripts_actually_call_the_guard(script, marker):
    """Страж, которого не позвали, не сторожит: проверяем вызов в обоих
    скриптах и то, что маркер совпадает с путём самого скрипта."""
    calls = _calls_in(script)
    assert "refuse_if_duplicate" in calls, f"{script.name}: страж не вызывается"
    assert "start_heartbeat" in calls, f"{script.name}: сердцебиения нет"
    text = script.read_text(encoding="utf-8")
    assert f'refuse_if_duplicate("{marker}")' in text, (
        f"{script.name}: маркер стража не совпадает с путём скрипта")


def test_the_bait_lock_is_gone_from_the_launcher():
    """Отрицательный контроль на прошлую попытку: файловый замок из лаунчера
    убран — вернётся, и модель снова получит то, что можно `rm`."""
    body = (ROOT / "station-skills" / "_runner" / "launch.sh").read_text(encoding="utf-8")
    assert "os.mkdir(lock)" not in body and ".lock/pid" not in body


def test_listdir_denied_is_blindness_not_a_crash(tmp_path, monkeypatch, capsys):
    """🔴 ТРЕТИЙ ЗАХОД, прогон ВТБ 17.08. `/proc/<self>` существует, а `listdir`
    на корне запрещён — песочница отдаёт отказ доступа. Страж падал С ТРАССОЙ,
    и модель, увидев её, шла ПАТЧИТЬ САМ ФАЙЛ вместо работы: «нужно пропатчить
    station_guard.py — в sandbox нет доступа к /proc для listdir».

    Страж, из-за которого чинят стража, хуже отсутствующего: он не только не
    ловит дубль, он ещё и уводит прогон в починку инструмента.
    """
    monkeypatch.chdir(tmp_path)
    proc = tmp_path / "proc"
    _self_visible(tmp_path)

    real = os.listdir

    def denied(path, *a, **kw):
        if os.path.realpath(str(path)) == os.path.realpath(str(proc)):
            raise PermissionError(13, "Permission denied", str(path))
        return real(path, *a, **kw)

    monkeypatch.setattr(os, "listdir", denied)
    refuse_if_duplicate(MARKER, proc_root=str(proc))  # не падает

    assert not capsys.readouterr().err, "страж шумит в stderr — это увидит модель"
    diag = (tmp_path / ".station-guard.diag").read_text(encoding="utf-8")
    assert "listdir" in diag and "не работает" in diag, f"слепота не записана:\n{diag}"
