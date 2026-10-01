"""Assignment delivery over configured SMTP and outbound-only Dialogs bot."""

from __future__ import annotations

import html
import json
import logging
import smtplib
import socket
import ssl
import time
import uuid
from collections.abc import Callable
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ThreadPoolExecutor,
    TimeoutError as FutureTimeoutError,
    wait,
)
from dataclasses import dataclass, replace
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from ouroboros.observability.event_record import record_event
from ouroboros.storage.settings.schema import AppSettings
from ouroboros.storage.settings.store import load_settings
from supervisor.messaging.dialogs import DialogsProvider

SmtpFactory = Callable[[str, int, float], Any]
DialogsProviderFactory = Callable[[AppSettings], Any]
Sleeper = Callable[[float], None]
Clock = Callable[[], float]

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _SmtpConfig:
    host: str
    port: int
    username: str
    password: str
    mail_from: str
    recipients: list[str]
    starttls_required: bool
    root_certificates: str
    client_certificate: str
    tls_version: str
    verify_tls: bool
    timeout_sec: float
    max_attempts: int
    retry_min_seconds: float
    retry_max_seconds: float


@dataclass(frozen=True)
class _DialogsConfig:
    endpoint: str
    bot_token: str
    group_id: str
    secure: bool
    root_certificates: str


@dataclass(frozen=True)
class _LogContext:
    drive_root: Path | None
    assignment_id: str
    task_id: str
    secrets: list[str]


_NETWORK_EXCEPTIONS = (
    EOFError,
    OSError,
    TimeoutError,
    socket.timeout,
    smtplib.SMTPServerDisconnected,
    smtplib.SMTPConnectError,
)


@dataclass(frozen=True)
class _Deadline:
    expires_at: float | None
    clock: Clock

    @classmethod
    def from_seconds(cls, seconds: float | None, clock: Clock) -> _Deadline:
        if seconds is None:
            return cls(expires_at=None, clock=clock)
        return cls(expires_at=clock() + max(float(seconds), 0.0), clock=clock)

    def remaining(self) -> float | None:
        if self.expires_at is None:
            return None
        return max(self.expires_at - self.clock(), 0.0)

    def expired(self) -> bool:
        remaining = self.remaining()
        return remaining is not None and remaining <= 0

    def clamp_timeout(self, timeout_sec: float) -> float:
        remaining = self.remaining()
        if remaining is None:
            return timeout_sec
        return min(timeout_sec, remaining)


def deliver_assignment(
    subject: str,
    body: str,
    *,
    repo_dir: Path | str | None = None,
    drive_root: Path | str | None = None,
    settings: AppSettings | None = None,
    smtp_factory: SmtpFactory | None = None,
    dialogs_provider_factory: DialogsProviderFactory = DialogsProvider,
    sleeper: Sleeper = time.sleep,
    retry_assignment_id: str = "",
    task_id: str = "",
    deadline_seconds: float | None = None,
    clock: Clock = time.monotonic,
) -> dict[str, Any]:
    """Deliver one assignment to configured channels without persisting delivery state."""

    clean_subject = str(subject or "").strip()
    clean_body = str(body or "").strip()
    clean_retry_id = str(retry_assignment_id or "").strip()
    if clean_retry_id:
        return {
            "status": "failed",
            "assignment_id": clean_retry_id,
            "error": (
                "ASSIGNMENT_RETRY_UNAVAILABLE: assignment delivery state is "
                "not persisted, so channel-specific retry is unavailable."
            ),
        }

    active_settings = settings or load_settings()
    secrets = _secret_values(active_settings)
    smtp_cfg = _smtp_config(active_settings)
    dialogs_cfg = _dialogs_config(active_settings)
    repo_path = Path(repo_dir or ".")
    drive_path = Path(drive_root) if drive_root is not None else None
    delivery_timeout = (
        float(deadline_seconds)
        if deadline_seconds is not None
        else float(active_settings.ASSISTANT_ASSIGNMENT_DELIVERY_TIMEOUT_SEC)
    )
    deadline = _Deadline.from_seconds(delivery_timeout, clock)
    active_smtp_factory = smtp_factory if smtp_factory is not None else _default_smtp_factory

    assignment_id = uuid.uuid4().hex
    state_recipients = smtp_cfg.recipients
    state_group_id = dialogs_cfg.group_id

    started_at = time.monotonic()
    log_ctx = _LogContext(
        drive_root=drive_path,
        assignment_id=assignment_id,
        task_id=str(task_id or "").strip(),
        secrets=secrets,
    )
    _emit_assignment_event(
        log_ctx,
        "assignment_delivery_started",
        {
            "retry": False,
            "state_persisted": False,
            "delivery_timeout_sec": delivery_timeout,
            "smtp_host": smtp_cfg.host,
            "smtp_port": smtp_cfg.port,
            "email_recipient_count": len(state_recipients),
            "sberchat_group_id": state_group_id,
        },
    )

    channel_results = _deliver_channels_parallel(
        clean_subject,
        clean_body,
        smtp_cfg,
        replace(dialogs_cfg, group_id=state_group_id),
        repo_path=repo_path,
        drive_path=drive_path,
        smtp_factory=active_smtp_factory,
        dialogs_provider_factory=dialogs_provider_factory,
        sleeper=sleeper,
        secrets=secrets,
        deadline=deadline,
        log_ctx=log_ctx,
        state_recipients=state_recipients,
    )
    email_result = channel_results["email"]
    sberchat_result = channel_results["sberchat"]
    result = {
        "status": _overall_status(email_result, sberchat_result),
        "assignment_id": assignment_id,
        "email": email_result,
        "sberchat": sberchat_result,
    }
    if email_result.get("status") == "skipped" and sberchat_result.get("status") == "skipped":
        result["error"] = "ASSIGNMENT_CONFIG_ERROR: no assignment delivery channel is configured."
    redacted = _redact_value(result, secrets)
    _emit_assignment_event(
        log_ctx,
        "assignment_delivery_finished",
        {
            "status": str(redacted.get("status") or ""),
            "email_status": str(email_result.get("status") or ""),
            "sberchat_status": str(sberchat_result.get("status") or ""),
            "duration_sec": _elapsed_seconds(started_at),
        },
    )
    return redacted


