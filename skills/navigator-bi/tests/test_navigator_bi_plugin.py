from __future__ import annotations

import importlib
import importlib.util
import json
import pathlib
import sys
from types import SimpleNamespace
from typing import Any

import pytest

_SKILL_DIR = pathlib.Path(__file__).resolve().parents[1]
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_PACKAGE = "test_navigator_bi_skill_payload"

_SETTINGS = {
    "NAVIGATOR_BI_BASE_URL": "https://navigator.example",
    "NAVIGATOR_BI_SBER_PDI": "secret-pdi",
    "NAVIGATOR_BI_MODEL_IDS": "1000000956",
}


class _FakeApi:
    """Sanctioned settings surface: get_settings returns the allowlisted keys."""

    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self.values = dict(_SETTINGS if values is None else values)
        self.requested_keys: list[str] | None = None
        self.logs: list[tuple[str, str, dict[str, Any]]] = []

    def get_settings(self, keys: Any) -> dict[str, Any]:
        self.requested_keys = list(keys)
        return {k: v for k, v in self.values.items() if k in set(keys)}

    def log(self, level: str, message: str, **fields: Any) -> None:
        self.logs.append((level, message, fields))


class FakeResponse:
    def __init__(self, payload: dict[str, Any] | None = None, *, error: str = "") -> None:
        self.payload = payload or {"rows": []}
        self.error = error

    def raise_for_status(self) -> None:
        if self.error:
            raise RuntimeError(self.error)

    def json(self) -> dict[str, Any]:
        return self.payload


class FakeClient:
    calls: list[dict[str, Any]] = []
    responses: list[FakeResponse] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def post(self, url: str, json: dict[str, Any]) -> FakeResponse:
        self.calls.append({"url": url, "json": json, "client_kwargs": self.kwargs})
        if self.responses:
            return self.responses.pop(0)
        return FakeResponse({"rows": []})


@pytest.fixture
def plugin_module(monkeypatch: pytest.MonkeyPatch):
    for name in list(sys.modules):
        if name == _PACKAGE or name.startswith(f"{_PACKAGE}."):
            sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        _PACKAGE,
        _SKILL_DIR / "plugin.py",
        submodule_search_locations=[str(_SKILL_DIR)],
    )
    assert spec is not None and spec.loader is not None
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[_PACKAGE] = plugin
    try:
        spec.loader.exec_module(plugin)
        FakeClient.calls = []
        FakeClient.responses = []
        # handlers/api share one payload ``api`` module object; patch its httpx once.
        monkeypatch.setattr(plugin.navigator_api.httpx, "Client", FakeClient)
        yield plugin
    finally:
        for name in list(sys.modules):
            if name == _PACKAGE or name.startswith(f"{_PACKAGE}."):
                sys.modules.pop(name, None)


def _submodule(plugin_module: Any, name: str) -> Any:
    return importlib.import_module(f"{_PACKAGE}.{name}")


def _app_settings(plugin_module: Any, **overrides: Any) -> Any:
    values = {**_SETTINGS, **overrides}
    return plugin_module.navigator_api.build_config({k: v for k, v in values.items() if v is not None})


def _ctx(tmp_path: pathlib.Path) -> SimpleNamespace:
    return SimpleNamespace(repo_dir=tmp_path, drive_root=tmp_path / "data")


def _call(plugin_module: Any, tmp_path: pathlib.Path, *, api: _FakeApi | None = None, **kwargs: Any) -> str:
    fake_api = api if api is not None else _FakeApi()
    return plugin_module._navigator_bi_search(fake_api, _ctx(tmp_path), **kwargs)


def _call_json(plugin_module: Any, tmp_path: pathlib.Path, **kwargs: Any) -> dict[str, Any]:
    result = _call(plugin_module, tmp_path, **kwargs)
    assert not result.startswith("⚠️ TOOL_ERROR"), result
    return json.loads(result)


def _structure_response() -> dict[str, Any]:
    return {"model": [_pl_model_node()]}


def _pl_model_node() -> dict[str, Any]:
    return {
        "nID": 1000000956,
        "sName": "Финансы группы.PL",
        "table": {
            "column": [
                {"sName": "Дата", "sDataType": "timestamp"},
                {"sName": "Наименование подразделения", "sDataType": "varchar"},
                {"sName": "Факт, за месяц", "sDataType": "numeric"},
            ]
        },
    }


def _mock_catalog(plugin_module: Any, *model_nodes: dict[str, Any]) -> Any:
    structure_catalog = _submodule(plugin_module, "structure_catalog")
    return structure_catalog.parse_structure_payload({"model": list(model_nodes)})


