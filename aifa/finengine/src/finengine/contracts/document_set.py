"""Область «набор документов»: `DocumentSet` (Срез 8, B9, docs/build-plan/
18-srez-08-struktury-i-neskolko-dokumentov.md, атом `set.assemble`).

Группировка идёт по подтверждённой сущности/периоду/типу документа, не по
имени файла или порядку прикрепления (карточка `set.assemble`). `DocumentSet`
— планировочный артефакт без измерения "какой концепт" (как `EvidenceBundleV2`/
`FormOutput`), не `EvidenceRecord`: это агрегат уже произведённых доказательств
(`EntityIdentity`/`RevisionInfo` каждого документа), а не новое независимое
доказательство со своим atom-производителем на уровне ОДНОГО факта.

`DocumentCandidate` — единственный вход `set.assemble`: то, что уже установил
СОБСТВЕННЫЙ однодокументный прогон этого документа (`id.entity` → `EntityIdentity`,
детектор периода, `id.revision` → `RevisionInfo | None`). `set.assemble` группирует
и отклоняет кандидатов по этим уже готовым полям — он не читает текст документа
заново и не переопределяет чужую идентичность (нерушимое решение №2 — повторное
определение допускается только как диагностика расхождения, не как рабочий путь).
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from .base import Contract, NonEmptyStr, new_id
from .document import EntityIdentity
from .periods import RevisionInfo

# Два реально производимых `set.assemble` исхода отказа (карточка, краевые
# случаи): документ другого подтверждённого эмитента ("entity_mismatch") и
# документ, чья идентичность недостаточно уверена, чтобы безопасно решить,
# принадлежит ли он набору ("unknown_identity"). Третий исход не заводится
# без реального producer'а, который бы его возвращал (CLAUDE.md §2 — не
# валидировать сценарии, которых нет).
DocumentSetRejectionReason = Literal["entity_mismatch", "unknown_identity"]


class DocumentCandidate(Contract):
    """Один физический документ-кандидат для `set.assemble` — вход, не выход:
    уже установленная (не заново читаемая) идентичность/период/тип/ревизия
    этого документа из его собственного однодокументного прогона."""

    document_id: NonEmptyStr
    document_version: NonEmptyStr
    entity: EntityIdentity
    period_ref: str | None = None
    doc_type: NonEmptyStr = "report"
    revision: RevisionInfo | None = None


class DocumentSetMember(Contract):
    """Один документ, принятый в набор — плоская проекция `DocumentCandidate`
    после прохождения проверки сущности (не хранит `EntityIdentity` целиком:
    `entity_id`/`canonical_name` уже всё, что нужно потребителю набора)."""

    document_id: NonEmptyStr
    document_version: NonEmptyStr
    entity_id: NonEmptyStr
    canonical_name: NonEmptyStr
    period_ref: str | None = None
    doc_type: NonEmptyStr = "report"
    revision: RevisionInfo | None = None


class DocumentSetRejection(Contract):
    """Кандидат, НЕ присоединённый к набору — явная запись, не молчаливый
    пропуск (карточка: «документ другого эмитента... не присоединяется
    молча»). Отсутствие записи здесь для отклонённого документа было бы
    самой ошибкой, которую эта карточка запрещает."""

    document_id: NonEmptyStr
    document_version: NonEmptyStr
    reason: DocumentSetRejectionReason
    detail: str = ""


class DocumentSet(Contract):
    """Доменный набор документов одной многодокументной задачи (B9). Полнота
    (`completeness`/`missing`) — функция ТОЛЬКО от `required_entities`/
    `required_doc_types`/`required_period_count`, переданных вызывающим
    (`Target`/`GoalType` через `set.assemble`) — не хардкод «если два документа»
    внутри этого контракта или его producer'а."""

    set_id: str = Field(default_factory=new_id)
    run_id: NonEmptyStr
    members: list[DocumentSetMember] = Field(default_factory=list)
    rejected: list[DocumentSetRejection] = Field(default_factory=list)
    required_entities: int = Field(default=1, ge=1)
    completeness: Literal["complete", "incomplete"] = "incomplete"
    missing: list[str] = Field(default_factory=list)


__all__ = [
    "DocumentSetRejectionReason",
    "DocumentCandidate",
    "DocumentSetMember",
    "DocumentSetRejection",
    "DocumentSet",
]
