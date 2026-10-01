"""Direct LLM provider adapters."""

from ouroboros.llm.providers.gigachat_b2bank import GigachatB2BankProvider
from ouroboros.llm.providers.openai_compat import OpenAICompatibleProvider

__all__ = [
    "GigachatB2BankProvider",
    "OpenAICompatibleProvider",
]
