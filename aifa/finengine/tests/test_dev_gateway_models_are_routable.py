"""Модели dev-гейтвея есть в белом списке прокси И в списке второго апстрима.

ДВА МЕСТА, БЕЗ КОТОРЫХ МОДЕЛЬ ГЕЙТВЕЯ НЕ РАБОТАЕТ:
  1. `INFPROXY_ALLOWED_MODELS` — иначе прокси отобьёт 400 «model is not
     allowed», и чат ляжет с видом «модель недоступна» (грабля 02.08, дайджест);
  2. `INFPROXY_ALT_MODELS` — решение владельца 07.09.2026: весь dev-агент на
     Cloud.ru. Модель гейтвея, не названная в ALT, тихо уедет на OpenRouter
     через польский прокси — именно так 03.09 «переведённый» стенд отдал все
     реальные вызовы OpenRouter.

Отрицательный контроль: добавить в fallbacks модель вне ALT — красный.
"""
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _gateway_models() -> set[str]:
    d = json.loads((REPO / "deploy/gateway/openclaw.dev.json").read_text(encoding="utf-8"))
    m = d["agents"]["defaults"]["model"]
    ids = [m["primary"], *m.get("fallbacks", []), d["agents"]["defaults"]["subagents"]["model"]]
    return {i.split("/", 1)[1] for i in ids}   # `openrouter/` — имя провайдера в OpenClaw, к модели не относится


def _run_sh_list(var: str) -> set[str]:
    src = (REPO / "deploy/infproxy/run.sh").read_text(encoding="utf-8")
    m = re.search(rf'-e {var}="([^"]*)"', src)
    assert m, f"{var} не найдена в run.sh"
    return {x.strip() for x in m.group(1).split(",") if x.strip()}


def test_gateway_models_are_in_the_proxy_allowlist():
    missing = _gateway_models() - _run_sh_list("INFPROXY_ALLOWED_MODELS")
    assert not missing, f"прокси отобьёт 400: {sorted(missing)}"


def test_gateway_models_go_to_cloudru_not_openrouter():
    missing = _gateway_models() - _run_sh_list("INFPROXY_ALT_MODELS")
    assert not missing, f"уедут на OpenRouter мимо решения владельца: {sorted(missing)}"