def _deliver_channels_parallel(
    subject: str,
    body: str,
    smtp_cfg: _SmtpConfig,
    dialogs_cfg: _DialogsConfig,
    *,
    repo_path: Path,
    drive_path: Path | None,
    smtp_factory: SmtpFactory,
    dialogs_provider_factory: DialogsProviderFactory,
    sleeper: Sleeper,
    secrets: list[str],
    deadline: _Deadline,
    log_ctx: _LogContext,
    state_recipients: list[str],
) -> dict[str, dict[str, Any]]:
    channel_results: dict[str, dict[str, Any]] = {}
    channel_started_at: dict[str, float] = {}
    timeout_results: dict[str, Callable[[], dict[str, Any]]] = {}
    tasks: dict[str, Callable[[], dict[str, Any]]] = {}

    if smtp_cfg.host:
        _emit_channel_started(
            log_ctx,
            "email",
            {
                "smtp_host": smtp_cfg.host,
                "smtp_port": smtp_cfg.port,
                "recipient_count": len(state_recipients),
            },
        )

        def email_task() -> dict[str, Any]:
            return _deliver_email(
                subject,
                body,
                smtp_cfg,
                smtp_factory=smtp_factory,
                sleeper=sleeper,
                secrets=secrets,
                deadline=deadline,
                recipients=state_recipients,
                log_ctx=log_ctx,
            )

        channel_started_at["email"] = time.monotonic()
        tasks["email"] = email_task
        timeout_results["email"] = lambda: _smtp_deadline_result(
            0,
            state_recipients,
            [],
            state_recipients,
            None,
        )
    else:
        channel_results["email"] = {"status": "skipped", "recipients": []}
        _emit_channel_finished(log_ctx, "email", channel_results["email"], skipped=True)

    if dialogs_cfg.bot_token:
        _emit_channel_started(log_ctx, "sberchat", {"group_id": dialogs_cfg.group_id})

        def sberchat_task() -> dict[str, Any]:
            return _deliver_dialogs(
                subject,
                body,
                dialogs_cfg,
                repo_dir=repo_path,
                drive_root=drive_path or Path("."),
                dialogs_provider_factory=dialogs_provider_factory,
                secrets=secrets,
            )

        channel_started_at["sberchat"] = time.monotonic()
        tasks["sberchat"] = sberchat_task
        timeout_results["sberchat"] = lambda: {
            "status": "failed",
            "group_id": dialogs_cfg.group_id,
            "error": "deadline_exceeded: assignment delivery deadline exhausted",
        }
    else:
        channel_results["sberchat"] = {"status": "skipped", "group_id": ""}
        _emit_channel_finished(log_ctx, "sberchat", channel_results["sberchat"], skipped=True)

    executor: ThreadPoolExecutor | None = None
    future_to_channel: dict[Future[dict[str, Any]], str] = {}
    try:
        if tasks:
            executor = ThreadPoolExecutor(max_workers=len(tasks), thread_name_prefix="assignment_delivery")
            for channel, task in tasks.items():
                future_to_channel[executor.submit(task)] = channel

            pending = set(future_to_channel)
            while pending:
                remaining = deadline.remaining()
                if remaining is not None and remaining <= 0:
                    break
                done, pending = wait(
                    pending,
                    timeout=remaining,
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    break
                for future in done:
                    channel = future_to_channel[future]
                    result = _future_result_or_failure(
                        channel,
                        future,
                        secrets=secrets,
                        smtp_cfg=smtp_cfg,
                        email_recipients=state_recipients,
                        dialogs_group_id=dialogs_cfg.group_id,
                    )
                    channel_results[channel] = result
                    _emit_channel_finished(
                        log_ctx,
                        channel,
                        result,
                        smtp_cfg=smtp_cfg if channel == "email" else None,
                        group_id=dialogs_cfg.group_id if channel == "sberchat" else "",
                        duration_sec=_duration_since(channel_started_at.get(channel)),
                    )

            if pending:
                done, pending = wait(pending, timeout=0.01)
                for future in done:
                    channel = future_to_channel[future]
                    result = _future_result_or_failure(
                        channel,
                        future,
                        secrets=secrets,
                        smtp_cfg=smtp_cfg,
                        email_recipients=state_recipients,
                        dialogs_group_id=dialogs_cfg.group_id,
                    )
                    channel_results[channel] = result
                    _emit_channel_finished(
                        log_ctx,
                        channel,
                        result,
                        smtp_cfg=smtp_cfg if channel == "email" else None,
                        group_id=dialogs_cfg.group_id if channel == "sberchat" else "",
                        duration_sec=_duration_since(channel_started_at.get(channel)),
                    )

            for future in pending:
                channel = future_to_channel[future]
                result = timeout_results[channel]()
                channel_results[channel] = result
                future.cancel()
                _emit_channel_finished(
                    log_ctx,
                    channel,
                    result,
                    smtp_cfg=smtp_cfg if channel == "email" else None,
                    group_id=dialogs_cfg.group_id if channel == "sberchat" else "",
                    duration_sec=_duration_since(channel_started_at.get(channel)),
                )
    finally:
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

    if "email" not in channel_results:
        channel_results["email"] = _smtp_deadline_result(0, state_recipients, [], state_recipients, None)
    if "sberchat" not in channel_results:
        channel_results["sberchat"] = {
            "status": "failed",
            "group_id": dialogs_cfg.group_id,
            "error": "deadline_exceeded: assignment delivery deadline exhausted",
        }
    return channel_results


def _future_result_or_failure(
    channel: str,
    future: Future[dict[str, Any]],
    *,
    secrets: list[str],
    smtp_cfg: _SmtpConfig,
    email_recipients: list[str],
    dialogs_group_id: str,
) -> dict[str, Any]:
    try:
        return _redact_value(future.result(), secrets)
    except FutureTimeoutError:
        if channel == "email":
            return _smtp_deadline_result(0, email_recipients, [], email_recipients, None)
        return {
            "status": "failed",
            "group_id": dialogs_group_id,
            "error": "deadline_exceeded: assignment delivery deadline exhausted",
        }
    except Exception as exc:
        error = _redact_text(_exception_text(exc), secrets)
        if channel == "email":
            return _smtp_failure(0, email_recipients or smtp_cfg.recipients, None, error)
        return {
            "status": "failed",
            "group_id": dialogs_group_id,
            "error": error,
        }


def _default_smtp_factory(host: str, port: int, timeout: float) -> smtplib.SMTP:
    return smtplib.SMTP(host=host, port=port, timeout=timeout)


def _smtp_config(settings: AppSettings) -> _SmtpConfig:
    return _SmtpConfig(
        host=str(settings.ASSISTANT_ASSIGNMENT_SMTP_HOST or "").strip(),
        port=max(int(settings.ASSISTANT_ASSIGNMENT_SMTP_PORT or 25), 1),
        username=str(settings.ASSISTANT_ASSIGNMENT_SMTP_USERNAME or "").strip(),
        password=_plain_secret(settings.ASSISTANT_ASSIGNMENT_SMTP_PASSWORD),
        mail_from=str(settings.ASSISTANT_ASSIGNMENT_MAIL_FROM or "").strip(),
        recipients=_parse_recipients(settings.ASSISTANT_ASSIGNMENT_MAIL_TO),
        starttls_required=bool(settings.ASSISTANT_ASSIGNMENT_SMTP_STARTTLS_REQUIRED),
        root_certificates=str(settings.ASSISTANT_ASSIGNMENT_SMTP_ROOT_CERTIFICATES or "").strip(),
        client_certificate=str(settings.ASSISTANT_ASSIGNMENT_SMTP_CLIENT_CERTIFICATE or "").strip(),
        tls_version=str(settings.ASSISTANT_ASSIGNMENT_SMTP_TLS_VERSION or "").strip(),
        verify_tls=bool(settings.ASSISTANT_ASSIGNMENT_SMTP_VERIFY_TLS),
        timeout_sec=max(float(settings.ASSISTANT_ASSIGNMENT_SMTP_TIMEOUT_SEC or 30.0), 0.1),
        max_attempts=max(int(settings.ASSISTANT_ASSIGNMENT_MAX_ATTEMPTS or 20), 1),
        retry_min_seconds=max(float(settings.ASSISTANT_ASSIGNMENT_RETRY_MIN_SECONDS or 1.0), 0.0),
        retry_max_seconds=max(float(settings.ASSISTANT_ASSIGNMENT_RETRY_MAX_SECONDS or 120.0), 0.0),
    )


def _dialogs_config(settings: AppSettings) -> _DialogsConfig:
    return _DialogsConfig(
        endpoint=str(settings.DIALOGS_GRPC_ENDPOINT or "").strip(),
        bot_token=_plain_secret(settings.ASSISTANT_DIALOGS_BOT_TOKEN),
        group_id=str(settings.ASSISTANT_DIALOGS_BOT_GROUP_ID or "").strip(),
        secure=bool(settings.DIALOGS_SECURE),
        root_certificates=str(settings.DIALOGS_ROOT_CERTIFICATES or "").strip(),
    )


def _emit_assignment_event(log_ctx: _LogContext | None, event_type: str, payload: dict[str, Any]) -> None:
    if log_ctx is None:
        return
    event_payload: dict[str, Any] = {
        "assignment_id": log_ctx.assignment_id,
        **payload,
    }
    if log_ctx.task_id:
        event_payload["task_id"] = log_ctx.task_id
    safe_payload = _redact_value(event_payload, log_ctx.secrets)
    log.info(
        "%s",
        json.dumps(
            {
                "type": event_type,
                **safe_payload,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    )
    if log_ctx.drive_root is not None:
        record_event(log_ctx.drive_root, "tools", event_type, safe_payload)


def _emit_channel_started(log_ctx: _LogContext, channel: str, payload: dict[str, Any]) -> None:
    _emit_assignment_event(
        log_ctx,
        "assignment_channel_started",
        {
            "channel": channel,
            **payload,
        },
    )


def _emit_channel_finished(
    log_ctx: _LogContext,
    channel: str,
    result: dict[str, Any],
    *,
    smtp_cfg: _SmtpConfig | None = None,
    group_id: str = "",
    skipped: bool = False,
    duration_sec: float = 0.0,
) -> None:
    payload: dict[str, Any] = {
        "channel": channel,
        "status": str(result.get("status") or ""),
        "duration_sec": duration_sec,
    }
    if skipped:
        payload["skipped"] = True
    if channel == "email":
        payload["recipient_count"] = len(_state_list(result.get("recipients")))
        payload["attempts"] = _as_int_or_none(result.get("attempts"))
        payload["smtp_code"] = _as_int_or_none(result.get("smtp_code"))
        if smtp_cfg is not None:
            payload["smtp_host"] = smtp_cfg.host
            payload["smtp_port"] = smtp_cfg.port
    if channel == "sberchat":
        payload["group_id"] = str(result.get("group_id") or group_id)
    error = str(result.get("error") or "").strip()
    if error:
        payload["error"] = error
    _emit_assignment_event(log_ctx, "assignment_channel_finished", payload)


def _emit_smtp_attempt_started(
    log_ctx: _LogContext | None,
    cfg: _SmtpConfig,
    *,
    attempt: int,
    recipient_count: int,
    timeout_sec: float,
) -> None:
    _emit_assignment_event(
        log_ctx,
        "assignment_smtp_attempt_started",
        {
            "channel": "email",
            "smtp_host": cfg.host,
            "smtp_port": cfg.port,
            "recipient_count": recipient_count,
            "attempt": attempt,
            "timeout_sec": timeout_sec,
        },
    )


def _emit_smtp_attempt_finished(
    log_ctx: _LogContext | None,
    cfg: _SmtpConfig,
    *,
    attempt: int,
    started_at: float,
    status: str,
    recipient_count: int,
    smtp_code: int | None = None,
    error: str = "",
    refused_count: int = 0,
) -> None:
    payload: dict[str, Any] = {
        "channel": "email",
        "smtp_host": cfg.host,
        "smtp_port": cfg.port,
        "recipient_count": recipient_count,
        "attempt": attempt,
        "smtp_code": smtp_code,
        "status": status,
        "duration_sec": _elapsed_seconds(started_at),
        "refused_count": refused_count,
    }
    if error:
        payload["error"] = error
    _emit_assignment_event(log_ctx, "assignment_smtp_attempt_finished", payload)


def _deliver_email(
    subject: str,
    body: str,
    cfg: _SmtpConfig,
    *,
    smtp_factory: SmtpFactory,
    sleeper: Sleeper,
    secrets: list[str],
    deadline: _Deadline,
    recipients: list[str] | None = None,
    log_ctx: _LogContext | None = None,
) -> dict[str, Any]:
    target_recipients = list(recipients or cfg.recipients)
    missing = _missing_smtp_fields(cfg)
    if missing:
        return {
            "status": "failed",
            "attempts": 0,
            "recipients": target_recipients,
            "smtp_code": None,
            "error": f"missing SMTP settings: {', '.join(missing)}",
        }
    if not target_recipients:
        return _smtp_failure(0, target_recipients, None, "missing SMTP recipients")

    remaining = list(target_recipients)
    accepted: list[str] = []
    permanent_refusals: dict[str, str] = {}
    last_code: int | None = None
    last_error = ""

    for attempt in range(1, cfg.max_attempts + 1):
        if deadline.expired():
            return _smtp_deadline_result(attempt - 1, target_recipients, accepted, remaining, last_code)
        smtp_timeout = deadline.clamp_timeout(cfg.timeout_sec)
        attempt_started = time.monotonic()
        _emit_smtp_attempt_started(
            log_ctx,
            cfg,
            attempt=attempt,
            recipient_count=len(remaining),
            timeout_sec=smtp_timeout,
        )
        try:
            refused = _smtp_attempt(
                subject,
                body,
                cfg,
                remaining,
                smtp_factory=smtp_factory,
                timeout_sec=smtp_timeout,
            )
        except smtplib.SMTPRecipientsRefused as exc:
            refused = dict(exc.recipients or {})
        except smtplib.SMTPSenderRefused as exc:
            last_code = _as_int_or_none(getattr(exc, "smtp_code", None))
            last_error = _redact_text(_exception_text(exc), secrets)
            _emit_smtp_attempt_finished(
                log_ctx,
                cfg,
                attempt=attempt,
                started_at=attempt_started,
                status="transient_error" if _is_transient_code(last_code) else "failed",
                recipient_count=len(remaining),
                smtp_code=last_code,
                error=last_error,
            )
            if _is_transient_code(last_code) and attempt < cfg.max_attempts:
                if _sleep_before_retry(attempt, cfg, deadline, sleeper):
                    continue
                return _smtp_deadline_result(attempt, target_recipients, accepted, remaining, last_code)
            return _smtp_failure(attempt, target_recipients, last_code, last_error)
        except smtplib.SMTPResponseException as exc:
            last_code = _as_int_or_none(getattr(exc, "smtp_code", None))
            last_error = _redact_text(_exception_text(exc), secrets)
            _emit_smtp_attempt_finished(
                log_ctx,
                cfg,
                attempt=attempt,
                started_at=attempt_started,
                status="transient_error" if _is_transient_code(last_code) else "failed",
                recipient_count=len(remaining),
                smtp_code=last_code,
                error=last_error,
            )
            if _is_transient_code(last_code) and attempt < cfg.max_attempts:
                if _sleep_before_retry(attempt, cfg, deadline, sleeper):
                    continue
                return _smtp_deadline_result(attempt, target_recipients, accepted, remaining, last_code)
            return _smtp_failure(attempt, target_recipients, last_code, last_error)
        except _NETWORK_EXCEPTIONS as exc:
            last_error = _redact_text(_exception_text(exc), secrets)
            _emit_smtp_attempt_finished(
                log_ctx,
                cfg,
                attempt=attempt,
                started_at=attempt_started,
                status="network_error" if attempt < cfg.max_attempts else "failed",
                recipient_count=len(remaining),
                smtp_code=None,
                error=last_error or "network error",
            )
            if attempt < cfg.max_attempts:
                if _sleep_before_retry(attempt, cfg, deadline, sleeper):
                    continue
                return _smtp_deadline_result(attempt, target_recipients, accepted, remaining, None)
            return _smtp_failure(attempt, target_recipients, None, last_error or "network error")
        except smtplib.SMTPException as exc:
            last_error = _redact_text(_exception_text(exc), secrets)
            _emit_smtp_attempt_finished(
                log_ctx,
                cfg,
                attempt=attempt,
                started_at=attempt_started,
                status="failed",
                recipient_count=len(remaining),
                smtp_code=None,
                error=last_error,
            )
            return _smtp_failure(attempt, target_recipients, None, last_error)

        refused = {str(addr): value for addr, value in dict(refused or {}).items()}
        refused_addrs = set(refused)
        accepted.extend(addr for addr in remaining if addr not in refused_addrs and addr not in accepted)
        if not refused:
            _emit_smtp_attempt_finished(
                log_ctx,
                cfg,
                attempt=attempt,
                started_at=attempt_started,
                status="partial_failure" if permanent_refusals else "sent",
                recipient_count=len(remaining),
                smtp_code=250,
                refused_count=len(permanent_refusals),
            )
            if permanent_refusals:
                return {
                    "status": "partial_failure",
                    "attempts": attempt,
                    "recipients": accepted,
                    "failed_recipients": sorted(permanent_refusals),
                    "smtp_code": last_code,
                    "error": _redact_text("; ".join(permanent_refusals.values()), secrets),
                }
            return _smtp_success(attempt, target_recipients, accepted)

        transient: list[str] = []
        for addr, refusal in refused.items():
            code, message = _refusal_code_message(refusal)
            last_code = code
            if _is_transient_code(code):
                transient.append(addr)
            else:
                permanent_refusals[addr] = message

        if transient and attempt < cfg.max_attempts:
            _emit_smtp_attempt_finished(
                log_ctx,
                cfg,
                attempt=attempt,
                started_at=attempt_started,
                status="transient_refusal",
                recipient_count=len(remaining),
                smtp_code=last_code,
                error=_redact_text(_refusal_summary(refused), secrets),
                refused_count=len(refused),
            )
            remaining = transient
            if _sleep_before_retry(attempt, cfg, deadline, sleeper):
                continue
            return _smtp_deadline_result(
                attempt,
                target_recipients,
                accepted,
                sorted(set(transient) | set(permanent_refusals)),
                last_code,
            )

        if accepted:
            _emit_smtp_attempt_finished(
                log_ctx,
                cfg,
                attempt=attempt,
                started_at=attempt_started,
                status="partial_failure",
                recipient_count=len(remaining),
                smtp_code=last_code,
                error=_redact_text(_refusal_summary(refused), secrets),
                refused_count=len(refused),
            )
            return {
                "status": "partial_failure",
                "attempts": attempt,
                "recipients": accepted,
                "failed_recipients": sorted(set(transient) | set(permanent_refusals)),
                "smtp_code": last_code,
                "error": _redact_text(_refusal_summary(refused), secrets),
            }
        _emit_smtp_attempt_finished(
            log_ctx,
            cfg,
            attempt=attempt,
            started_at=attempt_started,
            status="failed",
            recipient_count=len(remaining),
            smtp_code=last_code,
            error=_redact_text(_refusal_summary(refused), secrets),
            refused_count=len(refused),
        )
        return _smtp_failure(attempt, target_recipients, last_code, _redact_text(_refusal_summary(refused), secrets))

    if accepted:
        return {
            "status": "partial_failure",
            "attempts": cfg.max_attempts,
            "recipients": accepted,
            "failed_recipients": remaining,
            "smtp_code": last_code,
            "error": last_error or "SMTP retry attempts exhausted",
        }
    return _smtp_failure(cfg.max_attempts, target_recipients, last_code, last_error or "SMTP retry attempts exhausted")


def _smtp_attempt(
    subject: str,
    body: str,
    cfg: _SmtpConfig,
    recipients: list[str],
    *,
    smtp_factory: SmtpFactory,
    timeout_sec: float,
) -> dict[str, Any]:
    smtp = None
    try:
        smtp = smtp_factory(cfg.host, cfg.port, timeout_sec)
        smtp.ehlo()
        if cfg.starttls_required:
            smtp.starttls(context=_smtp_tls_context(cfg))
            smtp.ehlo()
        smtp.login(cfg.username, cfg.password)
        return dict(
            smtp.send_message(
                _build_message(subject, body, cfg.mail_from, recipients),
                from_addr=cfg.mail_from,
                to_addrs=recipients,
            )
            or {}
        )
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:
                pass


def _smtp_tls_context(cfg: _SmtpConfig) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=cfg.root_certificates or None)
    if not cfg.verify_tls:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    tls_version = _smtp_tls_version(cfg.tls_version)
    if tls_version is not None:
        context.minimum_version = tls_version
        context.maximum_version = tls_version
    if cfg.client_certificate:
        context.load_cert_chain(certfile=cfg.client_certificate)
    return context


def _smtp_tls_version(value: str) -> ssl.TLSVersion | None:
    match value:
        case "TLSv1.2":
            return ssl.TLSVersion.TLSv1_2
        case "TLSv1.3":
            return ssl.TLSVersion.TLSv1_3
        case _:
            return None


def _deliver_dialogs(
    subject: str,
    body: str,
    cfg: _DialogsConfig,
    *,
    repo_dir: Path,
    drive_root: Path,
    dialogs_provider_factory: DialogsProviderFactory,
    secrets: list[str],
) -> dict[str, Any]:
    missing = _missing_dialogs_fields(cfg)
    if missing:
        return {
            "status": "failed",
            "group_id": cfg.group_id,
            "error": f"missing Dialogs settings: {', '.join(missing)}",
        }

    del repo_dir, drive_root
    provider_settings = AppSettings(
        DIALOGS_GRPC_ENDPOINT=cfg.endpoint,
        DIALOGS_BOT_TOKEN=SecretStr(cfg.bot_token),
        DIALOGS_GROUP_ID=cfg.group_id,
        DIALOGS_SECURE=cfg.secure,
        DIALOGS_ROOT_CERTIFICATES=cfg.root_certificates,
    )
    try:
        provider = dialogs_provider_factory(provider_settings)
        provider.connect()
        ok, status = provider.send_text(cfg.group_id, _assignment_text(subject, body), fmt="markdown")
    except Exception as exc:
        return {
            "status": "failed",
            "group_id": cfg.group_id,
            "error": _redact_text(_exception_text(exc), secrets),
        }

    if ok:
        return {
            "status": "sent",
            "group_id": cfg.group_id,
            "message_id": _redact_text(str(status or ""), secrets),
        }
    return {
        "status": "failed",
        "group_id": cfg.group_id,
        "error": _redact_text(str(status or "Dialogs send failed"), secrets),
    }


def _build_message(subject: str, body: str, mail_from: str, recipients: list[str]) -> EmailMessage:
    message = EmailMessage()
    message["From"] = mail_from
    message["To"] = ", ".join(recipients)
    message["Subject"] = str(subject or "").strip()
    text_body = str(body or "").strip()
    message.set_content(text_body, subtype="plain", charset="utf-8")
    html_body = html.escape(text_body).replace("\n", "<br>\n")
    message.add_alternative(f"<html><body><p>{html_body}</p></body></html>", subtype="html", charset="utf-8")
    return message


def _assignment_text(subject: str, body: str) -> str:
    return f"{str(subject or '').strip()}\n\n{str(body or '').strip()}"


def _missing_smtp_fields(cfg: _SmtpConfig) -> list[str]:
    missing: list[str] = []
    if not cfg.host:
        missing.append("ASSISTANT_ASSIGNMENT_SMTP_HOST")
    if not cfg.username:
        missing.append("ASSISTANT_ASSIGNMENT_SMTP_USERNAME")
    if not cfg.password:
        missing.append("ASSISTANT_ASSIGNMENT_SMTP_PASSWORD")
    if not cfg.mail_from:
        missing.append("ASSISTANT_ASSIGNMENT_MAIL_FROM")
    if not cfg.recipients:
        missing.append("ASSISTANT_ASSIGNMENT_MAIL_TO")
    return missing


def _missing_dialogs_fields(cfg: _DialogsConfig) -> list[str]:
    missing: list[str] = []
    if not cfg.endpoint:
        missing.append("DIALOGS_GRPC_ENDPOINT")
    if not cfg.bot_token:
        missing.append("ASSISTANT_DIALOGS_BOT_TOKEN")
    if not cfg.group_id:
        missing.append("ASSISTANT_DIALOGS_BOT_GROUP_ID")
    return missing


def _parse_recipients(value: Any) -> list[str]:
    if isinstance(value, list):
        raw_items = value
    else:
        raw_items = str(value or "").replace(";", ",").split(",")
    recipients: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        recipients.append(text)
    return recipients


def _overall_status(email_result: dict[str, Any], dialogs_result: dict[str, Any]) -> str:
    statuses = [
        status for result in (email_result, dialogs_result) if (status := str(result.get("status") or "")) != "skipped"
    ]
    if statuses and all(status == "sent" for status in statuses):
        return "sent"
    if any(status in {"sent", "partial_failure"} for status in statuses):
        return "partial_failure"
    return "failed"


def _state_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _smtp_success(attempts: int, recipients: list[str], accepted: list[str]) -> dict[str, Any]:
    sent_recipients = accepted or recipients
    return {
        "status": "sent",
        "attempts": attempts,
        "recipients": sent_recipients,
        "smtp_code": 250,
    }


def _smtp_failure(attempts: int, recipients: list[str], code: int | None, error: str) -> dict[str, Any]:
    return {
        "status": "failed",
        "attempts": attempts,
        "recipients": recipients,
        "smtp_code": code,
        "error": error or "SMTP send failed",
    }


def _smtp_deadline_result(
    attempts: int,
    recipients: list[str],
    accepted: list[str],
    failed: list[str],
    code: int | None,
) -> dict[str, Any]:
    error = "deadline_exceeded: assignment delivery deadline exhausted"
    if accepted:
        return {
            "status": "partial_failure",
            "attempts": attempts,
            "recipients": accepted,
            "failed_recipients": sorted(set(failed)),
            "smtp_code": code,
            "error": error,
        }
    return _smtp_failure(attempts, recipients, code, error)


def _sleep_before_retry(
    attempt: int,
    cfg: _SmtpConfig,
    deadline: _Deadline,
    sleeper: Sleeper,
) -> bool:
    delay = _retry_delay(attempt, cfg)
    remaining = deadline.remaining()
    if remaining is None:
        sleeper(delay)
        return True
    if remaining <= 0:
        return False
    if delay >= remaining:
        sleeper(remaining)
        return False
    sleeper(delay)
    return True


def _elapsed_seconds(started_at: float) -> float:
    return max(time.monotonic() - started_at, 0.0)


def _duration_since(started_at: float | None) -> float:
    if started_at is None:
        return 0.0
    return _elapsed_seconds(started_at)


def _retry_delay(attempt: int, cfg: _SmtpConfig) -> float:
    raw_delay = cfg.retry_min_seconds * (2 ** max(attempt - 1, 0))
    if cfg.retry_max_seconds <= 0:
        return raw_delay
    return min(raw_delay, cfg.retry_max_seconds)


def _is_transient_code(code: int | None) -> bool:
    return code is not None and 400 <= code <= 499


def _refusal_code_message(refusal: Any) -> tuple[int | None, str]:
    if isinstance(refusal, tuple) and refusal:
        code = _as_int_or_none(refusal[0])
        message = _bytes_to_text(refusal[1]) if len(refusal) > 1 else ""
        return code, message
    return None, _bytes_to_text(refusal)


def _refusal_summary(refused: dict[str, Any]) -> str:
    parts: list[str] = []
    for addr, refusal in sorted(refused.items()):
        code, message = _refusal_code_message(refusal)
        prefix = str(code) if code is not None else "unknown"
        parts.append(f"{addr}: {prefix} {message}".strip())
    return "; ".join(parts)


def _as_int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _exception_text(exc: BaseException) -> str:
    if isinstance(exc, smtplib.SMTPResponseException):
        code = _as_int_or_none(getattr(exc, "smtp_code", None))
        error = _bytes_to_text(getattr(exc, "smtp_error", ""))
        if code is not None:
            return f"{code} {error}".strip()
    return str(exc)


def _bytes_to_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _plain_secret(value: Any) -> str:
    getter = getattr(value, "get_secret_value", None)
    if callable(getter):
        return str(getter() or "")
    return str(value or "")


def _secret_values(settings: AppSettings) -> list[str]:
    values = [
        _plain_secret(settings.ASSISTANT_ASSIGNMENT_SMTP_PASSWORD),
        _plain_secret(settings.ASSISTANT_DIALOGS_BOT_TOKEN),
    ]
    return [value for value in values if value]


def _redact_text(text: str, secrets: list[str]) -> str:
    redacted = str(text or "")
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _redact_value(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, str):
        return _redact_text(value, secrets)
    if isinstance(value, list):
        return [_redact_value(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: _redact_value(item, secrets) for key, item in value.items()}
    return value


__all__ = ["deliver_assignment"]
