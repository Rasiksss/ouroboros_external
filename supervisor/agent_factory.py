"""Single-owner agent factory for headless supervisor execution."""

from __future__ import annotations

import logging
import pathlib
import queue as queue_mod
import threading
from typing import Any

from ouroboros.agent.core import OuroborosAgent

log = logging.getLogger(__name__)


class AgentFactory:
    """Lazy singleton OuroborosAgent for the headless single-owner profile."""

    def __init__(
        self,
        repo_dir: pathlib.Path,
        drive_root: pathlib.Path,
        event_q: queue_mod.Queue[dict[str, Any]],
        media_senders: dict[str, Any] | None = None,
    ) -> None:
        self.repo_dir = pathlib.Path(repo_dir)
        self.drive_root = pathlib.Path(drive_root)
        self.event_q = event_q
        self.media_senders = dict(media_senders or {})
        self._agent: Any = None
        self._lock = threading.Lock()

    def get(self) -> OuroborosAgent:
        with self._lock:
            if self._agent is None:
                from ouroboros.agent import make_agent

                self._agent = make_agent(
                    repo_dir=str(self.repo_dir),
                    drive_root=str(self.drive_root),
                    event_queue=self.event_q,
                    media_senders=self.media_senders,
                )
            return self._agent

    def set_media_senders(self, media_senders: dict[str, Any]) -> None:
        with self._lock:
            if self._agent is not None:
                raise RuntimeError("media senders cannot change after agent creation")
            self.media_senders = dict(media_senders)

    def reset_for_tests(self) -> None:
        with self._lock:
            self._agent = None


__all__ = ["AgentFactory"]
