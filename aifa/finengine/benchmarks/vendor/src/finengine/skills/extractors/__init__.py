"""Extractor matrix package.

The classifier maps OCR text to a profile, then `get_extractor(profile_id)`
returns the registered per-cell extractor. All supported profiles are wired
below; `None` is reserved for unknown or explicitly unregistered profiles.

Full plan in memory/project_backlog_extractor_matrix.md.
"""

from finengine.skills.extractors._base import (
    EVENT_TYPES,
    ExtractContext,
    Extractor,
    ExtractStream,
    get_extractor,
    register,
    registered_profiles,
)
from finengine.skills.extractors._classifier import (
    PROFILE_IDS,
    Profile,
    detect_profile,
)

# Side-effect imports: each cell module calls register() at import time.
# Adding a new cell == add another import line below.
from finengine.skills.extractors import _ifrs_bank_en  # noqa: F401
from finengine.skills.extractors import _ifrs_bank_ru  # noqa: F401
from finengine.skills.extractors import _ind_as_nf_en  # noqa: F401
from finengine.skills.extractors import _ind_gaap_bank_en  # noqa: F401
from finengine.skills.extractors import _ifrs_nf_en  # noqa: F401
from finengine.skills.extractors import _ifrs_nf_ru  # noqa: F401
from finengine.skills.extractors import _us_gaap_bank_en  # noqa: F401
from finengine.skills.extractors import _us_gaap_nf_en  # noqa: F401

__all__ = [
    "EVENT_TYPES",
    "ExtractContext",
    "Extractor",
    "ExtractStream",
    "PROFILE_IDS",
    "Profile",
    "detect_profile",
    "get_extractor",
    "register",
    "registered_profiles",
]
