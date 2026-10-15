"""Per-user/per-project watchlist of monitored companies.

Storage: `data/users/{user_id}/projects/{project_id}/watchlist.json`
Shape:
    {
        "ru":    ["ВТБ", "Yandex", ...],
        "world": ["JP Morgan", "Apple", ...]
    }

On first read for a project, watchlist is seeded from the bundled
`daily_registry.yaml` (the customer-provided 79-company list). After
that the user can `update_watchlist(action, country, name)` to
add/remove names — that's why this is per-(user, project) storage
rather than a single global YAML.

`ru` / `world` are the two classification buckets used in the Daily
Digest playbook (Россия / Мировые рынки). When the user adds a new
company the agent must place it in one of these two buckets.
"""
from __future__ import annotations

import logging
from pathlib import Path

from finengine.store import state

log = logging.getLogger(__name__)

_REGISTRY_PATH = Path(__file__).parent / "ir_data" / "daily_registry.yaml"


def _registry_companies() -> dict[str, list[str]]:
    """Load the bundled default list (used to seed a fresh watchlist).
    Cheap to call each time — file is ~3KB and a fresh project doesn't
    happen often. Falls back to empty lists on any parse error so a
    malformed bundled file doesn't lock the user out of the feature."""
    try:
        # Tiny YAML parser already used by tools.py — but for nested
        # lists we need a real one. PyYAML is in scipy stack already.
        import yaml
        with _REGISTRY_PATH.open(encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
        companies = doc.get("companies", {})
        return {
            "ru": list(companies.get("ru", [])),
            "world": list(companies.get("world", [])),
        }
    except (OSError, ImportError, Exception) as e:  # noqa: BLE001
        log.warning("watchlist: failed to load bundled registry %s: %s",
                    _REGISTRY_PATH, e)
        return {"ru": [], "world": []}


def _ключ(project_id: str) -> str:
    """Ключ документа. Проект — в ключе, владелец — отдельно."""
    return state.safe_segment(project_id)


def load(user_id: str, project_id: str) -> dict[str, list[str]]:
    """Список наблюдения проекта. При первом обращении — из реестра."""
    data = state.get(state.WATCHLIST, owner=user_id, key=_ключ(project_id))
    if isinstance(data, dict) and ("ru" in data or "world" in data):
        return {"ru": list(data.get("ru", [])), "world": list(data.get("world", []))}
    # Первое обращение (или мусор) — засеваем из встроенного реестра.
    seeded = _registry_companies()
    save(user_id, project_id, seeded)
    return seeded


def save(user_id: str, project_id: str, data: dict[str, list[str]]) -> None:
    """Сохранить. Отказ хранилища не роняет тул — он должен ответить."""
    try:
        state.put(state.WATCHLIST, owner=user_id, key=_ключ(project_id), doc=data)
    except Exception as e:  # noqa: BLE001 — тул обязан ответить человеку
        log.warning("список наблюдения %s/%s не сохранился: %s", user_id, project_id, e)


def add(user_id: str, project_id: str, country: str, name: str) -> tuple[bool, str]:
    """Add company to bucket. Returns (changed, message)."""
    country = country.strip().lower()
    if country not in ("ru", "world"):
        return False, f"unknown country bucket {country!r} — use 'ru' or 'world'"
    name = name.strip()
    if not name:
        return False, "empty company name"
    data = load(user_id, project_id)
    # Case-insensitive dup check so "vtb" doesn't get added when "ВТБ"
    # already exists. Compare folded.
    existing_folded = {n.casefold() for n in data[country]}
    if name.casefold() in existing_folded:
        return False, f"«{name}» уже в watchlist ({country})"
    data[country].append(name)
    save(user_id, project_id, data)
    return True, f"добавлено «{name}» в {country}"


def remove(user_id: str, project_id: str, name: str) -> tuple[bool, str]:
    """Remove company from whichever bucket it lives in. Returns
    (changed, message). Match is case-insensitive."""
    name = name.strip()
    if not name:
        return False, "empty company name"
    data = load(user_id, project_id)
    folded = name.casefold()
    for bucket in ("ru", "world"):
        for i, existing in enumerate(data[bucket]):
            if existing.casefold() == folded:
                actual = data[bucket].pop(i)
                save(user_id, project_id, data)
                return True, f"убрано «{actual}» из {bucket}"
    return False, f"«{name}» не найдено в watchlist"
