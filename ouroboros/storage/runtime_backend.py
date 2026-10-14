from __future__ import annotations

from sqlalchemy.exc import InterfaceError, OperationalError, SQLAlchemyError, TimeoutError as PoolTimeoutError

from infra.sql_connection import store_deadline as store_deadline


class MessageAnchorMissingError(LookupError):  # A missing cursor differs from an empty visible tail.
    pass


class TaskIdentityConflictError(ValueError):
    pass


class DurableJobsDisabledError(ValueError):
    pass


class ExecutionClaimUnavailable(BaseException):
    """Abort execution through model/tool error handlers; the runtime owns recovery."""


#: What a caller may absorb when a best-effort store call fails. A ``TypeError`` or a
#: ``ValidationError`` from a malformed scope is a bug, not a store failure, and must surface.
STORE_FAILURES: tuple[type[Exception], ...] = (SQLAlchemyError, OSError)
STORE_UNREACHABLE: tuple[type[Exception], ...] = (OperationalError, InterfaceError, PoolTimeoutError, TimeoutError)
TERMINAL_CLAIM_MAX_BATCH = 50
IMMUTABLE_CONTEXT_SEED_MAX_CHARS = 32_000
