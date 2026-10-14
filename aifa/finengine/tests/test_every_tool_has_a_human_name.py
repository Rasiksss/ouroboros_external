"""У каждого инструмента есть человеческое название на экране (Б1, п.1).

ЖАЛОБА ВЛАДЕЛЬЦА 19.08: «имя инструмента `fe_registry_find` вклинивается в
середину предложения». Так и есть: хроника хода режется на такты в момент
вызова инструмента, между половинами фразы встаёт чип шага — и если подписи у
инструмента нет, в чипе стоит его техническое имя.

ПРИЧИНА — ДВА МЕСТА ДЛЯ ОДНОГО ЗНАНИЯ. Реестр тулов живёт в питоне
(`finengine.llm.tools.REGISTRY`), подписи — в тайпскрипте
(`web/src/lib/tool-labels.ts`). Заводится тул в одном месте, подпись во втором
не заводится, и узнаём мы об этом от пользователя. На 19.08 без подписи
остались четыре: `fe_calc`, `fe_registry_find`, `fe_methodology`,
`fe_news_lookup` — то есть КАЖДЫЙ тул, заведённый после сноса монолитов.

ОБРАТНОЕ НЕ ПРОВЕРЯЕТСЯ НАМЕРЕННО: подписи снесённых `fe_extract_metrics` и
`fe_analyze` должны остаться — по ним рисуется история старых диалогов.

ПРОВЕРКА СТОРОЖА ОТКАТОМ: убрать строку `fe_calc` из `tool-labels.ts` — тест
обязан покраснеть и назвать её.
"""
from __future__ import annotations

import re
from pathlib import Path

from finengine.llm.tools import REGISTRY

LABELS = Path(__file__).resolve().parents[2] / "web" / "src" / "lib" / "tool-labels.ts"


def _labelled() -> set[str]:
    body = LABELS.read_text(encoding="utf-8")
    table = body.split("const LABELS", 1)[1].split("\n};", 1)[0]
    return set(re.findall(r"^\s{2}([a-z_][a-z0-9_]*)\s*:", table, re.M))


def test_every_registered_tool_has_a_label():
    missing = sorted(set(REGISTRY) - _labelled())
    assert not missing, (
        f"инструменты без подписи: {missing}. Пользователь увидит техническое "
        f"имя посреди фразы агента — ровно жалоба 19.08 про fe_registry_find")


def test_the_labels_of_demolished_tools_are_kept_for_history():
    kept = _labelled()
    assert {"fe_extract_metrics", "fe_analyze"} <= kept, (
        "подписи снесённых монолитов удалены — старые диалоги начнут показывать "
        "технические имена там, где раньше были человеческие")


def test_the_table_is_actually_parsed():
    """Разбор файла не должен молча возвращать пустоту — иначе сторож пустой."""
    assert len(_labelled()) >= 20, (
        f"из tool-labels.ts разобрано {len(_labelled())} подписей — похоже, "
        f"формат файла изменился и проверка перестала что-либо видеть")
