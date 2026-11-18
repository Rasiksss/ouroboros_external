"""Область «базис»: сопоставимость двух чисел одного концепта (карточка
`norm.basis`, 16a). Стандарт/периметр/валюта/валовое-чистое/ревизия — не
одно измерение, поэтому `Basis` несёт их порознь; совпадение всех заданных
осей — единственное, что даёт `comparable` без явного моста.
"""
from __future__ import annotations

from typing import Literal

from .base import Contract, NonEmptyStr


class Basis(Contract):
    """Один срез базиса, к которому привязан факт. Поле не заданное (`None`)
    значит «эта ось не измерена этим производителем», а не «любая ось
    подходит» — `compare_basis` (`skills/atoms/norm_basis.py`) трактует
    отсутствие оси у ОБЕИХ сторон как совпадение по этой оси, но отсутствие
    только у одной стороны — как невозможность утверждать совпадение."""

    standard: str | None = None  # "ifrs" | "rsbu" | "us_gaap" | ...
    perimeter: str | None = None  # "consolidated" | "separate" | "group" | "parent"
    currency: str | None = None
    gross_net: str | None = None  # "gross" | "net"
    revision: str | None = None  # "original" | "restated"


BasisComparisonStatus = Literal["comparable", "adjusted", "not_comparable"]


class BasisComparison(Contract):
    """Результат сравнения двух `Basis` (used by `verify.cross`). Не
    `EvidenceRecord` — вычисляемый результат чистой функции, не независимое
    доказательство со своим производителем/версией; сторона, которой он
    нужен как finding (`verify.cross`), заворачивает его в
    `VerificationFinding.detail`, а не хранит отдельно."""

    status: BasisComparisonStatus
    bridge_rule: NonEmptyStr | None = None
    reason: str = ""


__all__ = ["Basis", "BasisComparisonStatus", "BasisComparison"]
