"""Vision LLM tools for uploaded images."""

from __future__ import annotations

import logging
import pathlib
import tempfile
from typing import Any

from new_core.tools.context import ToolContext
from new_core.tools.entry import ToolEntry
from new_core.tools.schema import ToolSchema

log = logging.getLogger(__name__)

_DEFAULT_VLM_MODEL = "anthropic/claude-sonnet-4.6"


def _get_vlm_model() -> str:
    """Return the dedicated vision model, falling back to the main model or default.

    Prefers OUROBOROS_MODEL_VISION so the explicit vision tool never assumes the
    main model is multimodal (it may be text-only, e.g. GLM).
    """
    from ouroboros.storage.settings.store import load_settings

    settings = load_settings()
    return (
        str(settings.OUROBOROS_MODEL_VISION or "").strip()
        or str(settings.OUROBOROS_MODEL or "").strip()
        or _DEFAULT_VLM_MODEL
    )


def _get_llm_client():
    """Lazy-import LLMClient to avoid circular imports."""
    from ouroboros.llm import LLMClient

    return LLMClient()


_IMAGE_MAGIC: list[tuple[bytes, str]] = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
]
_IMAGE_WEBP_MAGIC = (b"RIFF", b"WEBP")
_VLM_MAX_FILE_BYTES = 20 * 1024 * 1024


def _path_is_under(path: pathlib.Path, root: pathlib.Path) -> bool:
    """Return True if a resolved path is root itself or a descendant."""
    try:
        path.relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _detect_image_mime_for_vlm(raw: bytes) -> str:
    """Return MIME type string or empty string if not a recognised image."""
    for magic, mime in _IMAGE_MAGIC:
        if raw[: len(magic)] == magic:
            return mime
    if raw[:4] == _IMAGE_WEBP_MAGIC[0] and raw[8:12] == _IMAGE_WEBP_MAGIC[1]:
        return "image/webp"
    return ""


def _allowed_file_roots() -> list[pathlib.Path]:
    """Return uploads roots allowed for VLM file_path reads."""
    from ouroboros.app.config import DATA_DIR

    if DATA_DIR:
        return [pathlib.Path(DATA_DIR).expanduser().resolve() / "uploads"]
    return [pathlib.Path("~/Ouroboros/data/uploads").expanduser().resolve()]


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

    images: list[dict[str, Any]] = []
    if attachment_id:
        import base64

        from ouroboros.storage.blob.factory import create_storage_provider
        from ouroboros.storage.blob.protocol import ObjectTooLargeError
        from ouroboros.storage.blob.scopes import SCOPE_UPLOADS
        from ouroboros.storage.chat_attachments import MAX_IMAGE_BYTES, get_attachment

        if not any(str(value.get("id") or "") == attachment_id for value in ctx.attachments):
            return "⚠️ Attachment is not available in the current task."
        provider = create_storage_provider(data_dir=ctx.drive_root)
        metadata = get_attachment(provider, attachment_id)
        if metadata is None:
            return "⚠️ Attachment metadata was not found."
        try:
            with tempfile.TemporaryDirectory(prefix="ouroboros-image-") as temp_dir:
                path = pathlib.Path(temp_dir) / "image"
                provider.copy_to_path(
                    SCOPE_UPLOADS,
                    str(metadata.get("storage_key") or ""),
                    path,
                    max_bytes=MAX_IMAGE_BYTES,
                )
                attachment_bytes = path.read_bytes()
        except FileNotFoundError:
            return "⚠️ Attachment content was not found."
        except ObjectTooLargeError:
            return f"⚠️ File too large. Max {MAX_IMAGE_BYTES} bytes."
        mime = _detect_image_mime_for_vlm(attachment_bytes)
        if not mime:
            return "⚠️ Attachment is not a supported PNG, JPEG, GIF, or WEBP image."
        images.append({"base64": base64.b64encode(attachment_bytes).decode(), "mime": mime})
    elif file_path:
        import base64

        from ouroboros.app.config import DATA_DIR
        from ouroboros.storage.blob.drive import get_drive_storage
        from ouroboros.utils import drive_aware_exists, read_bytes

        fp = pathlib.Path(file_path).expanduser()
        resolved = fp.resolve()
        allowed = _allowed_file_roots()
        if not any(_path_is_under(resolved, root) for root in allowed):
            return (
                f"⚠️ file_path must be inside the uploads directory (data/uploads/). "
                f"Resolved path: {resolved}. Use send_photo or read_file for other paths."
            )

        drive_root = pathlib.Path(getattr(ctx, "drive_root", None) or DATA_DIR)
        drive = get_drive_storage(drive_root)
        rel = drive.rel_from_path(resolved)
        raw: bytes | None = None
        if rel and rel.startswith("uploads/") and drive_aware_exists(drive_root / rel):
            try:
                raw = read_bytes(drive_root / rel)
                log.info(
                    "storage_op operation=read_upload rel=%s backend=%s size=%d",
                    rel,
                    drive.backend_for_rel(rel),
                    len(raw),
                )
            except FileNotFoundError:
                raw = None
            except Exception as e:
                return f"⚠️ Failed to read image file: {e}"
        if raw is None:
            if not resolved.is_file():
                return f"⚠️ File not found: {file_path}"
            try:
                raw = resolved.read_bytes()
            except Exception as e:
                return f"⚠️ Failed to read image file: {e}"
        if len(raw) > _VLM_MAX_FILE_BYTES:
            return f"⚠️ File too large ({len(raw)} bytes). Max {_VLM_MAX_FILE_BYTES} bytes."
        # Fail closed: only recognized image bytes may reach the VLM.
        mime = _detect_image_mime_for_vlm(raw)
        if not mime:
            return (
                "⚠️ File does not appear to be a supported image (PNG/JPEG/GIF/WEBP). "
                "Only image files may be sent to the VLM via file_path."
            )
        images.append({"base64": base64.b64encode(raw).decode(), "mime": mime})
    elif image_url:
        images.append({"url": image_url})
    else:
        images.append({"base64": image_base64, "mime": image_mime})

    vlm_model = model or _get_vlm_model()

    try:
        client = _get_llm_client()
        text, usage = client.vision_query(
            prompt=prompt,
            images=images,
            model=vlm_model,
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
