"""Ленты 8-K/6-K не молчат из-за нас (19.08.2026).

РЕВИЗИЯ ВСЕХ ИСТОЧНИКОВ по просьбе владельца «доделай что осталось». Из 181
активного источника 60 не дали ни записи за НЕДЕЛЮ, и десять из них — ленты
EDGAR по Alibaba, JD.com, Baidu, Grab, Sea, TSMC, ASML, PagSeguro, Spotify и
Klarna. Они возвращали HTTP 200, правильный CIK и НОЛЬ записей — всегда.

ПРИЧИНА. Мы спрашивали у них форму 8-K. Её подают эмитенты США; иностранные
частные эмитенты подают 6-K и 20-F. Запрос по 8-K для них отдаёт валидную
пустую ленту, и так навсегда. Замер после смены формы: записи у 10 из 10
(было 0 из 10).

🔴 ДВЕ МОИ ОШИБКИ ПО ДОРОГЕ, ОБЕ ЧУТЬ НЕ УЕХАВШИЕ В ОТЧЁТ.

Первая. Первый замер дал 403 на нашем User-Agent и 200 на другом — я почти
записал «SEC блокирует нас за неверный UA». Повторная проверка ЧЕТЫРЁХ разных
UA дала 200 у всех, включая «блокирующий»: 403 был следствием моего же залпа
минутой раньше. Одно совпадение — не причина.

Вторая. Решив, что дело в частоте, я поставил паузу и объявил её лечением.
Замер до и после: залпом содержимое у 29 лент из 39, с паузой у 27 — разницы
нет. Пауза оставлена, но по другому основанию (соблюдение опубликованного
предела SEC в 10 запросов/сек), и это прямо написано в коде.
"""
from __future__ import annotations

import inspect
import threading
import time

import yaml

from finengine.skills.news import config as ncfg
from finengine.skills.news.connectors import edgar

_FOREIGN = {"Alibaba", "JD.Com", "Baidu", "Grab", "Sea (+Shopee)", "TSMC",
            "ASML", "PagSeguro", "Spotify", "Klarna"}


def test_foreign_issuers_are_asked_for_the_form_they_actually_file():
    """Отрицательный контроль: убрал `edgar_form: 6-K` у иностранцев — краснеет.

    Именно этот запрос десять лент отдавали пустыми навсегда.
    """
    cfg = ncfg.load()
    by_name = {s.display_name.split(" (SEC")[0]: s
               for s in cfg.sources if s.connector == "edgar"}
    missing = []
    for name in _FOREIGN:
        src = by_name.get(name)
        if src is None:
            continue
        if "type=6-K" not in src.url:
            missing.append(name)
    assert not missing, (
        f"у иностранных эмитентов спрашивается не та форма: {sorted(missing)} — "
        "их ленты вернут пустоту навсегда")


def test_us_issuers_still_get_8k():
    """Обратная сторона: американским эмитентам 6-K не подходит — они его не
    подают. Форма по умолчанию должна остаться 8-K."""
    cfg = ncfg.load()
    us = [s for s in cfg.sources if s.connector == "edgar"
          and s.display_name.split(" (SEC")[0] in ("JP Morgan", "Apple", "Tesla")]
    assert us, "фикстура не нашла американских эмитентов"
    for s in us:
        assert "type=8-K" in s.url, f"{s.display_name}: форма подменена"


def test_the_form_is_data_not_code():
    """Форма живёт в companies.yaml рядом с CIK: добавить эмитента должен
    уметь тот, кто не открывает config.py."""
    raw = yaml.safe_load(
        __import__("pathlib").Path(
            "src/finengine/skills/news/companies.yaml").read_text(encoding="utf-8"))

    def walk(o, acc):
        if isinstance(o, dict):
            if "edgar_form" in o:
                acc.append(o["edgar_form"])
            for v in o.values():
                walk(v, acc)
        elif isinstance(o, list):
            for v in o:
                walk(v, acc)
        return acc

    forms = walk(raw, [])
    assert len(forms) == 10, f"ожидалось 10 записей edgar_form, найдено {len(forms)}"
    assert set(forms) == {"6-K"}


def test_the_pause_holds_across_threads():
    """Коннекторы синхронные и живут в РАЗНЫХ потоках (`asyncio.to_thread`),
    поэтому счётчик обязан быть потоковым. Замеряем выдержку, а не строку."""
    edgar._last_call = 0.0
    started = time.monotonic()
    threads = [threading.Thread(target=edgar._throttle) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    spent = time.monotonic() - started
    assert spent >= 4 * edgar._MIN_INTERVAL_S * 0.8, (
        f"пять обращений уложились в {spent:.2f}с — пауза не держится")


def test_the_pause_does_not_claim_a_benefit_it_lacks():
    """Сторож против моей же ошибки: обоснование паузы обязано называть
    предел SEC, а не выдуманное лечение пустых лент."""
    src = inspect.getsource(edgar)
    i = src.index("_MIN_INTERVAL_S = ")
    around = src[max(0, i - 1500):i]
    assert "29 лент из 39" in around, "исчез замер, показавший отсутствие пользы"
    assert "10/сек" in around, "исчезло настоящее основание — предел SEC"
