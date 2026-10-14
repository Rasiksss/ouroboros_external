#!/usr/bin/env python3
"""Report argv, cwd, and the scrubbed env so skill_exec behaviour is assertable."""

from __future__ import annotations

import json
import os
import pathlib
import sys

# Keys the executor injects for every script run.
_INJECTED = ("OUROBOROS_SKILL_NAME", "OUROBOROS_SKILL_STATE_DIR")
# Declared in the manifest's env_from_settings; a non-core key is forwarded as-is.
_DECLARED = ("OUROBOROS_TEST_SKILL_TOKEN",)
# Core secrets must never reach a skill subprocess without an explicit grant.
_MUST_NOT_LEAK = (
    "GIGACHAT_B2BANK_CERT_PATH",
    "CLOUDRU_FOUNDATION_MODELS_API_KEY",
    "OUROBOROS_TELEGRAM_TOKEN",
    "TELEGRAM_BOT_TOKEN",
)


def main() -> int:
    env = {key: os.environ.get(key) for key in (*_INJECTED, *_DECLARED)}
    leaked = sorted(key for key in _MUST_NOT_LEAK if os.environ.get(key))
    state_dir = os.environ.get("OUROBOROS_SKILL_STATE_DIR", "")
    report = {
        "marker": "OUROBOROS_TEST_SCRIPT_OK",
        "argv": sys.argv[1:],
        "cwd": str(pathlib.Path.cwd()),
        "script": str(pathlib.Path(__file__).resolve()),
        "env": env,
        "state_dir_exists": bool(state_dir) and pathlib.Path(state_dir).is_dir(),
        # Guard against the scrubber regressing into a full os.environ passthrough.
        "leaked_core_keys": leaked,
        "env_key_count": len(os.environ),
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
