"""Читающий инструмент `aifa_atoms`: каталог атомов для агента (CONTRACTS.md §3.1, ADR-0064).

`list` — карточки без исходного кода, `get` — контракт, точная версия и исходный код одного атома. Вне очереди: ничего в
`LoopSession` не меняет. Доступен корню и подзадачам (планировщику, автору кода): каталог один.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from finengine.assistant import ouro_view
from finengine.assistant.loop_desk import LoopDesk
from finengine.assistant.loop_tools import no_loop
from finengine.assistant.ouro_view import Refusal
from finengine.assistant.tool_call import BoundCall
from finengine.config import settings
from finengine.skills.spec import atom_cards
from finengine.skills.spec.atom_cards import AtomCard, AtomSource
from finengine.skills.spec.atom_registry import AtomRegistry
from finengine.skills.spec.ephemeral import EphemeralAtom

ACTIONS = ("list", "get")
HEADER_NAMES = 20  # имён временных атомов в заголовке списка
KNOWN_ARGUMENTS = frozenset({"action", "name", "offset"})
HEADER = (
    "Здесь поставляемые атомы, которые можно ставить в шаги, и временные атомы этого Case (область в карточке). "
    "Гейты проверок и служебные атомы в шаги не включаются: планировщик достраивает их сам. "
    'Полный контракт и исходный код — aifa_atoms(action="get", name=…).'
)


class AtomsTool:
    name = "aifa_atoms"
    root_only = False

    def __init__(self, desk: LoopDesk) -> None:
        self._desk = desk

    async def run(self, call: BoundCall) -> str:
        problem = _problem_in(call.args)
        if problem:
            return ouro_view.refusal(Refusal.BAD_ARGUMENTS, problem)
        loop = await self._desk.open_loop_of(call)
        if loop is None:
            return no_loop()
        offset = call.args.get("offset", 0)
        limit = settings.ouroboros_result_limit
        # Снимок временных атомов берётся здесь, в потоке цикла событий: сдача атома меняет словарь сессии именно в нём,
        # а рабочий поток читал бы его на ходу.
        atoms = dict(loop.session.atoms)
        registry = loop.session.runtime.registry
        if call.args["action"] == "list":
            return await asyncio.to_thread(_listing, registry, atoms, offset, limit)
        return await asyncio.to_thread(_detail, registry, atoms, str(call.args["name"]).strip(), offset, limit)


def _listing(registry: AtomRegistry, atoms: Mapping[str, EphemeralAtom], offset: int, limit: int) -> str:
    all_cards = atom_cards.cards(registry, atoms)
    if offset and offset >= len(all_cards):
        return ouro_view.refusal(Refusal.BAD_ARGUMENTS, f"offset за пределами каталога: атомов {len(all_cards)}.")
    temporary = [card.name for card in all_cards if card.scope == atom_cards.TEMPORARY]
    shown = ", ".join(temporary[:HEADER_NAMES])
    if len(temporary) > HEADER_NAMES:
        shown += f" и ещё {len(temporary) - HEADER_NAMES}"
    header = f"{HEADER}\nВсего атомов: {len(all_cards)}; временных в этом Case: {shown or 'нет'}."
    entries = [(card.name, card.text()) for card in all_cards]
    return ouro_view.atoms_list(entries, offset=offset, header=header, limit=limit)


def _detail(
    registry: AtomRegistry, atoms: Mapping[str, EphemeralAtom], requested: str, offset: int, limit: int,
) -> str:
    name, _, version = (part.strip() for part in requested.partition("@"))
    all_cards = {card.name: card for card in atom_cards.cards(registry, atoms)}
    card = all_cards.get(name)
    if card is None:
        return ouro_view.refusal(
            Refusal.BAD_ARGUMENTS,
            f'Атома {name} в каталоге нет: перечень — aifa_atoms(action="list"); гейты и служебные атомы не показываются.',
        )
    if version and version != card.version:
        return ouro_view.refusal(
            Refusal.BAD_ARGUMENTS,
            f"Версия {version} атома {name} не актуальна: сейчас {card.ref}. Прежний код недоступен.",
        )
    source = atom_cards.source_of(registry, atoms, name)
    if source is None:  # карточка есть, а источника нет: каталог изменился между двумя чтениями
        return ouro_view.service_fault(f"атом {name} исчез из каталога")
    sections = _sections_of(atoms, card, source)
    total = sum(len(lines) for _, _, lines in sections)
    if offset and offset >= total:
        return ouro_view.refusal(Refusal.BAD_ARGUMENTS, f"offset за пределами ответа: строк {total}.")
    return ouro_view.atom_detail(
        card.ref, card.text(), source.helpers_text(), sections, name=name, offset=offset, limit=limit,
    )


def _sections_of(
    atoms: Mapping[str, EphemeralAtom], card: AtomCard, source: AtomSource,
) -> list[tuple[str, str, list[str]]]:
    """Читаемое частями: контракт временного атома целиком (у поставляемого он уже в карточке) и исходный код."""
    temporary = next((atom for atom in atoms.values() if atom.request.name == card.name), None)
    sections: list[tuple[str, str, list[str]]] = []
    if temporary is not None:
        sections.append(("Контракт", "", atom_cards.contract_of(temporary).splitlines()))
    sections.append(("Исходный код", "python", list(source.lines)))
    return sections


def _problem_in(args: dict[str, Any]) -> str:
    """Что не так с аргументами `aifa_atoms`; пусто — всё так."""
    extra = sorted(set(args) - KNOWN_ARGUMENTS)
    if extra:
        return f"Лишние аргументы: {', '.join(extra)}. Допустимы action, name, offset."
    action = args.get("action")
    if action not in ACTIONS:
        return 'action — "list" (карточки всех атомов) или "get" (контракт и исходный код одного).'
    name = args.get("name")
    if action == "get" and (not isinstance(name, str) or not name.strip()):
        return 'get требует name — имя атома из aifa_atoms(action="list").'
    if action == "list" and name:
        return "name нужен только для get."
    offset = args.get("offset", 0)
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        return "offset — целое число от 0: с какой записи list или строки исходника get продолжить."
    return ""
