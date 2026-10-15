"""Общие опоры тестов временных атомов: контракт и код «маржа EBITDA», который читает
два показателя отчёта. Модуль лежит рядом с тестами (как `ingress_support.py` в
tests/integration): каталоги тестов не пакеты, поэтому импорт идёт по имени модуля."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from finengine.skills.spec.ephemeral import AtomRequest
from finengine.skills.spec.target import InputSpec

MARGIN_SCHEMA = {
    "type": "object", "required": ["margin_pct"],
    "properties": {"margin_pct": {"type": "number", "x-unit": "percent"}},
}
MARGIN_CODE = """
def run(inputs):
    by = {m["concept"]: m["scalar_fact"]["value"] for m in inputs["ExtractedMetric"]}
    return {"margin_pct": by["EBITDA"] / by["REVENUE"] * 100}
"""


def margin_request(**changes) -> AtomRequest:
    fields = dict(
        name="eph.ebitda_margin", purpose="маржа EBITDA", output_type="dyn:ebitda_margin",
        output_schema=MARGIN_SCHEMA,
        requires=(InputSpec("ExtractedMetric", "EBITDA"), InputSpec("ExtractedMetric", "REVENUE")),
    )
    fields.update(changes)
    return AtomRequest(**fields)


def assert_event_fixture(name: str, events: list[dict]) -> None:
    path = Path(__file__).parent / "fixtures" / "sse" / name
    rendered = json.dumps(events, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if os.getenv("AIFA_UPDATE_FIXTURES") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
    assert path.read_text(encoding="utf-8") == rendered


def normalized_events(events: list[dict]) -> list[dict]:
    """Убрать случайные id/время из полного потока, сохранив порядок и payload событий."""
    normalizer = _StableEventNormalizer()
    return [normalizer(event) for event in events]


class _StableEventNormalizer:
    def __init__(self) -> None:
        self.ids: dict[str, str] = {}

    def __call__(self, value):
        if isinstance(value, dict):
            return {
                key: self(item)
                for key, item in value.items()
                if key not in {"created_at", "finished_at", "node_run_id", "run_id", "started_at"}
            }
        if isinstance(value, list):
            return [self(item) for item in value]
        if isinstance(value, str):
            normalized = value.replace(str(Path.cwd()), "<workspace>")
            return re.sub(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                self._uuid,
                normalized,
                flags=re.IGNORECASE,
            )
        return value

    def _uuid(self, match: re.Match) -> str:
        value = match.group(0).lower()
        if value not in self.ids:
            self.ids[value] = f"<uuid-{len(self.ids) + 1}>"
        return self.ids[value]
