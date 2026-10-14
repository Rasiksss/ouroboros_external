"""Область «запуск»: один сквозной прогон и версии, которые он замораживает."""
from __future__ import annotations

from datetime import datetime

from pydantic import Field

from .base import Contract, NonEmptyStr, new_id, utc_now
from .document import EntityIdentity
from .intent import ClassificationResult


class RunPassport(Contract):
    """Один запуск целиком: классификация зафиксирована здесь один раз
    (нерушимое решение №2) вместе с версиями реестра, методики, модели,
    инструкции и образа песочницы (A0), от которых зависело поведение атомов.

    `entity` (`id.entity`, Срез 1 B1) фиксируется в паспорте по той же логике,
    что и `classification` (`id.frame`): один раз на запуск, не переопределяется
    ниже по конвейеру.
    """

    run_id: str = Field(default_factory=new_id)
    goal_ir_id: NonEmptyStr
    classification: ClassificationResult
    entity: EntityIdentity
    registry_version: NonEmptyStr
    methodology_version: NonEmptyStr
    model_version: NonEmptyStr
    prompt_version: NonEmptyStr
    sandbox_image_version: NonEmptyStr
    created_at: datetime = Field(default_factory=utc_now)
