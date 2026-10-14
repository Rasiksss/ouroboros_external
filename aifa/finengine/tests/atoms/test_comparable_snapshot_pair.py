"""MONITOR_CHANGES: какие два снимка субъекта считаются сравнимыми.

Решение владельца (Срез 30, вариант A): сравнимы только снимки с разными
`document_version`/`period_refs` одного `subject_key`; повторные прогоны того
же документа и периода пропускаются. Причина: `SubjectProfile` дописывается
после КАЖДОГО успешного исполнения без дедупликации, и на dev у одного субъекта
за ночь накопилось 21 снимок повторов одного отчёта — «что изменилось» иначе
сравнило бы документ сам с собой и вернуло формально валидный, но пустой
`MonitorResult`."""
from __future__ import annotations

from finengine.skills.atoms.subject_history import comparable_snapshot_pair
from tests.slice11_fixtures import metric, profile


def _p(version: int, period: str, *, document_version: str | None = None):
    return profile(version, [metric(100 + version, period=period, document_version=document_version)])


def test_two_snapshots_of_different_periods_are_a_pair():
    older, newer = _p(1, "2025-Q1"), _p(2, "2025-Q2")
    assert comparable_snapshot_pair([older, newer]) == (older, newer)


def test_a_repeated_run_of_the_current_document_is_skipped():
    q1, q2, q2_again = _p(1, "2025-Q1"), _p(2, "2025-Q2"), _p(3, "2025-Q2")
    assert comparable_snapshot_pair([q1, q2, q2_again]) == (q1, q2_again)


def test_only_repeats_of_one_document_are_not_comparable():
    assert comparable_snapshot_pair([_p(1, "2025-Q1"), _p(2, "2025-Q1"), _p(3, "2025-Q1")]) is None


def test_the_same_period_from_a_different_document_version_is_comparable():
    original = _p(1, "2025-Q1", document_version="doc-v1")
    restated = _p(2, "2025-Q1", document_version="doc-v2")
    assert comparable_snapshot_pair([original, restated]) == (original, restated)


def test_fewer_than_two_snapshots_are_not_a_pair():
    assert comparable_snapshot_pair([]) is None
    assert comparable_snapshot_pair([_p(1, "2025-Q1")]) is None
