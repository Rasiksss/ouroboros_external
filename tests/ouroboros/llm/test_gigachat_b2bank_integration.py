from __future__ import annotations

import os

import pytest

from ouroboros.llm.models import parse_model_ref
from ouroboros.llm.providers.gigachat_b2bank import GigachatB2BankProvider


@pytest.mark.integration
def test_gigachat_b2bank_non_stream_smoke():
    if not os.environ.get("GIGACHAT_B2BANK_BASE_URL"):
        pytest.skip("GIGACHAT_B2BANK_BASE_URL is not configured")

    provider = GigachatB2BankProvider()
    try:
        target = provider.resolve_target(parse_model_ref("gigachat-b2bank::GigaChat-Pro"))
        message, usage = provider.chat(
            target,
            [{"role": "user", "content": "Reply with OK."}],
            tools=None,
            reasoning_effort="none",
            max_tokens=16,
            tool_choice="none",
            temperature=0.01,
            no_proxy=False,
        )
    finally:
        provider.close()

    assert message["role"] == "assistant"
    assert message["content"]
    assert usage["provider"] == "gigachat-b2bank"
