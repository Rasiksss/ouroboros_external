"""Подметание пустых каталогов — часть уборки, которой не нужна база.

Живёт отдельным файлом НАМЕРЕННО: на test_cleanup_deletes_only_verified_copies
стоит файловый pytestmark «нужна настоящая база», и тест, который базы не
трогает, пропускался бы вместе со всеми — локальный прогон терял бы проверку.
"""
from __future__ import annotations


def test_empty_directories_are_swept_bottom_up(tmp_path):
    """Пустые каталоги пользователей снимаются, непустые и верхний уровень — нет.

    Пустой каталог — не данные: сверять ему нечего, правило «только
    подтверждённая копия» на него не распространяется. 26.08 пустые папки уже
    соврали в отчёте: «2,3 МБ знаний чата» оказались инодовой сдачей.
    """
    from finengine.store.cleanup_volume import Уборщик, _подмести_пустые_каталоги

    к = tmp_path
    (к / "users/1/projects/п1").mkdir(parents=True)          # пустой — снять
    (к / "users/1/projects/п2/sources").mkdir(parents=True)  # пустая цепочка — снять обе
    (к / "users/1/chats/c1/knowledge").mkdir(parents=True)
    (к / "users/1/chats/c1/knowledge/chunks.jsonl").write_text("х", encoding="utf-8")

    _подмести_пустые_каталоги(Уборщик(к, dry_run=False), к)

    assert not (к / "users/1/projects/п1").exists(), "пустой каталог остался"
    assert not (к / "users/1/projects/п2").exists(), "цепочка пустых не снята снизу вверх"
    assert (к / "users/1/chats/c1/knowledge/chunks.jsonl").is_file(), "живой файл задет"
    assert (к / "users/1").is_dir(), "уровень пользователя снят — а он значит «пользователь есть»"

    # Сухой прогон ничего не трогает.
    (к / "users/1/projects/п3").mkdir(parents=True)
    _подмести_пустые_каталоги(Уборщик(к, dry_run=True), к)
    assert (к / "users/1/projects/п3").is_dir(), "сухой прогон удалил каталог"
