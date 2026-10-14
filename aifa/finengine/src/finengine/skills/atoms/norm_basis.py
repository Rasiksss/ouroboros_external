"""`norm.basis` — карточка 16a: зафиксировать базис сопоставимости и не
позволить численному сравнению молча перепрыгнуть через разницу
стандарта/периметра/валюты/валового-чистого/ревизии.

Единственный реальный потребитель в этой сессии — `verify.cross` (карточка
`norm.basis`: «Как встроить... ref.prior, verify.cross, многодокументной
сверке и ref.peer»; `ref.prior`/`ref.peer` — Срез 6b/11, вне периметра).
Мостов сопоставимости (IFRS↔РСБУ и т.п.) в этой сессии НЕ заводится: карточка
прямо запрещает «автоматически переводить IFRS↔РСБУ без явной методики», а
такой методики/источника сегодня в репозитории нет — таблица мостов остаётся
пустой (`_KNOWN_BRIDGES = {}`), и это законное, не временное состояние,
подлежащее пополнению только вместе с реальной методикой перехода.
"""
from __future__ import annotations

from finengine.contracts import Basis, BasisComparison

# Пусто намеренно (см. докстринг модуля) — заполняется только вместе с
# документированной методикой перехода, не эвристикой этого атома.
_KNOWN_BRIDGES: dict[tuple[str, str], str] = {}

_AXES = ("standard", "perimeter", "currency", "gross_net", "revision")


def compare_basis(a: Basis, b: Basis) -> BasisComparison:
    """Совпадение всех ЗАДАННЫХ осей на обеих сторонах — `comparable`.
    Known bridge для конкретной пары различающихся значений — `adjusted`.
    Иначе, включая случай «ось задана только с одной стороны» (нельзя
    утверждать совпадение того, что не измерено), — `not_comparable`."""
    mismatches: list[tuple[str, str, str]] = []
    for axis in _AXES:
        va, vb = getattr(a, axis), getattr(b, axis)
        if va is None and vb is None:
            continue
        if va != vb:
            mismatches.append((axis, str(va), str(vb)))

    if not mismatches:
        return BasisComparison(status="comparable", reason="все заданные оси базиса совпадают")

    if len(mismatches) == 1:
        axis, va, vb = mismatches[0]
        bridge = _KNOWN_BRIDGES.get((f"{axis}:{va}", f"{axis}:{vb}")) or _KNOWN_BRIDGES.get(
            (f"{axis}:{vb}", f"{axis}:{va}")
        )
        if bridge is not None:
            return BasisComparison(status="adjusted", bridge_rule=bridge, reason=f"мост по оси {axis!r}: {bridge}")

    detail = "; ".join(f"{axis}: {va!r} != {vb!r}" for axis, va, vb in mismatches)
    return BasisComparison(status="not_comparable", reason=f"несовместимый базис — {detail}")


__all__ = ["compare_basis"]
