"""
Ouroboros — headless personal agent runtime.

Packages: app/ (config, server lifecycle), agent/ (loop, context),
          llm/ (GigaChat client), policy/ (auth, safety, tools),
          storage/ (memory, settings, task results), skills/ (loader, extensions),
          ingress/ (A2A, SberChat), observability/ (health, pricing, events),
          gateway/ (HTTP routes), tools/ (built-in tools), contracts/ (frozen ABI).
"""

# IMPORTANT: Do NOT import agent/loop/llm/etc here!
# Eager imports here persist in forked worker processes as stale code,
# preventing hot-reload. Workers import make_agent directly.

__all__ = ["agent", "llm", "tools", "utils"]
