"""Векторный поиск снесён и не возвращается (15.08.2026).

ЗАЧЕМ ЭТОТ ФАЙЛ. Владелец потребовал «удалить вообще всё, что связано с FAISS».
На инвентаре выяснилось, что удалять одним куском нечего: `ingestion/indexer.py`
держал ДВА предмета в одном классе `VectorIndex` — хранилище документов (живое,
шесть потребителей) и векторный поиск (мёртвый). Предметы разделены: поиск
вырезан, хранилище осталось и переименовано в `ingestion/document_store.py`.

ЧЕМ БЫЛО ДОКАЗАНО, ЧТО ПОИСК МЁРТВ (замеры, а не рассуждение):
  1. файла `faiss.index` НЕТ НИГДЕ — на проде каталога `data/index` не
     существует вовсе, на dev он 4 КБ, локально в нём один `meta.pkl` на 793 б;
  2. боевой путь извлечения подставляет `_StubIndex` (`skills/extract_text.py`),
     и ветка `if index is None: VectorIndex()` не исполнялась;
  3. импорт `faiss` уже был опциональным — `add_document` ловил
     `ModuleNotFoundError` и работал дальше.

ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ:
  1. в коде нет ни `faiss`, ни модели эмбеддингов;
  2. у хранилища нет метода `search` — вернуть его значит вернуть вторую
     методику поиска, от которой уходили;
  3. хранилище документов ЖИВО и переживает перезапуск (сторож от того, что
     снос заодно унесёт метаданные — а на них стоит весь бэкенд);
  4. зависимости `faiss-cpu` и `sentence-transformers` не вернулись в pyproject.

⚠️ ЭТО НЕ ТО ЖЕ, ЧТО `test_rag_path_stays_removed.py`. Тот сторожит RAG-ПУТЬ
извлечения (`arch=finengine_rag`), снятый 13.08 — другой предмет, другой снос.

⚠️ КОГДА ПОИСК ПО ДОКУМЕНТАМ ПОНАДОБИТСЯ СНОВА (#195, «документы не влезают в
контекст») — он проектируется заново под целевую схему. Возврат ЭТОГО кода —
регрессия, и покраснеть должен именно этот файл.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: вернуть в любой модуль `src/finengine` строку
`import faiss` — покраснеет тест 1. Добавить `DocumentStore.search` — тест 2.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from finengine.ingestion.document_store import get_store
from finengine.store.documents import SqlDocumentStore
from finengine.models.document import CompanyType, DocType, DocumentMeta

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "finengine"

# Файл сноса объясняет, ЧТО снесено, и обязан называть предмет своими словами.
_ALLOWED = {"document_store.py"}


def _sources():
    return [p for p in SRC.rglob("*.py") if p.name not in _ALLOWED]


def test_no_module_imports_faiss_or_an_embedding_model():
    """Ни одного живого следа векторного стека в коде."""
    guilty: list[str] = []
    for p in _sources():
        text = p.read_text(encoding="utf-8", errors="ignore")
        for needle in ("import faiss", "faiss.", "SentenceTransformer",
                       "sentence_transformers"):
            if needle in text:
                guilty.append(f"{p.relative_to(REPO)}: {needle}")
    assert not guilty, (
        "векторный стек вернулся в код:\n  " + "\n  ".join(guilty)
        + "\nПоиск по документам проектируется заново (#195), а не воскрешается.")


def test_the_store_has_no_search():
    """У хранилища документов нет и не должно быть поиска по смыслу.

    Метод `search` был входом во ВТОРУЮ методику поиска — ту, из-за которой
    правка подсказок доезжала до одного пути и не доезжала до другого.
    """
    assert not hasattr(SqlDocumentStore, "search")
    assert not hasattr(SqlDocumentStore, "_encode_query")
    assert not hasattr(SqlDocumentStore, "_get_model")


def test_the_document_store_itself_survived(бд):
    """ГЛАВНЫЙ сторож сноса: метаданные документов НЕ уехали вместе с поиском.

    На них стоит весь бэкенд — список документов, чат по документу, станция.
    Документ добавляется БЕЗ кусков (полноконтекстный путь их не делает) и
    обязан пережить перезапуск: раньше запись метаданных была привязана к
    наличию векторов, и такие документы пропадали при рестарте.

    ⚠️ 26.08.2026 сторож переписан под базу. До этого он проверял тот же
    контракт на `meta.pkl` — последнем каталоге эпохи FAISS (он потому и звался
    `index`, что рядом лежал векторный индекс). Пикл снесён; контракт остался
    тот же, и «перезапуск» теперь — новый экземпляр над тем же адресом.
    """
    meta = DocumentMeta(
        filename="отчёт.pdf",
        doc_type=DocType.IFRS_REPORT,
        company_name="Тест",
        company_type=CompanyType.NON_FINANCIAL,
        period="2025",
    )
    store = SqlDocumentStore(бд)
    store.add_document(meta, chunks=[])

    # Перезапуск процесса = новый экземпляр над тем же адресом.
    again = SqlDocumentStore(бд)
    got = again.get_document(meta.doc_id)
    assert got is not None, "документ пропал после перезапуска"
    assert got.filename == "отчёт.pdf"
    assert [d.doc_id for d in again.list_documents()] == [meta.doc_id]


def test_the_index_directory_is_gone_with_the_vectors():
    """Каталог `data/index` звался так ради векторного индекса.

    Индекса нет с 15.08.2026, пикла реестра — с 26.08. Настройка `index_dir`
    снесена: последнее, чем эпоха FAISS держалась за код, — это имя каталога.
    Мутация: вернуть `index_dir` в настройки — тест краснеет.
    """
    from finengine.config import Settings

    assert not hasattr(Settings(), "index_dir")


def test_the_store_is_a_single_instance(бд, monkeypatch):
    """Экземпляр один на процесс: два разошлись бы по составу документов."""
    import finengine.ingestion.document_store as D
    from finengine.config import settings

    monkeypatch.setattr(settings, "documents_database_url", бд)
    D.reset_store()
    assert get_store() is get_store()
    D.reset_store()


@pytest.mark.parametrize("dep", ["faiss-cpu", "sentence-transformers", "torch"])
def test_the_heavy_stack_is_not_a_dependency(dep: str):
    """Зависимости не вернулись — в том числе как «опциональная группа».

    Группа `[ml]` держала их как «поставим, если понадобится», и именно она
    оставляла путь назад открытым.
    """
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue  # объяснение в комментарии — не зависимость
        assert dep not in stripped, f"{dep} снова объявлен: {line!r}"


def test_claude_md_does_not_invent_faiss_work_that_is_already_done():
    """CLAUDE.md не обещает сноса FAISS, которого не осталось.

    🔴 ЦЕНА. До 20.08 CLAUDE.md утверждал: «остались рудименты в 15 файлах,
    под снос». Файлов с упоминанием FAISS действительно пятнадцать, но во всех
    пятнадцати это комментарии, разделы документации и ДВА СТОРОЖА, охраняющих
    отсутствие — исполняемых ссылок ноль, что и доказывает
    `test_no_module_imports_faiss_or_an_embedding_model` выше. CLAUDE.md
    свежий контекст читает первым: строка заводила ему работу, которой не
    существует, и он шёл искать пятнадцать несуществующих рудиментов.

    Сторож парный к тому, что выше: пока код чист, документ не смеет говорить,
    что он грязен. Загрязнится код — покраснеет первый сторож, и тогда фразу
    можно вернуть осознанно.
    """
    text = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
    assert "рудименты в 15 файлах, под снос" not in text, (
        "в CLAUDE.md вернулось обещание снести FAISS: сносить нечего, "
        "исполняемых ссылок ноль — см. docs/РУДИМЕНТЫ.md §3.1")


# ─── База знаний чата снесена (07.09.2026) ───────────────────────────────
#
# РЕШЕНИЕ ВЛАДЕЛЬЦА: «снеси это, раз этим никто не пользуется, и чтобы у нас
# нигде не было FAISS-легаси кода». Что снесено: модуль `skills/news/chat_kb`
# (лексический BM25-поиск по кускам страниц, сложенным в базу конкретного
# чата), тулы `kb_search`/`kb_add_url`, REST `/chats/{id}/kb/*`, забиратель
# страниц `url_fetcher` (жил только ради базы) и зависимости `rank-bm25`,
# `readability-lxml`, `lxml`. К анализу отчётности база не подключалась никак:
# путь отчёта — OCR → станция → реестр, в телах навыков `kb_*` не упоминались.
#
# ⚠️ ПОПРАВКА К СОБСТВЕННОЙ ОШИБКЕ: 07.09 я назвал эту базу «живым векторным
# поиском». Неправда — cosine стоял только в описании тула, шапка модуля прямо
# говорила «BM25, fastembed на VPS не влез». Сносилась неиспользуемая функция,
# а не FAISS-рудимент. Сторож всё равно нужен: «вернуть маленькую базу чата» —
# самый вероятный путь, которым второй механизм поиска проберётся обратно.
#
# ПРОВЕРКА ОТКАТОМ: создать пустой `src/finengine/skills/news/chat_kb.py` —
# краснеет первый тест; вернуть `"kb_search": ToolDef(` в реестр — второй.

def test_the_chat_knowledge_base_module_is_gone():
    for снесённый in ("skills/news/chat_kb.py", "skills/news/url_fetcher.py"):
        assert not (SRC / снесённый).exists(), (
            f"{снесённый} вернулся — база знаний чата снесена 07.09.2026, "
            "никто ею не пользовался; поиск по документам проектируется заново (#195)")


def test_the_chat_knowledge_base_tools_are_not_registered():
    from finengine.llm import tools as T

    вернулись = sorted({"kb_search", "kb_add_url"} & set(T.REGISTRY))
    assert not вернулись, (
        f"тулы базы знаний чата снова в реестре: {вернулись} — оттуда они едут в "
        "плагин гейтвея и в каждый запрос к модели")


def test_the_chat_knowledge_base_routes_are_gone():
    src = (SRC / "api" / "personal_sources.py").read_text(encoding="utf-8")
    assert "/kb/" not in src and "chat_kb" not in src, (
        "REST-ручки базы знаний чата вернулись в personal_sources.py")


@pytest.mark.parametrize("dep", ["rank-bm25", "readability-lxml"])
def test_the_chat_kb_dependencies_are_gone(dep: str):
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    for line in text.split("\n"):
        if line.strip().startswith("#"):
            continue
        assert dep not in line, f"{dep} снова объявлен: {line!r}"
