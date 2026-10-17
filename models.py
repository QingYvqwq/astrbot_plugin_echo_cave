"""Data models shared by the storage, service and migration layers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

MEDIA_FIELDS = ("file_urls", "image_urls", "record_urls", "video_urls")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_datetime(value: Any) -> datetime:
    """Parse a datetime from the formats produced by this plugin or legacy data.

    Args:
        value: datetime, ISO-8601 string, epoch milliseconds or epoch seconds.

    Returns:
        A timezone-aware UTC datetime.

    Raises:
        ValueError: If the value cannot be interpreted as a datetime.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        # Legacy SQLite data may store timestamps as epoch milliseconds.
        seconds = float(value) / 1000.0 if abs(float(value)) > 10_000_000_000 else float(value)
        dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    elif isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return parse_datetime(int(text))
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    else:
        raise ValueError(f"unsupported datetime value: {value!r}")

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def json_list(value: Any) -> list[str]:
    """Normalize a list-ish database value to a list of strings.

    Args:
        value: list, JSON string, comma-separated string or None.

    Returns:
        A list containing only string values.
    """
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = [part.strip() for part in text.split(",") if part.strip()]
        value = parsed
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item) for item in value if item is not None and str(item) != ""]


@dataclass
class MediaRefs:
    file_urls: list[str] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)
    record_urls: list[str] = field(default_factory=list)
    video_urls: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any(getattr(self, name) for name in MEDIA_FIELDS)

    def unique(self) -> MediaRefs:
        return MediaRefs(
            file_urls=list(dict.fromkeys(self.file_urls)),
            image_urls=list(dict.fromkeys(self.image_urls)),
            record_urls=list(dict.fromkeys(self.record_urls)),
            video_urls=list(dict.fromkeys(self.video_urls)),
        )


@dataclass
class CaveRecord:
    public_id: int
    channel_id: str
    create_time: datetime
    user_id: str
    origin_user_id: str
    type: str
    content: str
    related_users: list[str] = field(default_factory=list)
    draw_count: int = 0
    pic_draw_count: int = 0
    file_urls: list[str] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)
    record_urls: list[str] = field(default_factory=list)
    video_urls: list[str] = field(default_factory=list)
    source_key: str | None = None
    entry_id: int | None = None

    def media(self) -> MediaRefs:
        return MediaRefs(
            file_urls=list(self.file_urls),
            image_urls=list(self.image_urls),
            record_urls=list(self.record_urls),
            video_urls=list(self.video_urls),
        )

    def apply_media(self, media: MediaRefs) -> None:
        media = media.unique()
        self.file_urls = media.file_urls
        self.image_urls = media.image_urls
        self.record_urls = media.record_urls
        self.video_urls = media.video_urls

    def normalized(self) -> dict[str, Any]:
        """Return a stable representation used for snapshot verification."""
        return {
            "id": self.public_id,
            "channelId": self.channel_id,
            "createTime": self.create_time.astimezone(timezone.utc).isoformat(),
            "userId": self.user_id,
            "originUserId": self.origin_user_id,
            "type": self.type,
            "content": self.content,
            "relatedUsers": list(self.related_users),
            "drawCount": self.draw_count,
            "picDrawCount": self.pic_draw_count,
            "fileUrls": list(self.file_urls),
            "imageUrls": list(self.image_urls),
            "recordUrls": list(self.record_urls),
            "videoUrls": list(self.video_urls),
        }

    def to_backup(self) -> dict[str, Any]:
        data = self.normalized()
        data["entryId"] = self.entry_id
        return data


@dataclass
class SendFailure:
    id: int
    cave_id: int
    channel_id: str
    guild_id: str
    platform_id: str
    self_id: str
    fail_time: datetime
    error_message: str
