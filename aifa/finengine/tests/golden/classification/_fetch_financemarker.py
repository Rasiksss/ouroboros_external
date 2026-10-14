"""One-shot helper (not a test): download real МСФО/РСБУ PDF annual reports
for MOEX-listed Russian issuers from financemarker.ru's report CDN, extract
text with PyMuPDF, and cross-check against the live classifier.

financemarker.ru is a third-party aggregator, not the primary filer — every
entry keeps its `source_url` so the underlying real filing can be traced.
URL pattern confirmed live (16.09.2026):
    https://cdn.financemarker.ru/reports/{year}/MOEX/{first letter}/{TICKER}_{year}_{month:02d}_{period}_{МСФО|РСБУ}.pdf

Usage: .venv/Scripts/python.exe tests/golden/classification/_fetch_financemarker.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

import fitz  # PyMuPDF  # noqa: E402

from finengine.skills.extractors._classifier import detect_profile  # noqa: E402

UA = "Mozilla/5.0 (research; contact@example.com)"
HERE = Path(__file__).resolve().parent
HEAD_WINDOW = 200_000

MANIFEST = [
    ("MTSS", "MTS", "МСФО", "ifrs_nf_ru"),
    ("CBOM", "Credit Bank of Moscow", "МСФО", "ifrs_bank_ru"),
    ("GAZP", "Gazprom", "МСФО", "ifrs_nf_ru"),
]


def build_url(ticker: str, form: str) -> str:
    letter = ticker[0]
    path = f"reports/2024/MOEX/{letter}/{ticker}_2024_12_Y_{form}.pdf"
    return "https://cdn.financemarker.ru/" + urllib.parse.quote(path)


def download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        dest.write_bytes(resp.read())


def extract_text(pdf_path: Path) -> str:
    doc = fitz.open(str(pdf_path))
    parts = [page.get_text("text") for page in doc]
    doc.close()
    return "\n".join(parts)


def main() -> None:
    scratch = HERE / "_scratch"
    scratch.mkdir(exist_ok=True)
    out = []
    for ticker, name, form, expected_profile in MANIFEST:
        url = build_url(ticker, form)
        pdf_path = scratch / f"{ticker}_{form}.pdf"
        try:
            download(url, pdf_path)
            text = extract_text(pdf_path)
            excerpt = text[:HEAD_WINDOW]
            profile = detect_profile(excerpt)
            match = profile.profile_id == expected_profile
            print(
                f"{ticker:6s} {form:6s} -> {profile.profile_id:16s} (expected {expected_profile}) "
                f"confidence_low={profile.confidence_low} match={match} chars={len(excerpt)}"
            )
            out.append({
                "ticker": ticker,
                "company": name,
                "form": form,
                "expected_profile": expected_profile,
                "detected_profile": profile.profile_id,
                "confidence_low": profile.confidence_low,
                "match": match,
                "reason": profile.reason,
                "source_url": url,
                "source_note": "third-party mirror (financemarker.ru) of the issuer's filed report",
                "excerpt": excerpt,
            })
        except Exception as exc:  # noqa: BLE001
            print(f"{ticker:6s} {form:6s} -> ERROR {exc}")
            out.append({"ticker": ticker, "company": name, "form": form, "expected_profile": expected_profile, "error": str(exc)})
        finally:
            pdf_path.unlink(missing_ok=True)
        time.sleep(0.3)
    (HERE / "_financemarker_raw3.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
