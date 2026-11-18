"""Run the browser transport tier: serve the configured address, or exit non-zero."""

from __future__ import annotations

import sys

from multiuser_config.bootstrap import serve_tier, stop_process_monitoring, tier_config


def main(argv: list[str] | None = None) -> int:
    del argv
    import uvicorn

    from multiuser_config.transport.browser.app import TIER, build_transport_app

    config = tier_config(build_transport_app(), tier=TIER)
    if config is None:
        # The lifespan that would release what composition attached never runs for this refusal.
        stop_process_monitoring()
        return 1
    return serve_tier(uvicorn.Server(config), tier=TIER)


if __name__ == "__main__":
    sys.exit(main())
