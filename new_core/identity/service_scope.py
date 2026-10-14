"""The identity of work no user asked for.

A boot check, an agent card, a journal sweep and the A2A protocol's own bookkeeping still
own rows and still have to say whose they are, but they have no conversation behind them.
They name themselves with this type rather than borrowing an ``OwnerScope`` outright: a
value that means "not a user" must not be accepted where a user's scope is required.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict

from new_core.identity.owner_scope import Axis, OwnerScope, SurfaceId


class ServiceJob(str, Enum):
    """The closed dictionary of author-less jobs.

    Closed for the same reason the surface dictionary is: the set is finite and known,
    and spelling it as a free string is what let an author-less identity be mistaken
    for a conversation.
    """

    STARTUP = "startup"
    JOURNAL_SWEEP = "journal-sweep"
    AGENT_CARD = "agent-card"
    A2A_TASK_STORE = "a2a-task-store"
    #: The instance's own retention sweep over working retrieval, whose checkpoint is a row
    #: like any other and therefore has to say whose it is.
    CONTEXT_CLEANUP = "context-cleanup"
    #: One transport's update cursor. It belongs to the deployment's connection to a
    #: provider rather than to any of the people talking through it.
    DIALOGS_CURSOR = "dialogs-cursor"
    #: The instance's configured owner, for a caller that holds no owner of its own. Every
    #: caller that does hold one addresses that owner instead.
    OWNER_STATE = "owner-state"
    #: An audit row about the instance itself — a boot, a sweep, a malformed event — which
    #: no owner asked for and which therefore belongs to none of them.
    RUNTIME_LOG = "runtime-log"
    #: Private state owned by deployment-installed skills, not by an individual owner.
    SKILL_STATE = "skill-state"

    # A str-mixin member renders as "ServiceJob.STARTUP" on 3.10; every consumer wants
    # the value, exactly as with ``SurfaceId``.
    __str__ = str.__str__


class ServiceScope(BaseModel):
    """The instance's ownership pair plus the job doing the work.

    Deliberately not an ``OwnerScope`` and not a subclass of one: subclassing would put a
    service identity everywhere a user's scope is accepted, which is the confusion this
    type exists to remove. It carries no surface and no chat because it has neither.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    owner_id: Axis
    agent_id: Axis
    job: ServiceJob

    def as_scope(self) -> OwnerScope:
        """An addressable scope for this job, for the storage calls that require one.

        A one-way widening, spelled out at the call site that needs it. The type itself
        stays outside the ``OwnerScope`` hierarchy, so a service identity still cannot be
        handed to anything that requires an author — in particular not to the
        persisted-message announcement that starts background-memory consolidation, which
        would otherwise run over the real owner's backlog.

        The surface is the bridge's and the chat holds the job's name, standing where a
        conversation would. Both are placeholders that match no stored row: a widened service
        scope is for statements keyed by the ownership pair, and a ``messages`` or
        ``context_summaries`` read under one matches nothing, silently. Author-less work that
        must cross an owner's conversations reads through ``IConversationRepository.list_pair_messages``,
        which takes the pair and names no chat.
        """
        return OwnerScope(
            owner_id=self.owner_id,
            agent_id=self.agent_id,
            surface_id=SurfaceId.SKILL_BRIDGE,
            chat_id=str(self.job),
        )


__all__ = ["ServiceJob", "ServiceScope"]
