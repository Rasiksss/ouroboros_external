"""Перенос диалогов из файлов в базу. Идемпотентен, исходные файлы не трогает.

Запускать там, где лежат и код, и том с данными, — в контейнере бэкенда:
    docker compose exec backend python -m finengine.store.migrate_dialogs --dry-run
    docker compose exec backend python -m finengine.store.migrate_dialogs

ПОЧЕМУ «дополняем, а не перезаписываем». Перенос почти наверняка запустят не
один раз (сначала сухой прогон, потом настоящий, потом ещё раз после того, как
кто-то поработал на базе). Если бы скрипт лил файл поверх базы, второй запуск
откатывал бы пользователя во вчерашний день — ровно та потеря, ради ухода от
которой всё и затевалось. Поэтому: диалог, который в базе уже есть, не
трогаем; переносим только отсутствующие. Файлы остаются на диске — они и есть
страховка на время переезда.

ДОПОЛНЯЕМ ВСЁ, А НЕ ТОЛЬКО ДИАЛОГИ. Это же правило действует на шапку — папки,
последний чат, надгробия: переносится то, чего в базе нет, независимо от того,
успел ли клиент запушить снимок. Почему это важно, написано у `migrate()`.
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from finengine.config import settings
from finengine.store.dialogs import DialogStore, SqlDialogStore

log = logging.getLogger(__name__)


def migrate(source_dir: Path, store: DialogStore, dry_run: bool = False) -> dict:
    """Перенести все `<user_id>.json` из папки в хранилище. Возвращает счётчики.

    Рядом с диалогами лежат `.bak-docfix`, `.bak-recover`, `.bak-sovcom` —
    ручные копии, снятые после прошлых потерь. Их суффикс не `.json`, поэтому
    в перенос они не попадают: восстановление из копии — отдельное осознанное
    действие человека, а не побочный эффект миграции.

    ДЕФЕКТ, ЗАКРЫТЫЙ ЗДЕСЬ (папки терялись на самой операции, которая их
    спасает). Раньше шапка — папки, последний чат, надгробия — переносилась
    только «в первый раз»: если в базе уже пусто И нет saved_at. Но порядок
    переезда, записанный в deploy/docker-compose.dev.yml, ровно обратный:
    сначала поднимают бэкенд с адресом базы, и только потом запускают перенос.
    В зазоре между этими шагами открытая вкладка пушит свой снимок по дебаунсу,
    в базе появляется строка с saved_at — и «первый раз» уже не наступает
    никогда. Итог был такой: чаты доехали, а папка «Мои банки» исчезла
    навсегда, причём отчёт про папки не говорил ни слова. Поэтому переносится
    ВСЁ, чего в базе нет, поштучно (папка — по id, надгробие — по ключу), а не
    «вся шапка или ничего». Скаляры (последний чат, saved_at) берём из файла
    только когда в базе пусто: там значение свежее файла.
    """
    report = {
        "users": 0,
        "dialogs_in_files": 0,
        "migrated": 0,
        "already_in_db": 0,
        # Диалог из файла, на который в базе стоит надгробие: пользователь
        # удалил его уже после первого переноса. Перенос его не воскрешает —
        # и не имеет права записать его себе в заслугу (см. ниже про счёт).
        "deleted_by_user": 0,
        "folders": 0,
        "skipped_broken": 0,
    }
    for path in sorted(source_dir.glob("*.json")):
        try:
            user_id = int(path.stem)
        except ValueError:
            continue  # файл не про пользователя — не наш
        try:
            with open(path, encoding="utf-8") as f:
                blob = json.load(f)
            if not isinstance(blob, dict):
                raise ValueError("blob is not a dict")
        except (OSError, json.JSONDecodeError, ValueError):
            report["skipped_broken"] += 1
            log.warning("перенос: файл %s не читается — пропущен", path.name)
            continue

        report["users"] += 1
        from_file = [
            p for p in blob.get("projects") or []
            if isinstance(p, dict) and p.get("id")
        ]
        report["dialogs_in_files"] += len(from_file)

        stored = store.load(user_id)
        have = {
            str(p["id"]) for p in stored["projects"]
            if isinstance(p, dict) and p.get("id")
        }
        fresh = [p for p in from_file if str(p["id"]) not in have]
        report["already_in_db"] += len(from_file) - len(fresh)

        # Шапка — то же правило слияния, что и у диалогов: берём поштучно то,
        # чего в базе нет. Своё база не отдаёт: её версия свежее файла.
        stored_folders = stored.get("folders") or []
        have_folders = {
            str(f["id"]) for f in stored_folders if isinstance(f, dict) and f.get("id")
        }
        new_folders = [
            f for f in blob.get("folders") or []
            if isinstance(f, dict) and f.get("id") and str(f["id"]) not in have_folders
        ]
        # ⚠️ НАДГРОБИЯ ИЗ ФАЙЛА В БАЗУ НЕ ЕДУТ, И ЭТО ГЛАВНОЕ ПРАВИЛО ПЕРЕНОСА.
        #
        # Надгробие — это НЕ данные, которые надо сберечь, это КОМАНДА НА
        # УДАЛЕНИЕ: `SqlDialogStore.save` сносит по нему строку. Правило переноса
        # («берём поштучно то, чего в базе нет, своё база не отдаёт — её версия
        # свежее файла») к данным применимо, а к командам НЕТ: старая команда
        # «удалить X», приехав из файла в более свежую базу, убивает чат X,
        # которого в файле нет вовсе.
        #
        # Так и вышло на прошлом шаге: убрав гейт «переносим шапку только в
        # пустую базу», перенос заодно начал сливать надгробия — и из чисто
        # дополняющего стал разрушающим. Три отягчающих: живой путь гасит
        # надгробия старше 30 суток, а перенос никакого срока не знал (в файле
        # 2,27 МБ они копились всю файловую эпоху); ломалась заявленная
        # идемпотентность — вычищенное по сроку надгробие заводилось из файла
        # заново на каждом прогоне; и отчёт об этом молчал.
        #
        # Правильный ответ — не «применять осторожнее», а НЕ ПРИМЕНЯТЬ: удаления,
        # сделанные до переезда, уже отражены в самом файле (удалённого диалога
        # там нет), поэтому переносить нечего. Надгробия нужны только живому
        # обмену с клиентом, чтобы вернувшаяся вкладка не воскресила удалённое.
        take_last = not stored.get("last_project_id") and bool(blob.get("last_project_id"))

        if dry_run:
            # Сухой прогон ничего не пишет, поэтому здесь считается намерение —
            # оценка сверху. Настоящий прогон считает по факту (ниже).
            report["migrated"] += len(fresh)
            report["folders"] += len(new_folders)
            continue
        if not (fresh or new_folders or take_last):
            continue

        state = dict(stored)
        state["projects"] = stored["projects"] + fresh
        state["folders"] = list(stored_folders) + new_folders
        # `deleted` берём из базы КАК ЕСТЬ: перенос надгробий не заводит.
        state["deleted"] = stored.get("deleted") or {}
        if take_last:
            state["last_project_id"] = blob.get("last_project_id")
        if not stored.get("saved_at"):
            state["saved_at"] = blob.get("saved_at")
        store.save(user_id, state, changed_ids={str(p["id"]) for p in fresh})
        report["folders"] += len(new_folders)

        # СЧЁТ ДИАЛОГОВ — ПО ФАКТУ ЗАПИСАННОГО, а не по намерению. Дефект:
        # счётчик увеличивали до save, а тот же save тут же удаляет строку
        # диалога, на который в базе стоит надгробие. Сценарий обычный: два
        # диалога перенесли, один пользователь удалил, перенос запустили ещё
        # раз — печаталось «перенесено: 1» при одном диалоге в таблице.
        # Поэтому перечитываем состояние и считаем то, что там осталось.
        landed = {
            str(p["id"]) for p in store.load(user_id)["projects"]
            if isinstance(p, dict) and p.get("id")
        }
        moved = sum(1 for p in fresh if str(p["id"]) in landed)
        report["migrated"] += moved
        report["deleted_by_user"] += len(fresh) - moved
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Перенос диалогов из файлов в базу")
    parser.add_argument(
        "--source", type=Path, default=settings.data_dir / "dialogs",
        help="папка с файлами <user_id>.json (по умолчанию data/dialogs)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="только посчитать, ничего не писать",
    )
    args = parser.parse_args(argv)

    url = (settings.dialogs_database_url or "").strip()
    if not url:
        # Адрес не печатаем ни в каком виде — в нём пароль.
        print("База не настроена: пуст FINENGINE_DIALOGS_DATABASE_URL. Переносить некуда.")
        return 2
    if not args.source.is_dir():
        print(f"Папка с диалогами не найдена: {args.source}")
        return 2

    report = migrate(args.source, SqlDialogStore(url), dry_run=args.dry_run)
    # Отчёт перечисляет ВСЕ виды перенесённого. Раньше он печатал одно
    # «перенесено: N» про диалоги, и это читалось как «переезд полный» — а
    # папки в тот момент могли не доехать вовсе, и заметить это было нечем.
    print(
        ("СУХОЙ ПРОГОН — ничего не записано\n" if args.dry_run else "")
        + f"пользователей в файлах: {report['users']}\n"
        + f"диалогов в файлах:      {report['dialogs_in_files']}\n"
        + f"перенесено диалогов:    {report['migrated']}\n"
        + f"уже было в базе:        {report['already_in_db']}\n"
        + f"удалены пользователем:  {report['deleted_by_user']}\n"
        + f"перенесено папок:       {report['folders']}\n"
        + f"нечитаемых файлов:      {report['skipped_broken']}\n"
        + "исходные файлы оставлены на месте"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover — точка входа CLI
    raise SystemExit(main())
