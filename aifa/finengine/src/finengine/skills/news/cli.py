"""CLI entrypoint for cron: `python -m finengine.skills.news.cli daily`.

systemd timer / OS cron / docker exec can call this without going
through the HTTP layer. Logs go to stdout (Docker captures them).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="news.cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("daily", help="run the daily news pipeline once")
    d.add_argument("--since-hours", type=int, default=48)
    d.add_argument("--top-n", type=int, default=60)
    d.add_argument("--model", type=str, default=None)

    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.cmd == "daily":
        from finengine.skills.news.pipeline import run_daily_news_pipeline
        result = asyncio.run(run_daily_news_pipeline(
            since_hours=args.since_hours,
            top_n_candidates=args.top_n,
            model=args.model,
        ))
        # Stable summary line for log scraping.
        print(
            f"news.cli: done run={result['run_id']} "
            f"raw={result['items_raw']} kept={result['items_kept']} "
            f"cost=${result['cost_usd']:.4f} time={result['duration_s']}s "
            f"error={result.get('error')}"
        )
        return 0 if not result.get("error") else 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
