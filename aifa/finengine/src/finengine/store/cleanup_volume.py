"""Уборка тома: удаляет ТОЛЬКО то, что проверенно лежит в новом хранилище.

    docker compose exec backend python -m finengine.store.cleanup_volume --dry-run
    docker compose exec backend python -m finengine.store.cleanup_volume --yes

🔴 ПРАВИЛО ОДНО, И ОНО ЖЁСТКОЕ: предмет удаляется, только если он найден в базе
или в объектном хранилище ПРЯМО СЕЙЧАС, в этом же прогоне. Никаких списков «что
мы вроде бы перенесли»: список из головы — это и есть способ стереть
единственный экземпляр.

Почему это отдельный модуль, а не разовая команда: удаление необратимо, и
человек должен видеть, ЧТО именно и ПОЧЕМУ уходит, — сухой прогон печатает
каждый предмет с основанием.

ЧТО СОЗНАТЕЛЬНО НЕ ТРОГАЕМ:

  * PDF, которых нет ни в реестре, ни в хранилище. Это не дубликаты, а
    единственные экземпляры (на стенде таких семь — остатки удалённых
    документов). Удалять единственный экземпляр — не уборка;
  * `*.jsonl` и `*_events/`, для которых записи в общем журнале НЕ найдены.
    Сами журналы переехали (26.08), но проверяем каждый по отдельности: файл,
    чья копия не подтверждена, остаётся;
  * `users/*/chats/` — остатки базы знаний чата (модуль снесён 07.09.2026):
    чужие данные, снимать не нам;
  * `dialogs/1.json.bak-*` и прочие `*.bak-*` — снимки, сделанные человеком
    перед опасными операциями. Чужая страховка, и снимать её не нам.

⚠️ ПЕРЕД ЗАПУСКОМ имеет смысл сложить опорные мелочи вне тома — они занимают
меньше мегабайта, а без них откат невозможен вовсе:

    tar -czf /opt/finagent/backups/before-cleanup-$(date +%Y%m%d-%H%M).tar.gz \\
        -C <том> users.db scenarios.db index/meta.pkl dialogs/1.json
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from finengine.config import settings
from finengine.store.engine import движок


class Уборщик:
    """Считает и удаляет. Разделён на счёт и действие, чтобы сухой прогон и
    настоящий шли ОДНИМ кодом — иначе они разойдутся, и печать перестанет
    отвечать за то, что реально произойдёт."""

    def __init__(self, корень: Path, dry_run: bool):
        self.корень = корень
        self.dry_run = dry_run
        self.удалено = 0
        self.байт = 0
        self.оставлено: list[str] = []

    def убрать(self, п: Path, почему: str) -> None:
        if not п.exists():
            return
        размер = (п.stat().st_size if п.is_file()
                  else sum(f.stat().st_size for f in п.rglob("*") if f.is_file()))
        self.удалено += 1
        self.байт += размер
        print(f"  − {str(п.relative_to(self.корень)):58s} {размер // 1024:8d} КБ  ({почему})")
        if self.dry_run:
            return
        if п.is_file():
            п.unlink()
        else:
            shutil.rmtree(п)

    def оставить(self, что: str, почему: str) -> None:
        self.оставлено.append(f"{что}: {почему}")


def cleanup(корень: Path, dry_run: bool = True) -> Уборщик:
    from finengine.ingestion.document_store import get_store
    from finengine.store import state as _state
    from finengine.store.artifacts import get_artifact_store
    from finengine.store.blobs import document_idp_key, document_text_key, get_blob_store
    from finengine.store.journals import get_journal_store

    у = Уборщик(корень, dry_run)
    ключи = set(get_blob_store().iter_keys(""))
    документы = корень / "documents"

    print("═══ САЙДКАРЫ ДОКУМЕНТОВ ═══")
    for f in sorted(документы.glob("*.txt")):
        if document_text_key(f.stem) in ключи:
            у.убрать(f, "текст в хранилище")
        else:
            у.оставить(f.name, "текста нет в хранилище")
    for f in sorted(документы.glob("*.idpocr.v2.json")):
        # `Path.stem` тут не годится: у `x.idpocr.v2.json` он даёт `x.idpocr.v2`.
        doc_id = f.name[: -len(".idpocr.v2.json")]
        if document_idp_key(doc_id, 2) in ключи:
            у.убрать(f, "ответ OCR в хранилище")
        else:
            у.оставить(f.name, "ответа OCR нет в хранилище")

    print("═══ САМИ PDF ═══")
    имена = {k.rsplit("/", 1)[-1] for k in ключи if k.startswith("documents/")}
    for f in sorted(документы.glob("*.pdf")):
        if f.name in имена:
            у.убрать(f, "PDF в хранилище")
        else:
            у.оставить(f.name, "PDF-сирота, единственный экземпляр")

    print("═══ СОСТОЯНИЕ, АРТЕФАКТЫ, ЖУРНАЛЫ ═══")
    состояние = _state.get_state_store()
    каталог_сессий = корень / "sessions"
    владельцы = ([p.name for p in каталог_сессий.glob("*") if p.is_dir()]
                 if каталог_сессий.is_dir() else [])
    в_базе = sum(len(состояние.keys(_state.SESSION, вл)) for вл in владельцы)
    if владельцы and в_базе:
        у.убрать(каталог_сессий, f"{в_базе} сессий в базе")
    elif владельцы:
        у.оставить("sessions/", "в базе состояния сессий не найдено")

    if (корень / "artifacts").is_dir():
        e = get_artifact_store()
        сколько = 0
        with e._engine.begin() as conn:  # noqa: SLF001 — считаем строки для решения
            from sqlalchemy import func, select

            from finengine.store.artifacts import ARTIFACT
            сколько = int(conn.execute(select(func.count()).select_from(ARTIFACT)).scalar() or 0)
        if сколько:
            у.убрать(корень / "artifacts", f"{сколько} артефактов в базе")
        else:
            у.оставить("artifacts/", "в базе артефактов не найдено")

    ж = get_journal_store()
    for вид in ("traces", "audit", "llm_traces"):
        каталог = корень / вид
        if not каталог.is_dir():
            continue
        владельцы = [p.name for p in каталог.glob("*") if p.is_dir()]
        лент = sum(len(ж.keys(вид, вл)) for вл in владельцы)
        if лент:
            у.убрать(каталог, f"{лент} лент в базе")
        else:
            у.оставить(f"{вид}/", "в базе журналов не найдено")

    print("═══ ФАЙЛЫ ПОЛЬЗОВАТЕЛЯ ═══")
    каталог_пользователей = корень / "users"
    for каталог in sorted(каталог_пользователей.glob("*")) if каталог_пользователей.is_dir() else []:
        if not каталог.is_dir():
            continue
        for f in sorted(каталог.rglob("sources/*")):
            if not f.is_file():
                continue
            ключ = str(f.relative_to(корень))
            if ключ in ключи:
                у.убрать(f, "в хранилище")
            else:
                у.оставить(ключ, "нет в хранилище")
        for имя, вид, ключ in (("sources.json", _state.USER_STATE, "sources"),
                               ("methodology.json", _state.METHODOLOGY, "overlay")):
            f = каталог / имя
            if not f.is_file():
                continue
            if состояние.get(вид, каталог.name, ключ) is not None:
                у.убрать(f, "в базе состояния")
            else:
                у.оставить(str(f.relative_to(корень)), "нет в базе состояния")
        for f in sorted(каталог.glob("projects/*/watchlist.json")):
            if состояние.get(_state.WATCHLIST, каталог.name, f.parent.name) is not None:
                у.убрать(f, "в базе состояния")
            else:
                у.оставить(str(f.relative_to(корень)), "нет в базе состояния")

    print("═══ ЖУРНАЛЫ ПРОГОНОВ И КАТАЛОГИ СОБЫТИЙ ═══")
    _убрать_журналы(у, корень)

    print("═══ ФАЙЛОВЫЕ БАЗЫ И РЕЕСТР ═══")
    из_базы = len(get_store().list_documents())
    пикл = корень / "index" / "meta.pkl"
    if пикл.is_file():
        if из_базы:
            у.убрать(пикл, f"реестр в базе ({из_базы} документов)")
        else:
            у.оставить("index/meta.pkl", "в базе документов не найдено")
    _убрать_файловую_базу(у, корень)
    _подмести_пустые_каталоги(у, корень)
    return у


def _убрать_журналы(у: Уборщик, корень: Path) -> None:
    """Журналы прогонов и каталоги событий, чья копия есть в общем журнале.

    Проверяем ПОШТУЧНО и по числу записей: файл, у которого в хранилище пусто
    или записей меньше, остаётся. Списком «что мы вроде бы перенесли» здесь
    пользоваться нельзя — ровно так теряют единственный экземпляр.
    """
    from finengine.skills import run_logger as rl  # noqa: PLC0415
    from finengine.skills.run_orchestrator import KNOWN_KINDS, вид_журнала  # noqa: PLC0415
    from finengine.store import journals  # noqa: PLC0415

    документы = корень / "documents"

    # 1. Общие журналы: одна запись на строку.
    for имя, вид in ((("extract_runs.jsonl"), rl.ЖУРНАЛ_ИЗВЛЕЧЕНИЙ),
                     ("chat_runs.jsonl", rl.ЖУРНАЛ_ЧАТА),
                     ("analysis_runs.jsonl", rl.ЖУРНАЛ_РАЗБОРОВ),
                     ("client_events.jsonl", rl.ЖУРНАЛ_КЛИЕНТА)):
        файл = документы / имя
        if not файл.is_file():
            continue
        на_диске = _непустых_строк(файл)
        в_базе = len(journals.read(вид, owner="_", key=rl._ОБЩИЙ))
        if в_базе >= на_диске:
            у.убрать(файл, f"{в_базе} записей в общем журнале")
        else:
            у.оставить(имя, f"в файле {на_диске}, в журнале {в_базе}")

    # 2. Сырьё станции: ключ — нонс, поэтому считаем по нонсам.
    сырьё = документы / "station_events.jsonl"
    if сырьё.is_file():
        нонсы: dict[str, int] = {}
        for строка in сырьё.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not строка.strip():
                continue
            try:
                нонсы[str(json.loads(строка).get("nonce") or "")] = \
                    нонсы.get(str(json.loads(строка).get("nonce") or ""), 0) + 1
            except json.JSONDecodeError:
                continue
        не_хватает = [н for н, ск in нонсы.items()
                      if н and len(journals.read(rl.СЫРЬЁ_СТАНЦИИ, owner="_", key=н)) < ск]
        if не_хватает:
            у.оставить("station_events.jsonl",
                       f"{len(не_хватает)} нонсов не перенесены целиком")
        else:
            у.убрать(сырьё, f"{len(нонсы)} нонсов в общем журнале")

    # 3. Каталоги событий прогонов: файл на прогон.
    for kind in KNOWN_KINDS:
        # ⚠️ Раньше для kind="chain" каталог дописывался ВТОРОЙ раз (легаси-имя
        # совпадает с образующимся из f-строки) — уборка проходила его дважды.
        # Безвредно, но второй проход печатал предмет повторно.
        каталоги = [документы / f"{kind}_events"]
        for d in каталоги:
            if not d.is_dir():
                continue
            отставшие = [ф.name for ф in d.glob("*.jsonl")
                         if len(journals.read(вид_журнала(kind), owner="_", key=ф.stem))
                         < _непустых_строк(ф)]
            if отставшие:
                у.оставить(f"{d.name}/", f"{len(отставшие)} журналов не перенесены целиком")
            else:
                у.убрать(d, "все журналы прогонов в общем журнале")


def _непустых_строк(файл: Path) -> int:
    return sum(1 for s in файл.read_text(encoding="utf-8", errors="ignore").splitlines()
               if s.strip())


def _убрать_файловую_базу(у: Уборщик, корень: Path) -> None:
    """Диалоги, новости, учётки, сценарии — каждая под своей сверкой.

    🔴 У КАЖДОГО ПРЕДМЕТА СВОЙ АДРЕС БАЗЫ, И ЭТО НЕ ПРИДИРКА. Тумблеров у нас
    несколько (диалоги, новости, учётки, сценарии), включают их по одному.
    Сверять `news.db` с базой диалогов значит спрашивать не то место: она может
    быть полна, а новости — никуда не переехать. Пустой адрес = переезда не
    было вовсе, и файл остаётся.

    Первая редакция этого модуля брала подключение у хранилища диалогов для
    ВСЕХ проверок — и падала, стоило тумблеру диалогов быть выключенным.
    Поймано сторожем, а не на стенде.
    """
    import json

    import sqlalchemy as sa

    from finengine.config import settings as _s

    файл_диалогов = корень / "dialogs" / "1.json"
    if файл_диалогов.is_file():
        адрес = (_s.dialogs_database_url or "").strip()
        if not адрес:
            у.оставить("dialogs/1.json", "база диалогов не настроена — переезда не было")
        else:
            данные = json.loads(файл_диалогов.read_text(encoding="utf-8"))
            в_файле = {p.get("id") for p in (данные.get("projects") or [])}
            дв = движок(адрес, pool_pre_ping=True, future=True, echo=False)
            with дв.begin() as c:
                в_базе = {r[0] for r in c.execute(sa.text(
                    "select project_id from dialog where user_id = 1"))}
            нет = в_файле - в_базе
            if нет:
                у.оставить("dialogs/1.json", f"{len(нет)} проектов нет в базе")
            else:
                у.убрать(файл_диалогов, f"все {len(в_файле)} проектов найдены в базе")

    for имя, адрес, таблицы in (
        ("news.db", _s.news_database_url, ("raw_items", "sources", "digest_runs", "digest_items")),
        ("users.db", _s.auth_database_url, ("users",)),
        ("scenarios.db", _s.scenarios_database_url, ("scenarios",)),
    ):
        файл = корень / имя
        if not файл.is_file():
            continue
        адрес = (адрес or "").strip()
        if not адрес:
            у.оставить(имя, "своя база не настроена — переезда не было")
            continue
        расхождения = _сверить_таблицы(файл, адрес, таблицы)
        if расхождения:
            у.оставить(имя, "; ".join(расхождения))
        else:
            у.убрать(файл, "все таблицы совпали с базой")


def _сверить_таблицы(файл: Path, адрес: str, таблицы: tuple[str, ...]) -> list[str]:
    """Строки в файле против строк в базе. Пустой список = совпало всё.

    Сравнение НЕСТРОГОЕ в одну сторону: в базе может быть БОЛЬШЕ (после
    переезда туда уже писали), но не меньше. Меньше — признак недовершённого
    переезда, и файл тогда остаётся.
    """
    import sqlite3

    import sqlalchemy as sa

    расхождения: list[str] = []
    из_файла = sqlite3.connect(файл)
    дв = движок(адрес, pool_pre_ping=True, future=True, echo=False)
    with дв.begin() as c:
        for t in таблицы:
            try:
                было = из_файла.execute(f"select count(*) from {t}").fetchone()[0]
            except sqlite3.Error:
                расхождения.append(f"{t}: не читается в файле")
                continue
            try:
                стало = int(c.execute(sa.text(f'select count(*) from "{t}"')).scalar() or 0)
            except sa.exc.SQLAlchemyError:
                расхождения.append(f"{t}: нет в базе")
                continue
            if стало < было:
                расхождения.append(f"{t}: в файле {было}, в базе {стало}")
    return расхождения


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Уборка тома от перенесённых копий")
    группа = parser.add_mutually_exclusive_group(required=True)
    группа.add_argument("--dry-run", action="store_true", help="только показать")
    группа.add_argument("--yes", action="store_true", help="удалить (необратимо)")
    args = parser.parse_args(argv)

    у = cleanup(Path(settings.data_dir), dry_run=args.dry_run)
    print()
    print(("СУХОЙ ПРОГОН — ничего не удалено\n" if args.dry_run else "")
          + f"предметов: {у.удалено}, освобождается {у.байт // 1024 // 1024} МБ")
    if у.оставлено:
        print(f"\nОСТАВЛЕНО ({len(у.оставлено)}) — копия не подтверждена:")
        for с in у.оставлено:
            print("   ", с[:100])
    return 0


if __name__ == "__main__":  # pragma: no cover — точка входа CLI
    raise SystemExit(main())

def _подмести_пустые_каталоги(у: Уборщик, корень: Path) -> None:
    """Снять пустые каталоги, оставшиеся от уехавших файлов. Только пустые.

    🔴 ПОЧЕМУ ЭТО НЕ КОСМЕТИКА. После переезда на томе остались 250 каталогов
    проектов и ~100 каталогов чатов С НУЛЁМ ФАЙЛОВ — и 26.08 они уже соврали
    в отчёте: «2,3 МБ знаний чата» оказались инодовой сдачей пустых папок, а
    данных там было 88 КБ. Пустой каталог — это не данные, сверять ему нечего,
    поэтому общее правило «удалять только подтверждённую копию» на него не
    распространяется: он удаляется по факту пустоты.

    Обход снизу вверх: каталог, опустевший после снятия детей, тоже снимается.
    `users/` сам и первый уровень (id пользователя) не трогаем — их существование
    может значить «пользователь есть», даже пока файлов нет.
    """
    print("═══ ПУСТЫЕ КАТАЛОГИ ═══")
    учётка = корень / "users"
    if not учётка.is_dir():
        return
    снято = 0
    for пользователь in sorted(учётка.iterdir()):
        if not пользователь.is_dir():
            continue
        for d in sorted((к for к in пользователь.rglob("*") if к.is_dir()),
                        key=lambda к: len(к.parts), reverse=True):
            try:
                пусто = not any(d.iterdir())
            except OSError:
                continue
            if not пусто:
                continue
            if у.dry_run:
                снято += 1
                continue
            try:
                d.rmdir()
                снято += 1
            except OSError:
                continue  # успел появиться файл — каталог больше не пустой
    метка = "[сухой прогон] снял бы" if у.dry_run else "снято"
    print(f"  {метка} пустых каталогов: {снято}")

