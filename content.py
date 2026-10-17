"""Helpers for the CQ-style JSON document stored in the content column.

Messages are stored as an array of segments such as
``{"type": "text", "data": {"text": "hi"}}``.  We deliberately keep that
format so existing databases and reindex backups remain importable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

from .models import MediaRefs

MEDIA_TYPES = ("image", "video", "file", "record")
MEDIA_FIELD_BY_TYPE = {
    "image": "image_urls",
    "video": "video_urls",
    "file": "file_urls",
    "record": "record_urls",
}
DEFAULT_EXTENSION = {
    "image": "png",
    "video": "mp4",
    "record": "mp3",
    "file": "bin",
}

_WINDOWS_ABS = re.compile(r"^[A-Za-z]:[\\/]")


class ContentError(ValueError):
    """Raised when a stored content document is malformed."""


def parse_content(content: str | list[Any] | dict[str, Any] | None) -> list[dict[str, Any]]:
    """Parse a content document into a list of segment dictionaries.

    Args:
        content: JSON text or an already decoded value.

    Returns:
        A list of segment dictionaries.

    Raises:
        ContentError: If the document is not a JSON array/object.
    """
    if content is None:
        raise ContentError("empty content")
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ContentError(f"invalid content JSON: {exc}") from exc
    else:
        parsed = content
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        raise ContentError("content must be a JSON array or object")
    if not all(isinstance(item, dict) and isinstance(item.get("type"), str) for item in parsed):
        raise ContentError("content contains an invalid segment")
    return parsed


def dump_content(segments: list[dict[str, Any]]) -> str:
    return json.dumps(segments, ensure_ascii=False, separators=(",", ":"))


def iter_segments(
    segments: list[dict[str, Any]] | dict[str, Any], *, include_nodes: bool = True
) -> Iterator[dict[str, Any]]:
    """Yield every segment, descending into ``node`` content arrays."""
    if isinstance(segments, dict):
        segments = [segments]
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        yield segment
        if not include_nodes:
            continue
        if segment.get("type") != "node":
            continue
        node_data = segment.get("data")
        if not isinstance(node_data, dict):
            continue
        children = node_data.get("content")
        if isinstance(children, str):
            try:
                children = json.loads(children)
            except json.JSONDecodeError:
                continue
        if isinstance(children, list):
            yield from iter_segments(children, include_nodes=True)


def is_media_type(value: str) -> bool:
    return value in MEDIA_TYPES


def is_absolute_path(value: str) -> bool:
    return bool(value) and (os.path.isabs(value) or bool(_WINDOWS_ABS.match(value)))


def is_http_url(value: str) -> bool:
    return bool(re.match(r"^https?://", value, re.IGNORECASE))


def is_file_uri(value: str) -> bool:
    return value.startswith("file:///")


def is_s3_uri(value: str) -> bool:
    return value.startswith("s3://")


def is_data_uri(value: str) -> bool:
    return value.startswith("data:")


def is_base64_uri(value: str) -> bool:
    return value.startswith("base64://")


def file_uri_to_path(value: str) -> str:
    from urllib.parse import unquote

    return unquote(value[len("file:///") :])


def path_to_file_uri(value: str | Path) -> str:
    return "file:///" + str(value).replace("\\", "/")


def parse_s3_uri(value: str) -> tuple[str, str] | None:
    if not is_s3_uri(value):
        return None
    remainder = value[len("s3://") :]
    slash = remainder.find("/")
    if slash < 0:
        return None
    return remainder[:slash], remainder[slash + 1 :]


def media_ref_from_element(
    element: dict[str, Any], legacy_media_root: str | Path | None = None
) -> str | None:
    """Extract the stored media reference from a segment.

    Preference order: ``data.file`` then ``data.url``.
    Bare filenames are resolved against the legacy media directory.
    """
    segment_type = element.get("type")
    if segment_type not in MEDIA_TYPES:
        return None
    data = element.get("data")
    if not isinstance(data, dict):
        return None
    file_value = data.get("file")
    url_value = data.get("url")
    # Prefer the concrete file reference over the remote URL.
    for candidate in (file_value, url_value):
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        value = candidate.strip()
        if (
            is_file_uri(value)
            or is_s3_uri(value)
            or is_http_url(value)
            or is_data_uri(value)
            or is_base64_uri(value)
            or is_absolute_path(value)
        ):
            return value
    if isinstance(file_value, str) and file_value.strip() and legacy_media_root:
        folder = Path(legacy_media_root) / f"{segment_type}s"
        return str(folder / file_value.strip())
    return None


def set_media_ref(element: dict[str, Any], reference: str) -> dict[str, Any]:
    data = dict(element.get("data") or {})
    data["file"] = reference
    data.pop("url", None)
    return {**element, "data": data}


def collect_media_refs(
    content: str | list[dict[str, Any]], legacy_media_root: str | Path | None = None
) -> MediaRefs:
    """Collect unique media references grouped by segment type."""
    segments = parse_content(content)
    refs = MediaRefs()
    for segment in iter_segments(segments):
        reference = media_ref_from_element(segment, legacy_media_root)
        if not reference:
            continue
        field = MEDIA_FIELD_BY_TYPE[segment["type"]]
        getattr(refs, field).append(reference)
    return refs.unique()


def message_contains_media(content: str | list[dict[str, Any]] | None) -> bool:
    try:
        segments = parse_content(content)
    except ContentError:
        return False
    return any(segment.get("type") in MEDIA_TYPES for segment in iter_segments(segments))


def is_picture_message(cave_type: str, content: str) -> bool:
    """Return True for normal (non-forward) records containing an image."""
    if cave_type != "msg":
        return False
    try:
        segments = parse_content(content)
    except ContentError:
        return False
    return any(segment.get("type") == "image" for segment in iter_segments(segments))


def first_and_last_are_text(content: str | list[dict[str, Any]]) -> tuple[bool, bool]:
    segments = parse_content(content)
    if not segments:
        return False, False
    return segments[0].get("type") == "text", segments[-1].get("type") == "text"


async def rewrite_media_refs(
    content: str,
    handler: Callable[[str], Awaitable[str | None]],
) -> tuple[str, bool]:
    """Recursively rewrite media references.

    Args:
        content: Stored content JSON.
        handler: Async callback receiving the current reference.  Returning a
            string replaces it; returning None keeps the current value.

    Returns:
        ``(new_content, changed)``.
    """
    segments = parse_content(content)
    changed = False

    async def rewrite_list(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        nonlocal changed
        result: list[dict[str, Any]] = []
        for segment in items:
            if segment.get("type") in MEDIA_TYPES:
                current = media_ref_from_element(segment)
                if current:
                    replacement = await handler(current)
                    if replacement and replacement != current:
                        segment = set_media_ref(segment, replacement)
                        changed = True
            elif segment.get("type") == "node":
                data = dict(segment.get("data") or {})
                children = data.get("content")
                if isinstance(children, str):
                    try:
                        children = json.loads(children)
                    except json.JSONDecodeError:
                        children = None
                if isinstance(children, list):
                    new_children = await rewrite_list(children)
                    if new_children is not children:
                        data["content"] = new_children
                        segment = {**segment, "data": data}
            result.append(segment)
        return result

    rewritten = await rewrite_list(segments)
    return dump_content(rewritten), changed


def content_fingerprint(
    cave_type: str, origin_user_id: str, content: str, origin_message_id: str | None = None
) -> str:
    """Build the deduplication key for a submission.

    The source message ID is preferred; a canonical content hash is used when
    the platform does not expose one.
    """
    if origin_message_id:
        return f"msg:{origin_message_id}"
    digest = hashlib.sha256(f"{cave_type}\x00{origin_user_id}\x00{content}".encode()).hexdigest()
    return f"sha256:{digest}"


def flatten_text(content: str | list[dict[str, Any]]) -> str:
    """Best-effort plain text rendering, used for non-OneBot forward fallback."""
    try:
        segments = parse_content(content)
    except ContentError:
        return ""
    parts: list[str] = []
    for segment in iter_segments(segments):
        segment_type = segment.get("type")
        data = segment.get("data") or {}
        if segment_type == "text":
            parts.append(str(data.get("text", "")))
        elif segment_type == "at":
            parts.append(f"@{data.get('qq', '')}")
        elif segment_type == "image":
            parts.append("[图片]")
        elif segment_type == "record":
            parts.append("[语音]")
        elif segment_type == "video":
            parts.append("[视频]")
        elif segment_type == "file":
            parts.append(f"[文件:{data.get('name', '')}]")
        elif segment_type in ("face",):
            parts.append("[表情]")
    return "".join(parts)


def plain_text_of_node(node: dict[str, Any]) -> str:
    data = node.get("data") or {}
    nickname = data.get("nickname") or data.get("user_id") or ""
    body = flatten_text(data.get("content") or [])
    return f"{nickname}: {body}"


def sanitize_filename(value: str, *, fallback: str = "media") -> str:
    name = Path(str(value).replace("\\", "/")).name
    name = re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-.")
    return name or fallback


def stable_transfer_name(reference: str, segment_type: str) -> str:
    """Deterministic target filename used by media migrations."""
    if is_file_uri(reference):
        source = Path(file_uri_to_path(reference))
    elif is_absolute_path(reference):
        source = Path(reference)
    elif is_s3_uri(reference):
        location = parse_s3_uri(reference)
        source = Path(location[1]) if location else Path("")
    elif is_http_url(reference):
        try:
            source = Path(reference.split("?", 1)[0])
        except ValueError:
            source = Path("")
    else:
        source = Path("")

    base = sanitize_filename(source.name, fallback="media")
    stem = Path(base).stem or "media"
    suffix = Path(base).suffix.lstrip(".") or DEFAULT_EXTENSION[segment_type]
    identity = normalize_source_identity(reference)
    digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12]
    return f"{stem}-{digest}.{suffix}"


def normalize_source_identity(reference: str) -> str:
    if is_file_uri(reference):
        return os.path.normcase(os.path.abspath(file_uri_to_path(reference))).replace("\\", "/")
    if is_absolute_path(reference):
        return os.path.normcase(os.path.abspath(reference)).replace("\\", "/")
    if is_s3_uri(reference):
        return reference.lower()
    return reference
