"""Шлюз модели не пускает куда попало нашим оплаченным ключом (#149).

ЗАЧЕМ. infproxy — единственное место, где живёт НАСТОЯЩИЙ ключ провайдера: он
подставляется в исходящий запрос вместо внутреннего токена. До этой правки
сквозной маршрут форвардил ЛЮБОЙ путь и ЛЮБОЙ метод — то есть кто угодно с
внутренним токеном мог ходить нашим ключом куда угодно в API провайдера, включая
служебные ручки счёта и управления ключами.

ДВЕ ГРАНИЦЫ, и вторая важнее первой:
  1. путь обязан быть в белом списке — закрывает ИЗВЕСТНОЕ;
  2. сквозной маршрут ТОЛЬКО ЧИТАЕТ — закрывает НЕИЗВЕСТНОЕ: провайдер заведёт
     новую изменяющую ручку, и она не проедет, даже если мы о ней не узнаем.

Прокси обслуживает ЖИВОЙ прод, поэтому тесты здесь — не формальность: ошибка в
белом списке это не «неудобство», а прод без модели. Проверяется настоящее
приложение через TestClient, upstream подменён.
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


def _app(monkeypatch, **env):
    """Свежий модуль прокси с заданным окружением (списки читаются при импорте)."""
    monkeypatch.setenv("INFPROXY_TOKEN", TOKEN)
    monkeypatch.setenv("INFPROXY_UPSTREAM_KEY", "sk-upstream-secret")
    monkeypatch.setenv("INFPROXY_MODEL_CONCURRENCY", "prod|qwen/qwen3.6-plus=20")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    sys.modules.pop("app", None)
    return importlib.import_module("app")


def _client(mod):
    from fastapi.testclient import TestClient

    return TestClient(mod.app)


AUTH = {"Authorization": f"Bearer {TOKEN}"}


# ───────────────────────── пути ─────────────────────────


@pytest.mark.parametrize("path", [
    "/v1/credits",          # баланс счёта — шлюзу не нужен, проверено живым вызовом
    "/v1/keys",             # управление ключами
    "/v1/auth/keys/new",
    "/v1/../admin",
    "/v1/anything/at/all",
])
def test_paths_outside_the_allowlist_are_refused(monkeypatch, path):
    """Именно это и было дырой: любой путь уезжал наверх с настоящим ключом."""
    mod = _app(monkeypatch, INFPROXY_ALLOWED_PATHS="/models,/generation,/auth/key")
    with _client(mod) as c:
        r = c.get(path, headers=AUTH)
    assert r.status_code == 404, f"{path} проехал (код {r.status_code})"
    assert "not proxied" in r.text


@pytest.mark.parametrize("path", ["/v1/models", "/v1/models/qwen%2Fqwen3.6-plus",
                                  "/v1/generation", "/v1/auth/key"])
def test_provider_surface_the_gateway_needs_still_works(monkeypatch, path, httpx_mock=None):
    """Список не должен быть уже, чем реально нужно шлюзу: каталог моделей,
    стоимость генерации и проверка ключа при старте — без них провайдер шлюза
    не поднимется, и это будет прод без модели."""
    mod = _app(monkeypatch, INFPROXY_ALLOWED_PATHS="/models,/generation,/auth/key")
    seen: dict = {}

    class _Resp:
        status_code = 200
        content = b'{"ok":true}'
        headers = {"content-type": "application/json"}

    async def _fake_request(method, url, **kw):
        seen["url"] = str(url)
        seen["method"] = method
        return _Resp()

    with _client(mod) as c:
        mod._http.request = _fake_request  # type: ignore[union-attr]
        r = c.get(path, headers=AUTH)
    assert r.status_code == 200, f"{path} отбит, хотя нужен шлюзу"
    assert seen["method"] == "GET"


def test_unauthorized_is_refused_before_anything_else(monkeypatch):
    mod = _app(monkeypatch)
    with _client(mod) as c:
        assert c.get("/v1/models").status_code == 401


# ───────────────────── только чтение ─────────────────────


@pytest.mark.parametrize("method", ["POST"])
def test_passthrough_is_read_only(monkeypatch, method):
    """Граница, которая закрывает НЕИЗВЕСТНОЕ. Даже разрешённый путь нельзя
    вызывать изменяющим методом: завтра у провайдера появится POST-ручка,
    меняющая настройки счёта, и мы о ней не узнаем."""
    mod = _app(monkeypatch, INFPROXY_ALLOWED_PATHS="/models,/generation,/auth/key")
    with _client(mod) as c:
        r = c.request(method, "/v1/models", headers=AUTH, json={})
    assert r.status_code == 405, f"{method} на сквозном маршруте проехал"


def test_model_calls_still_go_through_the_balanced_route(monkeypatch):
    """Запрет POST на сквозном маршруте не должен задеть вызовы модели — они
    идут отдельным, балансируемым маршрутом."""
    mod = _app(monkeypatch)
    calls: dict = {}

    async def _fake_post(url, content=None, headers=None, **kw):
        calls["url"] = str(url)

        class _R:
            status_code = 200
            content = b'{"choices":[]}'
            headers = {"content-type": "application/json"}

        return _R()

    with _client(mod) as c:
        mod._http.post = _fake_post  # type: ignore[union-attr]
        r = c.post("/v1/chat/completions", headers=AUTH,
                   json={"model": "qwen/qwen3.6-plus", "messages": []})
    assert r.status_code == 200, "вызов модели сломан — это прод без модели"
    assert calls["url"].endswith("/chat/completions")


# ───────────────────────── модели ─────────────────────────


def test_unknown_model_is_refused_not_given_a_new_lane(monkeypatch):
    """Раньше любое имя модели заводило себе лейн по дефолтному пределу:
    опечатка в настройке или чужой вызов молча уезжали на наш оплаченный ключ,
    и узнали бы мы об этом из счёта."""
    mod = _app(monkeypatch)
    with _client(mod) as c:
        r = c.post("/v1/chat/completions", headers=AUTH,
                   json={"model": "openai/gpt-4o", "messages": []})
    assert r.status_code == 400
    assert "not allowed" in r.text


def test_allowed_models_default_to_the_lanes_we_configured(monkeypatch):
    """Список моделей не надо вести дважды: лейны уже перечисляют ровно те
    модели, которые мы сознательно завели. «prod|qwen/x» → «qwen/x»."""
    mod = _app(monkeypatch, INFPROXY_MODEL_CONCURRENCY="prod|qwen/a=20,dev|qwen/a=8,qwen/b=4")
    assert mod.ALLOWED_MODELS == {"qwen/a", "qwen/b"}


def test_no_configuration_means_no_restriction_but_loudly(monkeypatch, caplog):
    """Забытая настройка не должна класть прод — но и молчать о ней нельзя:
    тихое «разрешено всё» это ровно тот молчаливый отказ, ради которого весь
    этот файл и правился."""
    import logging

    monkeypatch.setenv("INFPROXY_MODEL_CONCURRENCY", "")
    monkeypatch.setenv("INFPROXY_ALLOWED_MODELS", "")
    mod = _app(monkeypatch, INFPROXY_MODEL_CONCURRENCY="", INFPROXY_ALLOWED_MODELS="")
    assert mod.ALLOWED_MODELS == set()
    with caplog.at_level(logging.WARNING, logger="infproxy"), _client(mod):
        pass
    assert any("НЕ ОГРАНИЧЕНЫ" in r.message for r in caplog.records), (
        "прокси молча пропускает любую модель и не говорит об этом при старте")


# ───────────────────── потолок длины ответа ─────────────────────


def test_absurd_output_request_is_clamped_not_passed_through(monkeypatch):
    """Просьба на миллион токенов — либо ошибка вызывающего, либо утечка
    бюджета. Зажимаем просьбу, а не режем ответ: обрыв потока посреди json
    выглядел бы как поломка модели."""
    mod = _app(monkeypatch, INFPROXY_MAX_OUTPUT_TOKENS="8000")
    sent: dict = {}

    async def _fake_post(url, content=None, headers=None, **kw):
        import json as _j
        sent["body"] = _j.loads(content)

        class _R:
            status_code = 200
            content = b"{}"
            headers = {"content-type": "application/json"}

        return _R()

    with _client(mod) as c:
        mod._http.post = _fake_post  # type: ignore[union-attr]
        c.post("/v1/chat/completions", headers=AUTH,
               json={"model": "qwen/qwen3.6-plus", "messages": [], "max_tokens": 1_000_000})
    assert sent["body"]["max_tokens"] == 8000


def test_normal_output_request_is_untouched(monkeypatch):
    """Потолок не должен резать обычную работу: анализ просит 6500 токенов."""
    mod = _app(monkeypatch, INFPROXY_MAX_OUTPUT_TOKENS="8000")
    sent: dict = {}

    async def _fake_post(url, content=None, headers=None, **kw):
        import json as _j
        sent["body"] = _j.loads(content)

        class _R:
            status_code = 200
            content = b"{}"
            headers = {"content-type": "application/json"}

        return _R()

    with _client(mod) as c:
        mod._http.post = _fake_post  # type: ignore[union-attr]
        c.post("/v1/chat/completions", headers=AUTH,
               json={"model": "qwen/qwen3.6-plus", "messages": [], "max_tokens": 6500})
    assert sent["body"]["max_tokens"] == 6500


# ─────── список моделей сверяется с тем, что код РЕАЛЬНО зовёт ───────
# ДВЕ ГРАБЛИ 02.08, обе — «собрал список не из того источника»:
#   1. список из лейнов дал бы только qwen, а основная модель шлюза glm-5.2.
#      Поймано до выкатки, руками.
#   2. список из конфигов ОБОИХ шлюзов пропустил deepseek — на нём работает
#      классификатор новостей, который зовёт модель ИЗ КОДА, минуя конфиг
#      шлюза. НЕ поймано: дайджест лёг на живом стенде с «model is not allowed».
# Отсюда этот тест: белый список обязан покрывать всё, что встречается в коде.
# Он не заменяет проверку конфигов шлюзов (их код не знает), но закрывает ту
# половину, на которой я обжёгся.


def test_allowlist_covers_every_model_the_code_can_call():
    import re  # noqa: PLC0415

    src = REPO / "src" / "finengine"
    run_sh = (REPO / "deploy" / "infproxy" / "run.sh").read_text(encoding="utf-8")
    m = re.search(r'INFPROXY_ALLOWED_MODELS="([^"]+)"', run_sh)
    assert m, "в run.sh нет явного списка моделей — значит он выводится из лейнов"
    allowed = {x.strip() for x in m.group(1).split(",") if x.strip()}

    # Провайдерские идентификаторы вида «вендор/модель» в коде. Узкий набор
    # вендоров намеренно: иначе в улов попадут пути, имена файлов и заголовки.
    vendors = ("deepseek", "openai", "anthropic", "google", "meta-llama",
               "mistralai", "z-ai", "qwen", "moonshotai", "x-ai", "cohere")
    called: set[str] = set()
    for path in src.rglob("*.py"):
        for hit in re.findall(r"""["']([a-z0-9-]+/[a-z0-9._-]+)["']""",
                              path.read_text(encoding="utf-8")):
            if hit.split("/", 1)[0] in vendors:
                called.add(hit)

    missing = sorted(called - allowed)
    assert not missing, (
        f"код зовёт модели, которых нет в белом списке шлюза: {missing}. "
        f"Такой вызов отобьётся 400 — ровно так лёг классификатор новостей "
        f"на deepseek. Добавь в INFPROXY_ALLOWED_MODELS в deploy/infproxy/run.sh"
    )
