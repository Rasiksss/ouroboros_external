"""Stable result details shared by media delivery owners and callers."""

# The boolean in the existing ``tuple[bool, str]`` contract cannot distinguish
# a confirmed failure from a send whose remote outcome is unknown. Keep that
# distinction machine-readable instead of inferring it from transport wording.
DELIVERY_UNKNOWN = "delivery_status=unknown"


__all__ = ["DELIVERY_UNKNOWN"]
