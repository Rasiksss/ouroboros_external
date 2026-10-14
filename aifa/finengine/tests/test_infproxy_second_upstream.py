"""Второй апстрим: маршрут по модели, свой ключ, без прокси окружения.

ЗАЧЕМ (02.09.2026). Cloud.ru отдаёт те же модели по OpenAI-совместимому
протоколу и доступен с нашего VPS НАПРЯМУЮ: замер — 200 за 65 мс без прокси,
тогда как OpenRouter напрямую отвечает 403 и требует польский хоп. Но он же
дороже OpenRouter в 2,3–4,8 раза на одинаковых моделях, поэтому переключение
обязано быть явным и точечным, а не «включили провайдера».

Три границы, и каждая куплена рассуждением, а не вкусом:
  1. ПУСТОЙ СПИСОК = ВЫКЛЮЧЕНО. Забытая или полупустая настройка не должна
     увести НИ ОДИН вызов на более дорогого провайдера;
  2. ключ берётся СВОЙ. Токен клиента отображается в ключ OpenRouter; уехать с
     ним на Cloud.ru — это 401 у провайдера и утечка нашего ключа в чужой лог;
  3. клиент второго апстрима НЕ НАСЛЕДУЕТ HTTPS_PROXY. У контейнера он прописан
     ради OpenRouter; поехав через него, второй апстрим потерял бы единственное
     своё преимущество и получил бы лишнюю точку отказа.

Отрицательный контроль исполнен: снятие развилки в `_route` роняет тест про
маршрут, снятие `trust_env=False` — тест про прокси.

Сквозная проверка на стенде 02.09.2026 (отдельным контейнером, живой прокси не
трогался): GigaChat3.5 → Cloud.ru 200 за 0,7 с; qwen3.6-plus → OpenRouter 200
за 2,0 с; модель вне белого списка → 400.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "deploy" / "infproxy"))

TOKEN = "test-token"
ALT = "https://foundation-models.api.cloud.ru/v1"


def _app(monkeypatch, **env):
    monkeypatch.setenv("INFPROXY_TOKEN", TOKEN)
    monkeypatch.setenv("INFPROXY_UPSTREAM", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("INFPROXY_UPSTREAM_KEY", "sk-openrouter-secret")
    monkeypatch.setenv("INFPROXY_ALLOWED_MODELS", "qwen/qwen3.6-plus,ai-sage/GigaChat3.5-432B-A28B")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    sys.modules.pop("app", None)
    return importlib.import_module("app")


def _capture(mod, monkeypatch):
    """Перехватывает исходящий вызов: куда, с каким ключом, каким клиентом."""
    seen: dict = {}

    async def fake_unary(url, headers, raw, lane, sem, client=None):
        from fastapi.responses import JSONResponse
        seen.update(url=url, key=headers.get("Authorization", ""), client=client)
        return JSONResponse({"ok": True})

    monkeypatch.setattr(mod, "_unary", fake_unary)
    return seen


BODY = {"model": "ai-sage/GigaChat3.5-432B-A28B", "messages": [{"role": "user", "content": "."}]}
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def test_without_the_model_list_nothing_moves(monkeypatch):
    """Апстрим задан, список моделей пуст — вызов идёт на ОСНОВНОЙ."""
    from fastapi.testclient import TestClient
    mod = _app(monkeypatch, INFPROXY_UPSTREAM_ALT=ALT,
               INFPROXY_UPSTREAM_ALT_KEY="alt-secret", INFPROXY_ALT_MODELS="")
    seen = _capture(mod, monkeypatch)
    with TestClient(mod.app) as c:
        c.post("/v1/chat/completions", json=BODY, headers=AUTH)
    assert seen["url"].startswith("https://openrouter.ai"), (
        f"пустой список увёл вызов на второй апстрим: {seen['url']}")
    assert "sk-openrouter-secret" in seen["key"]


def test_a_named_model_goes_to_the_alt_with_its_own_key(monkeypatch):
    """Названная модель — второй апстрим и ЕГО ключ, не наш openrouter-овский."""
    from fastapi.testclient import TestClient
    mod = _app(monkeypatch, INFPROXY_UPSTREAM_ALT=ALT,
               INFPROXY_UPSTREAM_ALT_KEY="alt-secret",
               INFPROXY_ALT_MODELS="ai-sage/GigaChat3.5-432B-A28B")
    seen = _capture(mod, monkeypatch)
    with TestClient(mod.app) as c:
        c.post("/v1/chat/completions", json=BODY, headers=AUTH)
    assert seen["url"].startswith(ALT), f"поехало не туда: {seen['url']}"
    assert "alt-secret" in seen["key"], "ушёл не тот ключ"
    assert "sk-openrouter-secret" not in seen["key"], (
        "🔴 ключ OpenRouter уехал чужому провайдеру")
    assert seen["client"] is not None, "второй апстрим пошёл клиентом основного"


def test_a_model_not_in_the_list_stays_on_the_primary(monkeypatch):
    """Развилка точечная: соседняя модель остаётся на основном апстриме."""
    from fastapi.testclient import TestClient
    mod = _app(monkeypatch, INFPROXY_UPSTREAM_ALT=ALT,
               INFPROXY_UPSTREAM_ALT_KEY="alt-secret",
               INFPROXY_ALT_MODELS="ai-sage/GigaChat3.5-432B-A28B")
    seen = _capture(mod, monkeypatch)
    with TestClient(mod.app) as c:
        c.post("/v1/chat/completions",
               json={"model": "qwen/qwen3.6-plus", "messages": [{"role": "user", "content": "."}]},
               headers=AUTH)
    assert seen["url"].startswith("https://openrouter.ai"), seen["url"]
    assert seen["client"] is None


def test_the_alt_client_ignores_the_environment_proxy(monkeypatch):
    """У контейнера HTTPS_PROXY стоит ради OpenRouter — второй апстрим им не едет."""
    from fastapi.testclient import TestClient
    monkeypatch.setenv("HTTPS_PROXY", "http://144.31.112.12:8888")
    mod = _app(monkeypatch, INFPROXY_UPSTREAM_ALT=ALT,
               INFPROXY_UPSTREAM_ALT_KEY="alt-secret",
               INFPROXY_ALT_MODELS="ai-sage/GigaChat3.5-432B-A28B")
    with TestClient(mod.app):
        assert mod._http_alt is not None, "клиент второго апстрима не поднялся"
        assert mod._http_alt.trust_env is False, (
            "🔴 клиент второго апстрима наследует HTTPS_PROXY окружения — "
            "поедет через польский хоп, ради обхода которого он и заводился")
        assert mod._http is not None and mod._http.trust_env is True, (
            "основной апстрим ОБЯЗАН читать прокси из окружения: без него "
            "OpenRouter недоступен (проверено — 403 напрямую)")


# ───────────── точные URL: у баз разные хвосты, /v1 и /api/v1 ─────────────

def test_exact_urls_for_both_bases(monkeypatch):
    """Cloud.ru база кончается на `/v1`, OpenRouter — на `/api/v1`. Прокси
    клеит к ним ОДИН и тот же `/chat/completions`, и оба URL обязаны выйти
    верными до символа — иначе 404 от провайдера будет читаться как «модель
    недоступна». Проверка ПОЛНОЙ строкой, не префиксом.
    """
    from fastapi.testclient import TestClient
    mod = _app(monkeypatch, INFPROXY_UPSTREAM_ALT=ALT,
               INFPROXY_UPSTREAM_ALT_KEY="alt-secret",
               INFPROXY_ALT_MODELS="ai-sage/GigaChat3.5-432B-A28B")
    seen = _capture(mod, monkeypatch)
    with TestClient(mod.app) as c:
        c.post("/v1/chat/completions", json=BODY, headers=AUTH)
        alt_url = seen["url"]
        c.post("/v1/chat/completions",
               json={"model": "qwen/qwen3.6-plus", "messages": [{"role": "user", "content": "."}]},
               headers=AUTH)
        primary_url = seen["url"]
    assert alt_url == "https://foundation-models.api.cloud.ru/v1/chat/completions", alt_url
    assert primary_url == "https://openrouter.ai/api/v1/chat/completions", primary_url


def test_legacy_chat_path_composes_the_same_urls(monkeypatch):
    """Старая ручка `/chat/completions` (без /v1) — та же сборка, те же URL."""
    from fastapi.testclient import TestClient
    mod = _app(monkeypatch, INFPROXY_UPSTREAM_ALT=ALT,
               INFPROXY_UPSTREAM_ALT_KEY="alt-secret",
               INFPROXY_ALT_MODELS="ai-sage/GigaChat3.5-432B-A28B")
    seen = _capture(mod, monkeypatch)
    with TestClient(mod.app) as c:
        c.post("/chat/completions", json=BODY, headers=AUTH)
    assert seen["url"] == "https://foundation-models.api.cloud.ru/v1/chat/completions", seen["url"]


# ───────────── регистр имени: гейтвей шлёт строчными, Cloud.ru ждёт каноническое ─────────────

def test_lowercased_model_id_from_the_gateway_is_routed_and_canonicalised(monkeypatch):
    """07.09.2026: OpenClaw шлёт `deepseek-ai/deepseek-v4-pro`; побайтная сверка
    давала 400, а гейтвей показывал это как «provider rejected the request
    schema». Cloud.ru регистр чувствителен — наверх уходит каноническое имя."""
    import json as _json
    from fastapi.testclient import TestClient
    mod = _app(monkeypatch, INFPROXY_UPSTREAM_ALT=ALT, INFPROXY_UPSTREAM_ALT_KEY="alt-secret",
               INFPROXY_ALLOWED_MODELS="qwen/qwen3.6-plus,deepseek-ai/DeepSeek-V4-Pro",
               INFPROXY_ALT_MODELS="deepseek-ai/DeepSeek-V4-Pro")
    seen: dict = {}

    async def fake_unary(url, headers, raw, lane, sem, client=None):
        from fastapi.responses import JSONResponse
        seen.update(url=url, model=_json.loads(raw)["model"], lane=lane, client=client)
        return JSONResponse({"ok": True})

    monkeypatch.setattr(mod, "_unary", fake_unary)
    with TestClient(mod.app) as c:
        r = c.post("/v1/chat/completions", headers=AUTH,
                   json={"model": "deepseek-ai/deepseek-v4-pro", "messages": [{"role": "user", "content": "."}]})
    assert r.status_code == 200, f"строчный id отбит: {r.text}"
    assert seen["model"] == "deepseek-ai/DeepSeek-V4-Pro", f"наверх ушло {seen['model']!r} — Cloud.ru ответит 404"
    assert seen["url"] == "https://foundation-models.api.cloud.ru/v1/chat/completions"
    assert seen["lane"].endswith("|deepseek-ai/DeepSeek-V4-Pro"), "лейн раздвоился по регистру"


def test_unknown_model_is_still_refused_regardless_of_case(monkeypatch):
    """Канонизация не открывает дверь: чужое имя в любом регистре — 400."""
    from fastapi.testclient import TestClient
    mod = _app(monkeypatch, INFPROXY_ALLOWED_MODELS="qwen/qwen3.6-plus")
    with TestClient(mod.app) as c:
        r = c.post("/v1/chat/completions", headers=AUTH,
                   json={"model": "Anthropic/Claude-Opus-4.5", "messages": [{"role": "user", "content": "."}]})
    assert r.status_code == 400