def _dataset_response(*, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    active_rows = (
        rows
        if rows is not None
        else [
            {
                "DuMmYDuMmYD2": "2025-12-31T00:00:00",
                "DuMmYDuMmYD6": "Сбербанк",
                "DuMmYDuMmY10": -37964375118.72,
            }
        ]
    )
    return {
        "datasets": {
            "dataset": [
                {
                    "rows": {"row": active_rows},
                    "fields": {
                        "field": [
                            {"sID": "DuMmYDuMmYD2", "sName": "Дата"},
                            {"sID": "DuMmYDuMmYD6", "sName": "Наименование подразделения"},
                            {"sID": "DuMmYDuMmY10", "sName": "Факт, за месяц"},
                        ]
                    },
                }
            ]
        }
    }


def _singleton_dataset_response() -> dict[str, Any]:
    return {
        "datasets": {
            "dataset": {
                "rows": {
                    "row": {
                        "DuMmYDuMmYD2": "2025-12-31T00:00:00",
                        "DuMmYDuMmYD6": "Сбербанк",
                        "DuMmYDuMmY10": -37964375118.72,
                    }
                },
                "fields": {
                    "field": {"sID": "DuMmYDuMmYD2", "sName": "Дата"},
                },
            }
        }
    }


def _valid_pl_query(**overrides: Any) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "model": "Финансы Группы.PL",
        "conditions": [
            {"column": "Наименование подразделения", "operator": "=", "value": "Сбербанк"},
        ],
    }
    plan.update(overrides)
    return plan


# --------------------------------------------------------------------------- #
# Extension registration (replaces the old core-surface test).
# --------------------------------------------------------------------------- #


def test_extension_registers_navigator_bi_search_through_project_loader(tmp_path) -> None:
    from new_core.tools.catalog import ToolCatalog
    from new_core.tools.providers import ExtensionToolProvider
    from ouroboros.skills.extensions import loader as extension_loader
    from ouroboros.skills.loader import find_skill

    extension_loader.unload_extension("navigator-bi")
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    loaded = find_skill(drive_root, "navigator-bi", repo_path=str(_SKILL_DIR.parent))
    assert loaded is not None

    try:
        error = extension_loader.load_extension(loaded, dict, drive_root=drive_root)
        assert error is None
        tool_names = [name for name in extension_loader.snapshot()["tools"] if name.endswith("_navigator_bi_search")]
        assert len(tool_names) == 1
        surface_name = tool_names[0]
        tool = extension_loader.get_tool(surface_name)
        assert tool is not None
        assert tool["timeout_sec"] == 120
        schema = tool["schema"]
        assert schema["type"] == "object"
        assert schema["required"] == ["mode"]
        assert set(schema["properties"]) == {"mode", "model", "plan", "limit"}
        assert "Navigator BI" in str(tool["description"])
        assert tool["retrieval_aliases"] == ["navigator_bi", "navigator", "навигатор"]
        assert tool["parallel_safe"] is True
        # The declaration has to survive the trip into the descriptor the loop reads.
        catalog = ToolCatalog()
        catalog.initialize([ExtensionToolProvider(drive_root, _REPO_ROOT)])
        descriptor = catalog.find(surface_name)
        assert descriptor is not None
        assert descriptor.timeout_sec == 120
        assert descriptor.parallel_safe is True
        assert descriptor.retrieval_aliases == ("navigator_bi", "navigator", "навигатор")
    finally:
        extension_loader.unload_extension("navigator-bi")


def test_navigator_bi_manifest(tmp_path) -> None:
    from ouroboros.skills.loader import find_skill

    skill = find_skill(tmp_path, "navigator-bi", workspace_root=_REPO_ROOT)

    assert skill is not None
    assert skill.manifest.type == "extension"
    assert skill.manifest.entry == "plugin.py"
    assert skill.manifest.version
    assert skill.manifest.validate() == []
    assert {"tool", "net", "read_settings"} <= set(skill.manifest.permissions)
    assert "NAVIGATOR_BI_MODEL_IDS" in skill.manifest.env_from_settings
    assert "NAVIGATOR_BI_SBER_PDI" in skill.manifest.env_from_settings
    assert "navigator_bi_search" in skill.manifest.description
    assert "Never answer such questions from memory" in skill.manifest.description


def test_setting_keys_match_manifest_env_from_settings(tmp_path, plugin_module) -> None:
    from ouroboros.skills.loader import find_skill

    skill = find_skill(tmp_path, "navigator-bi", workspace_root=_REPO_ROOT)

    assert skill is not None
    assert list(plugin_module._SETTING_KEYS) == list(skill.manifest.env_from_settings)


# --------------------------------------------------------------------------- #
# Settings sourcing + hard-failure signaling.
# --------------------------------------------------------------------------- #


def test_handler_sources_settings_from_api_get_settings(plugin_module, tmp_path) -> None:
    FakeClient.responses = [FakeResponse(_structure_response())]
    api = _FakeApi()

    result = _call_json(plugin_module, tmp_path, api=api, mode="schema")

    assert result["status"] == "ok"
    # The sanctioned settings path: exactly the SKILL.md env_from_settings keys.
    assert api.requested_keys == list(plugin_module._SETTING_KEYS)
    assert len(plugin_module._SETTING_KEYS) == 6
    assert FakeClient.calls[0]["client_kwargs"]["headers"]["sberpdi"] == "secret-pdi"


