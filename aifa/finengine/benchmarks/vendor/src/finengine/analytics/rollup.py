"""Daily usage rollup — aggregate llm_traces JSONL into the usage_daily table.

This is the source of truth for per-user analytics (reports run, tokens, cost,
LLM time, approximate active time, daily activity). Run nightly via cron:

    python -m finengine.analytics.rollup             # roll the last ~35 days
    python -m finengine.analytics.rollup 2026-06-30  # roll one specific day

Idempotent: re-rolling a day overwrites that day's *trace-derived* columns and
leaves the live ``logins`` column (owned by the auth routes) untouched. A
"report" is one extraction run — a distinct ``run_id`` with ``kind="extract"``
(an extraction emits many LLM calls that all share the run id).

UTC throughout: trace timestamps are stamped with ``datetime.utcnow``, and the
trace files are partitioned by UTC date, so ``day`` here is a UTC ISO date.
"""
from __future__ import annotations

import datetime as _dt
import json as _json
import time as _time
from pathlib import Path

from finengine.config import settings


# Events more than this far apart start a new "session" when approximating
# active time from trace timestamps.
SESSION_GAP_S = 30 * 60
# How many trailing days rollup_recent() processes — covers the MAU (30d)
# window plus slack for late/idempotent re-rolls.
DEFAULT_ROLL_DAYS = 35


def _trace_dir() -> Path:
    return Path(settings.docs_dir) / "llm_traces"


def _iter_trace_records(day: str):
    """Yield parsed trace records for a UTC ISO day (YYYY-MM-DD). Silently
    skips a missing file or any unparseable line — a corrupt trace line must
    never break the rollup."""
    path = _trace_dir() / f"{day}.jsonl"
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield _json.loads(line)
            except Exception:
                continue


def _parse_ts(s: str) -> float | None:
    """Trace ts is 'YYYY-MM-DDTHH:MM:SSZ'. Return epoch seconds, or None."""
    if not s:
        return None
    try:
        return (
            _dt.datetime.fromisoformat(s.rstrip("Z"))
            .replace(tzinfo=_dt.timezone.utc)
            .timestamp()
        )
    except Exception:
        return None


def _sessionized_seconds(timestamps: list[float]) -> float:
    """Approximate active time: cluster sorted epoch timestamps with a
    ``SESSION_GAP_S`` gap and sum each cluster's span (last - first). A single
    isolated event contributes 0 (we can't know how long the user worked from
    one point). This is an approximation, not measured wall-clock."""
    if len(timestamps) < 2:
        return 0.0
    ts = sorted(timestamps)
    total = 0.0
    start = prev = ts[0]
    for t in ts[1:]:
        if t - prev > SESSION_GAP_S:
            total += prev - start
            start = t
        prev = t
    total += prev - start
    return total


def rollup_day(day: str) -> int:
    """Aggregate one UTC day's traces into usage_daily (trace-owned columns).

    Returns the number of user rows written. Idempotent: overwrites the
    trace-derived columns and preserves the live ``logins`` column.
    """
    from finengine.auth.db import SessionLocal, UsageDaily
    from finengine.skills.pricing import cost_usd

    agg: dict[int, dict] = {}
    run_ids: dict[int, set] = {}
    times: dict[int, list[float]] = {}
    for rec in _iter_trace_records(day):
        uid = rec.get("user_id")
        if not uid:
            continue  # anonymous / system calls are not per-user attributable
        uid = int(uid)
        a = agg.setdefault(
            uid,
            {
                "calls": 0,
                "tokens_in": 0,
                "tokens_out": 0,
                "tokens_cached": 0,
                "cost_usd": 0.0,
                "llm_seconds": 0.0,
            },
        )
        ti = int(rec.get("tokens_in") or 0)
        to = int(rec.get("tokens_out") or 0)
        tc = int(rec.get("tokens_cached") or 0)
        a["calls"] += 1
        a["tokens_in"] += ti
        a["tokens_out"] += to
        a["tokens_cached"] += tc
        a["llm_seconds"] += float(rec.get("latency_s") or 0.0)
        try:
            a["cost_usd"] += cost_usd(rec.get("model") or "", ti, to, tc)
        except Exception:
            pass
        if (rec.get("kind") or "") == "extract":
            rid = rec.get("run_id") or ""
            if rid:
                run_ids.setdefault(uid, set()).add(rid)
        t = _parse_ts(rec.get("ts") or "")
        if t is not None:
            times.setdefault(uid, []).append(t)

    session = SessionLocal()
    written = 0
    try:
        for uid, a in agg.items():
            row = session.get(UsageDaily, (uid, day))
            if row is None:
                row = UsageDaily(user_id=uid, day=day)
                session.add(row)
            row.calls = a["calls"]
            row.reports = len(run_ids.get(uid, ()))
            row.tokens_in = a["tokens_in"]
            row.tokens_out = a["tokens_out"]
            row.tokens_cached = a["tokens_cached"]
            row.cost_usd = round(a["cost_usd"], 6)
            row.llm_seconds = round(a["llm_seconds"], 3)
            row.active_seconds = round(_sessionized_seconds(times.get(uid, [])), 1)
            # `logins` is owned by the auth routes — never touched here.
            written += 1
        session.commit()
    finally:
        session.close()
    return written


def rollup_recent(days: int = DEFAULT_ROLL_DAYS, *, today: str | None = None) -> dict[str, int]:
    """Roll the last ``days`` UTC days (inclusive of today). Returns {day: rows}."""
    end = _dt.date.fromisoformat(today) if today else _dt.datetime.now(_dt.timezone.utc).date()
    out: dict[str, int] = {}
    for i in range(max(1, days)):
        d = (end - _dt.timedelta(days=i)).isoformat()
        out[d] = rollup_day(d)
    return out


# On-demand freshness for the admin panel. The nightly cron keeps history
# current; this rolls today (+ yesterday for UTC-boundary slack) when the
# panel is opened, throttled so repeated refreshes don't re-scan every time.
_LAST_FRESH = {"ts": 0.0}
_FRESH_TTL_S = 300


def ensure_fresh(ttl_s: int = _FRESH_TTL_S) -> None:
    """Roll today + yesterday on demand, at most once per ``ttl_s``. Best-effort
    — never raises into the request path."""
    now = _time.time()
    if now - _LAST_FRESH["ts"] < ttl_s:
        return
    _LAST_FRESH["ts"] = now
    try:
        today = _dt.datetime.now(_dt.timezone.utc).date()
        rollup_day(today.isoformat())
        rollup_day((today - _dt.timedelta(days=1)).isoformat())
    except Exception:
        pass


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        for _day in sys.argv[1:]:
            print(f"{_day}: {rollup_day(_day)} user rows")
    else:
        _res = rollup_recent()
        print(f"rolled {len(_res)} days, {sum(_res.values())} user-day rows")
