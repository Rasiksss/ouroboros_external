"""Внешний срез: новости, макро и решения регуляторов ВОКРУГ отчёта (#159).

ЗАЧЕМ. В строках отчётности причины не написано. Выручка просела — а почему,
знает лента: у маркетплейса сгорели пункты выдачи, у экспортёра сменился режим
пошлин, у банка регулятор поднял ставку. Раздел «Контекст извне» и есть это
объяснение рядом с цифрами.

ПОЧЕМУ СБОР ЖИВЁТ ЗДЕСЬ, А НЕ В ПРОМПТЕ. Составитель анализа — ОДИН вызов
модели БЕЗ инструментов (`run.py` → `llm.complete_with_usage`, tools туда не
передаются), и исполняется он в ЗАПЕЧАТАННОЙ станции, у которой нет выхода в
сеть, кроме прокси модели. Попросить его «загляни в новости» физически нельзя:
это была бы вежливая ложь в промпте — дотянуться он не сможет и, скорее всего,
напишет по памяти. Поэтому срез собирает КОД на бэкенде ДО анализа и подаёт
фактом через типизированный внешний контракт.

⚠️ ГЛАВНОЕ СВОЙСТВО МОДУЛЯ — `_accept`. Внешний факт встаёт в отчёт рядом с
числами, которые движок выверил по тексту, и выглядит ровно так же авторитетно.
Но проверить его пользователю НЕЧЕМ: это не строка баланса, а чужой текст из
интернета. Проверяемым его делает единственное — ИМЯ ИСТОЧНИКА, ССЫЛКА и ДАТА.
Поэтому здесь не «стараемся заполнить поля», а НЕ ПРОПУСКАЕМ: находка без
ссылки или без даты не становится строкой вовсе. Это механизм, а не
договорённость — строку неоткуда взять в обход `_accept`, конструктор
`ExternalFact` вызывается только оттуда.

ЧЕСТНАЯ ГРАНИЦА. Отраслевой и макро-фон достижимы: это публичные факты с
датой и ссылкой. ПОСТРОЧНОЕ сравнение с конкурентом — НЕТ, пока отчёт
конкурента не загружен: пересказ интернета, названный сравнением, читается как
выверенная таблица, хотя ею не является. Запрет доезжает до модели словами в
`framework.external_block`.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from finengine.contracts import RunPassport

log = logging.getLogger(__name__)


def user_allowed_web_search() -> bool:
    """Разрешил ли ПОЛЬЗОВАТЕЛЬ выход наружу на этом ходу.

    Тумблер «Поиск в интернете» в меню «+» композера — настоящий (с 08.10.2026
    по умолчанию включён): фронт шлёт флаг каждым ходом, бэк вырезает поисковые
    инструменты из набора модели. Наличие ключа Tavily (`web_search.is_enabled`) отвечает на
    другой вопрос — «умеем ли мы искать вообще», — и подменять им ответ на
    «разрешили ли нам» нельзя: сбор внешнего среза отправляет наружу ИМЯ
    КОМПАНИИ из отчёта пользователя. Это его согласие, а не наш стиль, и
    надпись «выключен» обязана значить выключен.

    Проверка стоит здесь, в единственной двери сбора, а не у вызывающего: так её
    нельзя забыть, добавив второй путь к сбору.
    """
    from finengine.llm.openclaw import web_search_allowed  # noqa: PLC0415

    return web_search_allowed()


# Одна причина на всё, что ходит наружу из цикла гибридного планировщика (`web.search`, атом-загрузка,
# пустой `explain.external`): её читает планировщик и пересказывает пользователю.
WEB_SEARCH_OFF_REASON = (
    "поиск в интернете выключен пользователем (включается в меню «+» у поля ввода: «Поиск в интернете»)"
)


class WebSearchOff(RuntimeError):
    """Атому нужна сеть, а пользователь выход наружу не разрешил."""


def require_web_search() -> None:
    """Та же дверь согласия для атомов, которые без сети не имеют смысла: не пусто, а отказ с причиной."""
    if not user_allowed_web_search():
        raise WebSearchOff(WEB_SEARCH_OFF_REASON)

# Сколько строк доживает до отчёта. Раздел — контекст, а не дайджест: два
# десятка заголовков утопят выводы по самой отчётности, ради которых пришли.
MAX_FACTS = 6
# Окно новостей. Отчёт публикуется с лагом, и объясняющие цифры события лежат
# и до, и после отчётной даты — полгода накрывает оба хвоста.
NEWS_WINDOW_DAYS = 180
_PER_QUERY = 5

# Дата приходит от поисковика в чужом формате. Разбираем ровно те, что реально
# встречаются в выдаче; всё, что не разобралось, — не дата, и строка выбывает.
_DATE_FORMATS = ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%a, %d %b %Y %H:%M:%S %Z",
                 "%a, %d %b %Y %H:%M:%S", "%d %b %Y")


@dataclass(frozen=True)
class ExternalFact:
    """Одна строка раздела «Контекст извне».

    Все четыре поля ОБЯЗАТЕЛЬНЫ и непусты — это гарантирует `_accept`, а не
    дисциплина вызывающего. `date` всегда ISO `ГГГГ-ММ-ДД`: раздел сортируется
    и читается по датам, а «15 января» и «Jan 15» рядом не сортируются.
    """

    text: str
    source: str
    url: str
    date: str


def _source_name(url: str) -> str:
    """Домен как имя источника: «interfax.ru», «cbr.ru».

    Заголовок новости для этой роли не годится — он описывает СОБЫТИЕ, а
    читателю нужно понять, КОМУ он верит, открывая ссылку.
    """
    m = re.match(r"https?://(?:www\.)?([^/:?#]+)", (url or "").strip(), re.IGNORECASE)
    return m.group(1).lower() if m else ""


def _iso_date(raw: str) -> str:
    """Чужой формат даты → ISO. Не разобралось — пустая строка (строка выбывает)."""
    s = (raw or "").strip()
    if not s:
        return ""
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    # Хвост времени/зоны — частый случай; пробуем голову «ГГГГ-ММ-ДД».
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d").date().isoformat()
        except ValueError:
            return ""
    return ""


def _accept(text: str, url: str, raw_date: str) -> ExternalFact | None:
    """ЕДИНСТВЕННАЯ дверь, через которую находка становится фактом отчёта.

    Возвращает `None` — значит строки не будет НИГДЕ: ни в промпте, ни в
    карточке. Именно этого требует инвариант: внешний факт без источника и
    даты неотличим от домысла, а рядом с выверенными числами домысел получает
    их авторитет даром.
    """
    body = " ".join((text or "").split())
    link = (url or "").strip()
    source = _source_name(link)
    day = _iso_date(raw_date)
    if not body or not source or not day:
        return None
    return ExternalFact(text=body[:400], source=source, url=link, date=day)


# Срез 6b (карточка `explain.external`, 16b): «маршрутизация по entity/
# jurisdiction/period, не по языку документа». Сигналы берутся из `RunPassport`,
# зафиксированного один раз (решение №2), от сильного к слабому:
# 1. регистрационные номера эмитента, найденные в тексте (`entity.external_ids`): ИНН/ОГРН
#    выдаёт ФНС России, CIK — SEC; это адрес регистрации, а не стиль отчёта;
# 2. национальный стандарт учёта (`classification.standard`): РСБУ, US GAAP, Ind AS — у каждого одна страна;
# 3. МСФО — стандарт международный и страны не называет: для него берётся ячейка профиля
#    (`classification.profile_id`), под которую выбран реестр показателей; `ifrs_*_ru` — слой
#    российского рынка. Это слабее первых двух (ячейку различает язык отчёта), поэтому идёт последним.
# Ничего не подошло — `None`: честное «юрисдикция не определена», не подстановка ближайшей.
# Часть 19 доводки: прежняя таблица ждала id ячейки там, где паспорт несёт ось `standard`
# (`ifrs`), и для любого МСФО-отчёта отдавала `None`.
_JURISDICTION_BY_REGISTRATION_ID: dict[str, str] = {"inn": "RU", "ogrn": "RU", "cik": "US"}
_JURISDICTION_BY_NATIONAL_STANDARD: dict[str, str] = {
    "rsbu": "RU", "us_gaap": "US", "ind_gaap": "IN", "ind_as": "IN",
}
_JURISDICTION_BY_IFRS_PROFILE: dict[str, str] = {"ifrs_nf_ru": "RU", "ifrs_bank_ru": "RU"}

# Коды юрисдикции → имена стран реестра (`ir_data/daily_registry.yaml`,
# `country`). Не РАСШИРЯЕМ сам реестр здесь — юрисдикция без известной
# страны реестра честно не находит регулятора (пустой список), а не
# выдумывает соответствие.
_COUNTRIES_BY_JURISDICTION: dict[str, set[str]] = {
    "RU": {"РФ"}, "US": {"США", "еврозона"},
}


def jurisdiction_of(passport: "RunPassport") -> str | None:
    by_registration = {
        _JURISDICTION_BY_REGISTRATION_ID[kind]
        for kind in passport.entity.external_ids if kind in _JURISDICTION_BY_REGISTRATION_ID
    }
    if len(by_registration) == 1:
        return next(iter(by_registration))
    classification = passport.classification
    return (
        _JURISDICTION_BY_NATIONAL_STANDARD.get(classification.standard)
        or _JURISDICTION_BY_IFRS_PROFILE.get(classification.profile_id)
    )


def _regulators_by_jurisdiction(jurisdiction: str) -> list[str]:
    countries = _COUNTRIES_BY_JURISDICTION.get(jurisdiction) or set()
    if not countries:
        return []
    try:
        from pathlib import Path

        import yaml
        path = Path(__file__).resolve().parents[1] / "ir_data" / "daily_registry.yaml"
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        log.warning("внешний контекст: реестр регуляторов не прочитан", exc_info=True)
        return []
    out = [str(r.get("name") or "").strip() for r in (doc.get("regulators") or [])
           if str(r.get("country") or "").strip() in countries]
    return [n for n in out if n][:2]


def _name_matches(canonical_name: str, *texts: str) -> bool:
    """Грубый, но честный фильтр релевантности (карточка `explain.external`:
    «новость о другой компании с похожим названием»). Сверяет ПЕРВОЕ
    значимое слово канонического имени (обычно бренд, не организационно-
    правовую форму) с текстом находки — не строгое совпадение полного имени,
    которое почти никогда не встречается в заголовке дословно."""
    words = [w for w in re.split(r"\s+", (canonical_name or "").strip()) if len(w) >= 4]
    if not words:
        return True  # короткое/пустое имя — фильтровать нечем, не блокируем находку вслепую
    token = words[0].casefold()
    haystack = " ".join(texts).casefold()
    return token in haystack


def _regulators(language: str) -> list[str]:
    """Имена регуляторов из ЗАВОДСКОГО реестра (`ir_data/daily_registry.yaml`).

    Берём из реестра, а не из литерала в коде: реестр — согласованный с
    заказчиком список того, за чем следим, и он уже правится через git-ревью.
    Свой список здесь разъехался бы с ним при первой правке реестра.

    Страна — по языку отчёта: русскоязычная отчётность живёт в контуре решений
    ЦБ и Минфина РФ, англоязычная — ФРС/ЕЦБ. Грубо, но честно: страну эмитента
    из текста отчёта мы не определяем и делать вид, что определяем, не будем.
    """
    countries = {"РФ"} if (language or "ru").lower().startswith("ru") else {"США", "еврозона"}
    try:
        from pathlib import Path

        import yaml
        path = Path(__file__).resolve().parents[1] / "ir_data" / "daily_registry.yaml"
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — нет реестра/битый yaml: идём без макро-запроса
        log.warning("внешний контекст: реестр регуляторов не прочитан", exc_info=True)
        return []
    out = [str(r.get("name") or "").strip() for r in (doc.get("regulators") or [])
           if str(r.get("country") or "").strip() in countries]
    return [n for n in out if n][:2]


def _queries(company_name: str, period: str, language: str, *, jurisdiction: str | None = None) -> list[str]:
    """Два запроса, не больше: про компанию и про макро-контур вокруг неё.

    Ограничение осознанное. Каждый запрос — деньги и секунды на живом ходе
    пользователя, а раздел всё равно ужимается до `MAX_FACTS`: третий запрос
    оплачивался бы, но до отчёта почти не доезжал.

    `jurisdiction` (Срез 6b, `explain.external`) — явный код юрисдикции из
    `RunPassport.classification.standard`, если вызывающий его знает;
    `None` — старый путь (`_regulators(language)`), сохранён для
    регрессионного сравнения (методология C4/C7), не второй рабочий маршрут.
    """
    ru = (language or "ru").lower().startswith("ru")
    company = (company_name or "").strip()
    period = (period or "").strip()
    if not company:
        return []  # без имени компании честного запроса не построить
    q_company = (f"{company} новости {period}".strip() if ru
                 else f"{company} news {period}".strip())
    out = [q_company]
    regs = _regulators_by_jurisdiction(jurisdiction) if jurisdiction else _regulators(language)
    if regs:
        out.append(" ".join(regs) + (" ключевая ставка решение регулирование" if ru
                                     else " rate decision policy outlook"))
    return out


async def collect(*, company_name: str, period: str = "", language: str = "ru",
                  limit: int = MAX_FACTS, jurisdiction: str | None = None,
                  require_name_match: bool = False) -> list[ExternalFact]:
    """Собрать внешний срез по компании и периоду отчёта. Пусто — не ошибка.

    Пустой результат — штатный исход (поиск не настроен, компания не названа,
    ничего с датой не нашлось), и он ЧЕСТЕН: раздела в отчёте просто не будет,
    а не появится пустой заголовок, намекающий, что снаружи тихо.

    `jurisdiction`/`require_name_match` — Срез 6b: явная маршрутизация по
    юрисдикции вместо языка и фильтр «другая компания с похожим названием»
    (карточка `explain.external`). Оба по умолчанию выключены — старые
    вызовы (`analysis/run.py`) не меняют поведение.
    """
    from finengine.skills import web_search as _ws

    if not user_allowed_web_search():
        log.info("внешний контекст: тумблер «Поиск в интернете» выключен — не ищу")
        return []
    queries = _queries(company_name, period, language, jurisdiction=jurisdiction)
    if not queries or not _ws.is_enabled():
        return []
    facts: list[ExternalFact] = []
    seen: set[str] = set()
    for q in queries:
        # `topic="news"` — не украшение: ТОЛЬКО в новостном режиме поисковик
        # отдаёт дату публикации, а без даты строка всё равно выбывает в
        # `_accept`. Обычный веб-поиск дал бы находки, которые нечем датировать.
        hits = await _ws.web_search(q, max_results=_PER_QUERY, topic="news",
                                    days=NEWS_WINDOW_DAYS)
        for h in hits:
            if require_name_match and not _name_matches(company_name, h.title or "", h.content or ""):
                continue
            fact = _accept(h.title or h.content, h.url, h.published_date)
            if fact is None or fact.url in seen:
                continue
            seen.add(fact.url)
            facts.append(fact)
    facts.sort(key=lambda f: f.date, reverse=True)  # свежее — выше
    return facts[:limit]


def to_rows(facts: list[ExternalFact]) -> list[dict]:
    """Факты → строки карточки (`external_context` контракта `report_structured`).

    Карточка наполняется ОТСЮДА, а не разбором markdown-ответа модели. Разница
    принципиальная: ссылку и дату в этих строках писал код по ответу источника,
    и переписать их модель не может даже случайно — её пересказ живёт только в
    тексте раздела.
    """
    return [{"text": f.text, "source": f.source, "url": f.url, "date": f.date}
            for f in facts]


def from_rows(rows: list[dict] | None) -> list[ExternalFact]:
    """Строки (JSON из файла станции) → факты, снова через `_accept`.

    Повторная проверка НЕ избыточна: между бэкендом и станцией лежит файл, и
    единственная гарантия, что в промпт не попадёт строка без источника, —
    прогнать её через ту же дверь ещё раз. Дверь одна на оба берега.
    """
    out: list[ExternalFact] = []
    for r in rows or []:
        fact = _accept(str(r.get("text") or ""), str(r.get("url") or ""),
                       str(r.get("date") or ""))
        if fact is not None:
            out.append(fact)
    return out


def format_for_prompt(facts: list[ExternalFact]) -> str:
    """Факты → строки для промпта. Источник и дата — В КАЖДОЙ строке.

    Не «список новостей, а источники в конце»: модель перескажет строку и
    потеряет привязку, а читатель получит утверждение без адреса. Ссылка идёт
    рядом с текстом, чтобы её нечем было отцепить.
    """
    return "\n".join(
        f"{i}. [{f.date} · {f.source}] {f.text} — {f.url}"
        for i, f in enumerate(facts, start=1)
    )


__all__ = ["ExternalFact", "MAX_FACTS", "NEWS_WINDOW_DAYS", "collect", "to_rows",
           "from_rows", "format_for_prompt", "jurisdiction_of",
           "WEB_SEARCH_OFF_REASON", "WebSearchOff", "require_web_search"]
