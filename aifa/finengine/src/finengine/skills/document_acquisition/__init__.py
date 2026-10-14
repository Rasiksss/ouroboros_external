"""A5 — получение финансовой отчётности из интернета (Срез 7,
docs/build-plan/17-srez-07-otchetnost-iz-interneta.md).

Поиск кандидатов → приоритет официального источника → проверка адреса и
redirect'ов (SSRF) → безопасное скачивание бинарного PDF → sha256 → дедуп →
регистрация обычного `Document` с происхождением приёма. Дальше — ровно тот
же `doc.probe`/`doc.textify`/классификация/извлечение/анализ, что у ручной
загрузки; отдельного «веб-анализа» здесь нет и не должно появиться.

Публичный вход — `find_and_register_report` (см. `acquire.py`)."""
from __future__ import annotations

from .acquire import AcquisitionOutcome, find_and_register_report
from .policy import RankedCandidate, SearchCandidate, SourceTier
from .registration import AcquisitionProvenance, RegistrationResult
from .url_safety import UnsafeUrlError

__all__ = [
    "AcquisitionOutcome", "find_and_register_report",
    "RankedCandidate", "SearchCandidate", "SourceTier",
    "AcquisitionProvenance", "RegistrationResult",
    "UnsafeUrlError",
]