def test_handler_reports_tool_error_on_invalid_settings(plugin_module, tmp_path) -> None:
    class _BrokenApi:
        def __init__(self) -> None:
            self.logs: list[tuple[str, str, dict[str, Any]]] = []

        def get_settings(self, keys):
            return {"NAVIGATOR_BI_TIMEOUT_SEC": "not-a-number"}

        def log(self, level: str, message: str, **fields: Any) -> None:
            self.logs.append((level, message, fields))

    api = _BrokenApi()
    result = plugin_module._navigator_bi_search(api, _ctx(tmp_path), mode="schema")

    assert result.startswith("⚠️ TOOL_ERROR")
    assert "settings invalid" in result
    assert FakeClient.calls == []
    # Cause is preserved in logs as field names only (no values — may embed the PDI).
    assert api.logs, "settings rejection must be logged"
    level, message, fields = api.logs[0]
    assert level == "error"
    assert "settings rejected" in message
    assert fields["fields"] == ["NAVIGATOR_BI_TIMEOUT_SEC"]
    assert "not-a-number" not in json.dumps(fields, default=str)


def test_needs_clarification_is_json_but_hard_error_is_tool_error(plugin_module, tmp_path) -> None:
    # needs_clarification (empty conditions) stays a soft, parseable JSON signal.
    FakeClient.responses = [FakeResponse(_structure_response())]
    soft = _call(plugin_module, tmp_path, mode="query", plan={"model": "Финансы Группы.PL", "conditions": []})
    assert not soft.startswith("⚠️ TOOL_ERROR")
    assert json.loads(soft)["status"] == "needs_clarification"

    # A genuine hard failure (disallowed operator, status="error") becomes ⚠️ TOOL_ERROR.
    FakeClient.responses = [FakeResponse(_structure_response())]
    hard = _call(
        plugin_module,
        tmp_path,
        mode="query",
        plan={
            "model": "Финансы Группы.PL",
            "conditions": [{"column": "Дата", "operator": "LIKE", "value": "%x%"}],
        },
    )
    assert hard.startswith("⚠️ TOOL_ERROR")
    assert "not allowed" in hard


def test_plugin_registers_with_declared_result_limit(tmp_path) -> None:
    # Pin the shipped 30k cap: the core honors register_tool(result_limit=...),
    # so this regression can't silently drop back to the runtime default.
    from ouroboros.skills.extensions import loader as extension_loader
    from ouroboros.skills.loader import find_skill

    extension_loader.unload_extension("navigator-bi")
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    loaded = find_skill(drive_root, "navigator-bi", repo_path=str(_SKILL_DIR.parent))
    assert loaded is not None
    try:
        error = extension_loader.load_extension(loaded, dict, drive_root=drive_root)
        assert error is None
        surface = next(name for name in extension_loader.snapshot()["tools"] if name.endswith("_navigator_bi_search"))
        tool = extension_loader.get_tool(surface)
        assert tool is not None
        assert tool["result_limit"] == 30_000
    finally:
        extension_loader.unload_extension("navigator-bi")


# --------------------------------------------------------------------------- #
# Handler behavior: query mode.
# --------------------------------------------------------------------------- #


def test_query_mode_happy_path_returns_rows_with_human_column_names(plugin_module, tmp_path) -> None:
    FakeClient.responses = [FakeResponse(_structure_response()), FakeResponse(_dataset_response())]

    result = _call_json(
        plugin_module,
        tmp_path,
        mode="query",
        plan={
            "model": "Финансы Группы.PL",
            "conditions": [
                {"column": "Наименование подразделения", "operator": "=", "value": "Сбербанк"},
            ],
            "limit": 20,
        },
    )

    assert result["status"] == "ok"
    assert result["model"] == "Финансы группы.PL"
    assert result["returned"] == 1
    assert result["limit"] == 20
    assert result["truncated"] is False
    assert result["has_more"] is False
    assert result["applied_conditions"] == [
        {"column": "Наименование подразделения", "operator": "=", "value": "Сбербанк"}
    ]
    row = result["rows"][0]
    assert "DuMmY" not in "".join(row.keys())
    assert row["Дата"] == "2025-12-31T00:00:00"
    assert row["Наименование подразделения"] == "Сбербанк"
    assert row["Факт, за месяц"] == -37964375118.72
    assert len(FakeClient.calls) == 2
    assert FakeClient.calls[0]["url"].endswith("/ai/dataset/structure")
    assert FakeClient.calls[0]["json"] == {"model_ids": ["1000000956"]}
    assert FakeClient.calls[1]["url"].endswith("/ai/dataset")
    assert FakeClient.calls[1]["json"]["dataset"]["sSource"] == "1000000956"


def test_query_mode_empty_result_returns_not_found_with_applied_conditions(plugin_module, tmp_path) -> None:
    FakeClient.responses = [FakeResponse(_structure_response()), FakeResponse(_dataset_response(rows=[]))]

    result = _call_json(
        plugin_module,
        tmp_path,
        mode="query",
        plan={
            "model": "Финансы Группы.PL",
            "conditions": [{"column": "Дата", "operator": ">=", "value": "2099-01-01"}],
        },
    )

    assert result["status"] == "not_found"
    assert result["rows"] == []
    assert result["returned"] == 0
    assert result["applied_conditions"] == [{"column": "Дата", "operator": ">=", "value": "2099-01-01"}]


