"""Область «периоды»: ось периода и информация о ревизии/пересчёте."""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import model_validator

from .base import Contract, NonEmptyStr

# Закрытый список (карточка `id.revision`, Срез 8/B9): "original" — подтверждённый
# базовый конец отношения (supersedes=None), "restated"/"reissued" — новая
# версия, доказанно связанная с исходной. Ни один producer до `id.revision`
# это поле не заполнял (см. `reference.py`/`ReferenceValue.revision` — «сегодня
# ни один producer её не прикрепляет»), поэтому сужение до Literal здесь не
# ломает существующих потребителей.
RevisionKind = Literal["original", "restated", "reissued"]


class PeriodAxis(Contract):
    """Момент (`instant`) или промежуток (`duration`) времени, к которому привязан факт."""

    period_id: NonEmptyStr
    kind: NonEmptyStr
    start: date | None = None
    end: date
    fiscal_year_end: str | None = None
    comparative_of: str | None = None

    @model_validator(mode="after")
    def _end_not_before_start(self):
        if self.start is not None and self.end < self.start:
            raise ValueError(f"end {self.end} is before start {self.start}")
        return self


class RevisionInfo(Contract):
    """Ревизия документа: оригинал, пересчёт или исправление, со связью на предыдущую.

    `supersedes` — `document_id` документа, который эта ревизия заменяет для
    представления (не уничтожает: обе версии остаются адресуемыми доказательствами,
    карточка `id.revision`, Срез 8/B9). `None` у `revision_kind="original"` —
    подтверждённый базовый конец отношения; `None` у `"restated"`/`"reissued"` —
    отношение установлено, но конкретный предшественник не идентифицирован."""

    revision_id: NonEmptyStr
    document_id: NonEmptyStr
    revision_kind: RevisionKind
    supersedes: str | None = None
    effective_date: date | None = None
