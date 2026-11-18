"""Internal doc-text delivery for gateway-side workers (#84, Фаза C, узел №1).

Суб-агент-воркер работает в СВОЕЙ запечатанной комнате станции: файлы туда
попадают ТОЛЬКО через files:upload с гейтвея (плагин workstation-ext). OCR-текст
документа живёт в бэке (data/documents/{doc_id}.txt) — этот роут отдаёт его
гейтвею-плагину, который загружает текст в комнату воркера (зеркало банковского
«OCR-тул на гейтвее пишет в комнату»).

Аддитивно и изолированно (как station_progress_ingest): свой /internal/* роут,
тот же внутренний секрет (gateway-токен, единый trust-домен server-to-server,
маршрут только по внутренней docker-сети). Owner-гейт остаётся в диспетчере ДО
заказа воркера (канон ARCHITECTURE_DECISIONS: комнату заказывает бэк-хендлер,
уже проверивший владельца) — этот роут доступен только плагину по токену.

Nonce-скоуп (#84, секьюрити-долг): токен демона общий на все комнаты, поэтому
записка из ЛЮБОЙ комнаты могла заказать ЛЮБОЙ doc_id. Теперь выдача связана с
нонсом хода: бэк при выдаче нонса регистрирует (nonce → doc_ids сессии, TTL),
записка несёт нонс, демон передаёт его сюда — отдаём только валидную пару.
In-process реестр (как ingest-шина) — один uvicorn-процесс, задокументировано.
"""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import PlainTextResponse

log = logging.getLogger(__name__)

router = APIRouter()

# doc_id — только безопасный алфавит (защита от path traversal в имени файла).
_DOC_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# nonce → разрешённые doc_id. TTL с запасом поверх mailbox (20 мин) — воркер
# может стартовать не сразу после выдачи нонса.
#
# 🔴 ХРАНИТСЯ В ОБЩЕМ КЭШЕ, А НЕ В ПАМЯТИ ПРОЦЕССА (architecture.md 3.2, MUST).
# Здесь был словарь уровня модуля. На двух репликах нонс выдавала бы одна, а
# документ у другой — и станция получала бы отказ «документ не разрешён» на
# ровном месте. Ключ — с приставкой, чтобы соседи по кэшу не пересеклись.
_TTL_SEC = 1800.0
_ПРИСТАВКА = "docscope:"
_CONTEXT_PREFIX = "doccontext:"


def _кэш():
    from finengine.store.cache import get_cache  # noqa: PLC0415

    return get_cache()


def register_nonce_docs(nonce: str, doc_ids: list[str]) -> None:
    """Вызывается при выдаче нонса ходу (agent.query_stream): документы сессии,
    которые воркер под этим нонсом вправе получить.

    ⚠️ Срок теперь ведёт КЭШ (`time.time` внутри него), а не `time.monotonic`
    здесь: монотонные часы у каждой реплики свои, и общий срок по ним не
    считается. Разница для нас безвредна — окно в тридцать минут, а не миллисекунды.
    """
    if nonce and doc_ids:
        _кэш().set(f"{_ПРИСТАВКА}{nonce}", sorted({str(d) for d in doc_ids}), _TTL_SEC)


def register_nonce_context(nonce: str, *, owner_id: int, case_id: str) -> None:
    """Bind the durable Case identity to the same short-lived delivery ticket.

    The station must not derive either value from a filename or nonce.  This
    record is transport metadata only: CaseRuntime remains the state owner.
    """
    if nonce and owner_id and case_id:
        _кэш().set(
            f"{_CONTEXT_PREFIX}{nonce}",
            {"owner_id": int(owner_id), "case_id": str(case_id)},
            _TTL_SEC,
        )


def nonce_context(nonce: str) -> dict[str, object]:
    value = _кэш().get(f"{_CONTEXT_PREFIX}{nonce}")
    return dict(value) if isinstance(value, dict) else {}


