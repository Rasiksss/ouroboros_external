"""Атом-загрузка (гибридный планировщик, часть 3): backend скачивает публичный файл по url.

Сеть подменена `httpx.MockTransport` и резолвером-двойником: `download_public_file`, которого
зовёт `ephemeral._download`, подставляется той же функцией с transport/resolver. Кода у атома нет —
комната станции (`local_room`) не нужна, и тест это проверяет.
"""
from __future__ import annotations

from functools import partial

import httpx
import pytest

from finengine.contracts import DynamicArtifact
from finengine.policy.sandbox_profiles import DYNAMIC_SANDBOX_PROFILES, NetworkAccess
from finengine.skills.document_acquisition.downloader import (
    MAX_PUBLIC_FILE_BYTES,
    DownloadRejected,
    download_public_file,
)
from finengine.skills.document_acquisition.url_safety import UnsafeUrlError
from finengine.skills.spec import ephemeral, sealed_room
from finengine.skills.spec.atom_registry import NodeArgs
from finengine.skills.spec.ephemeral import DOWNLOAD_SCHEMA, AtomRequest, EphemeralAtom, to_atom_def
from finengine.skills.spec.graph_executor import ExecutionContext, RunInputs, validate_sandbox_dispatch
from test_ephemeral_in_graph import _execute, _plan, _stored

CSV = "DATE,DCOILBRENTEU\n2026-01-02,78.1\n2026-01-05,79.4\n"
PUBLIC_IPS = {"data.example.org": "93.184.216.34", "other.example.net": "93.184.216.35",
              "cdn.data.example.org": "93.184.216.36", "private.example.org": "10.0.0.5",
              "metadata.internal": "169.254.169.254"}


def _resolver(hostname: str, port: int) -> list[str]:  # noqa: ARG001
    if hostname not in PUBLIC_IPS:
        raise OSError(f"нет хоста в тестовом резолвере: {hostname}")
    return [PUBLIC_IPS[hostname]]


def _atom(name: str = "eph.brent_csv", url: str = "https://data.example.org/brent.csv",
          output_type: str = "dyn:brent_csv") -> EphemeralAtom:
    return EphemeralAtom(AtomRequest(name=name, purpose="ряд Brent", output_type=output_type,
                                     output_schema=DOWNLOAD_SCHEMA, url=url))


