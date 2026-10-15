"""Распознанный текст: одно место, где решают, ГДЕ он лежит. И откат на файл.

🔴 ЗАЧЕМ. `docs/rules/architecture.md` 3.1 (MUST): «Нет состояния на файловой
системе контейнера… всё durable — во внешние хранилища». Текст документа жил
файлом `docs_dir/<doc_id>.txt`, и путь к нему СЕМЬ модулей строили сами:
`api/main.py`, `api/internal_docs.py`, `llm/tools.py`, `orchestrator/agent.py`,
`skills/two_stage_extractor.py` и две ветки `bulk_extractor`. Это тот самый
класс «одно решение в семи местах», которым мы платим весь месяц.

⚠️ ЧЕМ ЭТО КОНЧИЛОСЬ 25.08 — ЦЕНА ЗДЕСЬ ИЗМЕРЕНА, А НЕ ПРЕДПОЛОЖЕНА. Первый
заход перевёл ПИСАТЕЛЯ (текст стал публиковаться в хранилище), а читатель в
`two_stage_extractor` остался смотреть на файл. Файл больше не появлялся —
и движок честно добирал ожидание `_wait_for_pdf_text` пять раз по шестьдесят
секунд. Ход вис на ПЯТЬ МИНУТ, набор тестов вырос с 0,5 с до 300,8 с. Отказа
не было ни одного: всё «работало», просто молча ждало.

Отсюда три свойства, которые здесь охраняются. Они про КОНТРАКТ и про
ОТСУТСТВИЕ вредного, а не про формулировки — промпты меняются, правило нет:

  1. путь сайдкара НЕ строится нигде, кроме `store/blobs.py`;
  2. текст, лежащий ФАЙЛОМ, всё равно находится (откат — то, чего не хватило);
  3. без явной просьбы текст отдаётся ЦЕЛИКОМ.

Третье — тоже пойманная регрессия, а не гипотеза: умолчание в 200 000 символов
резало бы отчёт МСФО (~280 000) на четверти. Роут `/internal/doc-text` кормит
станцию полным документом, и увидели бы это не отказом, а недостающими
показателями в конце таблицы.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from finengine.store import blobs

ИСХОДНИКИ = Path(__file__).resolve().parents[1] / "src" / "finengine"


@pytest.fixture
def на_томе(tmp_path, monkeypatch):
    """Хранилище и прежнее место сайдкара — оба во временном каталоге."""
    from finengine.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "docs_dir", tmp_path / "documents")
    monkeypatch.setattr(settings, "s3_endpoint", "")
    (tmp_path / "documents").mkdir(parents=True, exist_ok=True)
    blobs._cached = None
    yield tmp_path
    blobs._cached = None


# ─────────────────────── 1. ОДНО МЕСТО, А НЕ СЕМЬ ───────────────────────


def _сам_строит_путь_сайдкара(дерево: ast.Module) -> bool:
    """Есть ли в дереве выражение вида `<...docs_dir...> / f"{x}.txt"`.

    Смотрим деревом, а не глазами по тексту: строка `.txt` попадается в
    комментариях и сообщениях, и поиск подстрокой краснел бы на них.
    """
    for узел in ast.walk(дерево):
        if not (isinstance(узел, ast.BinOp) and isinstance(узел.op, ast.Div)):
            continue
        слева = ast.unparse(узел.left)
        справа = ast.unparse(узел.right)
        if "docs_dir" in слева and справа.rstrip("'\"").endswith(".txt"):
            return True
    return False


def test_the_sidecar_path_is_built_in_exactly_one_module(движок_ast):
    """Путь сайдкара знает `store/blobs.py` — и больше никто.

    Мутация, которая это краснит: вернуть в любой из семи модулей строку
    `Path(settings.docs_dir) / f"{doc_id}.txt"`. Проверено откатом
    `two_stage_extractor` — тест падает с именем этого файла.

    `движок_ast` (conftest.py) несёт и station-skills/ — фильтруем на
    ИСХОДНИКИ (src/finengine/), как раньше делал собственный rglob.
    """
    виновные = [
        str(путь.relative_to(ИСХОДНИКИ))
        for путь, дерево in sorted(движок_ast.items())
        if ИСХОДНИКИ in путь.parents
        and путь.name != "blobs.py"
        and _сам_строит_путь_сайдкара(дерево)
    ]
    assert not виновные, (
        "путь сайдкара строится мимо store/blobs.py: " + ", ".join(виновные)
        + ". Так уже было: писателя перевели, а эти читатели остались смотреть "
          "на файл — ход завис на пять минут вместо отказа."
    )


# ──────────────────── 2. ОТКАТ НА ФАЙЛ — ТО, ЧЕГО НЕ ХВАТИЛО ────────────────


def test_text_lying_as_a_file_is_still_found(на_томе):
    """Файл, а не объект, — и текст всё равно найден.

    🔴 ЭТО РОВНО ТОТ ПРОПУСК, КОТОРЫЙ СТОИЛ ПЯТИ МИНУТ НА ХОД. Мутация: убрать
    в `document_text` ветку чтения файла — тест краснеет. Проверено.

    Свойство нужно трём контурам сразу: бэкенду посреди переезда (часть текстов
    уже в хранилище, часть ещё файлами), запечатанной комнате станции, куда
    текст приезжает файлом, и откату, если хранилище придётся выключить.
    """
    (на_томе / "documents" / "док-1.txt").write_text("выручка 100", encoding="utf-8")

    assert blobs.document_text("док-1") == "выручка 100"
    assert blobs.document_text_exists("док-1")


def test_text_published_to_the_store_is_found_without_any_file(на_томе):
    """Объект, а не файл, — и файла на диске не появилось."""
    blobs.save_document_text("док-2", "прибыль 20")

    assert blobs.document_text("док-2") == "прибыль 20"
    assert not (на_томе / "documents" / "док-2.txt").exists()


def test_the_store_wins_over_a_stale_file(на_томе):
    """Обе копии есть — берётся хранилище.

    Порядок не косметика: во время переезда рядом лежит СТАРЫЙ файл, и если
    победит он, правки после переезда будут молча теряться.
    """
    (на_томе / "documents" / "док-3.txt").write_text("старое", encoding="utf-8")
    blobs.save_document_text("док-3", "новое")

    assert blobs.document_text("док-3") == "новое"


def test_nothing_at_all_is_told_apart_from_empty(на_томе):
    """Нет текста — `None`. Пустой текст не публикуется и готовым не считается.

    Иначе статус документа говорит «распознан», а извлечение на пустом тексте —
    «ещё обрабатывается»: два ответа об одном документе, оба уверенные.
    """
    assert blobs.document_text("нет-такого") is None
    assert not blobs.document_text_exists("нет-такого")

    blobs.save_document_text("док-4", "   \n  ")
    assert blobs.document_text("док-4") is None
    assert not blobs.document_text_exists("док-4")


def test_removing_the_text_clears_both_places(на_томе):
    """Уборка временного текста станции чистит и объект, и файл.

    Временный текст живёт секунды, но посреди переезда он мог лечь на диск, а
    убираться из хранилища — и мусор копился бы молча.
    """
    (на_томе / "documents" / "док-5.txt").write_text("файлом", encoding="utf-8")
    blobs.save_document_text("док-5", "объектом")

    blobs.delete_document_text("док-5")

    assert blobs.document_text("док-5") is None
    assert not (на_томе / "documents" / "док-5.txt").exists()


# ─────────────── 3. ЦЕЛИКОМ, ПОКА НЕ ПОПРОСИЛИ ИНАЧЕ ───────────────


def test_the_whole_text_is_returned_unless_a_limit_is_asked_for(на_томе):
    """Без `limit` — весь текст. Мутация «вернуть умолчание 200_000» краснит.

    Полный отчёт МСФО — около 280 000 символов, и станция получает его целиком:
    извлечение у нас полноконтекстное. Умолчание с порогом резало бы отчёт на
    четверти, и обнаружилось бы это не отказом, а пустыми клетками в конце
    таблицы — то есть глазами и нескоро.
    """
    длинный = "х" * 250_000
    blobs.save_document_text("док-6", длинный)

    assert len(blobs.document_text("док-6")) == 250_000
    assert len(blobs.document_text("док-6", limit=1_000)) == 1_000


# ─────────────── СЫРОЙ ОТВЕТ OCR — ВТОРОЙ САЙДКАР ───────────────
# Он ДОРОГОЙ: около четырёх секунд на страницу, на банковском отчёте — десять
# минут работы облака. Путь к нему строили ЧЕТЫРЕ модуля плюс админский роут
# с перебором пары v2/v1 — тот же класс, что у текста.


def test_the_ocr_answer_lying_as_a_file_is_still_found(на_томе):
    """Откат на файл — тот же, что у текста, и по той же причине."""
    (на_томе / "documents" / "док-7.idpocr.v2.json").write_text('{"pages": 3}',
                                                                encoding="utf-8")

    assert blobs.document_idp_raw("док-7") == b'{"pages": 3}'
    assert blobs.document_idp_exists("док-7")


def test_the_newer_ocr_version_wins(на_томе):
    """🔴 ПОРЯДОК ВЕРСИЙ НЕСУЩИЙ. Мутация: спрашивать (1, 2) вместо (2, 1).

    v2 — нынешний постраничный конвейер, v1 — прогоны до смены клиента OCR.
    Отдать v1 там, где есть v2, значит молча вернуть худшее распознавание: не
    отказ, а тихо просевшее качество — его замечают не сразу и не по логам.
    """
    (на_томе / "documents" / "док-8.idpocr.json").write_text('{"v": 1}', encoding="utf-8")
    (на_томе / "documents" / "док-8.idpocr.v2.json").write_text('{"v": 2}', encoding="utf-8")

    assert blobs.document_idp_raw("док-8") == b'{"v": 2}'


def test_the_ocr_sink_publishes_only_what_was_written(на_томе):
    """Отказ распознавания не оставляет в хранилище пустышку.

    Иначе повторный прогон решит, что ответ уже есть, и не станет распознавать
    заново — документ навсегда останется без разбивки по страницам.
    """
    with blobs.document_idp_sink("док-9") as путь:
        pass  # распознавание упало, файл не написан
    assert blobs.document_idp_raw("док-9") is None
    assert not путь.exists(), "временный файл не убран"

    with blobs.document_idp_sink("док-10") as путь:
        путь.write_text('{"pages": 1}', encoding="utf-8")
    assert blobs.document_idp_raw("док-10") == b'{"pages": 1}'
    assert not путь.exists(), "временный файл не убран"


def test_the_sidecar_migration_moves_both_kinds(на_томе):
    """Переезд сайдкаров — условие любой уборки тома.

    Сами PDF уже в хранилище, а текст и ответ OCR — ещё нет: работают откатом
    на файл, и потому всё ВЫГЛЯДИТ переехавшим. Стереть каталог до этого
    переезда значит потерять распознавание всех документов.
    """
    from finengine.store.migrate_document_sidecars import migrate

    д = на_томе / "documents"
    (д / "a.txt").write_text("текст", encoding="utf-8")
    (д / "b.txt").write_text("", encoding="utf-8")          # пустой — не публикуем
    (д / "a.idpocr.v2.json").write_text('{"v": 2}', encoding="utf-8")
    (д / "c.idpocr.json").write_text('{"v": 1}', encoding="utf-8")

    отчёт = migrate(д, blobs.get_blob_store())

    assert отчёт == {"texts": 2, "texts_uploaded": 1, "texts_already": 0,
                     "texts_empty": 1, "idp": 2, "idp_uploaded": 2, "idp_already": 0}
    store = blobs.get_blob_store()
    assert store.exists(blobs.document_text_key("a"))
    assert not store.exists(blobs.document_text_key("b"))
    # ⚠️ `Path.stem` у `a.idpocr.v2.json` даёт `a.idpocr.v2`, а не `a` — ключ
    # обязан строиться срезом по суффиксу, иначе документ не найдёт свой ответ.
    assert store.exists(blobs.document_idp_key("a", 2))
    assert store.exists(blobs.document_idp_key("c", 1))

    assert migrate(д, store)["texts_already"] == 1