def test_query_mode_clamps_limit_and_sets_truncated(plugin_module, tmp_path) -> None:
    many_rows = [
        {"DuMmYDuMmYD2": f"2025-01-{index:02d}T00:00:00", "DuMmYDuMmYD6": f"Unit {index}"} for index in range(1, 6)
    ]
    FakeClient.responses = [FakeResponse(_structure_response()), FakeResponse(_dataset_response(rows=many_rows))]

    result = _call_json(
        plugin_module,
        tmp_path,
        mode="query",
        plan=_valid_pl_query(
            conditions=[{"column": "Наименование подразделения", "operator": "=", "value": "Сбербанк"}],
            limit=200,
        ),
    )

    assert result["status"] == "ok"
    assert result["limit"] == 100
    assert result["returned"] == 5
    assert result["truncated"] is False
    assert result["has_more"] is False


def test_query_mode_truncates_when_source_exceeds_clamped_limit(plugin_module, tmp_path) -> None:
    many_rows = [
        {"DuMmYDuMmYD2": f"2025-01-{index:02d}T00:00:00", "DuMmYDuMmYD6": f"Unit {index}"} for index in range(1, 105)
    ]
    FakeClient.responses = [FakeResponse(_structure_response()), FakeResponse(_dataset_response(rows=many_rows))]

    result = _call_json(
        plugin_module,
        tmp_path,
        mode="query",
        plan=_valid_pl_query(
            conditions=[{"column": "Наименование подразделения", "operator": "=", "value": "Сбербанк"}],
            limit=200,
        ),
    )

    assert result["status"] == "ok"
    assert result["limit"] == 100
    assert result["returned"] == 100
    assert result["truncated"] is True
    assert result["has_more"] is True
    assert len(result["rows"]) == 100


def test_query_mode_unknown_model_returns_needs_clarification_json(plugin_module, tmp_path) -> None:
    FakeClient.responses = [FakeResponse(_structure_response())]

    # needs_clarification is a soft, parseable JSON signal, not a hard TOOL_ERROR.
    result = _call_json(
        plugin_module,
        tmp_path,
        mode="query",
        plan={"model": "Финансы Группы.ЧКД", "conditions": []},
    )

    assert result["status"] == "needs_clarification"
    assert "Unknown model" in result["message"]
    assert len(FakeClient.calls) == 1
    assert FakeClient.calls[0]["json"] == {"model_ids": ["1000000956"]}


def test_query_mode_api_error_returns_redacted_tool_error(plugin_module, tmp_path) -> None:
    FakeClient.responses = [FakeResponse(error="request failed for secret-pdi")]

    result = _call(
        plugin_module,
        tmp_path,
        mode="query",
        plan=_valid_pl_query(),
    )

    assert result.startswith("⚠️ TOOL_ERROR")
    assert "secret-pdi" not in result
    assert "[REDACTED]" in result


def test_missing_model_ids_returns_tool_error(plugin_module, tmp_path) -> None:
    api = _FakeApi({"NAVIGATOR_BI_BASE_URL": "https://navigator.example", "NAVIGATOR_BI_SBER_PDI": "secret-pdi"})

    result = _call(plugin_module, tmp_path, api=api, mode="schema")

    assert result.startswith("⚠️ TOOL_ERROR")
    assert "NAVIGATOR_BI_MODEL_IDS" in result
    assert FakeClient.calls == []


def test_missing_identity_returns_tool_error(plugin_module, tmp_path) -> None:
    api = _FakeApi({"NAVIGATOR_BI_BASE_URL": "https://navigator.example", "NAVIGATOR_BI_MODEL_IDS": "1000000956"})

    result = _call(plugin_module, tmp_path, api=api, mode="query", plan=_valid_pl_query())

    assert result.startswith("⚠️ TOOL_ERROR")
    assert "NAVIGATOR_BI_SBER_PDI" in result
    assert "NAVIGATOR_BI_EMAIL" in result
    assert "secret-pdi" not in result
    assert FakeClient.calls == []


def test_query_mode_api_error_redacts_bearer_token(plugin_module, tmp_path) -> None:
    FakeClient.responses = [FakeResponse(error="request failed for Bearer secret-token")]

    result = _call(
        plugin_module,
        tmp_path,
        mode="query",
        plan=_valid_pl_query(),
    )

    assert result.startswith("⚠️ TOOL_ERROR")
    assert "secret-token" not in result
    assert "[REDACTED]" in result


def test_unsupported_mode_returns_tool_error(plugin_module, tmp_path) -> None:
    result = _call(plugin_module, tmp_path, mode="delete")

    assert result.startswith("⚠️ TOOL_ERROR")
    assert "Unsupported mode" in result
    assert FakeClient.calls == []


# --------------------------------------------------------------------------- #
# Handler behavior: schema mode.
# --------------------------------------------------------------------------- #


