"""Read frozen demo snapshots from /app/demo/."""
from __future__ import annotations
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# Path inside the container — see Dockerfile.backend (copies deploy/demo/ here).
DEMO_DIR = Path("/app/demo")


@dataclass
class DemoMeta:
    slug: str
    title: str
    description: str = ""
    company_name: str = ""
    period_current: str = ""
    period_prev: str = ""
    unit: str = ""

    @classmethod
    def from_dict(cls, slug: str, d: dict) -> "DemoMeta":
        return cls(
            slug=slug,
            title=d.get("title") or slug,
            description=d.get("description", ""),
            company_name=d.get("company_name", ""),
            period_current=d.get("period_current", ""),
            period_prev=d.get("period_prev", ""),
            unit=d.get("unit", ""),
        )


def list_demos() -> list[DemoMeta]:
    if not DEMO_DIR.exists():
        return []
    out: list[DemoMeta] = []
    for sub in sorted(DEMO_DIR.iterdir()):
        if not sub.is_dir():
            continue
        meta_path = sub / "meta.json"
        pdf_path = sub / "document.pdf"
        ext_path = sub / "extraction.json"
        if not (meta_path.exists() and pdf_path.exists() and ext_path.exists()):
            continue
        try:
            meta = DemoMeta.from_dict(sub.name, json.loads(meta_path.read_text()))
        except Exception:
            continue
        out.append(meta)
    return out


def get_demo(slug: str) -> Optional[DemoMeta]:
    for d in list_demos():
        if d.slug == slug:
            return d
    return None


def get_pdf_path(slug: str) -> Path:
    return DEMO_DIR / slug / "document.pdf"


def load_extraction(slug: str) -> dict:
    """Load the frozen extraction.json for a demo. Format mirrors the SSE
    'done' event + accumulated metrics so the UI can replay it instantly."""
    p = DEMO_DIR / slug / "extraction.json"
    return json.loads(p.read_text())
