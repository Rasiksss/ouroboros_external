"""Company name / type / standard / year detection."""

from __future__ import annotations

from finengine.skills.bulk_extractor import (
    _company_name_from_filename,
    _detect_company_from_text,
    _detect_company_type,
    _detect_reporting_standard,
    _detect_year_from_text,
    _prefer_detected_company_name,
    _reconcile_company_name,
)

__all__ = [
    "company_name_from_filename",
    "detect_company_from_text",
    "detect_company_type",
    "detect_reporting_standard",
    "detect_year_from_text",
    "prefer_detected_company_name",
    "reconcile_company_name",
]


company_name_from_filename = _company_name_from_filename
detect_company_from_text = _detect_company_from_text
detect_company_type = _detect_company_type
detect_reporting_standard = _detect_reporting_standard
detect_year_from_text = _detect_year_from_text
prefer_detected_company_name = _prefer_detected_company_name
reconcile_company_name = _reconcile_company_name