def test_schema_mode_without_model_returns_configured_catalog(plugin_module, tmp_path) -> None:
    FakeClient.responses = [FakeResponse(_structure_response())]

    result = _call_json(plugin_module, tmp_path, mode="schema")

    assert result["status"] == "ok"
    assert result["backend"] == "navigator_bi"
    # Legacy staged rollout is over: the scope is whatever the catalog returns.
    assert "stage" not in result
    assert len(result["models"]) == 1
    assert result["columns"] == []
    assert "allowed_operators" in result
    assert result["limits"] == {"default_rows": 20, "max_rows": 100, "max_conditions": 10}
    ready = result["models"][0]
    assert ready["metadata_status"] == "ready"
    assert ready["nID"] == 1000000956
    assert ready["sName"] == "Финансы группы.PL"
    assert len(FakeClient.calls) == 1
    assert FakeClient.calls[0]["json"] == {"model_ids": ["1000000956"]}


def test_schema_mode_with_ready_model_returns_live_columns(plugin_module, tmp_path) -> None:
    FakeClient.responses = [FakeResponse(_structure_response())]

    result = _call_json(plugin_module, tmp_path, mode="schema", model="Финансы Группы.PL")

    assert result["status"] == "ok"
    assert len(result["models"]) == 1
    column_names = [column["sName"] for column in result["columns"]]
    assert column_names == ["Дата", "Наименование подразделения", "Факт, за месяц"]
    assert result["columns"][0]["sDataType"] == "timestamp"
    assert len(FakeClient.calls) == 1
    assert FakeClient.calls[0]["url"].endswith("/ai/dataset/structure")
    assert FakeClient.calls[0]["json"] == {"model_ids": ["1000000956"]}


def test_schema_mode_unknown_model_returns_needs_clarification_json(plugin_module, tmp_path) -> None:
    FakeClient.responses = [FakeResponse(_structure_response())]

    result = _call_json(plugin_module, tmp_path, mode="schema", model="Неизвестная модель")

    assert result["status"] == "needs_clarification"
    assert "Unknown model" in result["message"]
    assert len(FakeClient.calls) == 1


def test_query_mode_unknown_model_rejected_after_catalog_fetch(plugin_module, tmp_path) -> None:
    FakeClient.responses = [FakeResponse(_structure_response())]

    result = _call_json(
        plugin_module,
        tmp_path,
        mode="query",
        plan={"model": "Неизвестная модель", "conditions": []},
    )

    assert result["status"] == "needs_clarification"
    assert "Unknown model" in result["message"]
    assert len(FakeClient.calls) == 1


def test_query_mode_unknown_column_rejected_before_dataset_post(plugin_module, tmp_path) -> None:
    FakeClient.responses = [FakeResponse(_structure_response())]

    # Unknown column is a soft clarification signal (request schema and retry), not a hard failure.
    result = _call_json(
        plugin_module,
        tmp_path,
        mode="query",
        plan={
            "model": "Финансы Группы.PL",
            "conditions": [{"column": "Несуществующая колонка", "operator": "=", "value": "x"}],
        },
    )

    assert result["status"] == "needs_clarification"
    assert "not allowed" in result["message"]
    assert "Несуществующая колонка" in result["message"]
    assert len(FakeClient.calls) == 1
    assert FakeClient.calls[0]["url"].endswith("/ai/dataset/structure")


def test_parse_dataset_response_accepts_singleton_nodes(plugin_module, tmp_path) -> None:
    FakeClient.responses = [FakeResponse(_structure_response()), FakeResponse(_singleton_dataset_response())]

    result = _call_json(
        plugin_module,
        tmp_path,
        mode="query",
        plan=_valid_pl_query(),
    )

    assert result["status"] == "ok"
    assert result["returned"] == 1
    assert result["rows"][0]["Дата"] == "2025-12-31T00:00:00"
    assert "DuMmYDuMmYD2" not in result["rows"][0]


# --------------------------------------------------------------------------- #
# Payload transport / schema unit tests (direct against payload submodules).
# --------------------------------------------------------------------------- #


def test_configured_model_ids_parses_comma_list(plugin_module) -> None:
    api = _submodule(plugin_module, "api")
    settings = _app_settings(plugin_module, NAVIGATOR_BI_MODEL_IDS=" 1000000956 , 2000000001,1000000956 ")

    assert api.configured_model_ids(settings) == [1000000956, 2000000001]


def test_api_redacts_identity(plugin_module) -> None:
    api = _submodule(plugin_module, "api")
    settings = _app_settings(plugin_module)

    assert "[REDACTED]" in api.redact(settings, "request failed for secret-pdi")
    assert "secret-pdi" not in api.redact(settings, "request failed for secret-pdi")
    assert "secret-pdi" not in api.redact(settings, "sberpdi: secret-pdi")
    assert "sberpdi: [REDACTED]" in api.redact(settings, "sberpdi: secret-pdi")


def test_post_structure_uses_configured_model_ids(plugin_module) -> None:
    FakeClient.responses = [FakeResponse({"model": [_pl_model_node()]})]
    structure_catalog = _submodule(plugin_module, "structure_catalog")

    structure_catalog.StructureCatalog.fetch(_app_settings(plugin_module))

    assert len(FakeClient.calls) == 1
    assert FakeClient.calls[0]["json"] == {"model_ids": ["1000000956"]}