def _serve(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    """Подменить сеть загрузки; возвращает журнал запросов."""
    seen: list[httpx.Request] = []

    def _handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(ephemeral, "download_public_file", partial(
        download_public_file, transport=httpx.MockTransport(_handle), resolver=_resolver))
    return seen


def _csv_ok(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
    return httpx.Response(200, content=CSV.encode(), headers={"content-type": "text/csv; charset=utf-8"})


def _run(atom: EphemeralAtom) -> list[DynamicArtifact]:
    ctx = ExecutionContext(run_id="r", run_passport=None, inputs=RunInputs(), evidence_by_key={}, requires=())
    return to_atom_def(atom).executor(NodeArgs(), ctx)


@pytest.fixture(autouse=True)
def no_room(monkeypatch: pytest.MonkeyPatch) -> list:
    """Загрузке комната не нужна: любой вызов `run_code` — ошибка теста."""
    calls: list = []

    def _boom(*args, **kwargs):
        calls.append(args)
        raise AssertionError("атом-загрузка не должна исполнять код в комнате")

    monkeypatch.setattr(sealed_room, "run_code", _boom)
    return calls


# ---------------------------------------------------------------------------
# успех
def test_download_puts_the_text_into_a_dynamic_artifact_with_external_origin(monkeypatch) -> None:
    _serve(monkeypatch, _csv_ok)
    atom = _atom()

    (artifact,) = _run(atom)

    assert isinstance(artifact, DynamicArtifact)
    assert artifact.payload == {"url": "https://data.example.org/brent.csv",
                                "content_type": "text/csv", "text": CSV}
    assert artifact.origin == ["external"]
    assert artifact.output_type == "dyn:brent_csv" and artifact.producer == atom.producer
    assert artifact.input_evidence_ids == [], "у загрузки нет входов: данных Case она не видит"


def test_download_asks_for_data_formats_not_pdf(monkeypatch) -> None:
    seen = _serve(monkeypatch, _csv_ok)

    _run(_atom())

    assert "text/csv" in seen[0].headers["accept"] and "pdf" not in seen[0].headers["accept"]


def test_download_payload_url_is_the_final_one_after_a_redirect_inside_the_host(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/brent.csv":
            return httpx.Response(302, headers={"location": "https://data.example.org/v2/brent.csv"})
        return _csv_ok(request)

    _serve(monkeypatch, handler)

    (artifact,) = _run(_atom())

    assert artifact.payload["url"] == "https://data.example.org/v2/brent.csv"


def test_non_utf8_bytes_do_not_break_the_download(monkeypatch) -> None:
    _serve(monkeypatch, lambda request: httpx.Response(200, content=b"a,b\n\xff\xfe,1\n"))

    (artifact,) = _run(_atom())

    assert artifact.payload["text"].startswith("a,b\n") and "�" in artifact.payload["text"]


# ---------------------------------------------------------------------------
# отказы — исключения, их увидит итог хода
def test_a_redirect_to_another_host_is_rejected_and_never_requested(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://other.example.net/brent.csv"})

    seen = _serve(monkeypatch, handler)

    with pytest.raises(DownloadRejected, match="other.example.net.*data.example.org"):
        _run(_atom())

    assert [r.url.host for r in seen] == ["data.example.org"], "на чужой узел запрос не уходит"


def test_a_private_address_is_rejected_by_the_ssrf_guard(monkeypatch) -> None:
    seen = _serve(monkeypatch, _csv_ok)

    with pytest.raises(UnsafeUrlError, match="приватный"):
        _run(_atom(url="https://private.example.org/brent.csv"))

    assert seen == []


def test_a_redirect_to_a_private_address_is_rejected_by_the_ssrf_guard_before_the_host_rule(monkeypatch) -> None:
    _serve(monkeypatch, lambda request: httpx.Response(302, headers={"location": "http://metadata.internal/x"}))

    with pytest.raises(UnsafeUrlError, match="приватный"):
        _run(_atom())


def test_a_file_over_the_limit_is_rejected_while_streaming(monkeypatch) -> None:
    body = b"x" * (MAX_PUBLIC_FILE_BYTES + 1)
    _serve(monkeypatch, lambda request: httpx.Response(200, content=body))

    with pytest.raises(DownloadRejected, match="лимит"):
        _run(_atom())


def test_the_limit_holds_without_content_length(monkeypatch) -> None:
    """`content=bytes` в MockTransport сам ставит Content-Length; здесь заголовка нет — считается поток."""
    async def chunks():
        for _ in range(MAX_PUBLIC_FILE_BYTES // 1024 + 2):
            yield b"x" * 1024

    _serve(monkeypatch, lambda request: httpx.Response(200, content=chunks()))

    with pytest.raises(DownloadRejected, match="во время передачи"):
        _run(_atom())


@pytest.mark.parametrize("location", [
    "//other.example.net/brent.csv",                # без схемы: узел меняется, схема остаётся
    "https://data.example.org@other.example.net/",  # userinfo: «похоже на свой узел», а ходит на чужой
    "https://93.184.216.35/brent.csv",              # IP-литерал публичного адреса
    "https://data.example.org.other.example.net/",  # свой узел как префикс чужого
])
def test_a_redirect_that_leaves_the_host_is_rejected_whatever_its_form(monkeypatch, location: str) -> None:
    seen = _serve(monkeypatch, lambda request: httpx.Response(302, headers={"location": location}))

    with pytest.raises((DownloadRejected, UnsafeUrlError)):
        _run(_atom())

    assert [r.url.host for r in seen] == ["data.example.org"], "дальше первого запроса дело не идёт"


def test_a_relative_redirect_stays_on_the_host(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/brent.csv":
            return httpx.Response(302, headers={"location": "/files/../brent_v2.csv"})
        return _csv_ok(request)

    seen = _serve(monkeypatch, handler)

    assert _run(_atom())[0].payload["url"] == "https://data.example.org/brent_v2.csv"
    assert {r.url.host for r in seen} == {"data.example.org"}


def test_http_error_status_is_rejected(monkeypatch) -> None:
    _serve(monkeypatch, lambda request: httpx.Response(404))

    with pytest.raises(DownloadRejected, match="404"):
        _run(_atom())


async def test_download_public_file_takes_max_bytes_and_a_subdomain_of_the_allowed_domain() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "data.example.org":
            return httpx.Response(301, headers={"location": "https://cdn.data.example.org/a.csv"})
        return httpx.Response(200, content=b"12345678")

    kwargs = dict(allowed_domain="data.example.org", transport=httpx.MockTransport(handler), resolver=_resolver)

    ok = await download_public_file("https://data.example.org/a.csv", **kwargs)
    assert ok.content == b"12345678" and ok.redirect_chain == ("https://cdn.data.example.org/a.csv",)

    with pytest.raises(DownloadRejected, match="лимит"):
        await download_public_file("https://data.example.org/a.csv", max_bytes=4, **kwargs)


# ---------------------------------------------------------------------------
# профиль и AtomDef
def test_the_profile_is_external_enrichment_for_exactly_one_host_and_passes_dispatch() -> None:
    definition = to_atom_def(_atom())

    profile = validate_sandbox_dispatch(definition)

    assert definition.execution_kind == "io" and definition.requires == ()
    assert profile is DYNAMIC_SANDBOX_PROFILES[definition.sandbox_profile]
    assert profile.network_access is NetworkAccess.ALLOWLIST
    assert profile.allowed_hosts == ("data.example.org",)
    assert profile.code_execution is False


def test_two_download_atoms_for_one_host_both_build_and_share_the_profile() -> None:
    first = to_atom_def(_atom("eph.brent_csv", "https://data.example.org/brent.csv"))
    second = to_atom_def(_atom("eph.wti_csv", "https://data.example.org/wti.csv", "dyn:wti_csv"))
    other = to_atom_def(_atom("eph.gas_csv", "https://other.example.net/gas.csv", "dyn:gas_csv"))

    assert first.sandbox_profile == second.sandbox_profile != other.sandbox_profile
    assert DYNAMIC_SANDBOX_PROFILES[other.sandbox_profile].allowed_hosts == ("other.example.net",)


def test_the_version_depends_on_the_url() -> None:
    assert _atom().version != _atom(url="https://data.example.org/wti.csv").version


# ---------------------------------------------------------------------------
# в графе: узел исполняется, результат лежит в Case
def test_download_node_in_a_graph_stores_the_text_in_the_case(base, monkeypatch, severstal_text) -> None:
    _serve(monkeypatch, _csv_ok)
    runtime = base.with_atoms([to_atom_def(_atom())])
    planned = _plan(runtime, "eph.brent_csv", "dyn:brent_csv")

    result = _execute(runtime, planned, severstal_text)

    assert result.succeeded
    (stored,) = _stored(runtime, "dyn:brent_csv")
    assert stored.payload["text"] == CSV and stored.origin == ["external"]


def test_a_failed_download_fails_the_node_with_the_reason_and_stores_nothing(
    base, monkeypatch, severstal_text,
) -> None:
    _serve(monkeypatch, lambda request: httpx.Response(302, headers={"location": "https://other.example.net/x"}))
    runtime = base.with_atoms([to_atom_def(_atom())])
    planned = _plan(runtime, "eph.brent_csv", "dyn:brent_csv")

    result = _execute(runtime, planned, severstal_text)

    root = result.node_runs[planned.record.graph.root]
    assert root.status == "failed" and "other.example.net" in root.error
    assert _stored(runtime, "dyn:brent_csv") == []
