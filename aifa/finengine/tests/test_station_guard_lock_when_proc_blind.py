"""Страж дубля работает и там, где /proc отказан (#185).

ЧТО СЛУЧИЛОСЬ 18.08. В комнате шли ТРИ извлечения по одному документу
(Банк ДОМ.РФ, нонс 060f41c1…, pid 847/868/879), и страж не сказал ни слова:
`.station-guard.diag` содержал шесть строк «listdir(/proc) отказал: Permission
denied». Песочница не отдаёт процессу /proc — а вся проверка стояла только на
нём. Три копии делили один канал модели, и за 24 минуты не появилось ни одного
числа.

Здесь проверяется ВТОРОЙ признак — замок с отметкой времени: он виден изнутри
песочницы, а протухшая отметка сама освобождает комнату, поэтому мёртвый
хозяин её не запирает.

⚠️ /proc НЕ ЗАМЕНЁН, А ДОПОЛНЕН: он неподделываем, замок — нет. Слепота одного
больше не означает «пропускай».
"""
from __future__ import annotations

import os
import time

import pytest

from finengine.skills import station_guard as g

MARKER = "aifa-extract/scripts/extract.py"


@pytest.fixture()
def blind_room(tmp_path, monkeypatch):
    """Комната как в песочнице: /proc есть, но себя в нём не видно."""
    fake_proc = tmp_path / "proc"
    fake_proc.mkdir()
    monkeypatch.chdir(tmp_path)
    return str(fake_proc)


def test_second_copy_is_refused_when_proc_is_blind(blind_room):
    g.refuse_if_duplicate(MARKER, proc_root=blind_room)  # первый занял замок

    with pytest.raises(SystemExit) as exc:
        g.refuse_if_duplicate(MARKER, proc_root=blind_room)

    assert exc.value.code == 5


def test_refusal_names_the_owner_pid(blind_room, capsys):
    """Отказ обязан назвать, КОГО завершить: без pid совет «kill» бесполезен."""
    g.refuse_if_duplicate(MARKER, proc_root=blind_room)
    with pytest.raises(SystemExit):
        g.refuse_if_duplicate(MARKER, proc_root=blind_room)

    err = capsys.readouterr().err
    assert f"pid {os.getpid()}" in err
    assert "ОТКАЗ" in err


def test_stale_lock_does_not_lock_the_room_forever(blind_room):
    """Хозяин умер — отметка перестала обновляться, замок стал ничей.

    Это ровно то возражение, из-за которого файл-замок когда-то сняли:
    протухший замок запирал комнату навсегда. Отметка его снимает.
    """
    g.refuse_if_duplicate(MARKER, proc_root=blind_room)
    path = g._lock_path(MARKER)
    old = time.time() - g._STALE_SEC - 5
    os.utime(path, (old, old))

    g.refuse_if_duplicate(MARKER, proc_root=blind_room)  # не должно бросить


def test_other_script_has_its_own_lock(blind_room):
    """Извлечение и анализ идут рядом ЗАКОННО. С общим файлом они отнимали бы
    замок друг у друга и обнуляли защиту обоим."""
    g.refuse_if_duplicate(MARKER, proc_root=blind_room)

    g.refuse_if_duplicate("aifa-analyze/scripts/analyze.py", proc_root=blind_room)


def test_no_room_outside_linux(tmp_path, monkeypatch):
    """Отрицательный контроль: нет /proc вовсе — запрещать нечего, и замок не
    заводим, иначе прогоны бенчмарка в одном cwd глушили бы друг друга."""
    monkeypatch.chdir(tmp_path)
    g.refuse_if_duplicate(MARKER, proc_root=str(tmp_path / "нет-такого"))
    assert not os.path.exists(g._lock_path(MARKER))
