"""Вопрос, заданный вместе с загрузкой файла, не умирает молча.

ДЕФЕКТ (четвёртый эшелон, Ш0). Пользователь прикладывает отчёт и ТЕМ ЖЕ ходом
спрашивает по нему. Распознавание идёт фоном, и сайдкар `.txt` ложится на
секунды позже самого файла. Комната просила текст через демона, получала
мгновенный 404, демон писал отказ, а обёртка извлечения честно срабатывала
fail-fast'ом — `FETCH-DENIED`, код возврата 3. Вопрос умирал, и пользователь
видел отказ там, где надо было просто подождать пару секунд.

⚠️ ПРАВИЛО УЖЕ БЫЛО ЗАВЕДЕНО — НА ДРУГОМ ПУТИ. Инлайновый тул ждёт ровно этого
и ровно по этой причине: `llm/tools._fe_doc_text_wait`, «the user asks right
after upload (race: the .txt sidecar lands a bit after the file)». Станционный
путь остался без ожидания — правило в одном месте из двух, тот же класс, что
двойные enum'ы и приведение к году.

ГРАНИЦА, КОТОРУЮ ВАЖНО ДЕРЖАТЬ: ждать имеет смысл только то, что
действительно обрабатывается. Несуществующий документ обязан отбиваться СРАЗУ,
а не через минуту — иначе опечатка в идентификаторе станет минутой тишины.

ПРОВЕРКА СТОРОЖЕЙ ОТКАТОМ (мутации прогнаны 21.08.2026, каждая покраснела):
  · вернуть мгновенный 404 при отсутствии сайдкара → падает
    `test_the_text_that_lands_a_moment_later_is_still_delivered`;
  · снять проверку «знает ли хранилище о документе» → падает
    `test_an_unknown_document_is_refused_at_once` (по времени: минута вместо
    мгновенного отказа);
  · `_OCR_WAIT_SEC = 0` → падает первый тест.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from finengine.api import internal_docs


class _Doc:
    """Ровно то, что спрашивает ожидание: документ в хранилище есть."""

    doc_id = "doc-1"
    company_name = ""
    filename = "report.pdf"


@pytest.fixture()
def _store_knows(monkeypatch, tmp_path):
    """Хранилище знает документ — значит он обрабатывается."""
    class _Store:
        def get_document(self, doc_id):
            return _Doc() if doc_id == "doc-1" else None

    monkeypatch.setattr("finengine.ingestion.document_store.get_store", lambda: _Store())
    # ⚠️ ПУТЬ БОЛЬШЕ НЕ ПЕРЕДАЁТСЯ АРГУМЕНТОМ: `_text_when_ready` принимает
    # только doc_id, а где лежит текст — решает `store/blobs` (хранилище, иначе
    # файл в `docs_dir`). Значит каталог обязан быть подменён здесь, иначе тест
    # писал бы сайдкар в один каталог, а код искал в другом.
    from finengine.config import settings as _s
    monkeypatch.setattr(_s, "docs_dir", tmp_path)
    monkeypatch.setattr(_s, "data_dir", tmp_path)
    monkeypatch.setattr(_s, "s3_endpoint", "")
    import finengine.store.blobs as _blobs
    _blobs._cached = None


@pytest.mark.asyncio
async def test_the_text_that_lands_a_moment_later_is_still_delivered(tmp_path, _store_knows,
                                                                    monkeypatch):
    """Сайдкар лёг на секунду позже — текст всё равно доехал."""
    monkeypatch.setattr(internal_docs, "_OCR_WAIT_SEC", 10.0)
    path = tmp_path / "doc-1.txt"

    async def _land_later():
        await asyncio.sleep(1.2)
        path.write_text("===PAGE 1===\nОтчёт", encoding="utf-8")

    task = asyncio.create_task(_land_later())
    text = await internal_docs._text_when_ready("doc-1")
    await task
    assert text and "Отчёт" in text, (
        "текст, приехавший через секунду после вопроса, не доставлен — значит "
        "вопрос, заданный вместе с загрузкой файла, снова умирает молча")


@pytest.mark.asyncio
async def test_an_unknown_document_is_refused_at_once(tmp_path, _store_knows, monkeypatch):
    """Опечатка в идентификаторе — отказ сразу, а не минута тишины."""
    monkeypatch.setattr(internal_docs, "_OCR_WAIT_SEC", 10.0)
    started = time.monotonic()
    text = await internal_docs._text_when_ready("нетакого")
    spent = time.monotonic() - started
    assert text is None
    assert spent < 1.0, (
        f"несуществующий документ отбивался {spent:.1f} с — ждать имеет смысл "
        "только то, что действительно обрабатывается")


@pytest.mark.asyncio
async def test_a_ready_text_is_returned_without_waiting(tmp_path, _store_knows, monkeypatch):
    """Готовый текст отдаётся мгновенно — ожидание не облагает обычный случай."""
    monkeypatch.setattr(internal_docs, "_OCR_WAIT_SEC", 10.0)
    path = tmp_path / "doc-1.txt"
    path.write_text("готовый текст", encoding="utf-8")
    started = time.monotonic()
    text = await internal_docs._text_when_ready("doc-1")
    assert text == "готовый текст"
    assert time.monotonic() - started < 0.5, "готовый текст ждал — налог на всех"


@pytest.mark.asyncio
async def test_an_ocr_that_never_finishes_gives_up(tmp_path, _store_knows, monkeypatch):
    """Распознавание не поспело — отказ, а не вечное ожидание.

    Обёртка в комнате ждёт доставки 90 секунд; ответить позже её дедлайна — то
    же самое, что не ответить, только дороже.
    """
    monkeypatch.setattr(internal_docs, "_OCR_WAIT_SEC", 1.0)
    started = time.monotonic()
    text = await internal_docs._text_when_ready("doc-1")
    spent = time.monotonic() - started
    assert text is None
    assert 0.9 <= spent < 5.0, f"сдался за {spent:.1f} с при окне 1 с"


def test_the_window_stays_under_the_room_deadline():
    """Окно ожидания обязано быть УЖЕ, чем ждёт обёртка комнаты (90 с)."""
    assert internal_docs._OCR_WAIT_SEC < 90.0, (
        f"ждём {internal_docs._OCR_WAIT_SEC} с при дедлайне комнаты 90 с — "
        "ответ позже её дедлайна равнозначен молчанию")
