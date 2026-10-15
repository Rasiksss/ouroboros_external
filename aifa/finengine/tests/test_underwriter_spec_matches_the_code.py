"""Спека андеррайтера (#158) не расходится с кодом, на который ссылается.

ЗАЧЕМ СТОРОЖ НА ДОКУМЕНТ. Это не проза для чтения, а документ, по которому
владелец принимает решение о работе: какие показатели уже есть, каких не хватает,
что заводить. Его утверждения — проверяемые, и проверять их обязан не человек.

Первая редакция §8 была посчитана разбором, который молча пропустил 55 файлов
эталона из 86 (они через ТОЧКУ С ЗАПЯТОЙ), и не совпала НИ ОДНА строка таблицы,
а один вывод был прямо ложным: «показателя нет в русской разметке» при том, что
он там есть с цитатой из отчёта. По этому выводу владельцу предлагалось оплатить
замер уже размеченного. Такую ошибку невозможно заметить чтением — только
прогоном.

ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ: enum'ы, названные существующими, существуют; enum'ы,
предложенные как новые, действительно новые; числа §8 сходятся с корпусом при
ЧЕСТНОМ разборе. Формулировки и выводы — не проверяются: это работа человека.

ЧЕГО НЕ ЛОВИТ (ревизия 21.08.2026, решение «О»). Половина утверждений считается
по корпусу и реестру — это исполнение; но сверка с документом идёт ПО ФОРМЕ
СТРОКИ: `| \\`ENUM\\` | N/111 |`. Переверстают таблицу §8 (другой разделитель,
перенос, добавленная колонка) — сторож покраснеет, хотя ни одно утверждение
документа не изменилось. И наоборот: он молчит обо всём, чего в §8 нет вовсе —
пропущенный показатель здесь не отличим от отсутствующего. Исполнить «документ
не врёт владельцу» иначе нечем: предмет охраны — сам документ.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SPEC = REPO / "docs" / "UNDERWRITER_RISK_SKILL.md"
GOLDEN = REPO / "benchmarks" / "vendor" / "tests" / "test_benchmark" / "data" / "golden"

pytestmark = pytest.mark.skipif(not SPEC.is_file(), reason="спека не заведена")


def _text() -> str:
    return SPEC.read_text(encoding="utf-8")


def _golden_rows():
    """Строки эталона с ЧЕСТНЫМ разбором: разделитель определяется по заголовку.

    Ровно эта строчка и была пропущена в первой редакции — 55 файлов из 86
    разделены точкой с запятой, и «всё через запятую» их не видит.
    """
    for f in sorted(GOLDEN.rglob("*.csv")):
        head = f.open(encoding="utf-8-sig").readline()
        delim = ";" if head.count(";") > head.count(",") else ","
        with f.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh, delimiter=delim):
                yield f.relative_to(GOLDEN).parts[0], row


def test_the_corpus_really_is_mixed_delimiter():
    """Предпосылка всей проверки. Если корпус когда-нибудь приведут к одному
    разделителю, честный разбор останется верным, а этот тест напомнит, что
    предупреждение в §8 пора снять."""
    semi = sum(1 for f in GOLDEN.rglob("*.csv")
               if ";" in f.open(encoding="utf-8-sig").readline())
    total = sum(1 for _ in GOLDEN.rglob("*.csv"))
    assert total, "золотой набор не найден — проверки ниже зелены на пустом месте"
    assert semi, ("в корпусе больше нет точки с запятой — предупреждение в §8 "
                  "устарело, снимите его")


# Перемерено 15.08.2026 после переноса РСБУ: корпус вырос с 86 CSV / 85
# документов до 112 / 111. Выросли все строки, кроме FUTURE_CAPEX_LIABILITIES —
# в РСБУ-разметке этого показателя нет.
CORPUS_DOCS = 111


@pytest.mark.parametrize("enum,expected", [
    ("MODIFIED_AUDITORS_OPINION", 92),
    ("INTEREST_PAID", 59),
    ("LONG_TERM_DEBT", 46),
    ("SHORT_TERM_BORROWINGS", 46),
    ("DOUBTFUL_ACCOUNTS_ALLOWANCE", 25),
    ("COMMITTED_CREDIT_FACILITIES", 16),
    ("GUARANTEES", 14),
    ("FUTURE_CAPEX_LIABILITIES", 9),
])
def test_the_coverage_table_matches_the_corpus(enum, expected):
    """Каждая строка таблицы §8 — пересчитывается, а не переписывается."""
    docs = {r.get("doc_name") for cell, r in _golden_rows()
            if (r.get("enum") or "").strip() == enum}
    assert len(docs) == expected, (
        f"§8 обещает {expected} документов по {enum}, в корпусе {len(docs)} — "
        f"таблица разошлась с эталоном")
    assert f"| `{enum}` | {expected}/{CORPUS_DOCS} |" in _text(), (
        f"строка про {enum} в §8 не найдена в обещанном виде")


def test_the_retracted_claim_stays_retracted():
    """Показатель ЕСТЬ в русской ячейке. Утверждение обратного стоило бы
    владельцу оплаченного замера уже размеченного."""
    in_ru = {r.get("doc_name") for cell, r in _golden_rows()
             if cell == "ifrs_nf_ru"
             and (r.get("enum") or "").strip() == "COMMITTED_CREDIT_FACILITIES"}
    assert in_ru, "показателя не стало в русской ячейке — перечитайте §3.5 и §8"
    assert "не найден ни в одном русскоязычном" not in _text(), (
        "отменённое утверждение вернулось в документ")


def _spec_enums(section_head: str) -> set[str]:
    """enum'ы, названные в разделе спеки (ВЕРХНИЙ_РЕГИСТР в обратных кавычках)."""
    body = _text().split(section_head, 1)[-1]
    return set(re.findall(r"`([A-Z][A-Z0-9_]{3,})`", body))


