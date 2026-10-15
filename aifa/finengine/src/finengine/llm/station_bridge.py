"""File-RPC LLM client for the SEALED Workstation station (no network).

The Workstation ws-sandbox blocks all sockets at the seccomp level, so the
station cannot call the model over HTTP. Instead this drop-in writes each
request as a file in ``FE_RPC_DIR`` and polls for a response file, which a
gateway-side model-bridge daemon fills by calling infproxy. The station never
touches the network — its only capability through the bridge is "make a model
call via infproxy", nothing else.

Surface mirrors ``finengine.llm.openrouter`` so the station swaps transparently
(see the FE_RPC_DIR-gated monkeypatch in the station scripts). See
``deploy/station-bridge/bridge.py`` for the daemon and ARCHITECTURE_DECISIONS.md
decision 5 (model from station mediated by the gateway).
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars as _cv
import json
import os
import time
import uuid
from typing import AsyncIterator

_MAX_COMPLETION_TOKENS = 16000
_TEMPERATURE = 0.6


def _rpc_dir() -> str:
    return os.environ.get("FE_RPC_DIR", "/workspace/.model-rpc")


def _poll_sec() -> float:
    return float(os.environ.get("FE_RPC_POLL_SEC", "0.1"))


def _timeout_sec() -> float:
    # 400 > демонских 360 (FE_RPC_UPSTREAM_TIMEOUT): демон успевает ответить
    # (успех или говорящая ошибка) ВНУТРИ окна клиента — ретрай идёт по resp-файлу,
    # а не по слепому таймауту, и не плодит параллельных дублей вызова модели.
    # Сам клиентский таймаут остаётся санитарным рубежом на случай мёртвого демона.
    return float(os.environ.get("FE_RPC_TIMEOUT_SEC", "400"))


_REASONING_CTX: _cv.ContextVar[str | None] = _cv.ContextVar(
    "finengine_station_reasoning_effort", default=None
)


def set_reasoning_effort(effort: str | None) -> None:
    """Mirror of openrouter.set_reasoning_effort (task-local override)."""
    if effort and effort.lower() in ("low", "medium", "high"):
        _REASONING_CTX.set(effort.lower())
    else:
        _REASONING_CTX.set(None)


def _write_atomic(path: str, data: str) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, path)  # atomic: daemon never sees a partial .req.json


def _model_for_request(model: str | None) -> str | None:
    """Имя модели, которое уедет демону; None — «решает демон» (FE_RPC_DEFAULT_MODEL).

    В запечатанной комнате настройка модели НЕ ВИДНА: лаунчер экспортирует
    только нонс и FE_RPC_DIR, поэтому `settings.openrouter_model` там всегда
    равен умолчанию config.py, и вызывающие честно передают его как «модель».
    07.09.2026 так ушли все вызовы извлечения: станция настроена на DeepSeek
    через Cloud.ru (FE_RPC_DEFAULT_MODEL), а комната слала qwen на OpenRouter —
    ключ кончился (402), затем 403, прогоны падали на стадии сжатия. Пины в
    экстракторе сняли утром того же дня — умолчание настройки вело туда же.
    Имя модели станции знает ОДИН компонент — демон моста; без явной настройки
    в окружении комнаты выбирает он. Сторож: test_the_room_does_not_choose_the_model.
    """
    if not model:
        return None
    if os.environ.get("FINENGINE_OPENROUTER_MODEL"):
        return model  # в комнате настроено явно — уважаем
    from finengine.config import settings  # noqa: PLC0415 — лениво, как openrouter ниже
    return None if model == settings.openrouter_model else model


async def complete_with_usage(
    prompt: str,
    model: str | None = None,
    system: str | None = None,
    retries: int = 3,
    response_format: dict | None = None,
    max_output: int | None = None,
    *,
    seed: int | None = None,
    temperature: float | None = None,
    pin_provider: str | None = None,
    reasoning_effort: str | None = None,
    omit_max_output: bool = False,
) -> tuple[str, dict]:
    """Drop-in for openrouter.complete_with_usage over file-RPC.

    Ретраи зеркалят inline openrouter (range(retries) + бэкофф 2*(n+1)): раньше
    параметр retries ИГНОРИРОВАЛСЯ — один медленный вызов (>таймаута) валил
    заземление анализа целиком (флейк B2/OTP, «без заземления»)."""
    d = _rpc_dir()
    os.makedirs(d, exist_ok=True)

    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    base_req: dict = {
        "model": _model_for_request(model),  # None → daemon picks its default station model
        "messages": messages,
        # `None` — ЯВНОЕ «потолка нет» (#257, стадия сжатия двухстадийного
        # извлечения). Просто НЕ класть ключ нельзя: демон трактует его
        # отсутствие как «поставь свой дефолт 16000», и потолок вернулся бы
        # обходным путём, обрезав выжимку на середине.
        "max_completion_tokens": (
            None if omit_max_output else (max_output or _MAX_COMPLETION_TOKENS)
        ),
        "temperature": _TEMPERATURE if temperature is None else temperature,
    }
    eff = (reasoning_effort or _REASONING_CTX.get() or "").lower() or None
    if eff in ("low", "medium", "high"):
        base_req["reasoning_effort"] = eff
    if response_format:
        base_req["response_format"] = response_format
    if seed is not None:
        base_req["seed"] = seed

    # lazy — `openrouter` тянет тяжёлый `openai`, а этот модуль ставится ВМЕСТО
    # него в комнате без сети (та же причина, что у `install_if_sealed` ниже).
    from finengine.llm.openrouter import ModelSilent  # noqa: PLC0415

    poll = _poll_sec()
    attempts = max(1, retries)
    last_err: Exception | None = None
    for attempt in range(attempts):
        rid = uuid.uuid4().hex  # свежий rid на попытку: старый resp не путается с новым
        req_path = os.path.join(d, f"{rid}.req.json")
        resp_path = os.path.join(d, f"{rid}.resp.json")
        _write_atomic(req_path, json.dumps({"id": rid, **base_req}, ensure_ascii=False))

        deadline = time.monotonic() + _timeout_sec()
        try:
            while True:
                if os.path.exists(resp_path):
                    with open(resp_path, encoding="utf-8") as f:
                        resp = json.load(f)
                    try:
                        os.remove(resp_path)
                    except OSError:
                        pass
                    if resp.get("error"):
                        # МОЛЧАНИЕ МОДЕЛИ — ОТКАЗ, КОТОРЫЙ НЕ ПОВТОРЯЮТ. Инлайновый
                        # путь вывел его из петли 11.08 (`openrouter.ModelSilent`),
                        # мостовой остался без этого до 20.08: демон уже отвечал
                        # «бюджет ушёл на размышления», а клиент честно слал вызов
                        # заново — тот же бюджет, тот же исход, до 400 с на попытку.
                        # Признак ставит демон: причину знает только его сторона.
                        if resp.get("silent"):
                            raise ModelSilent(f"station model-bridge: {resp['error']}")
                        raise RuntimeError(f"station model-bridge error: {resp['error']}")
                    usage = resp.get("usage") or {"in": 0, "out": 0, "cached": 0, "model": model or ""}
                    return resp.get("text", ""), usage
                if time.monotonic() > deadline:
                    for p in (req_path, resp_path):
                        try:
                            os.remove(p)
                        except OSError:
                            pass
                    raise TimeoutError(
                        f"station model-bridge timeout after {_timeout_sec()}s (rid={rid})")
                await asyncio.sleep(poll)
        except ModelSilent:
            raise  # ровно как inline: повтор даст тот же исход — не тратим 400 с
        except Exception as e:  # noqa: BLE001 — мост/модель, ретраим как inline
            last_err = e
            if attempt + 1 < attempts:
                await asyncio.sleep(2 * (attempt + 1))
    raise last_err if last_err is not None else RuntimeError("station model-bridge: no attempts")


async def complete(
    prompt: str,
    model: str | None = None,
    system: str | None = None,
    retries: int = 3,
    response_format: dict | None = None,
    max_output: int | None = None,
    *,
    seed: int | None = None,
    temperature: float | None = None,
    pin_provider: str | None = None,
    reasoning_effort: str | None = None,
    omit_max_output: bool = False,
) -> str:
    text, _ = await complete_with_usage(
        prompt=prompt, model=model, system=system, retries=retries,
        response_format=response_format, max_output=max_output, seed=seed,
        temperature=temperature, pin_provider=pin_provider, reasoning_effort=reasoning_effort,
        omit_max_output=omit_max_output,
    )
    return text


async def stream(
    prompt: str,
    system_prompt: str | None = None,
    model: str | None = None,
    response_format: dict | None = None,
    max_output: int | None = None,
    usage_sink: dict | None = None,
    reasoning_effort: str | None = None,
) -> AsyncIterator[str]:
    """No true streaming over file-RPC — yield the whole completion once
    (same pragmatic shortcut as the GigaChat wrapper)."""
    text, usage = await complete_with_usage(
        prompt=prompt, model=model, system=system_prompt,
        max_output=max_output, reasoning_effort=reasoning_effort,
    )
    if usage_sink is not None:
        usage_sink.update(usage)
    yield text


def install_if_sealed() -> None:
    """Подменить http-клиент модели файловым мостом, если мы в запечатанной
    станции (признак — выставленный `FE_RPC_DIR`). Вне станции — no-op, сетевой
    dev-путь не задет.

    Живёт в ядре, а не копией в каждом входном скрипте намеренно: копий уже две
    (`extract.py`, `analyze.py`), и с появлением исполнителя спек была бы третья.
    Без этой подмены движок в станции пошёл бы в HTTP, упёрся в seccomp-запрет
    сокетов и свалился бы в путь, которому нужны отсутствующие в образе
    библиотеки, — то есть непонятной ошибкой вместо работы."""
    # 🔴 ПРИЗНАК СТАНЦИИ — КАТАЛОГ МОСТА, А НЕ ПЕРЕМЕННАЯ (17.08, прогон ВТБ).
    # Раньше мост ставился ТОЛЬКО по заданному `FE_RPC_DIR`, а прогресс
    # (`station_progress`) писал в тот же каталог ПО УМОЛЧАНИЮ. Асимметрия
    # стоила прогона владельца: модель запустила `extract.py` напрямую, минуя
    # лаунчер, который переменную и выставляет, — прогресс на экран шёл, а
    # вызовы модели ушли в HTTP, упёрлись в запечатанную сеть и вернули пустой
    # батч. Пользователь увидел таблицу из 110 прочерков и объяснение «нет
    # доступа к модели через bridge», сочинённое самой моделью.
    #
    # Теперь достаточно, чтобы каталог моста СУЩЕСТВОВАЛ: его создаёт комната
    # станции, и вне станции его нет — значит признак тот же, а обход лаунчера
    # больше не обезоруживает движок.
    if not os.environ.get("FE_RPC_DIR") and not os.path.isdir(_rpc_dir()):
        return
    from finengine.llm import openrouter as _or  # noqa: PLC0415 — только в станции
    _or.complete = complete
    _or.complete_with_usage = complete_with_usage
    _or.stream = stream
    _or.set_reasoning_effort = set_reasoning_effort


# ── КЭШ ПЕРВОЙ СТАДИИ: спросить и положить через мост ────────────────────────
#
# Комната запечатана и живёт восемнадцать минут — помнить сжатие ей негде.
# Помнит бэкенд, носит демон, тем же способом, что текст документа.
#
# ⚠️ ОТКАЗ КЭША — НЕ ОТКАЗ ПРОГОНА, и это правило важнее самого кэша. Любая
# беда здесь означает только, что сжатие придётся посчитать заново: 216 секунд
# вместо нуля. Уронить из-за этого извлечение значило бы поменять ускорение на
# надёжность — ровно наоборот тому, зачем кэш заводили.
# ⚠️ ЖДЁМ СЕКУНДЫ, А НЕ ДЕСЯТКИ, И ТОЛЬКО ОДИН РАЗ ЗА ПРОЦЕСС.
#
# Демон опрашивает комнату каждые 0,1 с (`FE_RPC_POLL_SEC`), то есть живой
# отвечает заведомо быстрее секунды. Долгое ожидание нужно только МЁРТВОМУ —
# и это ровно тот молчаливый налог, который мы уже платили однажды: старый
# демон + новая полка = двадцать секунд на каждый разбор (см. методику, M1).
#
# Здесь налог был бы хуже: сжатий за прогон несколько. Поэтому одна проба —
# и, если демон не ответил, кэш выключается до конца процесса. Ускорение,
# которое замедляет, — не ускорение.
_CACHE_WAIT_SEC = 2.0
_cache_dead = False


def _cache_ask(note: dict) -> dict:
    """Положить записку и дождаться ответа. Пусто — считаем промахом."""
    global _cache_dead
    if _cache_dead:
        return {}
    d = _rpc_dir()
    if not os.path.isdir(d):
        return {}
    rid = uuid.uuid4().hex
    _write_atomic(os.path.join(d, f"{rid}.cache.json"), json.dumps({"id": rid, **note},
                                                                   ensure_ascii=False))
    resp = os.path.join(d, f"{rid}.cacheresp.json")
    deadline = time.monotonic() + _CACHE_WAIT_SEC
    while time.monotonic() < deadline:
        if os.path.exists(resp):
            with open(resp, encoding="utf-8") as f:
                body = json.load(f)
            with contextlib.suppress(OSError):
                os.remove(resp)
            return body if isinstance(body, dict) else {}
        time.sleep(0.2)
    # Демон не ответил: он либо старой выкатки и про эти записки не знает, либо
    # его нет вовсе. Больше не спрашиваем — иначе каждое сжатие платит секунды
    # ни за что. Записку уберём за собой, чтобы не копить мусор в комнате.
    _cache_dead = True
    with contextlib.suppress(OSError):
        os.remove(os.path.join(d, f"{rid}.cache.json"))
    return {}


def compression_cached(nonce: str, fingerprint: str) -> str:
    """Готовое сжатие по отпечатку промпта или пустая строка."""
    if not nonce or not fingerprint:
        return ""
    body = _cache_ask({"nonce": nonce, "fingerprint": fingerprint})
    return str(body.get("payload") or "") if body.get("hit") else ""


def remember_compression(nonce: str, fingerprint: str, payload: str) -> None:
    """Запомнить РАЗОБРАННОЕ сжатие. Мусор сюда не кладут — см. шапку роута."""
    if not nonce or not fingerprint or not payload:
        return
    _cache_ask({"nonce": nonce, "fingerprint": fingerprint, "payload": payload})
