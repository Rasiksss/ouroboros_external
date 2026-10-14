from __future__ import annotations

import ssl
import time
import uuid
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.schema import ToolSchema
from ouroboros.observability.aef_tracing import with_agent_header, with_trace_header
from ouroboros.storage.attachment_validation import image_signature
from ouroboros.storage.settings.schema import AppSettings
from ouroboros.storage.settings.store import load_settings
from ouroboros.tools.service.messaging import send_image_data

TIMEOUT_SEC = 300
_CONNECT_SEC = 10
_REQUEST_DEADLINE_SEC = 240
_POLL_INTERVAL_SEC = 5
_MAX_ERROR_BODY_CHARS = 2_000
_API_PATH = "/api/fusionbrain/api/v1"


class ImageGenerationError(Exception):
    pass


class PipelineData(BaseModel):
    query: str
    style: str | None = None


class ImageRequest(BaseModel):
    pipeline_id: str
    pipeline_data: PipelineData


class ExecuteResponse(BaseModel):
    request_id: str


class StatusResult(BaseModel):
    censored: bool = False
    result_image: str | None = None


class StatusResponse(BaseModel):
    status: str
    result: StatusResult = Field(default_factory=StatusResult)


class FusionBrainClient:
    def __init__(self, settings: AppSettings) -> None:
        base_url = settings.FUSIONBRAIN_BASE_URL.strip().rstrip("/")
        self._pipeline_id = settings.FUSIONBRAIN_PIPELINE_ID.strip()
        self._cert_path = settings.FUSIONBRAIN_CERT_PATH.get_secret_value().strip()
        self._key_path = settings.FUSIONBRAIN_KEY_PATH.get_secret_value().strip()
        self._ca_path = settings.FUSIONBRAIN_CA_PATH.get_secret_value().strip()
        self._verify_ssl = settings.FUSIONBRAIN_VERIFY_SSL
        if not base_url:
            raise ImageGenerationError("FUSIONBRAIN_BASE_URL is not set")
        if not self._pipeline_id:
            raise ImageGenerationError("FUSIONBRAIN_PIPELINE_ID is not set")
        if bool(self._cert_path) != bool(self._key_path):
            raise ImageGenerationError("FUSIONBRAIN_CERT_PATH and FUSIONBRAIN_KEY_PATH must be set together")
        # The service documents its address with the API path already on it.
        self._base_url = base_url if base_url.endswith(_API_PATH) else f"{base_url}{_API_PATH}"

    def generate(self, query: str, style: str | None) -> bytes:
        request = ImageRequest(
            pipeline_id=self._pipeline_id,
            pipeline_data=PipelineData(query=query, style=style),
        )
        deadline = time.monotonic() + _REQUEST_DEADLINE_SEC
        with self._client() as client:
            request_id = self._execute(client, request)
            file_id = self._await_file(client, request_id, deadline)
            image = self._download(client, file_id)
        # The runtime does not kill an abandoned tool thread: a late picture would still arrive.
        if time.monotonic() > deadline:
            raise ImageGenerationError(f"the image took longer than {_REQUEST_DEADLINE_SEC}s and was not delivered")
        return image

    def _execute(self, client: httpx.Client, request: ImageRequest) -> str:
        response = client.post(f"{self._base_url}/pipeline/execute", json=request.model_dump(exclude_none=True))
        self._raise_for_status(response, "execute")
        return ExecuteResponse.model_validate(self._json(response)).request_id

    def _await_file(self, client: httpx.Client, request_id: str, deadline: float) -> str:
        while True:
            response = client.post(f"{self._base_url}/pipeline/status", json={"request_id": request_id})
            self._raise_for_status(response, "status")
            status = StatusResponse.model_validate(self._json(response))
            if status.status == "ERROR":
                raise ImageGenerationError("the pipeline failed to draw the image")
            if status.status == "DONE":
                if status.result.censored:
                    raise ImageGenerationError("the prompt was rejected by the censor")
                if not status.result.result_image:
                    raise ImageGenerationError("the finished generation carries no file")
                return status.result.result_image
            if time.monotonic() + _POLL_INTERVAL_SEC > deadline:
                raise ImageGenerationError(f"the image was not ready in {_REQUEST_DEADLINE_SEC}s")
            time.sleep(_POLL_INTERVAL_SEC)

    def _download(self, client: httpx.Client, file_id: str) -> bytes:
        response = client.post(f"{self._base_url}/storage/download/{file_id}")
        self._raise_for_status(response, "download")
        if not image_signature(response.content):
            raise ImageGenerationError(f"the file carries no image: {self._bounded_body(response)}")
        return response.content

    def _client(self) -> httpx.Client:
        headers = with_agent_header(with_trace_header({"X-Request-ID": str(uuid.uuid4())}))
        return httpx.Client(
            timeout=httpx.Timeout(_REQUEST_DEADLINE_SEC, connect=_CONNECT_SEC),
            headers=headers,
            verify=self._ssl_context(),
        )

    def _ssl_context(self) -> ssl.SSLContext:
        # httpx 0.28 drops `cert=` when `verify=` is a path, so the context is built here.
        context = ssl.create_default_context(cafile=self._ca_path or None)
        if not self._verify_ssl:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        if self._cert_path:
            context.load_cert_chain(self._cert_path, self._key_path)
        return context

    def _raise_for_status(self, response: httpx.Response, step: str) -> None:
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            raise ImageGenerationError("the pipeline is busy, try again later")
        if not response.is_success:
            raise ImageGenerationError(f"{step}: HTTP {response.status_code}: {self._bounded_body(response)}")

    def _json(self, response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise ImageGenerationError(f"the response is not JSON: {self._bounded_body(response)}") from exc

    @staticmethod
    def _bounded_body(response: httpx.Response) -> str:
        body = response.text.strip() or "empty response body"
        if len(body) > _MAX_ERROR_BODY_CHARS:
            return body[:_MAX_ERROR_BODY_CHARS] + "... (truncated)"
        return body


def _image_generate_handler(ctx: ToolContext, query: str, style: str | None = None) -> str:
    prompt = (query or "").strip()
    if not prompt:
        return "⚠️ TOOL_ERROR (image_generate): query must not be empty"
    try:
        image = FusionBrainClient(load_settings()).generate(prompt, (style or "").strip() or None)
    except (ImageGenerationError, ValidationError, httpx.RequestError) as exc:
        return f"⚠️ TOOL_ERROR (image_generate): {exc}"
    return send_image_data(ctx, image, caption="")


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            name="image_generate",
            schema=ToolSchema(
                name="image_generate",
                description=(
                    "Draw one image with Kandinsky from a text description and deliver it to the user. "
                    "Drawing takes up to a few minutes. Read the image-generation skill before passing a style."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "What to draw, in plain language.",
                        },
                        "style": {
                            "type": "string",
                            "description": "Style name, e.g. picasso. Omit for no particular style.",
                        },
                    },
                    "required": ["query"],
                },
            ),
            handler=_image_generate_handler,
            timeout_sec=TIMEOUT_SEC,
        ),
    ]
