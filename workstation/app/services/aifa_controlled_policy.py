"""AIFA-specific control-plane execution fence.

This module deliberately sits beside the generic Workstation ``PolicyEngine``.
The generic engine remains the source of truth for sandbox/path/environment
policy, while this fence restricts commands only when the workspace is routed
to the controlled AIFA workflow.
"""

from __future__ import annotations

import json
import logging
import shlex
from pathlib import Path

from app.utils.exceptions.common import PolicyDeniedException
from app.utils.logging import redact_log_value
from app.utils.paths import map_virtual_to_host

_SHELL_BINARIES = {"sh", "bash", "dash", "zsh"}
_SHELL_COMMAND_FLAGS = {"-c", "-lc"}
_SHELL_CONTROL_TOKENS = {";", "&&", "||", "|", ">", ">>", "<", "<<"}

_AIFA_PIPELINE_SCRIPT = "skills/aifa/scripts/aifa_pipeline.py"
_TASK_ROUTER_SCRIPT = "skills/task-router/scripts/route_task.py"
_AIFA_STATE_PATH = "/workspace/aifa_pipeline_state.json"
_AGENT_MODE_STATE_PATH = "/workspace/agent_mode_state.json"

logger = logging.getLogger(__name__)


class AifaControlledPolicy:
    """Restrict control-plane commands for a routed AIFA workspace."""

    def validate_exec(self, command: list[str], *, rootfs_path: str) -> None:
        raw_command = " ".join(command).replace("\\", "/")
        tokens = self._normalized_shell_command(command)
        script = self._command_script(tokens)
        raw_script_path = tokens[1].replace("\\", "/") if len(tokens) >= 2 else ""

        if raw_script_path.startswith("/workspace/") and script in {
            _AIFA_PIPELINE_SCRIPT,
            _TASK_ROUTER_SCRIPT,
        }:
            self._deny(
                "Use the workspace-relative control-plane script path for exec; "
                "/workspace is only for file tools",
                command=command,
                details={
                    "providedScript": raw_script_path,
                    "requiredScript": script,
                    "retryWithRelativePath": True,
                },
            )

        # Router state is control-plane data. It may only be created by the
        # structured router tool and consumed by the canonical router command.
        if (
            ".task-router/" in raw_command
            or ".task_router/" in raw_command
            or (
                any(
                    name in raw_command
                    for name in ("task_router_input.json", "agent_mode_state.json")
                )
                and script != _TASK_ROUTER_SCRIPT
            )
        ):
            self._deny(
                "Task router input/output must be created through the structured router tool",
                command=command,
                details=self._router_recovery_details(include_write_flags=True),
            )

        # Routing is constrained even before an AIFA run state exists.
        if script == _TASK_ROUTER_SCRIPT:
            if self._is_canonical_task_router_command(tokens[2:]):
                return
            self._deny(
                "Task router CLI requires the canonical task_router_input.json contract",
                command=command,
                details=self._router_recovery_details(include_write_flags=False),
            )

        state_path = map_virtual_to_host(rootfs_path, _AIFA_STATE_PATH)
        state = self._read_json_file(rootfs_path, _AIFA_STATE_PATH)
        mode = self._read_json_file(rootfs_path, _AGENT_MODE_STATE_PATH)
        decision = str(mode.get("decision") or "") if mode else ""
        controlled = decision == "financial" or (decision != "general" and state_path.is_file())
        if not controlled:
            return

        next_step = str(state.get("next_step") or "unknown") if state else "unknown"
        if self._is_allowed_controlled_command(
            tokens,
            next_step=next_step,
            has_state=state_path.is_file(),
        ):
            return

        next_command = str(state.get("next_command") or "") if state else ""
        allowed_model_artifacts = state.get("allowed_model_artifacts") if state else []
        if not isinstance(allowed_model_artifacts, list):
            allowed_model_artifacts = []
        step = self._option_value(tokens[2:], "--step") if script == _AIFA_PIPELINE_SCRIPT else None
        if step == "submit_artifact":
            message = (
                "submit_artifact is not an aifa_pipeline.py step. Submit the requested model "
                "artifact through aifa_submit_artifact or the structured compatibility operation "
                "on workstation_materialize_script."
            )
        else:
            message = (
                "AIFA controlled mode permits only the pipeline step expected by the current "
                "state or a structured model-artifact submission"
            )
        self._deny(
            message,
            command=command,
            details={
                "nextStep": next_step,
                "nextCommand": next_command,
                "requiredModelArtifacts": allowed_model_artifacts,
                "preferredSubmissionTool": "aifa_submit_artifact",
                "fallbackSubmissionTool": "workstation_materialize_script",
                "fallbackOperation": "aifa_submit_artifact",
                "allowedScripts": [_AIFA_PIPELINE_SCRIPT, _TASK_ROUTER_SCRIPT],
                "pipelineCliHasSubmitArtifactStep": False,
                "retryWithArbitraryScript": False,
            },
        )

    def _deny(
        self,
        message: str,
        *,
        command: list[str],
        details: dict[str, object],
    ) -> None:
        logger.warning(
            "policy.aifa_controlled_exec_denied",
            extra={
                "diagnostics": {
                    "phase": "aifa-controlled-exec-denied",
                    "command": redact_log_value(command),
                    "reason": message,
                    **details,
                }
            },
        )
        raise PolicyDeniedException(message, details=details)

    def _router_recovery_details(self, *, include_write_flags: bool) -> dict[str, object]:
        details: dict[str, object] = {
            "requiredCommand": (
                "python3 skills/task-router/scripts/route_task.py "
                "--input task_router_input.json --output agent_mode_state.json"
            ),
            "preferredTool": "task_router_submit_input",
            "fallbackTool": "workstation_materialize_script",
            "fallbackOperation": "task_router_submit_input",
            "retryWithHeredoc": False,
        }
        if include_write_flags:
            details["retryWithWrite"] = False
        return details

    def _command_script(self, tokens: list[str]) -> str | None:
        if len(tokens) < 2 or Path(tokens[0]).name.lower() not in {"python", "python3"}:
            return None
        return self._normalize_workspace_script(tokens[1])

    def _read_json_file(self, rootfs_path: str, virtual_path: str) -> dict[str, object]:
        path = map_virtual_to_host(rootfs_path, virtual_path)
        if not path.exists() or not path.is_file():
            return {}
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _normalized_shell_command(self, command: list[str]) -> list[str]:
        if (
            len(command) >= 3
            and Path(command[0]).name.lower() in _SHELL_BINARIES
            and command[1] in _SHELL_COMMAND_FLAGS
        ):
            payload = command[2]
            if "`" in payload or "$(" in payload or "\n" in payload or "\r" in payload:
                return []
            lexer = shlex.shlex(payload, posix=True, punctuation_chars=";&|<>")
            lexer.whitespace_split = True
            lexer.commenters = ""
            try:
                tokens = list(lexer)
            except ValueError:
                return []
        else:
            tokens = list(command)
        if any(token in _SHELL_CONTROL_TOKENS for token in tokens):
            return []
        return tokens

    def _is_allowed_controlled_command(
        self,
        tokens: list[str],
        *,
        next_step: str,
        has_state: bool,
    ) -> bool:
        if len(tokens) < 2 or Path(tokens[0]).name.lower() not in {"python", "python3"}:
            return False
        script = self._normalize_workspace_script(tokens[1])
        if script == _TASK_ROUTER_SCRIPT:
            return self._is_canonical_task_router_command(tokens[2:])
        if script != _AIFA_PIPELINE_SCRIPT:
            return False

        step = self._option_value(tokens[2:], "--step")
        if step is None:
            return False
        return step in self._allowed_aifa_steps(next_step=next_step, has_state=has_state)

    def _normalize_workspace_script(self, value: str) -> str:
        script = value.replace("\\", "/")
        if script.startswith("/workspace/"):
            script = script[len("/workspace/") :]
        while script.startswith("./"):
            script = script[2:]
        return script

    def _option_value(self, tokens: list[str], option: str) -> str | None:
        values: list[str] = []
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token == option:
                if index + 1 >= len(tokens):
                    return None
                values.append(tokens[index + 1])
                index += 2
                continue
            if token.startswith(f"{option}="):
                values.append(token.split("=", 1)[1])
            index += 1
        return values[0] if len(values) == 1 else None

    def _canonical_workspace_name(self, value: str) -> str:
        normalized = value.replace("\\", "/")
        if normalized.startswith("/workspace/"):
            normalized = normalized[len("/workspace/") :]
        while normalized.startswith("./"):
            normalized = normalized[2:]
        return normalized

    def _is_canonical_task_router_command(self, tokens: list[str]) -> bool:
        if len(tokens) != 4:
            return False
        input_path = self._option_value(tokens, "--input")
        output_path = self._option_value(tokens, "--output")
        if input_path is None or output_path is None:
            return False
        allowed_tokens = {"--input", input_path, "--output", output_path}
        if any(token not in allowed_tokens for token in tokens):
            return False
        return (
            self._canonical_workspace_name(input_path) == "task_router_input.json"
            and self._canonical_workspace_name(output_path) == "agent_mode_state.json"
        )

    def _allowed_aifa_steps(self, *, next_step: str, has_state: bool) -> set[str]:
        if not has_state:
            return {"init_run"}
        if next_step == "classify_document":
            return {"pdf_to_text", "prepare_local_fallback", "prepare_ocr", "classify_document"}
        if next_step == "prepare_scans_then_validate_pipeline_closure":
            return {"validate_pipeline_closure"}
        if next_step == "financial_dialogue":
            return {"calculate_batch", "init_run"}
        if next_step == "attach":
            return {"init_run"}
        if next_step in {
            "validate_classification",
            "plan_extraction",
            "validate_extraction",
            "calculate",
            "validate_summary",
            "build_excel",
            "build_pdf",
            "validate_final_artifacts",
        }:
            return {next_step}
        return set()
