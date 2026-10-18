"""Временные атомы: контракт от планировщика → код от автора или адрес загрузки → AtomDef."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from functools import partial
from urllib.parse import urlsplit

from pydantic import BaseModel

from finengine.config import settings
from finengine.contracts import DynamicArtifact, FileArtifact
from finengine.policy.sandbox_profiles import (
    NetworkAccess, SandboxProfile, external_enrichment, register_dynamic_profile,
)
from finengine.skills.analysis.external import require_web_search
from finengine.skills.document_acquisition.downloader import DownloadRejected, download_public_file
from finengine.skills.document_acquisition.page_text import decode_text, html_to_text, is_html, meta_charset
from finengine.skills.spec import sealed_room
from finengine.skills.spec.atom_registry import AtomDef, AtomRegistry, NodeArgs
from finengine.skills.spec.room_files import persist_room_files
from finengine.skills.spec.target import InputSpec, OutputSpec
from finengine.store.cases import evidence_id_of

_NAME = re.compile(r"^eph\.[a-z0-9_]{3,40}$")
_ORIGIN_BY_TYPE = {"ExtractedMetric": "reported", "CalculatedMetric": "calculated",
                   "UserProvidedMetric": "user_provided", "ExtractedStructure": "reported",
                   "QuoteEvidence": "reported", "ConditionEvidence": "reported"}

_PROBLEM_PREVIEW = 500
# NodeRun.error — строка: по этому началу DynamicRun отличает сбой станции от ошибки кода.
STATION_FAULT_PREFIX = "комната станции недоступна: "

DOWNLOAD_SCHEMA = {
    "type": "object", "required": ["url", "content_type", "text"],
    "properties": {"url": {"type": "string"}, "content_type": {"type": "string"},
                   "text": {"type": "string"}},
}

ROOM_PROFILE = register_dynamic_profile("sealed_room", SandboxProfile(
    name="sealed_room", network_access=NetworkAccess.NONE, code_execution=True,
    rare_explicit_grant=True,
    description="Сгенерированный код в запечатанной комнате станции: без сети и окружения backend.",
))


@dataclass(frozen=True)
class AtomRequest:
    """Контракт атома, который описал планировщик."""
    name: str
    purpose: str
    output_type: str
    output_schema: dict                 # для загрузки — DOWNLOAD_SCHEMA (ставит parse_decision)
    requires: tuple[InputSpec, ...] = ()
    url: str = ""                       # непусто → атом-загрузка

    @property
    def is_download(self) -> bool:
        return bool(self.url)


@dataclass(frozen=True)
class EphemeralAtom:
    """Контракт + код, прошедший самопроверку (у загрузки кода нет)."""
    request: AtomRequest
    code: str = ""

    @property
    def version(self) -> str:
        blob = json.dumps(asdict(self.request), sort_keys=True, ensure_ascii=False) + self.code
        return hashlib.sha256(blob.encode()).hexdigest()[:12]

    @property
    def producer(self) -> str:
        return f"{self.request.name}@{self.version}"


def check_request(request: AtomRequest, registry: AtomRegistry) -> list[str]:
    """Правила, которые держит код, а не промпт. Пустой список — контракт годен."""
    problems = []
    if not _NAME.match(request.name):
        problems.append(f"имя {request.name!r} не вида eph.<snake_case>")
    if not request.output_type.startswith("dyn:"):
        problems.append("временный атом производит только dyn:* — типы отчётности ему запрещены")
    if request.is_download:
        problems += _download_problems(request)
    elif not request.requires:
        problems.append("атому с кодом без входов не из чего считать: внешние данные — атомом-загрузкой (url)")
    for spec in request.requires:
        if not spec.input_type.startswith("dyn:") and not registry.producers_of(spec.input_type, spec.concept):
            problems.append(f"вход {spec.input_type}:{spec.concept} никто не производит")
    problems += [f"поле {path} без x-unit" for path in _numbers_without_unit(request.output_schema)]
    return problems


def to_atom_def(atom: EphemeralAtom) -> AtomDef:
    """Временный атом как обычная запись реестра: дальше его не отличить от встроенного."""
    request = atom.request
    common = {
        "name": request.name, "version": atom.version, "requires": request.requires,
        "produces": (OutputSpec(output_type=request.output_type),),
        "cache_policy": "run_scoped", "cost_class": "cheap",
    }
    if request.is_download:
        return AtomDef(**common, sandbox_profile=_download_profile(request.url),
                       execution_kind="io", executor=partial(_download, atom))
    return AtomDef(**common, sandbox_profile=ROOM_PROFILE, execution_kind="generated",
                   executor=partial(_execute, atom))


def group_inputs(evidence: tuple[BaseModel, ...]) -> dict[str, list[dict]]:
    """Вход кода атома: «тип → список объектов». Тот же вид — в самопроверке и в графе."""
    grouped: dict[str, list[dict]] = {}
    for item in evidence:
        label = item.output_type if isinstance(item, DynamicArtifact) else type(item).__name__
        grouped.setdefault(label, []).append(item.model_dump(mode="json"))
    return grouped


def schema_problem(output: dict, schema: dict) -> str | None:
    # Ленивый импорт: код только backend (в станции не исполняется), а страж
    # test_station_has_what_the_code_imports считает весь skills/ кодом станции,
    # и jsonschema нет в образе станции. Решение владельца 01.10.2026.
    import jsonschema  # noqa: PLC0415

    try:
        jsonschema.validate(output, schema)
    except jsonschema.ValidationError as exc:
        # exc.message содержит само значение (до мегабайта) — в промпт автора и в NodeRun.error оно не идёт целиком
        return f"результат не соответствует output_schema: {exc.message[:_PROBLEM_PREVIEW]} (путь {list(exc.path)})"
    return None


def _execute(atom: EphemeralAtom, args: NodeArgs, ctx) -> list[DynamicArtifact | FileArtifact]:
    # В код уходят ТОЛЬКО результаты зависимостей. ctx.inputs (текст и байты
    # документа) не передаётся: атом не может достать число из отчёта в обход
    # read.value и гейтов.
    evidence = ctx.dependencies()
    result = sealed_room.run_code(
        atom.code, group_inputs(evidence), collect_files=True, trace_id=ctx.inputs.extra.get("trace_id", ""),
    )
    if result.error:
        raise RuntimeError(f"{STATION_FAULT_PREFIX}{result.error}" if result.station_fault else result.error)
    problem = schema_problem(result.output, atom.request.output_schema)
    if problem:
        raise ValueError(problem)
    produced = DynamicArtifact(
        output_type=atom.request.output_type, payload=result.output,
        origin=_origin(evidence), producer=atom.producer,
        input_evidence_ids=[eid for eid in map(evidence_id_of, evidence) if eid],
    )
    # Порт выхода: файлы из out/ — результаты того же узла, с родословной от его JSON-результата.
    files = persist_room_files(
        result.files, produced=produced, template_id=atom.request.name,
        template_version=atom.version, ctx=ctx,
    )
    return [produced, *files]


def _download(atom: EphemeralAtom, args: NodeArgs, ctx) -> list[DynamicArtifact]:  # noqa: ARG001
    # Исполняется в backend, в рабочем потоке исполнителя графа (не в корутине): asyncio.run
    # здесь — тот же приём, что у атомов, зовущих модель. Ошибки загрузки (UnsafeUrlError,
    # DownloadRejected) не перехватываются: узел падает, планировщик видит причину в итоге хода.
    # Тумблер «Поиск в интернете» — та же дверь, что у `web.search`: выключен — узел падает с причиной.
    require_web_search()
    host = urlsplit(atom.request.url).hostname
    result = asyncio.run(download_public_file(atom.request.url, allowed_domain=host))
    page = is_html(result.content_type)
    text = decode_text(result.content, result.charset or (meta_charset(result.content) if page else ""))
    if text is None:
        # PDF, архив, текст в необъявленной кодировке: знаки замены вместо байтов выглядели бы как данные.
        raise DownloadRejected(
            f"{result.final_url}: не текст (content-type {result.content_type!r}) — атом-загрузка читает "
            "страницы и текстовые файлы данных; возьми другой адрес из находок"
        )
    if page:
        # Страница — текстом без разметки и не длиннее предела; файлы данных (CSV, JSON) — как есть.
        text = html_to_text(text)[:settings.download_page_text_chars]
    payload = {"url": result.final_url, "content_type": result.content_type, "text": text}
    return [DynamicArtifact(output_type=atom.request.output_type, payload=payload,
                            origin=["external"], producer=atom.producer)]


def _download_profile(url: str) -> str:
    """Свой профиль на каждый узел: сеть только туда, куда указывает url атома."""
    host = urlsplit(url).hostname
    return register_dynamic_profile(f"download:{host}", external_enrichment(
        allowed_hosts=(host,), description=f"Загрузка публичного файла с {host}."))


def _download_problems(request: AtomRequest) -> list[str]:
    parts = urlsplit(request.url)
    problems = []
    if parts.scheme not in ("http", "https") or not parts.hostname:
        problems.append("url атома-загрузки — только http(s) с именем узла")
    if request.requires:
        problems.append("атом-загрузка не получает входов: соединять скачанное с данными отчёта "
                        "должен отдельный атом с кодом")
    return problems


def _origin(evidence) -> list[str]:
    origins = {"generated"}
    for item in evidence:
        if isinstance(item, DynamicArtifact):
            origins.update(item.origin)
        elif type(item).__name__ in _ORIGIN_BY_TYPE:
            origins.add(_ORIGIN_BY_TYPE[type(item).__name__])
    return sorted(origins)


def _numbers_without_unit(schema: dict, path: str = "$") -> list[str]:
    """Пути числовых полей JSON Schema без x-unit."""
    missing = []
    if schema.get("type") in ("number", "integer") and "x-unit" not in schema:
        missing.append(path)
    for key, sub in (schema.get("properties") or {}).items():
        missing += _numbers_without_unit(sub, f"{path}.{key}")
    if isinstance(schema.get("items"), dict):
        missing += _numbers_without_unit(schema["items"], f"{path}[]")
    return missing
