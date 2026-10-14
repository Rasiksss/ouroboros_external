"""Rename the agent inside identity documents written before the rename.

The identity document is seeded once and never replaced — existing content, including a
hand-edited one, is what the deployment keeps. So a new name in the prompt and in the seed
reaches new deployments only; every running one would keep introducing itself by the old
name, with nothing in the logs to say why.

The substitution is on the sentence that names the agent rather than on the whole document,
so a deployment whose identity was edited keeps the edit and still gets the new name. Rows of
other kinds are left alone: there the old name is a fact somebody recorded, not the agent
speaking about itself.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

_OLD_NAME_SENTENCE = "I am Ouroboros."
_NEW_NAME_SENTENCE = "I am ГигаАгент."

_RENAME = sa.text(
    "UPDATE memory SET content = replace(content, :old, :new) "
    "WHERE kind = 'identity' AND content LIKE '%' || :old || '%'"
)


def _rename(old: str, new: str) -> None:
    op.get_bind().execute(_RENAME, {"old": old, "new": new})


def upgrade() -> None:
    _rename(_OLD_NAME_SENTENCE, _NEW_NAME_SENTENCE)


def downgrade() -> None:
    _rename(_NEW_NAME_SENTENCE, _OLD_NAME_SENTENCE)