def test_post_structure_sends_required_headers(plugin_module) -> None:
    api = _submodule(plugin_module, "api")
    FakeClient.responses = [FakeResponse({"model": []})]

    api.post_structure(_app_settings(plugin_module), [1000000956])

    assert len(FakeClient.calls) == 1
    call = FakeClient.calls[0]
    assert call["url"] == "https://navigator.example/ai/dataset/structure"
    assert call["json"] == {"model_ids": ["1000000956"]}
    headers = call["client_kwargs"]["headers"]
    assert headers["datapreview"] == "1"
    assert headers["Nav-Client-Platform"] == "3"
    assert headers["sberpdi"] == "secret-pdi"
    # No core trace header: the payload no longer depends on aef_tracing.
    assert "X-Trace-Id" not in headers
    assert "email" not in headers


def test_post_structure_sends_email_header_only(plugin_module) -> None:
    api = _submodule(plugin_module, "api")
    settings = _app_settings(plugin_module, NAVIGATOR_BI_SBER_PDI=None, NAVIGATOR_BI_EMAIL="user@example.com")
    FakeClient.responses = [FakeResponse({"model": []})]

    api.post_structure(settings, [1000000956])

    headers = FakeClient.calls[0]["client_kwargs"]["headers"]
    assert headers["email"] == "user@example.com"
    assert "sberpdi" not in headers


def test_post_structure_sends_both_identity_headers(plugin_module) -> None:
    api = _submodule(plugin_module, "api")
    settings = _app_settings(plugin_module, NAVIGATOR_BI_EMAIL="user@example.com")
    FakeClient.responses = [FakeResponse({"model": []})]

    api.post_structure(settings, [1000000956])

    headers = FakeClient.calls[0]["client_kwargs"]["headers"]
    assert headers["sberpdi"] == "secret-pdi"
    assert headers["email"] == "user@example.com"


def test_catalog_entries_reflect_structure_models(plugin_module) -> None:
    structure_catalog = _submodule(plugin_module, "structure_catalog")
    live = _mock_catalog(plugin_module, _pl_model_node())
    entries = structure_catalog.catalog_entries(live)

    assert len(entries) == 1
    assert entries[0]["sName"] == "Финансы группы.PL"
    assert entries[0]["nID"] == 1000000956
    assert entries[0]["metadata_status"] == "ready"


def test_resolve_model_accepts_case_insensitive_sname(plugin_module) -> None:
    filters = _submodule(plugin_module, "filters")
    live = _mock_catalog(plugin_module, _pl_model_node())
    info = filters.resolve_model("финансы группы.pl", structure_catalog=live)
    assert info.s_name == "Финансы группы.PL"


def test_resolve_model_accepts_numeric_nid(plugin_module) -> None:
    filters = _submodule(plugin_module, "filters")
    live = _mock_catalog(plugin_module, _pl_model_node())
    info = filters.resolve_model("1000000956", structure_catalog=live)
    assert info.s_name == "Финансы группы.PL"


def test_resolve_model_rejects_unknown_nid(plugin_module) -> None:
    filters = _submodule(plugin_module, "filters")
    live = _mock_catalog(plugin_module, _pl_model_node())
    with pytest.raises(filters.PlanValidationError) as exc_info:
        filters.resolve_model("9999999999", structure_catalog=live)
    assert exc_info.value.status == "needs_clarification"
    assert "Unknown model" in exc_info.value.message


def test_validate_query_plan_rejects_unknown_model(plugin_module) -> None:
    filters = _submodule(plugin_module, "filters")
    live = _mock_catalog(plugin_module, _pl_model_node())
    with pytest.raises(filters.PlanValidationError) as exc_info:
        filters.validate_query_plan(
            {"model": "Финансы Группы.ЧКД", "conditions": []},
            structure_catalog=live,
        )

    assert exc_info.value.status == "needs_clarification"
    assert "Unknown model" in exc_info.value.message


def test_validate_query_plan_rejects_unknown_operator(plugin_module) -> None:
    filters = _submodule(plugin_module, "filters")
    live = _mock_catalog(plugin_module, _pl_model_node())
    with pytest.raises(filters.PlanValidationError) as exc_info:
        filters.validate_query_plan(
            {
                "model": "Финансы Группы.PL",
                "conditions": [{"column": "Дата", "operator": "LIKE", "value": "%x%"}],
            },
            structure_catalog=live,
        )

    assert exc_info.value.status == "error"
    assert "not allowed" in exc_info.value.message


def test_navigator_condition_serializes_in_values(plugin_module) -> None:
    filters = _submodule(plugin_module, "filters")
    wire = filters.navigator_condition(
        {"column": "Наименование подразделения", "operator": "in", "value": ["A", "B", "C"]}
    )

    assert wire["sRightExp"] == "A|||B|||C"
    assert wire["sLeftExp"] == "Наименование подразделения"


