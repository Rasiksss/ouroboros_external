"""Мост станции обязан принимать всё, что принимает `openrouter`.

ЗАЧЕМ. `station_bridge` объявлен «drop-in» заменой сетевого клиента модели:
`install_if_sealed()` подменяет функции прямо в модуле `openrouter`, и весь
движок, написанный под сетевую сигнатуру, продолжает звать те же имена — но
уже в запечатанной станции. Пока сигнатуры совпадают, подмена незаметна.

Совпадение НИКТО НЕ ПРОВЕРЯЛ, и 17.08.2026 это стоило владельцу сорванного
прогона: при переносе двухстадийного извлечения (#257) в `openrouter` добавился
параметр `omit_max_output`, в мост — нет. Локально всё зелено (тесты и бенчмарк
идут сетевым путём), а живой ход в станции падал на
`complete_with_usage() got an unexpected keyword argument 'omit_max_output'`.
Ровно болезнь «два места»: правку сделали в одном, второе о ней не узнало.

КАК. Список подменяемых имён берётся ИЗ САМОЙ `install_if_sealed` разбором
AST, а не переписывается сюда руками: появится пятая подмена — она попадёт
под проверку сама, без правки этого файла. Копия списка была бы третьим местом.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from finengine.llm import openrouter, station_bridge

_SRC = Path(station_bridge.__file__)


def _installed_pairs() -> list[tuple[str, str]]:
    """Пары (имя в openrouter, имя в мосте) из тела `install_if_sealed`."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "install_if_sealed"
    )
    pairs: list[tuple[str, str]] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign):
            continue
        target = node.targets[0]
        if (isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and isinstance(node.value, ast.Name)):
            pairs.append((target.attr, node.value.id))
    return pairs


def test_install_replaces_something():
    """Отрицательный контроль к разбору AST: пустой список означал бы, что
    проверка ниже проходит вхолостую."""
    assert len(_installed_pairs()) >= 4, "разбор install_if_sealed ничего не нашёл"


@pytest.mark.parametrize(("net_name", "bridge_name"), _installed_pairs(),
                         ids=[p[0] for p in _installed_pairs()])
def test_bridge_accepts_every_network_parameter(net_name, bridge_name):
    """Каждый параметр сетевой функции обязан быть у мостовой — иначе вызов,
    законный на сетевом пути, падает в станции TypeError'ом."""
    net = inspect.signature(getattr(openrouter, net_name)).parameters
    bridge = inspect.signature(getattr(station_bridge, bridge_name)).parameters
    missing = [n for n in net if n not in bridge]
    assert not missing, (
        f"{net_name}: мост не принимает {missing} — вызов из станции упадёт")
    for name, p in net.items():
        assert bridge[name].kind == p.kind, (
            f"{net_name}.{name}: у моста другой род параметра "
            f"({bridge[name].kind} вместо {p.kind}) — вызов по позиции разъедется")


@pytest.mark.parametrize(("net_name", "bridge_name"), _installed_pairs(),
                         ids=[p[0] for p in _installed_pairs()])
def test_bridge_demands_nothing_extra(net_name, bridge_name):
    """Обратная сторона: мост не вправе требовать своих обязательных
    параметров — их вызывающий код не передаст, он о мосте не знает."""
    net = inspect.signature(getattr(openrouter, net_name)).parameters
    bridge = inspect.signature(getattr(station_bridge, bridge_name)).parameters
    extra_required = [
        n for n, p in bridge.items()
        if n not in net and p.default is inspect.Parameter.empty
        and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
    ]
    assert not extra_required, f"{bridge_name}: мост требует лишнее {extra_required}"


# ───────── контракт «потолка нет» доезжает до демона ─────────
# Сигнатуры мало: параметр можно принять и молча проигнорировать, и тогда
# стадия сжатия снова получит потолок. Проверяем, что именно уходит в файл
# запроса, который читает демон.


def _request_written(monkeypatch, tmp_path, **kwargs) -> dict:
    """Тело запроса, которое мост положил бы демону (демон не поднимается)."""
    import asyncio
    import json

    monkeypatch.setenv("FE_RPC_DIR", str(tmp_path))
    written: list[dict] = []

    def _fake_write(path: str, data: str) -> None:
        written.append(json.loads(data))
        raise TimeoutError("дальше не идём: демона в тесте нет")

    monkeypatch.setattr(station_bridge, "_write_atomic", _fake_write)
    with pytest.raises(TimeoutError):
        asyncio.run(station_bridge.complete_with_usage(prompt="x", retries=1, **kwargs))
    assert written, "мост не записал запрос"
    return written[0]


def test_omit_max_output_sends_explicit_null(monkeypatch, tmp_path):
    """`None` в ключе — единственный способ сказать демону «потолка нет»:
    отсутствие ключа он трактует как «поставь дефолт 16000»."""
    req = _request_written(monkeypatch, tmp_path, omit_max_output=True)
    assert req["max_completion_tokens"] is None


def test_default_still_carries_a_ceiling(monkeypatch, tmp_path):
    """Отрицательный контроль: без флага потолок обязан остаться, иначе
    правка сняла бы его со всех вызовов движка разом."""
    req = _request_written(monkeypatch, tmp_path)
    assert req["max_completion_tokens"] == station_bridge._MAX_COMPLETION_TOKENS
