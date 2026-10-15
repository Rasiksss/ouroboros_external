"""Доверие центру Минцифры включается ТОЧЕЧНО, а не для всего трафика.

ЗАЧЕМ ПРАВКА. `rosstat.gov.ru` отдаёт данные (794 КБ проверено из прод-контейнера),
но его сертификат выдан центром Минцифры, которого нет в штатном хранилище.
Из-за этого 5–7 российских макропоказателей были недоступны.

ЗАЧЕМ ИМЕННО ТОЧЕЧНО — ЭТО ГЛАВНОЕ СВОЙСТВО. Установка центра в общее
хранилище означала бы, что он может поручиться за ЛЮБОЙ домен: openrouter.ai,
api.tavily.com — туда уходят наши ключи и документы пользователей. Выигрыш —
несколько государственных сайтов, цена — поверхность для перехвата всего
нашего трафика. Поэтому список адресов закрытый, а сторож следит, чтобы он не
разросся молча.

⚠️ ЧЕГО ЭТИ СТОРОЖА НЕ ЛОВЯТ (дописано ревизией 21.08.2026). Четыре из шести
ИСПОЛНЯЮТ код (`verify_for`, `is_ru_gov_host`, `ru_gov_ssl_context`) — они
настоящие. Два остальных исполнить нечем, и вот их граница:
  * `test_missing_certificates_do_not_silently_disable_verification` читает
    ДЕРЕВО РАЗБОРА этого модуля. Ослабление проверки в СОСЕДНЕМ модуле — в
    сборщике лент, в клиенте модели, где угодно — он не увидит вовсе;
  * `test_certificates_are_copied_into_the_runtime_image` ищет строку `COPY`
    в Dockerfile. Он доказывает НАМЕРЕНИЕ скопировать, а не факт: собранный
    образ он не открывает, каталог `deploy/certs/` на непустоту не проверяет
    и просроченный сертификат внутри него принимает молча.
"""
from __future__ import annotations

import pathlib
import ssl

import pytest

from finengine.net import ru_gov_tls as t


def test_context_is_built_from_shipped_certificates():
    """Отрицательный контроль: убрал файл сертификата — контекст становится
    None, и вызывающий получает честный отказ проверки."""
    assert t.ru_gov_ssl_context() is not None, "сертификаты не доехали в сборку"
    assert isinstance(t.ru_gov_ssl_context(), ssl.SSLContext)


@pytest.mark.parametrize("url", [
    "https://rosstat.gov.ru/opendata",
    "https://www.rosstat.gov.ru/",
    "rosstat.gov.ru",
])
def test_state_sites_get_the_extra_trust(url):
    assert t.is_ru_gov_host(url)
    assert t.verify_for(url) is not True


@pytest.mark.parametrize("url", [
    "https://api.tavily.com/search",
    "https://openrouter.ai/api/v1/chat/completions",
    "https://api.stlouisfed.org/fred/series",
    "https://news.google.com/rss",
    "https://rosstat.gov.ru.evil.example/",   # похожий, но чужой домен
])
def test_everything_else_keeps_the_standard_check(url):
    """ГЛАВНАЯ ПРОВЕРКА. Сюда уходят ключи и документы — расширенное доверие
    тут недопустимо. Последний случай отдельно: хитрый домен, который лишь
    НАЧИНАЕТСЯ как государственный, доверия получать не должен."""
    assert t.verify_for(url) is True, f"{url} получил расширенное доверие"


def test_host_list_stays_short_and_explicit():
    """Список — не место для роста «на всякий случай»: каждый адрес расширяет
    то, за что государственный центр ручается внутри нашего процесса."""
    assert len(t._RU_GOV_HOSTS) <= 4, "список адресов разросся — пересмотреть осознанно"
    assert all(h.endswith(".gov.ru") for h in t._RU_GOV_HOSTS)


def test_missing_certificates_do_not_silently_disable_verification():
    """Если файлов нет — возвращаем штатную проверку, а НЕ `verify=False`.
    Молчаливое отключение было бы хуже недоступности: мы бы принимали в
    финансовый выпуск числа, подлинность которых никто не подтвердил.

    Проверяем ИСПОЛНЯЕМЫЙ КОД разбором дерева, а не текст файла: первая
    редакция этого сторожа искала подстроку и краснела на собственном
    комментарии, где объясняется, почему мы так НЕ делаем.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(t))
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "verify":
            assert not (isinstance(node.value, ast.Constant)
                        and node.value.value is False), "проверка отключается в коде"
        if isinstance(node, ast.Attribute) and node.attr in ("CERT_NONE", "CERT_OPTIONAL"):
            raise AssertionError(f"в коде ослабляется проверка: {node.attr}")
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if getattr(tgt, "attr", "") == "check_hostname":
                    assert not (isinstance(node.value, ast.Constant)
                                and node.value.value is False), "снята проверка имени хоста"


def test_certificates_are_copied_into_the_runtime_image():
    """СТОРОЖ НА ДОСТАВКУ, а не на наличие файлов рядом.

    Первая редакция правки прошла все проверки локально — файлы лежали рядом с
    модулем — и не заработала на проде: `Dockerfile.backend` копирует
    `deploy/` поимённо (чтобы секреты не утекли в образ), и каталог с
    сертификатами туда не попал. В контейнере `_CERT_DIR.exists()` было False,
    контекст не собирался, Росстат остался закрыт.

    Ровно та же «недоставка», что стоила нам #252 и #253: объявлено, проверено,
    но до места работы не доехало.
    """
    root = pathlib.Path(__file__).resolve().parents[1]
    df = (root / "deploy" / "Dockerfile.backend").read_text(encoding="utf-8")
    assert "COPY deploy/certs/" in df, (
        "сертификаты не копируются в образ — на стенде Росстат останется закрыт")