def test_schema_payload_without_models_is_empty(plugin_module) -> None:
    schema = _submodule(plugin_module, "schema")
    payload = schema.schema_payload()

    assert payload["status"] == "ok"
    assert payload["tool"] == "navigator_bi_search"
    assert payload["models"] == []
    assert payload["limits"] == {"default_rows": 20, "max_rows": 100, "max_conditions": 10}


def test_parse_structure_payload_duplicate_sname_last_wins(plugin_module) -> None:
    structure_catalog = _submodule(plugin_module, "structure_catalog")
    catalog = structure_catalog.parse_structure_payload(
        {
            "model": [
                {"nID": 1, "sName": "MISNAV-56420", "table": {"column": []}},
                {"nID": 2, "sName": "misnav-56420", "table": {"column": [{"sName": "Дата"}]}},
            ]
        }
    )

    assert catalog.by_nid[1].s_name == "MISNAV-56420"
    assert catalog.by_nid[2].s_name == "misnav-56420"
    assert catalog.by_normalized_sname["misnav-56420"].n_id == 2


@pytest.mark.parametrize(
    ("plan", "expected_status", "message_fragment"),
    [
        (
            {"model": "Foo", "conditions": []},
            "needs_clarification",
            "Unknown model",
        ),
        (
            {
                "model": "Финансы Группы.PL",
                "conditions": [{"column": "Дата", "operator": "LIKE", "value": "%x%"}],
            },
            "error",
            "not allowed",
        ),
        (
            {
                "model": "Финансы Группы.PL",
                "conditions": [{"column": "Дата", "operator": "=", "value": f"v{index}"} for index in range(11)],
            },
            "error",
            "Too many conditions",
        ),
        (
            {
                "model": "Финансы Группы.PL",
                "conditions": [{"column": "Дата", "operator": "=", "value": ""}],
            },
            "needs_clarification",
            "requires a value",
        ),
        (
            {
                "model": "Финансы Группы.PL",
                "conditions": [
                    {
                        "type": "or",
                        "conditions": [
                            {"column": "Дата", "operator": "=", "value": "2025-01-01"},
                        ],
                    }
                ],
            },
            "error",
            "flat AND",
        ),
        (
            {"model": "Финансы Группы.PL", "conditions": [], "extra_key": True},
            "error",
            "Unknown plan keys",
        ),
        (
            {"model": "Финансы Группы.PL", "conditions": []},
            "needs_clarification",
            "At least one filter condition",
        ),
    ],
    ids=[
        "unknown_model",
        "like_operator",
        "eleven_conditions",
        "empty_comparison_value",
        "or_tree",
        "unknown_plan_key",
        "empty_conditions",
    ],
)
def test_validation_matrix_rejects_invalid_plans(
    plugin_module,
    tmp_path,
    plan: dict[str, Any],
    expected_status: str,
    message_fragment: str,
) -> None:
    FakeClient.responses = [FakeResponse(_structure_response())]

    result = _call(plugin_module, tmp_path, mode="query", plan=plan)

    if expected_status == "needs_clarification":
        # Soft, parseable JSON signal — the model refines and retries.
        parsed = json.loads(result)
        assert parsed["status"] == "needs_clarification"
        assert message_fragment in parsed["message"]
    else:
        assert result.startswith("⚠️ TOOL_ERROR")
        assert expected_status in result
        assert message_fragment in result
    assert len(FakeClient.calls) == 1
    assert FakeClient.calls[0]["json"] == {"model_ids": ["1000000956"]}


@pytest.mark.parametrize(
    ("plan", "expected_http_calls"),
    [
        (_valid_pl_query(), 2),
        (
            _valid_pl_query(
                conditions=[
                    {
                        "column": "Наименование подразделения",
                        "operator": "in",
                        "value": ["Сбербанк", "Группа Сбербанка России", "ДЗО"],
                    }
                ],
            ),
            2,
        ),
        (
            _valid_pl_query(
                conditions=[
                    {"column": "Дата", "operator": ">=", "value": "2025-01-01"},
                    {"column": "Наименование подразделения", "operator": "!=", "value": "X"},
                ],
            ),
            2,
        ),
        (_valid_pl_query(limit=200), 2),
    ],
    ids=["single_condition", "in_three_values", "two_condition_and", "limit_clamped"],
)
def test_validation_matrix_accepts_valid_plans_with_http(
    plugin_module,
    tmp_path,
    plan: dict[str, Any],
    expected_http_calls: int,
) -> None:
    FakeClient.responses = [FakeResponse(_structure_response()), FakeResponse(_dataset_response())]

    result = _call_json(plugin_module, tmp_path, mode="query", plan=plan)

    assert result["status"] in {"ok", "not_found"}
    assert len(FakeClient.calls) == expected_http_calls
    if plan.get("limit") == 200:
        assert result["limit"] == 100

    in_condition = next(
        (item for item in plan.get("conditions", []) if item.get("operator") == "in"),
        None,
    )
    if in_condition is not None:
        dataset_call = FakeClient.calls[1]
        wire_conditions = dataset_call["json"]["dataset"]["conditions"]["r"]
        assert wire_conditions[0]["sRightExp"] == "Сбербанк|||Группа Сбербанка России|||ДЗО"


