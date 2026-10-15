"""Изолированная проверка релея прогресса (#83, Фаза 3) — БЕЗ живой станции и
без рвязки боевого пути. Прогоняет три звена насквозь на временных файлах:

  станц-писатель (station_progress.emit → progress-<nonce>.ndjson)
    → tail-логика демона (bridge._relay_progress, POST замокан)
      → бэк-приёмник (station_progress_ingest: маршрутизация по нонсу в очередь)

Плюс инварианты: без нонса emit — no-op; частичная строка не релеится; чужой
нонс не попадает в очередь; неверный токен → 401.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path


BR_PATH = Path(__file__).resolve().parents[1] / "deploy" / "station-bridge" / "bridge.py"


def _load_bridge():
    spec = importlib.util.spec_from_file_location("station_bridge_daemon", BR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_emit_noop_without_nonce(tmp_path, monkeypatch):
    from finengine.llm import station_progress as sp
    monkeypatch.setenv("FE_RPC_DIR", str(tmp_path))
    monkeypatch.delenv("FE_PROGRESS_NONCE", raising=False)
    sp.emit({"type": "plan", "steps": ["a"]})
    assert list(tmp_path.glob("progress-*.ndjson")) == []
    assert sp.enabled() is False


def test_emit_writes_ndjson(tmp_path, monkeypatch):
    from finengine.llm import station_progress as sp
    monkeypatch.setenv("FE_RPC_DIR", str(tmp_path))
    monkeypatch.setenv("FE_PROGRESS_NONCE", "nnc123")
    sp.emit({"type": "plan", "steps": ["Чтение", "Извлечение"]})
    sp.emit({"type": "metric_row", "metric": {"enum": "REVENUE"}})
    f = tmp_path / "progress-nnc123.ndjson"
    lines = f.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["type"] == "plan"
    assert json.loads(lines[1])["metric"]["enum"] == "REVENUE"


def test_full_relay_chain(tmp_path, monkeypatch):
    """emit → daemon tail → ingest queue, end-to-end on temp files."""
    from finengine.llm import station_progress as sp
    from finengine.api import station_progress_ingest as ingest

    nonce = "sess-abc"
    # station side: FE_RPC_DIR is the session's .model-rpc; the daemon globs
    # <glob>/progress-*.ndjson. Lay out <tmp>/s1/workspace/.model-rpc/.
    rpc = tmp_path / "s1" / "workspace" / ".model-rpc"
    rpc.mkdir(parents=True)
    monkeypatch.setenv("FE_RPC_DIR", str(rpc))
    monkeypatch.setenv("FE_PROGRESS_NONCE", nonce)

    sp.emit({"type": "extract_start", "total": 3})
    sp.emit({"type": "metric_row", "metric": {"enum": "REVENUE", "value_current": 100}})
    sp.emit({"type": "metric_row", "metric": {"enum": "NET_INCOME", "value_current": 20}})

    # backend side: register a consumer queue for the nonce.
    async def run():
        q = ingest.register(nonce)

        # daemon side: load bridge, point its glob at our temp tree, mock POST to
        # call the ingest route directly (proves routing without HTTP transport).
        br = _load_bridge()
        br.PROGRESS_GLOB = str(tmp_path / "*" / "workspace" / ".model-rpc")
        br.PROGRESS_INGEST_URL = "http://stub/internal/station-progress"
        br.PROGRESS_TOKEN = ""

        posted = {}

        class _Resp:
            status_code = 200

        class _Client:
            async def post(self, url, json=None, headers=None):
                # emulate the backend route: push events onto the nonce queue
                res = await ingest.ingest(ingest.ProgressBatch(**json), authorization="")
                posted["res"] = res
                return _Resp()

        await br._relay_progress(_Client())

        # consumer drains what the relay delivered
        got = []
        while not q.empty():
            got.append(q.get_nowait())
        ingest.unregister(nonce)
        return got, posted.get("res")

    got, res = asyncio.run(run())
    assert res == {"accepted": 3, "dropped": 0}
    assert [e["type"] for e in got] == ["extract_start", "metric_row", "metric_row"]
    assert got[1]["metric"]["enum"] == "REVENUE"


def test_partial_line_not_relayed(tmp_path, monkeypatch):
    """A line still being written (no trailing newline) must NOT be relayed until
    complete — else the daemon ships half a JSON object."""
    from finengine.api import station_progress_ingest as ingest

    rpc = tmp_path / "s1" / "workspace" / ".model-rpc"
    rpc.mkdir(parents=True)
    f = rpc / "progress-partial.ndjson"
    f.write_text('{"type":"plan","steps":["a"]}\n{"type":"metric_row","met', encoding="utf-8")

    async def run():
        q = ingest.register("partial")
        br = _load_bridge()
        br.PROGRESS_GLOB = str(tmp_path / "*" / "workspace" / ".model-rpc")
        br.PROGRESS_INGEST_URL = "http://stub"
        br.PROGRESS_TOKEN = ""

        class _Resp:
            status_code = 200

        class _Client:
            async def post(self, url, json=None, headers=None):
                await ingest.ingest(ingest.ProgressBatch(**json), authorization="")
                return _Resp()

        await br._relay_progress(_Client())
        got = []
        while not q.empty():
            got.append(q.get_nowait())
        # now the partial line completes; second tick relays it
        f.write_text('{"type":"plan","steps":["a"]}\n{"type":"metric_row","metric":{"enum":"X"}}\n', encoding="utf-8")
        await br._relay_progress(_Client())
        while not q.empty():
            got.append(q.get_nowait())
        ingest.unregister("partial")
        return got

    got = asyncio.run(run())
    assert len(got) == 2
    assert got[0]["type"] == "plan"
    assert got[1]["metric"]["enum"] == "X"


def test_ingest_unknown_nonce_dropped():
    from finengine.api import station_progress_ingest as ingest

    async def run():
        return await ingest.ingest(
            ingest.ProgressBatch(nonce="ghost", events=[{"type": "plan"}]),
            authorization="",
        )

    res = asyncio.run(run())
    assert res["accepted"] == 0 and res["dropped"] == 1 and res["reason"] == "no_consumer"


def test_ingest_bad_token_401(monkeypatch):
    from fastapi import HTTPException
    from finengine.api import station_progress_ingest as ingest

    monkeypatch.setenv("FINENGINE_OPENCLAW_GATEWAY_TOKEN", "secret-xyz")

    async def run():
        ingest.register("n")
        try:
            await ingest.ingest(ingest.ProgressBatch(nonce="n", events=[]), authorization="Bearer wrong")
            return None
        except HTTPException as e:
            return e.status_code
        finally:
            ingest.unregister("n")

    assert asyncio.run(run()) == 401
