"""Project-specific permission and role enums.

Add values here when you introduce new protected endpoints — never hardcode
permission strings at call sites.
"""

from __future__ import annotations

from enum import StrEnum


class UserPermission(StrEnum):
    """Permissions returned by the external auth service.

    Keep the string value in sync with whatever the auth provider emits.
    Example value kept here so endpoints compile on a fresh skeleton.
    """

    EXAMPLE_VIEW = "EXAMPLE_VIEW"


class UserRole(StrEnum):
    """High-level roles that can bypass fine-grained permission checks."""

    ADMIN = "Admin"
