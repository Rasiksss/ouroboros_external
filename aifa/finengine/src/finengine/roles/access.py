from __future__ import annotations

from typing import Any


ADMIN_ACCOUNT_ROLE = "admin"
ALL_EXTRACTION_ROLE = "all"
OTHER_ROLE = "other"

EXTRACTION_ROLES: tuple[str, ...] = (
    OTHER_ROLE,
    "risks",
    "baifp",
    "die",
    "cib",
    "dgr",
)

ROLE_LABELS: dict[str, str] = {
    OTHER_ROLE: "Общая",
    "risks": "Риски",
    "baifp": "БАИФП",
    "die": "ДИЭ",
    "cib": "CIB",
    "dgr": "ДГР",
    ALL_EXTRACTION_ROLE: "Все показатели",
    ADMIN_ACCOUNT_ROLE: "Админ",
}

_ROLE_ALIASES: dict[str, str] = {
    "": OTHER_ROLE,
    "default": OTHER_ROLE,
    "general": OTHER_ROLE,
    "other": OTHER_ROLE,
    "общая": OTHER_ROLE,
    "общий": OTHER_ROLE,
    "без роли": OTHER_ROLE,
    "риски": "risks",
    "risk": "risks",
    "risks": "risks",
    "баифп": "baifp",
    "bafp": "baifp",
    "baifp": "baifp",
    "дие": "die",
    "диэ": "die",
    "die": "die",
    "сиб": "cib",
    "cib": "cib",
    "дгр": "dgr",
    "dgr": "dgr",
    "мглоб": "dgr",
    "мл глоб": "dgr",
    "mglob": "dgr",
    "admin": ADMIN_ACCOUNT_ROLE,
    "админ": ADMIN_ACCOUNT_ROLE,
    "all": ALL_EXTRACTION_ROLE,
    "все": ALL_EXTRACTION_ROLE,
    "все показатели": ALL_EXTRACTION_ROLE,
}


def _role_tokens(roles: str | list[str] | tuple[str, ...] | set[str] | None) -> list[str]:
    if roles is None:
        return []
    if isinstance(roles, str):
        normalized = roles.replace(";", ",").replace("|", ",")
        return [part.strip() for part in normalized.split(",") if part.strip()]
    tokens: list[str] = []
    for role in roles:
        if isinstance(role, str):
            tokens.extend(_role_tokens(role))
        else:
            token = str(role).strip()
            if token:
                tokens.append(token)
    return tokens


def normalize_account_roles(roles: str | list[str] | tuple[str, ...] | set[str] | None) -> list[str]:
    """Normalize persisted account role grants.

    The auth table historically stored a single role in `role`. New invites can
    grant several roles; to keep the migration tiny and backward compatible, the
    same column stores comma-separated role ids.
    """

    normalized: list[str] = []
    for token in _role_tokens(roles):
        role = _ROLE_ALIASES.get(token.lower())
        if role in EXTRACTION_ROLES and role not in normalized:
            normalized.append(role)
    return normalized or [OTHER_ROLE]


def serialize_account_roles(roles: str | list[str] | tuple[str, ...] | set[str] | None) -> str:
    return ",".join(normalize_account_roles(roles))


def primary_account_role(roles: str | list[str] | tuple[str, ...] | set[str] | None) -> str:
    for role in normalize_account_roles(roles):
        if role != OTHER_ROLE:
            return role
    return OTHER_ROLE


def normalize_account_role(role: str | None) -> str:
    """Normalize persisted account role.

    Admin is intentionally not an extraction role. If an old row has NULL,
    empty text or an unknown value, the account behaves as `other`.
    """

    return primary_account_role(role)


def normalize_extraction_role(role: str | None, *, default: str = OTHER_ROLE) -> str:
    normalized = _ROLE_ALIASES.get((role or "").strip().lower())
    if normalized in EXTRACTION_ROLES or normalized == ALL_EXTRACTION_ROLE:
        return normalized
    return primary_account_role(default)


def role_label(role: str | None) -> str:
    return ROLE_LABELS.get(normalize_extraction_role(role), ROLE_LABELS[OTHER_ROLE])


def _is_admin(user: Any) -> bool:
    return bool(getattr(user, "is_admin", False))


def default_role_for_user(user: Any) -> str:
    if _is_admin(user):
        return OTHER_ROLE
    return primary_account_role(getattr(user, "role", None))


def available_roles_for_user(user: Any) -> list[str]:
    if _is_admin(user):
        return [*EXTRACTION_ROLES, ALL_EXTRACTION_ROLE]

    account_roles = normalize_account_roles(getattr(user, "role", None))
    granted_roles = [role for role in account_roles if role != OTHER_ROLE]
    if not granted_roles:
        return [OTHER_ROLE]
    return [*granted_roles, OTHER_ROLE]


def user_can_extract_as(user: Any, role: str | None) -> bool:
    requested = normalize_extraction_role(role, default=default_role_for_user(user))
    return requested in available_roles_for_user(user)