def nonce_scopes() -> dict[str, set[str]]:
    """Все живые пропуска: нонс → разрешённые doc_id. Для тестов и разбора.

    🔴 ЗАЧЕМ ПУБЛИЧНО. Два сторожа безопасности перечисляют скоупы, чтобы
    убедиться, что чужой документ в них не попал (`docs/rules/security.md`
    §3.1 — блокер). Раньше они читали внутренний словарь модуля и упали, как
    только хранилище сменилось. Свойство, которое они охраняют, важнее способа
    хранения — значит спрашивать его надо через дверь, а не через окно.
    """
    кэш = _кэш()
    итог: dict[str, set[str]] = {}
    for k in кэш.keys(_ПРИСТАВКА):
        значение = кэш.get(k)
        if значение:
            итог[k[len(_ПРИСТАВКА):]] = set(значение)
    return итог


def reset_scopes() -> None:
    """Забыть все пропуска. Для тестов и только для них.

    🔴 ПОЧЕМУ ПУБЛИЧНАЯ ФУНКЦИЯ, А НЕ `_scope.clear()` В ШЕСТИ ФАЙЛАХ. Раньше
    ровно так и было: шесть фикстур лезли во внутренний словарь модуля. Стоило
    заменить хранилище — и все шесть упали разом на `AttributeError`. Тест
    вправе просить «начни с чистого листа», но не вправе знать, чем этот лист
    сделан.
    """
    кэш = _кэш()
    for k in кэш.keys(_ПРИСТАВКА):
        кэш.delete(k)
    for k in кэш.keys(_CONTEXT_PREFIX):
        кэш.delete(k)


def _allowed(nonce: str, doc_id: str) -> bool:
    разрешённые = _кэш().get(f"{_ПРИСТАВКА}{nonce}")
    return bool(разрешённые) and doc_id in разрешённые


def nonce_is_live(nonce: str) -> bool:
    """Живой ли нонс хода. Не про КОНКРЕТНЫЙ документ, а про сам пропуск.

    Нужен соседям, которым документ не адресован (кэш первой стадии): им важно
    только, что просит живой ход, а не случайный прохожий. Скоуп документов
    остаётся здесь единственным — второго реестра нонсов не заводим.
    """
    nonce = (nonce or "").strip()
    if not nonce:
        return False
    return bool(_кэш().get(f"{_ПРИСТАВКА}{nonce}"))


def _token() -> str:
    # Единственная реализация правила — `api/internal_auth`. Здесь остаётся
    # обёртка: её зовут тесты и соседние модули.
    from finengine.api.internal_auth import gateway_token  # noqa: PLC0415

    return gateway_token()


# Сколько ждать распознавание. Обёртка извлечения в комнате ждёт доставки 90 с
# (`aifa-extract/scripts/extract_run.sh`), и наше окно обязано быть УЖЕ её:
# ответить позже её дедлайна — то же самое, что не ответить.
_OCR_WAIT_SEC = 60.0


