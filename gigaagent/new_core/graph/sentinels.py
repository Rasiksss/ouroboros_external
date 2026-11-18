"""Reserved endpoint markers for graph topology.

``START`` and ``END`` are unique ``str`` instances. Subclassing ``str`` keeps node
and edge signatures plain ``str`` — no separate sentinel type leaks into them —
while identity (``is START`` / ``is END``) drives the endpoint semantics (an edge
terminates only on the real ``END`` object). To close the value-collision gap that
identity alone leaves open, the builder additionally rejects any node name or edge
source that *value*-equals a marker, so a plain ``str`` "START"/"END" can never be
registered or executed as an ordinary node. ``Sentinel`` itself is an implementation
detail of this module; callers only ever use ``START`` and ``END``.
"""

from __future__ import annotations

from typing import Final


class Sentinel(str):
    """A reserved marker that is also a ``str``, distinguished only by identity."""

    __slots__ = ()

    def __repr__(self) -> str:
        return str.__str__(self)


#: Virtual source: ``add_edge(START, "entry")`` declares the entry node.
START: Final = Sentinel("START")

#: Terminal destination: reaching ``END`` stops the run and returns the state.
END: Final = Sentinel("END")