def test_validate_query_plan_rejects_too_many_in_values(plugin_module) -> None:
    filters = _submodule(plugin_module, "filters")
    live = _mock_catalog(plugin_module, _pl_model_node())
    with pytest.raises(filters.PlanValidationError) as exc_info:
        filters.validate_query_plan(
            {
                "model": "Финансы Группы.PL",
                "conditions": [
                    {
                        "column": "Наименование подразделения",
                        "operator": "in",
                        "value": [f"value-{index}" for index in range(21)],
                    }
                ],
            },
            structure_catalog=live,
        )

    assert exc_info.value.status == "error"
    assert "max 20" in exc_info.value.message


def test_validate_query_plan_rejects_oversized_condition_value(plugin_module) -> None:
    filters = _submodule(plugin_module, "filters")
    live = _mock_catalog(plugin_module, _pl_model_node())
    with pytest.raises(filters.PlanValidationError) as exc_info:
        filters.validate_query_plan(
            {
                "model": "Финансы Группы.PL",
                "conditions": [{"column": "Дата", "operator": "=", "value": "x" * 501}],
            },
            structure_catalog=live,
        )

    assert exc_info.value.status == "error"
    assert "500 characters" in exc_info.value.message


def test_validate_query_plan_rejects_empty_in_values(plugin_module) -> None:
    filters = _submodule(plugin_module, "filters")
    live = _mock_catalog(plugin_module, _pl_model_node())
    with pytest.raises(filters.PlanValidationError) as exc_info:
        filters.validate_query_plan(
            {
                "model": "Финансы Группы.PL",
                "conditions": [{"column": "Дата", "operator": "in", "value": []}],
            },
            structure_catalog=live,
        )

    assert exc_info.value.status == "needs_clarification"
    assert "at least one value" in exc_info.value.message


def test_validate_query_plan_rejects_empty_conditions(plugin_module) -> None:
    filters = _submodule(plugin_module, "filters")
    live = _mock_catalog(plugin_module, _pl_model_node())
    with pytest.raises(filters.PlanValidationError) as exc_info:
        filters.validate_query_plan(
            {"model": "Финансы Группы.PL", "conditions": []},
            structure_catalog=live,
        )

    assert exc_info.value.status == "needs_clarification"
    assert "At least one filter condition" in exc_info.value.message


def test_validate_query_plan_rejects_list_for_scalar_operator(plugin_module) -> None:
    filters = _submodule(plugin_module, "filters")
    live = _mock_catalog(plugin_module, _pl_model_node())
    with pytest.raises(filters.PlanValidationError) as exc_info:
        filters.validate_query_plan(
            {
                "model": "Финансы Группы.PL",
                "conditions": [{"column": "Дата", "operator": "=", "value": ["A", "B"]}],
            },
            structure_catalog=live,
        )

    assert exc_info.value.status == "needs_clarification"
    assert "single scalar value" in exc_info.value.message


def test_validate_query_plan_rejects_separator_in_in_list_item(plugin_module) -> None:
    filters = _submodule(plugin_module, "filters")
    live = _mock_catalog(plugin_module, _pl_model_node())
    with pytest.raises(filters.PlanValidationError) as exc_info:
        filters.validate_query_plan(
            {
                "model": "Финансы Группы.PL",
                "conditions": [
                    {"column": "Наименование подразделения", "operator": "in", "value": ["A|||B"]},
                ],
            },
            structure_catalog=live,
        )

    assert exc_info.value.status == "error"
    assert "|||" in exc_info.value.message


# --------------------------------------------------------------------------- #
# Prompt + skill wiring.
# --------------------------------------------------------------------------- #


def test_prompts_and_skill_reference_navigator_bi() -> None:
    root = _REPO_ROOT
    system = (root / "prompts" / "SYSTEM.md").read_text(encoding="utf-8")
    skill = root / "skills" / "navigator-bi" / "SKILL.md"
    skill_text = skill.read_text(encoding="utf-8")

    # Routing lives in the skill (manifest description/when_to_use + body) and
    # the tool description — NOT in the system prompt, where a stale block
    # would outrank the universal skill instructions.
    assert "navigator" not in system.lower()
    assert skill.exists()
    assert "Role: extraction, not analysis" in skill_text
    assert "[мое допущение]" in skill_text
    assert "calculator" in skill_text
    assert "Data freshness" in skill_text
    assert "Workflow (mandatory order)" in skill_text
    assert "User-facing presentation" in skill_text
    assert "Do not reveal to the user" in skill_text
    assert "You may show Navigator model names" in skill_text
    assert "business language" in skill_text
    assert "Division disambiguation" in skill_text
    assert "⚠️ TOOL_ERROR" in skill_text
    assert "Stage 1" not in skill_text

    from ouroboros.skills.loader import check_skills

    report = check_skills(root / "data-test", workspace_root=root)
    rows = {item["name"]: item for item in report["skills"]}
    assert rows["navigator-bi"]["eligible"] is True
