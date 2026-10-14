"""Vision LLM tools for uploaded images."""

from __future__ import annotations

import base64
import logging
import pathlib

from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.schema import ToolSchema
from ouroboros.llm.client import LLMClient, VisionImage
from ouroboros.storage.attachment_validation import image_signature
from ouroboros.storage.blob.drive import get_drive_storage
from ouroboros.storage.blob.factory import get_storage_provider
from ouroboros.storage.blob.protocol import ObjectTooLargeError
from ouroboros.storage.chat_attachments import read_attachment_bytes
from ouroboros.storage.settings import store as settings_store
from ouroboros.storage.settings.paths import load_path_settings
from ouroboros.tools.attachments import resolve_reachable_attachment
from ouroboros.tools.service.artifacts import resolve_artifact

log = logging.getLogger(__name__)

_DEFAULT_VLM_MODEL = "anthropic/claude-sonnet-4.6"


def _path_is_under(path: pathlib.Path, root: pathlib.Path) -> bool:
    """Return True if a resolved path is root itself or a descendant."""
    try:
        path.relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _allowed_file_roots() -> list[pathlib.Path]:
    """Return uploads roots allowed for VLM file_path reads."""
    return [load_path_settings().data_dir.expanduser().resolve() / "uploads"]


def _vlm_query(
    ctx: ToolContext,
    prompt: str,
    attachment_id: str = "",
    image_url: str = "",
    image_base64: str = "",
    image_mime: str = "image/png",
    file_path: str = "",
    model: str = "",
) -> str:
    """Analyze one image from uploads file_path, public URL, or base64."""
    if not attachment_id and not image_url and not image_base64 and not file_path:
        return "⚠️ Provide one of: attachment_id, file_path, image_url, or image_base64."

    settings = settings_store.load_settings()
    image_limit = settings.OUROBOROS_ATTACHMENT_IMAGE_MAX_BYTES
    images: list[VisionImage] = []
    if attachment_id:
        provider = get_storage_provider()
        metadata = resolve_reachable_attachment(ctx, attachment_id)
        if metadata is None:
            return "⚠️ Attachment metadata was not found."
        try:
            attachment_bytes = read_attachment_bytes(
                ctx.scope, provider, str(metadata.get("storage_key") or ""), mime="image/png"
            )
        except ObjectTooLargeError:
            return f"⚠️ File too large. Max {image_limit} bytes."
        if attachment_bytes is None:
            return "⚠️ Attachment content was not found."
        mime = image_signature(attachment_bytes)
        if not mime:
            return "⚠️ Attachment is not a supported PNG, JPEG, GIF, or WEBP image."
        images.append({"base64": base64.b64encode(attachment_bytes).decode(), "mime": mime})
    elif file_path:
        fp = pathlib.Path(file_path).expanduser()
        resolved = fp.resolve()
        allowed = _allowed_file_roots()
        if not any(_path_is_under(resolved, root) for root in allowed):
            return (
                f"⚠️ file_path must be inside the uploads directory (data/uploads/). "
                f"Resolved path: {resolved}. Use send_photo or read_file for other paths."
            )

        drive_root = pathlib.Path(ctx.drive_root)
        drive = get_drive_storage(drive_root)
        rel = drive.rel_from_path(resolved)
        metadata = resolve_artifact(ctx, rel) if rel else None
        if metadata is None:
            return "⚠️ File not found."
        try:
            raw = read_attachment_bytes(ctx.scope, drive.provider, metadata["storage_key"], mime="image/png")
        except ObjectTooLargeError:
            return f"⚠️ File too large. Max {image_limit} bytes."
        if raw is None:
            # Raw paths can point at another owner's bytes; only the scoped provider may read them.
            return f"⚠️ File not found: {file_path}"
        # Fail closed: only recognized image bytes may reach the VLM.
        mime = image_signature(raw)
        if not mime:
            return (
                "⚠️ File does not appear to be a supported image (PNG/JPEG/GIF/WEBP). "
                "Only image files may be sent to the VLM via file_path."
            )
        images.append({"base64": base64.b64encode(raw).decode(), "mime": mime})
    elif image_url:
        if not image_url.lower().startswith(("http://", "https://")):
            return "⚠️ image_url must be an HTTP(S) URL. Use image_base64 for inline images."
        images.append({"url": image_url})
    else:
        if len(image_base64) > ((image_limit + 2) // 3) * 4:
            return f"⚠️ Image too large. Max {image_limit} bytes."
        try:
            image_size = len(base64.b64decode(image_base64, validate=True))
        except ValueError:
            return "⚠️ Image base64 is invalid."
        if image_size > image_limit:
            return f"⚠️ Image too large. Max {image_limit} bytes."
        images.append({"base64": image_base64, "mime": image_mime})

    vlm_model = (
        model or settings.OUROBOROS_MODEL_VISION.strip() or settings.OUROBOROS_MODEL.strip() or _DEFAULT_VLM_MODEL
    )

    try:
        client = LLMClient()
        text, usage = client.vision_query(
            prompt=prompt,
            images=images,
            model=vlm_model,
            stop_requested=ctx.stop_requested,
        )

        # A model call outside the main loop, so its tokens have to be reported here or
        # they go uncounted. The channel decides where they land.
        ctx.events.usage(vlm_model, usage, source="vlm_query")

        return text or "(no response from VLM)"
    except Exception as e:
        log.warning("vlm_query failed: %s", e, exc_info=True)
        return f"⚠️ VLM query failed: {e}"


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry(
            name="vlm_query",
            schema=ToolSchema(
                name="vlm_query",
                description=(
                    "Analyze any image using a Vision LLM. "
                    "Provide one of: attachment_id (preferred for chat attachments), "
                    "file_path (local file, preferred for generated artifacts), "
                    "image_url (public URL), or image_base64 (base64-encoded PNG/JPEG). "
                    "Use file_path for files already on disk (e.g. data/uploads/ attachments). "
                    "Use for: analyzing charts, reading diagrams, and understanding screenshots or photos."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "attachment_id": {
                            "type": "string",
                            "description": "Image attachment id from the current user message",
                        },
                        "prompt": {
                            "type": "string",
                            "description": "What to analyze or describe about the image",
                        },
                        "file_path": {
                            "type": "string",
                            "description": "Local file path to image (preferred — reads from disk, avoids base64 in arguments). Must be inside data/uploads/ directory.",
                        },
                        "image_url": {
                            "type": "string",
                            "description": "Public URL of the image to analyze",
                        },
                        "image_base64": {
                            "type": "string",
                            "description": "Base64-encoded image data",
                        },
                        "image_mime": {
                            "type": "string",
                            "description": "MIME type for base64 image (default: image/png)",
                        },
                        "model": {
                            "type": "string",
                            "description": "VLM model to use (default: OUROBOROS_MODEL_VISION, falling back to OUROBOROS_MODEL)",
                        },
                    },
                    "required": ["prompt"],
                },
            ),
            handler=_vlm_query,
            timeout_sec=90,
        ),
    ]
