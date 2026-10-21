"""Служба расчётов в процессе теста: настоящее приложение на TCP-порту, куда ходит ouroboros профиля
`contract`.

Ouroboros — контейнер, служба — процесс теста (почему — `compose.contract.yml`): подмены
конвейера живут в
процессе службы, и контейнерной службе пришлось бы нести тестовые подмены в образе. Сеть при этом
настоящая:
ouroboros вызывает `POST /internal/ouro/v1/<tool>` на хост через `host.docker.internal`, служба
открывает
к ouroboros WebSocket хода — оба конца по TCP.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pipeline_fixture
import pytest

from stand import ContractStand

ENGINE_PORT = 18183
ENGINE_TOKEN = "contract-engine-token"
# Из контейнера на хост: Docker Desktop пробрасывает `host.docker.internal` на loopback хоста; на
# Linux
# (`host-gateway`) служба должна слушать адрес моста — тогда `AIFA_CONTRACT_ENGINE_BIND=0.0.0.0`.
BIND_ENV = "AIFA_CONTRACT_ENGINE_BIND"
START_TIMEOUT_SEC = 20.0


@contextmanager
def running_engine(
    stand: ContractStand,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    doc: Callable[[], str] = pipeline_fixture.fixture_doc,
    real_documents: bool = False,
) -> Iterator[str]:
    """Приложение службы с фикстурным конвейером; отдаёт базовый адрес `http://127.0.0.1:<порт>`."""
    import uvicorn
    from finengine.api import main as app_main
    from finengine.assistant import composition
    from finengine.auth.db import User
    from finengine.auth.dependencies import get_current_user
    from finengine.config import settings

    pipeline_fixture.install(monkeypatch, tmp_path, doc=doc, real_documents=real_documents)
    monkeypatch.setattr(settings, "ouroboros_url", stand.ouro_http_url)
    monkeypatch.setattr(settings, "aifa_engine_token", ENGINE_TOKEN)
    composition.subtask_waits.cache_clear()
    composition.turn_gateway.cache_clear()
    composition.tool_api.cache_clear()
    composition.loop_desk.cache_clear()
    app_main.app.dependency_overrides[get_current_user] = lambda: User(
        id=pipeline_fixture.OWNER, username="contract-analyst"
    )
    # lifespan выключен: старт приложения поднимает планировщики и схемы баз, которых в тесте нет.
    config = uvicorn.Config(
        app_main.app,
        host=os.environ.get(BIND_ENV, "127.0.0.1"),
        port=ENGINE_PORT,
        lifespan="off",
        log_level="warning",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="contract-engine", daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{ENGINE_PORT}"
    try:
        _wait_until_up(base)
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        app_main.app.dependency_overrides.pop(get_current_user, None)
        composition.subtask_waits.cache_clear()
        composition.turn_gateway.cache_clear()
        composition.tool_api.cache_clear()
        composition.loop_desk.cache_clear()


def _wait_until_up(base: str) -> None:
    deadline = time.monotonic() + START_TIMEOUT_SEC
    while time.monotonic() < deadline:
        try:
            # Любой ответ, в том числе 401/503 внутреннего API, значит «слушает».
            httpx.post(f"{base}/internal/ouro/v1/aifa_open", timeout=1.0)
            return
        except httpx.TransportError:
            time.sleep(0.1)
    raise RuntimeError(f"служба не поднялась за {START_TIMEOUT_SEC} с на {base}")
