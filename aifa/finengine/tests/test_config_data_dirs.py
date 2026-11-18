"""Каталоги данных: отказ — человекочитаемый, падение — с запасным путём в temp.

Регресс на под 16.11.2026: read-only корневая ФС контейнера и неприменённый
FINENGINE_DATA_DIR со стенда роняли импорт `finengine.config` — сервис не
поднимался вовсе.
"""
import os
import tempfile

import pytest

from finengine import config as config_module
from finengine.config import Settings


def test_ensure_dir_creates_and_returns_path(tmp_path):
    target = tmp_path / "data"
    assert Settings._ensure_dir(target) == target
    assert target.is_dir()


def test_ensure_dir_unreadable_parent_raises_oserror(tmp_path):
    # Каталог внутри ФАЙЛА: mkdir гарантированно падает на любой ОС, а тип
    # отказа важен — model_post_init ловит именно OSError.
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("x")
    with pytest.raises(OSError, match="каталог данных"):
        Settings._ensure_dir(not_a_dir / "data")


def test_ensure_dir_existing_unwritable_raises_oserror(tmp_path, monkeypatch):
    target = tmp_path / "data"
    target.mkdir()
    monkeypatch.setattr(config_module.os, "access", lambda *args, **kwargs: False)
    with pytest.raises(OSError, match="недоступен на запись"):
        Settings._ensure_dir(target)


def test_model_post_init_falls_back_to_temp(monkeypatch, tmp_path):
    def raiser(path):
        raise OSError(f"read-only file system: {path}")

    monkeypatch.setattr(Settings, "_ensure_dir", staticmethod(raiser))
    monkeypatch.setattr(
        Settings, "_temp_dir", staticmethod(lambda name, причина: tmp_path / name)
    )

    settings = Settings()
    assert settings.data_dir == tmp_path / "data"
    assert settings.docs_dir == tmp_path / "documents"


def test_temp_dir_returns_writable_path_under_temp():
    fallback = Settings._temp_dir("unit-test-fallback", OSError("причина"))
    try:
        assert fallback.is_dir()
        assert tempfile.gettempdir() in str(fallback)
        probe = fallback / "probe.txt"
        probe.write_text("ok")
        assert probe.read_text() == "ok"
    finally:
        os.remove(fallback / "probe.txt") if (fallback / "probe.txt").exists() else None
