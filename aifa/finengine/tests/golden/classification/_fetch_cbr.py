"""One-shot helper (not a test): fetch real CBR published-form pages for
Russian banks (RAS forms 0409806 balance sheet + 0409807 income statement)
and cross-check them against the live classifier.

regnum values confirmed against `https://www.cbr.ru/banking_sector/credit/FullCoList/`
(16.09.2026): Sberbank=1481, VTB=1000, Alfa-Bank=1326, Gazprombank=354,
Rosselkhozbank=3349.

Usage: .venv/Scripts/python.exe tests/golden/classification/_fetch_cbr.py
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from finengine.skills.extractors._classifier import detect_profile  # noqa: E402

UA = "Mozilla/5.0 (research; contact@example.com)"
HERE = Path(__file__).resolve().parent

BANKS = [
    ("Сбербанк", 1481),
    ("ВТБ", 1000),
    ("Альфа-Банк", 1326),
    ("Газпромбанк", 354),
    ("Россельхозбанк", 3349),
]
DT = "202404"  # публикуемая форма за 1кв2024 — доступна для всех пятерых на дату сбора


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


def strip_tags(html: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;|&#160;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text


def main() -> None:
    out = []
    for name, regnum in BANKS:
        try:
            balance = strip_tags(_get(f"https://cbr.ru/banking_sector/credit/coinfo/f806/?regnum={regnum}&dt={DT}"))
            time.sleep(0.3)
            income = strip_tags(_get(f"https://cbr.ru/banking_sector/credit/coinfo/f807/1904/?regnum={regnum}&dt={DT}"))
            time.sleep(0.3)
            excerpt = balance + "\n\n" + income
            profile = detect_profile(excerpt)
            match = profile.profile_id == "rsbu_bank_ru"
            print(f"{name:16s} regnum={regnum} -> {profile.profile_id:14s} confidence_low={profile.confidence_low} match={match} chars={len(excerpt)}")
            out.append({
                "bank": name,
                "regnum": regnum,
                "expected_profile": "rsbu_bank_ru",
                "detected_profile": profile.profile_id,
                "confidence_low": profile.confidence_low,
                "match": match,
                "reason": profile.reason,
                "source_url": f"https://cbr.ru/banking_sector/credit/coinfo/f806/?regnum={regnum}&dt={DT} ; https://cbr.ru/banking_sector/credit/coinfo/f807/1904/?regnum={regnum}&dt={DT}",
                "period": DT,
                "excerpt": excerpt,
            })
        except Exception as exc:  # noqa: BLE001
            print(f"{name:16s} -> ERROR {exc}")
            out.append({"bank": name, "regnum": regnum, "error": str(exc)})
        time.sleep(0.3)
    (HERE / "_cbr_raw.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
