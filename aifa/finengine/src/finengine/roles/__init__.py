"""Role access, metric scopes and client-facing formatters."""

from finengine.roles.access import (
    ADMIN_ACCOUNT_ROLE,
    ALL_EXTRACTION_ROLE,
    EXTRACTION_ROLES,
    ROLE_LABELS,
    available_roles_for_user,
    default_role_for_user,
    normalize_account_role,
    normalize_account_roles,
    normalize_extraction_role,
    primary_account_role,
    role_label,
    serialize_account_roles,
    user_can_extract_as,
)
from finengine.roles.formatter import build_formatted_table, write_formatted_xlsx, write_generic_wide_xlsx
from finengine.roles.scope import (
    RolePlan,
    RoleSegmentPolicy,
    build_role_plan,
    role_segment_policy,
    role_should_include_consolidated_group,
    role_should_include_segments,
    role_supported_for_company_type,
)

__all__ = [
    "ADMIN_ACCOUNT_ROLE",
    "ALL_EXTRACTION_ROLE",
    "EXTRACTION_ROLES",
    "ROLE_LABELS",
    "RolePlan",
    "RoleSegmentPolicy",
    "available_roles_for_user",
    "build_formatted_table",
    "build_role_plan",
    "default_role_for_user",
    "normalize_account_role",
    "normalize_account_roles",
    "normalize_extraction_role",
    "primary_account_role",
    "role_label",
    "role_segment_policy",
    "role_should_include_consolidated_group",
    "role_should_include_segments",
    "role_supported_for_company_type",
    "serialize_account_roles",
    "user_can_extract_as",
    "write_formatted_xlsx",
    "write_generic_wide_xlsx",
]
