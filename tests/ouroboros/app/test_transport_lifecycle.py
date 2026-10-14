from __future__ import annotations

import logging

from ouroboros.app.transport_lifecycle import report_missing_dialogs_transport
from tests.helpers.parametrize import parametrize_from_dict


@parametrize_from_dict(
    {
        "dialogs": {"owner_source": "dialogs", "reported": True},
        "sberchat": {"owner_source": "sberchat", "reported": True},
        "browser": {"owner_source": "browser", "reported": False},
        "a2a": {"owner_source": "a2a", "reported": False},
        "unclaimed": {"owner_source": "", "reported": False},
    }
)
def test_missing_dialogs_report_uses_only_the_supplied_owner_source(owner_source, reported, caplog) -> None:
    with caplog.at_level(logging.ERROR, logger="ouroboros.app.transport_lifecycle"):
        report_missing_dialogs_transport(owner_source)

    assert bool(caplog.records) is reported
    if reported:
        assert "dialogs transport is disabled" in caplog.text
