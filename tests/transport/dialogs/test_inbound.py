from types import SimpleNamespace

from ouroboros.storage.settings.schema import AppSettings
from tests.helpers.parametrize import parametrize_from_dict
from transport.dialogs import DialogsProvider


def _provider():
    replies = []
    provider = DialogsProvider(
        AppSettings(
            OUROBOROS_MAX_INBOUND_MESSAGE_CHARS=10,
            DIALOGS_HISTORY_RECOVERY_ENABLED=False,
            DIALOGS_REPLY_ENABLED=False,
            DIALOGS_BOT_NICK="ouroboros_bot",
        )
    )
    provider._bot = SimpleNamespace(
        messaging=SimpleNamespace(send_message_sync=lambda peer, text, **kwargs: replies.append(text)),
        user_info=SimpleNamespace(user=SimpleNamespace(peer=SimpleNamespace(id="bot"))),
    )
    provider._out_peer_by_chat_id["peer-42"] = SimpleNamespace(id="peer-42")
    return provider, replies


def _update(text, *, group=False, file=False):
    content = (
        {"document_message": SimpleNamespace(name="report.txt", size=3, caption=text, file_id=10, access_hash=20)}
        if file
        else {"text_message": SimpleNamespace(text=text)}
    )
    return SimpleNamespace(
        peer=SimpleNamespace(id="peer-42", type=2 if group else 1),
        sender_peer=SimpleNamespace(id="user-42"),
        message=SimpleNamespace(mid="message-1", **content),
    )


@parametrize_from_dict(
    {
        "direct": {"text": "x" * 11, "group": False, "history": False, "reply": True},
        "slash argument": {"text": "/ask " + "x" * 11, "group": False, "history": False, "reply": True},
        "unaddressed group": {"text": "x" * 11, "group": True, "history": False, "reply": False},
        "imported history": {"text": "x" * 11, "group": False, "history": True, "reply": False},
    }
)
def test_oversized_dialogs_text_is_refused_before_queue(text, group, history, reply):
    provider, replies = _provider()
    update = _update(text, group=group)

    if history:
        inbound, _reason = provider._normalize_sdk_update_message(update, history_only=True)
        assert inbound is None
    else:
        provider._on_text_message(update)

    assert provider._incoming.empty()
    assert bool(replies) is reply
    if reply:
        assert "Максимальная длина — 10 символов" in replies[0]


@parametrize_from_dict({"direct": {"group": False}, "unaddressed group": {"group": True}})
def test_oversized_dialogs_caption_is_refused_before_file_workers(group):
    provider, replies = _provider()
    try:
        provider._on_file_passed(_update("x" * 11, group=group, file=True))

        assert provider._file_workers == []
        assert provider._incoming.empty()
        assert bool(replies) is not group
    finally:
        provider.close()


def test_dialogs_message_at_the_limit_is_queued():
    provider, replies = _provider()

    provider._on_text_message(_update("я" * 10))

    assert provider._incoming.get_nowait().text == "я" * 10
    assert replies == []
