"""C13 (откат) — DocumentMeta.sha256/acquisition (Срез 7) аддитивны.

`DOCUMENT.meta` — JSON-колонка, хранящая `DocumentMeta.model_dump_json()`
целиком (`store/documents.py::_to_meta`). Новые поля с дефолтами обязаны
читаться из СТАРОЙ строки (откат кода при новой БД — новые поля просто не
появятся в объекте) и СТАРЫЙ код обязан не упасть на НОВОЙ строке — Pydantic
по умолчанию игнорирует лишние ключи, но это стоит проверить явно, а не
полагаться на память о поведении библиотеки."""
from __future__ import annotations

import json

from finengine.models.document import DocumentMeta


def test_legacy_json_without_sha256_or_acquisition_still_parses():
    """Строка, записанная ДО этого среза — ни `sha256`, ни `acquisition` в
    JSON нет вовсе (не просто пустые — поля физически отсутствовали)."""
    legacy = {
        "doc_id": "d1", "filename": "report.pdf", "company_name": "Северсталь",
        "period": "Q1 2025", "owner_id": 7,
    }
    meta = DocumentMeta(**legacy)
    assert meta.sha256 == ""
    assert meta.acquisition == {}


def test_new_fields_round_trip_through_json():
    meta = DocumentMeta(
        doc_id="d2", filename="report.pdf", owner_id=1,
        sha256="a" * 64, acquisition={"source_url": "https://www.cbr.ru/r.pdf", "source_tier": "official_regulator"},
    )
    restored = DocumentMeta(**json.loads(meta.model_dump_json()))
    assert restored.sha256 == meta.sha256
    assert restored.acquisition == meta.acquisition


def test_acquisition_field_does_not_collide_with_meta_card():
    """meta_card — своя, уже боевая семантика (#189: doc_kind/issuer/period);
    acquisition — новая. Регресс здесь означал бы, что кто-то по ошибке начал
    писать происхождение приёма в `meta_card` и тем самым тихо отключил
    построение настоящей метакарты (`if not meta.meta_card: build_card(...)`
    в `api/main.py::_parse_doc_in_background`)."""
    meta = DocumentMeta(doc_id="d3", filename="x.pdf", owner_id=1,
                         acquisition={"source_url": "https://www.cbr.ru/r.pdf"})
    assert meta.meta_card == {}
    assert "acquisition" not in meta.meta_card
