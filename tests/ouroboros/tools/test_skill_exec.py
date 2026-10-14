"""Skill exec tools without review_skill."""

from __future__ import annotations

import json
import mimetypes
import pathlib
import sys

import pytest

from new_core.tools.context import ToolContext
from ouroboros.tools import skill_exec as skill_exec_mod
from tests.helpers.audit_events import sql_audit_event_repository
from tests.helpers.outbound import RecordingOutbound
from tests.helpers.parametrize import parametrize_from_dict
from tests.helpers.shared import make_skill_surface, make_tool_ctx

_MANIFEST = """---
name: {name}
description: test
version: 0.1
type: script
runtime: python3
{permissions}scripts:
  - name: scripts/run.py
---
"""


def _write_skill(repo: pathlib.Path, name: str, *, permissions: str = "") -> None:
    skills_root = repo / "skills" / name
    skills_root.mkdir(parents=True)
    (skills_root / "SKILL.md").write_text(_MANIFEST.format(name=name, permissions=permissions), encoding="utf-8")
    scripts = skills_root / "scripts"
    scripts.mkdir()
    (scripts / "run.py").write_text("print('hi')\n", encoding="utf-8")


@pytest.fixture
def ctx(tmp_path: pathlib.Path, monkeypatch) -> ToolContext:
    del monkeypatch
    repo = tmp_path / "repo"
    # net is trusted for distribution skills that declare it, so the subprocess tests below
    # run with NetworkPolicy.ALLOW and don't require host network isolation.
    _write_skill(repo, "alpha", permissions="permissions: [net]\n")
    return make_tool_ctx(
        repo_dir=repo,
        drive_root=tmp_path,
        skill_surface=make_skill_surface(repo_dir=repo, drive_root=tmp_path),
    )


def test_distribution_skill_exec_uses_the_published_package(ctx: ToolContext) -> None:
    result = json.loads(skill_exec_mod._handle_skill_exec(ctx, skill="distribution:alpha", script="scripts/run.py"))
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "hi"


