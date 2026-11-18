"""Workspace seeding (spec §8) and reconciliation (spec §10).

No-ops: nothing existing in this codebase is copied into a new WorkStation
session. The call sites in ``manager.py`` exist so a future task has one
place to land real behavior:

- ``seed_workspace`` — spec §8's real job is materializing the skill catalog
  and its manifest-declared scripts into the session, a prerequisite for
  ever routing ``skill_exec`` through WorkStation; it must skip
  ``paths.INBOUND_SUBDIR`` (spec §9) and skip entirely when the access
  level is ``"none"`` (spec §8.4).
- ``reconcile_workspace`` — spec §10's manifest/hash reconciliation for a
  restored session; persistence itself is not implemented, so there is no
  "restored" branch to reconcile yet.
"""

from __future__ import annotations

from ouroboros.egress.workstation.handle import WorkstationHandle


def seed_workspace(handle: WorkstationHandle) -> None:
    return None


def reconcile_workspace(handle: WorkstationHandle) -> None:
    return None


__all__ = ["reconcile_workspace", "seed_workspace"]
