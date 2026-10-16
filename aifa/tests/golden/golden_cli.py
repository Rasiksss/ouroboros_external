"""CLI опорной записи (ADR-0005, F5).

    export AIFA_GOLDEN_BASE_URL=https://finagent.homyakagent.ru/api   # корень API службы
    export AIFA_GOLDEN_USER=...  AIFA_GOLDEN_PASSWORD=...             # не в git
    export AIFA_GOLDEN_BUILD=<коммит стенда>                          # необязательно

    python aifa/tests/golden/golden_cli.py record SCENARIO.json --out OUT.json
    python aifa/tests/golden/golden_cli.py compare A.json B.json [--text presence]
    python aifa/tests/golden/golden_cli.py stability SCENARIO.json --out-dir DIR [--text presence]

`stability` пишет сценарий дважды и сравнивает записи — проверка самого инструмента. Сценарий —
JSON: `{"scenario": "имя", "documents": ["файл.pdf"], "turns": ["реплика", ...]}`, пути —
относительно файла сценария. `--text presence` — для живой модели, чей текст каждый раз иной.

Коды возврата: 0 — записано/равны, 1 — различия, 2 — ошибка предусловия.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from golden_client import ClientError, ClientSettings
from golden_compare import TextMode, compare
from golden_format import RecordingError, read, write
from golden_record import load_scenario, record_scenario


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = _parser().parse_args(argv)
    environment = os.environ if env is None else env
    try:
        if args.command == "compare":
            return _compare(Path(args.first), Path(args.second), args.text)
        if args.command == "record":
            return _record(Path(args.scenario), Path(args.out), environment)
        return _stability(Path(args.scenario), Path(args.out_dir), args.text, environment)
    except (ClientError, RecordingError, OSError, ValueError) as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 2


def _compare(first: Path, second: Path, text: TextMode) -> int:
    result = compare(read(first), read(second), text=text)
    print(result.report())
    return 0 if result.equal else 1


def _record(scenario: Path, out: Path, env: Mapping[str, str]) -> int:
    settings = ClientSettings.from_env(env)
    recording = record_scenario(load_scenario(scenario), settings)
    write(recording, out)
    print(f"записано: {out} ({len(recording.turns)} ход(ов))")
    return 0


def _stability(scenario: Path, out_dir: Path, text: TextMode, env: Mapping[str, str]) -> int:
    settings = ClientSettings.from_env(env)
    loaded = load_scenario(scenario)
    paths = [out_dir / f"{loaded.name}-run{number}.json" for number in (1, 2)]
    for path in paths:
        write(record_scenario(loaded, settings), path)
        print(f"записано: {path}")
    return _compare(paths[0], paths[1], text)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="golden_cli", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("record", help="записать сценарий на стенде")
    record.add_argument("scenario")
    record.add_argument("--out", required=True)
    cmp = commands.add_parser("compare", help="сравнить две записи (первая — опора)")
    cmp.add_argument("first")
    cmp.add_argument("second")
    cmp.add_argument("--text", choices=("exact", "presence"), default="exact")
    stability = commands.add_parser("stability", help="записать дважды и сравнить")
    stability.add_argument("scenario")
    stability.add_argument("--out-dir", required=True)
    stability.add_argument("--text", choices=("exact", "presence"), default="exact")
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
