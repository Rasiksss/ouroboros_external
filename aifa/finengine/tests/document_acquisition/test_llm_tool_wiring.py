"""fe_find_report_online — Срез 7: тул реально дозвонён до REGISTRY/execute()
и до белого списка `deploy/tools_whitelist.yaml` (execute() отказывает тулам
не из списка, даже если агент как-то узнал их имя — без записи в whitelist
тул был бы построен, но НЕДОСТИЖИМ)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from finengine.llm import tools as _tools
from finengine.llm.tools import ToolContext


def test_tool_is_registered_with_required_company_param():
    tool = _tools.REGISTRY["fe_find_report_online"]
    schema = tool.to_openclaw()
    assert schema["name"] == "fe_find_report_online"
    assert schema["parameters"]["required"] == ["company"]


def test_tool_is_in_deploy_whitelist():
    whitelist_path = Path(__file__).parents[2] / "deploy" / "tools_whitelist.yaml"
    text = whitelist_path.read_text(encoding="utf-8")
    assert "fe_find_report_online" in text, (
        "тул зарегистрирован в REGISTRY, но не в deploy/tools_whitelist.yaml — "
        "execute() откажет в вызове (см. docstring execute())"
    )


async def test_missing_company_returns_error(monkeypatch):
    from finengine.skills import web_search as ws
    monkeypatch.setattr(ws, "is_enabled", lambda: True)
    out = json.loads(await _tools._h_fe_find_report_online({}, ToolContext(user_id=1)))
    assert "error" in out


async def test_search_disabled_returns_friendly_error(monkeypatch):
    from finengine.skills import web_search as ws
    monkeypatch.setattr(ws, "is_enabled", lambda: False)
    out = json.loads(await _tools._h_fe_find_report_online(
        {"company": "Северсталь"}, ToolContext(user_id=1),
    ))
    assert "error" in out
    assert "TAVILY" in out["error"].upper() or "не настроен" in out["error"]


async def test_missing_user_context_returns_error(monkeypatch):
    from finengine.skills import web_search as ws
    monkeypatch.setattr(ws, "is_enabled", lambda: True)
    out = json.loads(await _tools._h_fe_find_report_online(
        {"company": "Северсталь"}, ToolContext(user_id=None),
    ))
    assert "error" in out


async def test_successful_call_returns_expected_json_shape(monkeypatch):
    from finengine.skills import web_search as ws
    monkeypatch.setattr(ws, "is_enabled", lambda: True)

    import finengine.skills.document_acquisition as da

    async def fake_find_and_register_report(company, *, owner_id, period=""):
        assert company == "Северсталь"
        assert owner_id == 42
        return da.AcquisitionOutcome(
            status="registered", doc_id="doc-abc", sha256="deadbeef",
            source_url="https://www.cbr.ru/report.pdf", source_tier="official_regulator",
            candidates_tried=1,
        )

    monkeypatch.setattr(da, "find_and_register_report", fake_find_and_register_report)

    out = json.loads(await _tools._h_fe_find_report_online(
        {"company": "Северсталь"}, ToolContext(user_id=42),
    ))
    assert out == {
        "status": "registered", "doc_id": "doc-abc", "source_url": "https://www.cbr.ru/report.pdf",
        "source_tier": "official_regulator", "candidates_tried": 1, "reason": "",
    }


async def test_execute_dispatches_through_whitelist(monkeypatch):
    from finengine.skills import web_search as ws
    monkeypatch.setattr(ws, "is_enabled", lambda: False)
    out = json.loads(await _tools.execute("fe_find_report_online", {"company": "X"}, ToolContext(user_id=1)))
    assert "error" in out  # доходит до хендлера, не блокируется как неизвестный тул
