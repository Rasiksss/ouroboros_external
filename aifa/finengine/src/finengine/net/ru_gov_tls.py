"""Доверие удостоверяющему центру Минцифры — ТОЧЕЧНО, по списку адресов.

ЗАЧЕМ. У `rosstat.gov.ru` сертификат выдан центром Минцифры, которого нет в
штатном хранилище. Проба 17.08.2026 из прод-контейнера: с проверкой —
`CERTIFICATE_VERIFY_FAILED`, без проверки — HTTP 200 и 794 КБ. То есть сайт
доступен, мешает только отсутствие корня. Из-за этого 5–7 российских
макропоказателей были нам недоступны.

⚠️ ПОЧЕМУ НЕ СТАВИМ В ОБЩЕЕ ХРАНИЛИЩЕ. Установка означала бы, что этот центр
может поручиться за ЛЮБОЙ домен — включая openrouter.ai и api.tavily.com, куда
уходят наши ключи и данные пользователей. Это настоящая поверхность для
перехвата, а выигрыш — доступ к нескольким государственным сайтам. Поэтому
доверие включается только для адресов из `_RU_GOV_HOSTS`, а весь остальной
трафик идёт на штатном хранилище без изменений.

⚠️ ПРОМЕЖУТОЧНЫЙ НУЖЕН ИМЕННО «ДЛЯ SSL», А НЕ ИЗ СВОДНОГО НАБОРА. В публикации
Минцифры лежит промежуточный с ключом `D1:E1:71…` — цепочку Росстата он НЕ
закрывает, там нужен `77:3D:D9…`. Правильный адрес указан в самом сертификате
сайта, в расширении Authority Information Access. Сервер Росстата свой
промежуточный не присылает вовсе — это ошибка в их настройке, и лечится она
только тем, что мы возим его с собой.
"""
from __future__ import annotations

import ssl
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

# Адреса, ради которых всё затевалось. Список закрытый и правится осознанно:
# каждый новый адрес — это расширение того, за что ручается государственный
# центр внутри нашего процесса.
_RU_GOV_HOSTS = frozenset({
    "rosstat.gov.ru",
    "www.rosstat.gov.ru",
})

_CERT_DIR = Path(__file__).resolve().parents[3] / "deploy" / "certs"
_CERTS = (
    "russian_trusted_root_ca.pem",
    "russian_trusted_sub_ca_ssl_2024.pem",
)


def is_ru_gov_host(url_or_host: str) -> bool:
    """Нужно ли этому адресу расширенное доверие."""
    host = urlsplit(url_or_host).hostname if "//" in url_or_host else url_or_host
    return (host or "").lower() in _RU_GOV_HOSTS


@lru_cache(maxsize=1)
def ru_gov_ssl_context() -> ssl.SSLContext | None:
    """Контекст: штатное хранилище ПЛЮС центры Минцифры.

    `None`, если файлов сертификатов нет рядом — тогда вызывающий должен
    ходить как раньше и получить честный отказ проверки, а не тихо отключить
    её. Молчаливое `verify=False` здесь было бы хуже недоступности: мы бы
    принимали в финансовый выпуск числа, подлинность которых никто не
    подтвердил.
    """
    ctx = ssl.create_default_context()
    loaded = 0
    for name in _CERTS:
        path = _CERT_DIR / name
        if path.exists():
            ctx.load_verify_locations(cafile=str(path))
            loaded += 1
    return ctx if loaded == len(_CERTS) else None


def verify_for(url: str):
    """Значение параметра `verify=` для httpx под конкретный адрес.

    Возвращает контекст с центрами Минцифры для государственных адресов и
    `True` (штатная проверка) для всех остальных.
    """
    if is_ru_gov_host(url):
        ctx = ru_gov_ssl_context()
        if ctx is not None:
            return ctx
    return True