# Токены §2, которые НЕ являются показателями реестра, — перечислены осознанно.
# Список закрытый: иначе опечатка в имени показателя молча попадёт сюда же и
# перестанет проверяться. Ровно так первая версия этого сторожа и проглядела
# переименование `TOTAL_DEBT` в несуществующий enum — оно просто выпадало из
# проверяемого набора вместо того, чтобы покраснеть.
_NOT_REGISTRY_ENUMS = {
    # Псевдо-показатели роли «Риски»: подпись «LTM» подставляется форматтером
    # (roles/formatter.py), в реестре их нет и быть не должно — про это §2.6.
    "SALES_REVENUE_LTM", "EBITDA_LTM", "NET_INCOME_LTM",
    # Величина, предлагаемая к заведению — она и должна отсутствовать (§3).
    "NET_DEBT_FOR_RISKS",
}


def test_every_metric_the_spec_names_as_existing_really_exists():
    """Раздел «что умеем сегодня» не смеет называть несуществующий показатель.

    Проверяются ВСЕ имена в разделе, а не заранее выбранный список: иначе
    опечатка или переименование просто выпадают из проверки, и документ тихо
    начинает обещать то, чего нет.
    """
    import sys

    sys.path.insert(0, str(REPO / "src"))
    from finengine.skills.metric_schema import registry_for_profile

    reg = registry_for_profile("ifrs_nf_ru")
    known = {m.get("enum") for m in reg["extractable"]} | {
        row[0] for row in reg["calculated"]}

    body = _text().split("## 2.", 1)[-1].split("## 3.", 1)[0]
    named = set(re.findall(r"`([A-Z][A-Z0-9_]{3,})`", body))
    assert len(named) > 20, "раздел «что умеем» опустел — перечитайте документ"

    unknown = sorted(named - known - _NOT_REGISTRY_ENUMS)
    assert not unknown, (
        f"§2 называет показатели, которых нет в ячейке ifrs_nf_ru: {unknown}. "
        f"Либо опечатка, либо им место в §3 «чего не хватает».")

    stale = sorted(_NOT_REGISTRY_ENUMS & known)
    assert not stale, (
        f"{stale} завелись в реестре — вычеркните их из _NOT_REGISTRY_ENUMS, "
        f"иначе исключение начнёт прикрывать настоящую ошибку")


def test_metrics_the_spec_proposes_are_not_already_there():
    """Предложить завести то, что уже заведено, — это работа, оплаченная зря."""
    import sys

    sys.path.insert(0, str(REPO / "src"))
    from finengine.skills.metric_schema import registry_for_profile

    reg = registry_for_profile("ifrs_nf_ru")
    known = {m.get("enum") for m in reg["extractable"]}
    body = _text().split("### Ш3.", 1)[-1].split("### Ш4.", 1)[0]
    # Зачёркнутое (~~…~~) — отменённое утверждение, а не предложение завести.
    body = re.sub(r"~~.*?~~", "", body, flags=re.S)
    proposed = set(re.findall(r"`([A-Z][A-Z0-9_]{3,})`", body))
    already = sorted(proposed & known)
    assert not already, (
        f"Ш3 предлагает завести то, что уже есть в ячейке: {already}")
