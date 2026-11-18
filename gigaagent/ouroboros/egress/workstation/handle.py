"""Per-turn AEF WorkStation handle: session + bound file bridge + command executor.

What every tool handler actually touches — ``.files`` and ``.commands``, no
third surface (spec §19.2: exactly two internal interfaces, no more).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ouroboros.egress.workstation.bridge import WorkstationFileBridge
from ouroboros.egress.workstation.client import WorkstationClient, WorkstationConfig
from ouroboros.egress.workstation.executor import WorkstationCommandExecutor
from ouroboros.egress.workstation.session import SessionRecord


@dataclass
class WorkstationHandle:
    client: WorkstationClient = field(repr=False)
    config: WorkstationConfig
    record: SessionRecord = field(repr=False)

    files: WorkstationFileBridge = field(init=False, repr=False)
    commands: WorkstationCommandExecutor = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.commands = WorkstationCommandExecutor(
            self.client,
            session_id=self.record.session_id,
            token=self.record.token,
            config=self.config,
        )
        self.files = WorkstationFileBridge(
            self.client,
            self.commands,
            session_id=self.record.session_id,
            token=self.record.token,
            config=self.config,
        )


__all__ = ["WorkstationHandle"]
