"""`python -m stub_model [--scenario файл.json] [--host ..] [--port 8000]` — запуск в контейнере."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from stub_model.scenario import ScenarioError, load_scenario
from stub_model.server import StubModelServer


def main() -> int:
    parser = argparse.ArgumentParser(prog="stub_model", description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 — внутри контейнера, порт наружу не публикуется
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--scenario", type=Path, help="сценарий при старте; иначе — PUT /_scenario")
    args = parser.parse_args()
    try:
        scenario = load_scenario(args.scenario) if args.scenario else None
    except (ScenarioError, OSError) as error:
        print(f"stub-model: {error}", file=sys.stderr)
        return 2
    server = StubModelServer.start(args.host, args.port, scenario)
    print(
        f"stub-model: слушает {server.url}, сценарий {scenario.name if scenario else None!r}",
        flush=True,
    )
    server.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
