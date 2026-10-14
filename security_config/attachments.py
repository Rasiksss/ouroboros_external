from dataclasses import dataclass

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


@dataclass(frozen=True)
class AttachmentFormat:
    mime: str
    aliases: frozenset[str]


ATTACHMENT_FORMATS = {
    ".pdf": AttachmentFormat("application/pdf", frozenset({"application/pdf"})),
    ".xlsx": AttachmentFormat(XLSX_MIME, frozenset({XLSX_MIME})),
    ".docx": AttachmentFormat(DOCX_MIME, frozenset({DOCX_MIME})),
    ".pptx": AttachmentFormat(PPTX_MIME, frozenset({PPTX_MIME})),
    ".txt": AttachmentFormat("text/plain", frozenset({"text/plain"})),
    ".md": AttachmentFormat("text/markdown", frozenset({"text/markdown", "text/plain"})),
    ".csv": AttachmentFormat("text/csv", frozenset({"text/csv", "text/plain"})),
    ".json": AttachmentFormat("application/json", frozenset({"application/json"})),
    ".png": AttachmentFormat("image/png", frozenset({"image/png"})),
    ".jpg": AttachmentFormat("image/jpeg", frozenset({"image/jpeg"})),
    ".jpeg": AttachmentFormat("image/jpeg", frozenset({"image/jpeg"})),
    ".webp": AttachmentFormat("image/webp", frozenset({"image/webp"})),
    ".gif": AttachmentFormat("image/gif", frozenset({"image/gif"})),
}

IMAGE_SUFFIXES = {
    rule.mime: suffix for suffix, rule in reversed(ATTACHMENT_FORMATS.items()) if rule.mime.startswith("image/")
}
SAFE_INLINE_IMAGE_MIMES = frozenset(IMAGE_SUFFIXES)