def test_skill_exec_blocks_a_skill_excluded_by_the_agent_allowlist(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _write_skill(repo, "alpha")
    ctx = make_tool_ctx(
        repo_dir=repo,
        drive_root=tmp_path,
        skill_surface=make_skill_surface(
            repo_dir=repo,
            drive_root=tmp_path,
            settings={"agents.defaults.skills": []},
        ),
    )
    monkeypatch.setattr(
        skill_exec_mod,
        "SandboxRunner",
        lambda: pytest.fail("blocked skill created a sandbox runner"),
    )

    result = skill_exec_mod._handle_skill_exec(ctx, skill="distribution:alpha", script="scripts/run.py")

    assert result == (
        "⚠️ SKILL_EXEC_BLOCKED: skill 'distribution:alpha' is not eligible for execution (agent allowlist blocks this skill)."
    )


def test_skill_exec_blocks_a_provider_ineligible_skill_before_side_effects(
    ctx: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from new_core.skills.models import SkillBlockReason, SkillId

    loaded = ctx.skill_surface.get(SkillId("distribution", "alpha"))
    assert loaded is not None
    loaded.eligible = False
    loaded.blockers = [SkillBlockReason("invalid_package", "provider policy blocks this skill")]
    monkeypatch.setattr(ctx.skill_surface, "get", lambda *_args, **_kwargs: loaded)
    monkeypatch.setattr(
        skill_exec_mod,
        "SandboxRunner",
        lambda: pytest.fail("blocked skill created a sandbox runner"),
    )

    result = skill_exec_mod._handle_skill_exec(ctx, skill="distribution:alpha", script="scripts/run.py")

    assert result == (
        "⚠️ SKILL_EXEC_BLOCKED: skill 'distribution:alpha' is not eligible for execution (provider policy blocks this skill)."
    )


def test_skill_exec_lifecycle_event_is_reported(ctx: ToolContext, tmp_path: pathlib.Path) -> None:
    """The payload the supervisor's skill_lifecycle handler routes on is unchanged.

    The tool now names the event and what happened; type, timestamp, task id and delivery
    belong to the events channel.
    """
    from ouroboros.tools.events import PublishedToolEvents
    from tests.helpers.agent_events import RecordingAgentEvents

    sink = RecordingAgentEvents()
    ctx.events = PublishedToolEvents(
        sink, task_id="t-skill", scope=ctx.scope, audit_event_repository=sql_audit_event_repository()
    )

    result = json.loads(skill_exec_mod._handle_skill_exec(ctx, skill="distribution:alpha", script="scripts/run.py"))
    assert result["exit_code"] == 0

    event = sink.published[0]
    assert event.event_type == "skill_exec_finished"
    assert event.task_id == "t-skill"
    assert event.payload["skill"] == "alpha"
    assert event.payload["script"] == "scripts/run.py"
    assert event.payload["exit_code"] == 0
    assert event.ts


def test_skill_exec_lifecycle_falls_back_to_the_event_stream(
    ctx: ToolContext, tmp_path: pathlib.Path, monkeypatch
) -> None:
    """Without a publisher the event still has to land: recorded, and republished locally."""
    from ouroboros.tools.events import PublishedToolEvents

    audit = sql_audit_event_repository()
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "ouroboros.observability.event_bus.publish_event",
        lambda topic, data: published.append((topic, data)),
    )
    ctx.events = PublishedToolEvents(None, task_id="t-skill", audit_event_repository=audit)

    json.loads(skill_exec_mod._handle_skill_exec(ctx, skill="distribution:alpha", script="scripts/run.py"))

    recorded = [(e.event_type, e.metadata) for e in audit.list_audit_events(event_type="skill_exec_finished")]
    assert [name for name, _ in recorded] == ["skill_exec_finished"]
    assert recorded[0][1]["skill"] == "alpha"
    assert [topic for topic, _ in published] == ["skill.lifecycle"]


class _CapturingRunner:
    """SandboxRunner stand-in recording the policy and returning a canned result."""

    captured: dict = {}
    result_kwargs: dict = {}

    def run(self, cmd, policy, *, env, stop_requested=None):
        _CapturingRunner.captured["cmd"] = cmd
        _CapturingRunner.captured["policy"] = policy
        _CapturingRunner.captured["stop_requested"] = stop_requested
        from ouroboros.contracts.sandbox_policy import SandboxResult

        kwargs = {
            "stdout": b"hi\n",
            "stderr": b"",
            "returncode": 0,
            "timed_out": False,
            "overflowed": False,
            "denied": False,
            "denial_reason": None,
            "audit_events": [],
            **_CapturingRunner.result_kwargs,
        }
        return SandboxResult(**kwargs)


def test_skill_exec_passes_the_task_stop_to_the_sandbox(ctx: ToolContext, capture_runner) -> None:
    stopped = False
    ctx.stop_requested = lambda: stopped

    skill_exec_mod._handle_skill_exec(ctx, skill="distribution:alpha", script="scripts/run.py")

    assert capture_runner.captured["stop_requested"] is ctx.stop_requested


@pytest.fixture
def capture_runner(monkeypatch):
    _CapturingRunner.captured = {}
    _CapturingRunner.result_kwargs = {}
    monkeypatch.setattr(skill_exec_mod, "SandboxRunner", _CapturingRunner)
    return _CapturingRunner


def test_net_permission_maps_to_network_allow(ctx: ToolContext, capture_runner) -> None:
    from ouroboros.contracts.sandbox_policy import NetworkPolicy

    skill_exec_mod._handle_skill_exec(ctx, skill="distribution:alpha", script="scripts/run.py")
    assert capture_runner.captured["policy"].network == NetworkPolicy.ALLOW


def test_no_net_permission_maps_to_network_deny(tmp_path: pathlib.Path, capture_runner) -> None:
    from ouroboros.contracts.sandbox_policy import NetworkPolicy

    repo = tmp_path / "repo"
    _write_skill(repo, "beta")
    ctx = make_tool_ctx(
        repo_dir=repo,
        drive_root=tmp_path,
        skill_surface=make_skill_surface(repo_dir=repo, drive_root=tmp_path),
    )

    skill_exec_mod._handle_skill_exec(ctx, skill="distribution:beta", script="scripts/run.py")
    assert capture_runner.captured["policy"].network == NetworkPolicy.DENY


def test_denied_sandbox_result_surfaces_as_skill_exec_denied(ctx: ToolContext, capture_runner) -> None:
    capture_runner.result_kwargs = {
        "denied": True,
        "denial_reason": "network=deny cannot be enforced on this host",
        "returncode": -1,
        "stdout": b"",
    }
    out = skill_exec_mod._handle_skill_exec(ctx, skill="distribution:alpha", script="scripts/run.py")
    assert out.startswith("⚠️ SKILL_EXEC_DENIED:")
    assert "cannot be enforced" in out


def test_output_caps_configurable_via_settings(ctx: ToolContext, capture_runner, monkeypatch) -> None:
    settings = skill_exec_mod.load_settings().model_copy(
        update={
            "OUROBOROS_SKILL_EXEC_MAX_STDOUT_BYTES": 1234,
            "OUROBOROS_SKILL_EXEC_MAX_STDERR_BYTES": 567,
        }
    )
    monkeypatch.setattr(skill_exec_mod, "load_settings", lambda: settings)

    skill_exec_mod._handle_skill_exec(ctx, skill="distribution:alpha", script="scripts/run.py")
    policy = capture_runner.captured["policy"]
    assert policy.stdout_cap == 1234
    assert policy.stderr_cap == 567

    capture_runner.result_kwargs = {"overflowed": True}
    out = skill_exec_mod._handle_skill_exec(ctx, skill="distribution:alpha", script="scripts/run.py")
    assert out.startswith("⚠️ SKILL_EXEC_OVERFLOW:")
    assert "stdout<=1234B" in out and "stderr<=567B" in out

    capture_runner.result_kwargs = {"timed_out": True, "stdout": b"x" * 2000}
    out = skill_exec_mod._handle_skill_exec(ctx, skill="distribution:alpha", script="scripts/run.py")
    assert out.startswith("⚠️ SKILL_EXEC_TIMEOUT:")
    assert "truncated stdout at 1234 bytes" in out


def test_non_positive_caps_fall_back_to_defaults(ctx: ToolContext, capture_runner, monkeypatch) -> None:
    settings = skill_exec_mod.load_settings().model_copy(
        update={
            "OUROBOROS_SKILL_EXEC_MAX_STDOUT_BYTES": 0,
            "OUROBOROS_SKILL_EXEC_MAX_STDERR_BYTES": -1,
        }
    )
    monkeypatch.setattr(skill_exec_mod, "load_settings", lambda: settings)

    skill_exec_mod._handle_skill_exec(ctx, skill="distribution:alpha", script="scripts/run.py")
    policy = capture_runner.captured["policy"]
    assert policy.stdout_cap == skill_exec_mod._MAX_STDOUT_BYTES
    assert policy.stderr_cap == skill_exec_mod._MAX_STDERR_BYTES


def test_oversized_caps_clamped_to_ceiling(ctx: ToolContext, capture_runner, monkeypatch) -> None:
    settings = skill_exec_mod.load_settings().model_copy(
        update={
            "OUROBOROS_SKILL_EXEC_MAX_STDOUT_BYTES": 2_000_000_000,
            "OUROBOROS_SKILL_EXEC_MAX_STDERR_BYTES": 2_000_000_000,
        }
    )
    monkeypatch.setattr(skill_exec_mod, "load_settings", lambda: settings)

    skill_exec_mod._handle_skill_exec(ctx, skill="distribution:alpha", script="scripts/run.py")
    policy = capture_runner.captured["policy"]
    assert policy.stdout_cap == skill_exec_mod._HARD_STREAM_CAP_CEILING_BYTES
    assert policy.stderr_cap == skill_exec_mod._HARD_STREAM_CAP_CEILING_BYTES


# --out leaves a file in the workdir; --in <name> reads one skill_exec put there.
_ROUND_TRIP_SCRIPT = """import os, pathlib, sys
workdir = pathlib.Path(os.environ["OUROBOROS_SKILL_WORKDIR"])
if sys.argv[1] == "--out":
    (workdir / "out.txt").write_bytes(b"built")
else:
    print(sys.argv[2], (workdir / sys.argv[2]).read_bytes().decode())
"""


def _install(ctx: ToolContext, script: str, *, fs: bool) -> None:
    skill_dir = ctx.repo_dir / "skills" / "alpha"
    # net keeps these subprocess tests running on hosts without unshare --net
    # (network deny is fail-closed); fs is the permission under test.
    permissions = "permissions: [fs, net]\n" if fs else "permissions: [net]\n"
    (skill_dir / "SKILL.md").write_text(_MANIFEST.format(name="alpha", permissions=permissions), encoding="utf-8")
    (skill_dir / "scripts" / "run.py").write_text(script, encoding="utf-8")


def _run(ctx: ToolContext, *args: str) -> str:
    ctx.skill_surface = make_skill_surface(repo_dir=ctx.repo_dir, drive_root=ctx.drive_root)
    return skill_exec_mod._handle_skill_exec(ctx, skill="distribution:alpha", script="scripts/run.py", args=list(args))


def _stored_bytes(ctx: ToolContext, rel: str) -> bytes | None:
    from ouroboros.storage.blob.factory import get_storage_provider
    from ouroboros.storage.chat_attachments import read_attachment_bytes
    from ouroboros.tools.service.artifacts import resolve_artifact

    file = resolve_artifact(ctx, rel)
    return (
        read_attachment_bytes(ctx.scope, get_storage_provider(), file["storage_key"], mime=file["mime"])
        if file
        else None
    )


def _store(ctx: ToolContext, rel: str, *, task_id: str, content: bytes = b"x") -> str:
    from tests.helpers.files import stored_file

    stored_file(
        scope=ctx.scope,
        data=content,
        filename=pathlib.Path(rel).name,
        producer_task=task_id,
        artifact_path=rel.removeprefix("uploads/"),
    )
    return rel


@parametrize_from_dict(
    {
        "fs declared": {"fs": True},
        "no fs": {"fs": False},
    }
)
def test_skill_exec_gives_a_workdir_only_to_a_skill_with_the_fs_permission(ctx: ToolContext, fs: bool) -> None:
    _install(ctx, "import os\nprint(os.environ.get('OUROBOROS_SKILL_WORKDIR', '<absent>'))\n", fs=fs)

    result = json.loads(_run(ctx))

    assert (result["stdout"].strip() != "<absent>") is fs
    assert result["artifacts"] == []
    assert list((ctx.drive_root / "tmp").glob("ouroboros-skill-*")) == []


@pytest.mark.parametrize("filename,content", [("out.txt", b"built"), ("image.webp", b"RIFF1234WEBP")])
@pytest.mark.parametrize("absolute", [False, True])
def test_skill_exec_publishes_what_the_script_leaves_and_hands_it_back_by_name(
    ctx: ToolContext, tmp_path: pathlib.Path, monkeypatch, isolate_settings, filename, content, absolute
) -> None:
    from ouroboros.tools.service.messaging import get_tools

    monkeypatch.setattr(mimetypes, "guess_type", mimetypes.MimeTypes(filenames=()).guess_type)
    isolate_settings.save_settings(
        {"OUROBOROS_ATTACHMENT_DOCUMENT_MAX_BYTES": 8, "OUROBOROS_ATTACHMENT_IMAGE_MAX_BYTES": 12}
    )
    ctx.task_id = "task-1"
    script = _ROUND_TRIP_SCRIPT.replace('"out.txt"', repr(filename)).replace('b"built"', repr(content))
    _install(ctx, script, fs=True)

    built = json.loads(_run(ctx, "--out"))

    artifact = f"uploads/task_artifacts/task-1/{filename}"
    assert built["artifacts"] == [artifact]
    assert _stored_bytes(ctx, artifact) == content

    reference = str(ctx.drive_root / "unused" / ".." / artifact) if absolute else artifact
    read = json.loads(_run(ctx, "--in", reference))

    assert read["stdout"].strip() == f"{filename} {content.decode()}"
    assert read["artifacts"] == []

    ctx.task_workspace = tmp_path / "workspace"
    ctx.task_workspace.mkdir()
    ctx.outbound = RecordingOutbound()
    send_file = next(entry.handler for entry in get_tools() if entry.name == "send_file")

    assert send_file(ctx, file_path=artifact).startswith("OK:")
    from tests.helpers.files import outbound_bytes

    assert outbound_bytes(ctx.outbound.files[0][0], ctx.scope) == content
    # The flat name an earlier release handed to a model still resolves, for one release.
    assert send_file(ctx, file_path=f"uploads/{filename}").startswith("OK:")
    assert outbound_bytes(ctx.outbound.files[1][0], ctx.scope) == content


@parametrize_from_dict(
    {
        "unavailable explicit input": {
            "stored": {"uploads/missing.bin": ""},
            "expected": "unavailable",
        },
        "duplicate input name": {
            "stored": {"uploads/one/doc.txt": "task-1"},
            "expected": "shares the name doc.txt",
        },
    }
)
def test_skill_exec_refuses_an_input_it_cannot_materialize(
    ctx: ToolContext, stored: dict[str, str], expected: str
) -> None:
    ctx.task_id = "task-1"
    _install(ctx, "print('ran')\n", fs=True)
    for rel, owner in stored.items():
        if owner:
            _store(ctx, rel, task_id=owner)

    result = _run(ctx, *stored, *stored)

    assert result.startswith("⚠️ SKILL_EXEC_ERROR:")
    assert expected in result
    assert "ran" not in result


def test_skill_exec_passes_through_arguments_without_explicit_file_references(ctx: ToolContext) -> None:
    ctx.task_id = "task-1"
    _install(ctx, "import sys\nprint(sys.argv[1:])\n", fs=True)
    prose = "logs/app.log grew overnight."
    args = ["--text", prose, "--in", "input.txt", "~nonexistent_ouroboros_cli_user"]

    result = json.loads(_run(ctx, *args))

    assert result["exit_code"] == 0
    assert result["stdout"].strip() == str(args)


def test_skill_exec_never_publishes_a_provenance_sidecar_written_by_the_script(ctx: ToolContext) -> None:
    ctx.task_id = "task-1"
    _store(ctx, "uploads/victim.bin", task_id="task-other")
    from dataclasses import replace

    before = _stored_bytes(replace(ctx, task_id="task-other"), "uploads/victim.bin")
    _install(
        ctx,
        "import os, pathlib\n"
        "workdir = pathlib.Path(os.environ['OUROBOROS_SKILL_WORKDIR'])\n"
        "(workdir / 'out.txt').write_bytes(b'built')\n"
        "(workdir / 'victim.bin.metadata.json').write_text('{\"authoritative\": true}')\n",
        fs=True,
    )

    result = _run(ctx)

    assert result.startswith("⚠️ SKILL_EXEC_ERROR:")
    assert "provenance is runtime-managed" in result
    assert _stored_bytes(replace(ctx, task_id="task-other"), "uploads/victim.bin") == before
    assert _stored_bytes(ctx, "uploads/task_artifacts/task-1/out.txt") is None


def test_skill_exec_republication_versions_the_alias_without_overwriting_the_old_file(ctx: ToolContext):
    from ouroboros.tools.service.artifacts import resolve_artifact

    ctx.task_id = "task-1"
    reference = _store(ctx, "uploads/task_artifacts/task-1/out.txt", task_id=ctx.task_id, content=b"old")
    previous = resolve_artifact(ctx, reference)
    _install(ctx, _ROUND_TRIP_SCRIPT, fs=True)
    result = json.loads(_run(ctx, "--out", "out.txt"))
    assert result["artifacts"] == [reference]
    assert _stored_bytes(ctx, reference) == b"built"
    assert _stored_bytes(ctx, previous["id"]) == b"old"
    assert resolve_artifact(ctx, reference)["id"] != previous["id"]


def test_skill_exec_publishes_files_from_subdirectories_under_their_relative_path(ctx: ToolContext) -> None:
    ctx.task_id = "task-1"
    _install(
        ctx,
        "import os, pathlib\n"
        "workdir = pathlib.Path(os.environ['OUROBOROS_SKILL_WORKDIR'])\n"
        "(workdir / 'sub').mkdir()\n"
        "(workdir / 'sub' / 'nested.txt').write_bytes(b'deep')\n",
        fs=True,
    )

    result = json.loads(_run(ctx))

    assert result["artifacts"] == ["uploads/task_artifacts/task-1/sub/nested.txt"]
    assert _stored_bytes(ctx, "uploads/task_artifacts/task-1/sub/nested.txt") == b"deep"


@parametrize_from_dict(
    {
        "script failed": {
            "script": _ROUND_TRIP_SCRIPT + "raise SystemExit(3)\n",
            "limits": {},
            "expected": "⚠️ SKILL_EXEC_FAILED",
        },
        "file over the limit": {
            "script": _ROUND_TRIP_SCRIPT,
            "limits": {"OUROBOROS_ATTACHMENT_DOCUMENT_MAX_BYTES": 3},
            "expected": "⚠️ SKILL_EXEC_ERROR: out.txt is 5 bytes, over the",
        },
        "WEBP over the image limit": {
            "script": _ROUND_TRIP_SCRIPT + "(workdir / 'image.webp').write_bytes(b'RIFF1234WEBP')\n",
            "limits": {"OUROBOROS_ATTACHMENT_IMAGE_MAX_BYTES": 11},
            "expected": "⚠️ SKILL_EXEC_ERROR: image.webp is 12 bytes, over the 11 bytes limit",
        },
    }
)
def test_skill_exec_publishes_nothing_after_a_failed_run_or_over_the_limit(
    ctx: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
    isolate_settings,
    script: str,
    limits: dict[str, int],
    expected: str,
) -> None:
    monkeypatch.setattr(mimetypes, "guess_type", mimetypes.MimeTypes(filenames=()).guess_type)
    ctx.task_id = "task-1"
    _install(ctx, script, fs=True)
    if limits:
        isolate_settings.save_settings(limits)

    result = _run(ctx, "--out")

    assert result.startswith(expected)
    assert not (ctx.drive_root / "uploads").exists()
    assert list((ctx.drive_root / "tmp").glob("ouroboros-skill-*")) == []


def test_python_runtime_resolves_to_the_running_interpreter_not_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(skill_exec_mod.shutil, "which", lambda name: f"/usr/bin/{name}")

    assert skill_exec_mod._resolve_runtime_binary("python3") == sys.executable


def test_python_runtime_falls_back_to_path_without_a_usable_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "executable", "")
    monkeypatch.setattr(skill_exec_mod.shutil, "which", lambda name: f"/usr/bin/{name}")

    assert skill_exec_mod._resolve_runtime_binary("python3") == "/usr/bin/python3"


