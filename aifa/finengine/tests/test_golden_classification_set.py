"""C0.1: `tests/golden/classification/` — не менее 50 реальных отчётов,
все действующие профили покрыты, плюс набор неоднозначных случаев.

Каждый `excerpt` — реальный текст, а не выдумка (см. README.md: способ
подтверждения — независимая идентичность компании, не подгонка под
классификатор). Этот тест повторно прогоняет живой `detect_profile()` на
сохранённом тексте — если классификатор изменится и разойдётся с
`cross_check`, тест это заметит.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from finengine.skills.extractors._classifier import PROFILE_IDS, detect_profile

ROOT = Path(__file__).resolve().parent / "golden" / "classification"
CASES = json.loads((ROOT / "cases.json").read_text(encoding="utf-8"))
AMBIGUOUS_PATH = ROOT / "ambiguous_cases.json"
AMBIGUOUS = json.loads(AMBIGUOUS_PATH.read_text(encoding="utf-8")) if AMBIGUOUS_PATH.exists() else []


def test_at_least_fifty_confident_cases():
    assert len(CASES) >= 50


def test_every_active_profile_is_covered():
    counts = Counter(c["profile_id"] for c in CASES)
    missing = set(PROFILE_IDS) - set(counts)
    assert not missing, f"нет ни одного примера для профилей: {missing}"


def test_every_case_has_a_real_source_and_a_substantial_excerpt():
    for c in CASES:
        assert c["source_url"].startswith("http"), f"{c['id']}: source_url не похож на реальный URL"
        assert len(c["excerpt"]) >= 500, f"{c['id']}: excerpt подозрительно короткий для реального отчёта"


def test_stored_excerpt_still_reclassifies_as_recorded():
    """Перепрогон живого классификатора на сохранённом тексте — не только чтение поля."""
    mismatches = []
    for c in CASES:
        profile = detect_profile(c["excerpt"])
        if profile.profile_id != c["profile_id"]:
            mismatches.append((c["id"], c["company"], profile.profile_id, c["profile_id"]))
    assert not mismatches, f"классификатор разошёлся с зафиксированной правдой: {mismatches}"


def test_ids_are_unique():
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids))


def test_ambiguous_set_exists_and_is_genuinely_ambiguous():
    assert len(AMBIGUOUS) >= 3, "нужен отдельный набор неоднозначных случаев (C0.1)"
    for c in AMBIGUOUS:
        profile = detect_profile(c["excerpt"])
        genuinely_ambiguous = profile.confidence_low or profile.profile_id != c.get("plausible_profile_id")
        assert genuinely_ambiguous, (
            f"{c['id']}: классификатор уверенно и правильно определил профиль — "
            "это не неоднозначный случай, а обычный"
        )