async def _text_when_ready(doc_id: str) -> str | None:
    """Текст документа, дождавшись распознавания. `None` — не дождались.

    ДЕФЕКТ, КОТОРЫЙ ЭТО ЗАКРЫВАЕТ (четвёртый эшелон, Ш0). Пользователь
    прикладывает файл и ТЕМ ЖЕ ходом задаёт вопрос. Распознавание идёт фоном и
    сайдкар `.txt` ложится на секунды позже файла. Комната просила текст,
    получала мгновенный 404, демон писал отказ, обёртка честно срабатывала
    fail-fast'ом (`FETCH-DENIED`, код 3) — и вопрос, заданный вместе с
    загрузкой, УМИРАЛ МОЛЧА.

    ⚠️ ЭТО ПРАВИЛО УЖЕ БЫЛО ЗАВЕДЕНО — НА ДРУГОМ ПУТИ. Инлайновый тул ждёт
    ровно этого и ровно по этой причине (`llm/tools._fe_doc_text_wait`: «the
    user asks right after upload (race: the .txt sidecar lands a bit after the
    file)»). Станционный путь остался без ожидания, то есть правило жило в
    одном месте из двух — тот же класс, что двойные enum'ы и приведение к году.

    ⚠️ НЕСУЩЕСТВУЮЩИЙ ДОКУМЕНТ ОТБИВАЕТСЯ СРАЗУ, а не через минуту: ждать
    имеет смысл только то, что действительно обрабатывается. Различаем по
    хранилищу документов — оно знает о файле с момента загрузки, задолго до
    сайдкара.
    """
    from finengine.ingestion.document_store import get_store  # noqa: PLC0415
    from finengine.store.blobs import document_text, document_text_when_ready  # noqa: PLC0415

    # 🔴 ГОТОВЫЙ ТЕКСТ ОТДАЁМ СРАЗУ, ДО ВСЯКИХ ПРОВЕРОК. Порядок не косметика:
    # реестр нужен, чтобы решить, СТОИТ ЛИ ЖДАТЬ, а не чтобы отказать в том, что
    # уже есть. Спроси его первым — и документ с готовым текстом, но не
    # зарегистрированный, получил бы 404.
    готовый = document_text(doc_id)
    if готовый is not None:
        return готовый

    # Текста нет. Знает ли о документе хранилище? Не знает — ждать нечего.
    if get_store().get_document(doc_id) is None:
        return None

    сказано = False

    async def _сказать_один_раз(_прошло: float) -> None:
        nonlocal сказано
        if not сказано:
            log.info("doc-text: %s ещё распознаётся — жду до %.0f с", doc_id, _OCR_WAIT_SEC)
            сказано = True

    текст = await document_text_when_ready(
        doc_id, timeout_s=_OCR_WAIT_SEC, poll_s=1.0, on_wait=_сказать_один_раз)
    if текст is None:
        log.warning("doc-text: распознавание %s не поспело за %.0f с — отдаю 404",
                    doc_id, _OCR_WAIT_SEC)
    return текст


@router.get("/internal/doc-text/{doc_id}")
async def doc_text(doc_id: str, nonce: str = "",
                   authorization: str = Header(default="")) -> PlainTextResponse:
    from finengine.api.internal_auth import require_gateway_token  # noqa: PLC0415

    require_gateway_token(authorization)
    if not _DOC_ID_RE.match(doc_id):
        raise HTTPException(status_code=400, detail="bad doc_id")
    if not _allowed(nonce.strip(), doc_id):
        raise HTTPException(status_code=403, detail="doc not allowed for nonce")

    text = await _text_when_ready(doc_id)
    if text is None:
        raise HTTPException(status_code=404, detail="no ocr text for doc")
    # #160: комната воркера ТОЛЬКО ЗДЕСЬ называет пару (нонс, doc_id) — дальше
    # в релее прогресса и в его `done` документа уже нет. Без этой связи почтовый
    # ящик не знает, ПО КАКОМУ документу пришёл результат, и не может сказать
    # агенту «это уже извлечено» — а тот выводит статус из молчания и запускает
    # движок второй раз (замер Мечела: 253,5 с впустую). Догадка по составу
    # сессии тут запрещена: в ходу с несколькими документами промах привязал бы
    # результат к чужому отчёту.
    from finengine.skills import grounding_bus as _bus  # noqa: PLC0415

    _bus.bind_worker_doc(nonce.strip(), doc_id)
    # Мета документа (имя компании/файла) — заголовком (base64: имена кириллицей,
    # а HTTP-заголовки latin-1). Демон положит ocr.meta.json рядом с ocr.txt —
    # движок в комнате получит имя, а не будет угадывать по тексту (квартальники
    # без титульного листа детектор не берёт — пустое имя в карточке/Excel).
    headers = {}
    try:
        from finengine.ingestion.document_store import get_store  # noqa: PLC0415
        doc = get_store().get_document(doc_id)
        if doc is not None:
            import base64  # noqa: PLC0415
            import json as _json  # noqa: PLC0415
            meta = {"company_name": getattr(doc, "company_name", "") or "",
                    "filename": getattr(doc, "filename", "") or "",
                    **nonce_context(nonce.strip())}
            headers["X-Doc-Meta"] = base64.b64encode(
                _json.dumps(meta, ensure_ascii=False).encode("utf-8")).decode("ascii")
    except Exception:  # noqa: BLE001 — мета best-effort, текст важнее
        pass
    return PlainTextResponse(text, headers=headers)
