"""Outbound AEF WorkStation client: sandbox sessions and the tools they back."""

from __future__ import annotations

from ouroboros.egress.workstation.attachments_sync import sync_turn_attachments
from ouroboros.egress.workstation.bridge import FileStat, WorkstationFileBridge
from ouroboros.egress.workstation.client import (
    CreateSessionResult,
    ErrorCode,
    ExecResult,
    SessionState,
    SessionStatus,
    UploadResult,
    WorkstationAccessDenied,
    WorkstationApiError,
    WorkstationClient,
    WorkstationConfig,
    WorkstationError,
    WorkstationProtocolError,
    WorkstationTransportError,
)
from ouroboros.egress.workstation.executor import ExecOutcome, WorkstationCommandExecutor
from ouroboros.egress.workstation.handle import WorkstationHandle
from ouroboros.egress.workstation.manager import WorkstationManager, workstation_manager
from ouroboros.egress.workstation.paths import INBOUND_SUBDIR, WorkstationPathError
from ouroboros.egress.workstation.seeding import reconcile_workspace, seed_workspace
from ouroboros.egress.workstation.session import PROCESS_INSTANCE_ID, SessionRecord, scope_key_for, session_id_for

__all__ = [
    "INBOUND_SUBDIR",
    "PROCESS_INSTANCE_ID",
    "CreateSessionResult",
    "ErrorCode",
    "ExecOutcome",
    "ExecResult",
    "FileStat",
    "SessionRecord",
    "SessionState",
    "SessionStatus",
    "UploadResult",
    "WorkstationAccessDenied",
    "WorkstationApiError",
    "WorkstationClient",
    "WorkstationCommandExecutor",
    "WorkstationConfig",
    "WorkstationError",
    "WorkstationFileBridge",
    "WorkstationHandle",
    "WorkstationManager",
    "WorkstationPathError",
    "WorkstationProtocolError",
    "WorkstationTransportError",
    "reconcile_workspace",
    "scope_key_for",
    "seed_workspace",
    "session_id_for",
    "sync_turn_attachments",
    "workstation_manager",
]
