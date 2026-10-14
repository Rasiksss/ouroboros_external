"""One-shot helper (not a test): fetch a real SEC 10-K excerpt per ticker and
cross-check it against the live `detect_profile()` classifier before it goes
into `cases.json`. Not part of the golden set itself — the OUTPUT (a JSON
entry with a real `source_url` and verbatim excerpt) is what gets committed.

Lesson from the first two attempts (kept here, not in a commit message,
because the next person touching this script will hit the same trap): a
short window anchored on one phrase ("prepared in accordance...", or even
the audit-report heading) is unreliable — real 10-Ks repeat both in a table
of contents, in MD&A boilerplate, and in the real notes, and which occurrence
is "the real one" varies per filer. Feeding the classifier the same
HEAD_WINDOW (200k chars) it uses in production, and then verifying that
exact stored excerpt re-classifies correctly, is what actually holds up.

Usage: .venv/Scripts/python.exe tests/golden/classification/_fetch_edgar.py
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from finengine.skills.extractors._classifier import detect_profile  # noqa: E402

UA = "AIFA-CLAW research contact@example.com"
HERE = Path(__file__).resolve().parent
HEAD_WINDOW = 200_000


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _tickers() -> dict[str, int]:
    raw = json.loads((HERE / "_company_tickers.json").read_text(encoding="utf-8"))
    return {v["ticker"]: v["cik_str"] for v in raw.values()}


def latest_10k_document(cik: int) -> tuple[str, str, str]:
    data = json.loads(_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json"))
    recent = data["filings"]["recent"]
    for i, form in enumerate(recent["form"]):
        if form == "10-K":
            accession = recent["accessionNumber"][i]
            doc = recent["primaryDocument"][i]
            date = recent["filingDate"][i]
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{doc}"
            return url, accession, date
    raise LookupError(f"no 10-K in recent filings for CIK {cik}")


def strip_tags(html: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;|&#160;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text


MANIFEST = [
    ("DIS", "us_gaap_nf_en"),
    ("XOM", "us_gaap_nf_en"),
]


def main() -> None:
    cik_by_ticker = _tickers()
    out = []
    for ticker, expected_profile in MANIFEST:
        try:
            cik = cik_by_ticker[ticker]
            doc_url, accession, filing_date = latest_10k_document(cik)
            time.sleep(0.25)
            raw = _get(doc_url)
            text = strip_tags(raw)
            excerpt = text[:HEAD_WINDOW]
            profile = detect_profile(excerpt)
            match = profile.profile_id == expected_profile
            if not match:
                # fall back to the full document once before giving up — a few
                # filers (Citi, Wells Fargo) bury financial-statement content
                # past the 200k head window.
                full_profile = detect_profile(text)
                if full_profile.profile_id == expected_profile:
                    excerpt = text[: HEAD_WINDOW * 2]
                    profile = detect_profile(excerpt)
                    match = profile.profile_id == expected_profile
            print(
                f"{ticker:6s} -> {profile.profile_id:16s} (expected {expected_profile}) "
                f"confidence_low={profile.confidence_low} match={match} chars={len(excerpt)}  {doc_url}"
            )
            out.append(
                {
                    "ticker": ticker,
                    "expected_profile": expected_profile,
                    "detected_profile": profile.profile_id,
                    "confidence_low": profile.confidence_low,
                    "match": match,
                    "reason": profile.reason,
                    "source_url": doc_url,
                    "filing_date": filing_date,
                    "excerpt": excerpt,
                }
            )
        except Exception as exc:  # noqa: BLE001 — best-effort one-shot script, not production code
            print(f"{ticker:6s} -> ERROR {exc}")
            out.append({"ticker": ticker, "expected_profile": expected_profile, "error": str(exc)})
        time.sleep(0.25)
    (HERE / "_edgar_raw3.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