def test_non_python_runtime_resolves_via_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(skill_exec_mod.shutil, "which", lambda name: f"/usr/bin/{name}")

    assert skill_exec_mod._resolve_runtime_binary("bash") == "/usr/bin/bash"


def test_two_tasks_publish_one_name_and_each_gets_its_own_bytes(ctx: ToolContext, tmp_path: pathlib.Path) -> None:
    """The key space is per task, so the second publication is not a collision at all."""
    from dataclasses import replace

    from ouroboros.tools.service.artifacts import materialize, publish

    published = {}
    for task_id, content in (("task-1", b"one"), ("task-2", b"two")):
        workdir = tmp_path / f"work-{task_id}"
        workdir.mkdir()
        (workdir / "report.docx").write_bytes(content)
        task_ctx = replace(ctx, task_id=task_id)
        published[task_id] = publish(task_ctx, task_ctx.scope, workdir, [workdir / "report.docx"])[0]

    assert published["task-1"] == "uploads/task_artifacts/task-1/report.docx"
    assert published["task-2"] == "uploads/task_artifacts/task-2/report.docx"
    for task_id, content in (("task-1", b"one"), ("task-2", b"two")):
        into = tmp_path / f"into-{task_id}"
        into.mkdir()
        task_ctx = replace(ctx, task_id=task_id)
        materialized, error = materialize(task_ctx, published[task_id], into)
        assert error == ""
        assert materialized is not None and materialized.read_bytes() == content


def test_publishing_without_a_task_is_refused_and_writes_nothing(ctx: ToolContext, tmp_path: pathlib.Path) -> None:
    from ouroboros.tools.service.artifacts import publish

    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "out.txt").write_bytes(b"orphan")

    with pytest.raises(ValueError, match="published by a task"):
        publish(ctx, ctx.scope, workdir, [workdir / "out.txt"])

    assert not (ctx.drive_root / "uploads").exists()
