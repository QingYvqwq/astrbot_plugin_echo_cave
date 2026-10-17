"""Media storage, delivery and migration for the echo cave plugin."""

from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import shutil
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from . import content as content_mod
from .content import (
    MEDIA_TYPES,
    file_uri_to_path,
    is_absolute_path,
    is_base64_uri,
    is_data_uri,
    is_file_uri,
    is_http_url,
    is_s3_uri,
    parse_content,
    parse_s3_uri,
    path_to_file_uri,
    stable_transfer_name,
)
from .settings import Settings

LOG = logging.getLogger("astrbot_plugin_echo_cave")
DEFAULT_EXTENSIONS = {"image": "png", "video": "mp4", "record": "mp3", "file": "bin"}
MB = 1024 * 1024


class MediaError(RuntimeError):
    pass


@dataclass
class TransferPlan:
    next_ref: str
    commit: Callable[[], Awaitable[None]] | None = None
    rollback: Callable[[], Awaitable[None]] | None = None


@dataclass
class MutationStats:
    records_updated: int = 0
    media_copied: int = 0
    media_moved: int = 0
    media_uploaded: int = 0
    media_deleted: int = 0
    media_skipped: int = 0
    media_failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "recordsUpdated": self.records_updated,
            "mediaCopied": self.media_copied,
            "mediaMoved": self.media_moved,
            "mediaUploaded": self.media_uploaded,
            "mediaDeleted": self.media_deleted,
            "mediaSkipped": self.media_skipped,
            "mediaFailed": self.media_failed,
        }


@dataclass
class RewriteResult:
    content: str
    changed: bool
    plans: list[TransferPlan] = field(default_factory=list)


class MediaManager:
    def __init__(
        self,
        data_dir: str | Path,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
        s3_client_factory: Callable[[], Any] | None = None,
        cleanup_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.settings = settings
        self.cleanup_callback = cleanup_callback
        self.media_root = self.data_dir / "media"
        self.v2_root = self.media_root / "v2"
        self.legacy_root = self.media_root / "legacy"
        self._http = http_client
        self._owns_http = http_client is None
        self._s3_factory = s3_client_factory
        self._s3_clients: dict[tuple[Any, ...], Any] = {}
        self._base64_cache: OrderedDict[str, str] = OrderedDict()
        self._base64_cache_size = 200
        self._cleanup_tasks: dict[str, asyncio.Task] = {}
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # Paths / references
    # ------------------------------------------------------------------
    @staticmethod
    def encode_channel(channel_id: str) -> str:
        encoded = quote(str(channel_id), safe="")
        if encoded in ("", ".", ".."):
            encoded = encoded.replace(".", "%2E") or "_"
        return encoded

    def channel_dir(self, channel_id: str, media_type: str) -> Path:
        return self.v2_root / self.encode_channel(channel_id) / f"{media_type}s"

    def legacy_dir(self, media_type: str) -> Path:
        return self.legacy_root / f"{media_type}s"

    def is_managed_path(self, path: str | Path) -> bool:
        try:
            resolved = Path(path).resolve()
            root = self.media_root.resolve()
            return resolved == root or root in resolved.parents
        except (OSError, RuntimeError):
            return False

    def is_v2_path(self, path: str | Path) -> bool:
        try:
            resolved = Path(path).resolve()
            root = self.v2_root.resolve()
            return resolved == root or root in resolved.parents
        except (OSError, RuntimeError):
            return False

    def ref_to_path(self, reference: str) -> Path | None:
        if is_file_uri(reference):
            return Path(file_uri_to_path(reference))
        if is_absolute_path(reference):
            return Path(reference)
        return None

    def _file_extension(self, reference: str, media_type: str) -> str:
        source_name = ""
        if is_file_uri(reference):
            source_name = Path(file_uri_to_path(reference)).name
        elif is_absolute_path(reference):
            source_name = Path(reference).name
        elif is_s3_uri(reference):
            location = parse_s3_uri(reference)
            source_name = Path(location[1]).name if location else ""
        elif is_http_url(reference):
            source_name = Path(urlparse(reference).path).name
        suffix = Path(source_name).suffix.lstrip(".").lower()
        return suffix or DEFAULT_EXTENSIONS[media_type]

    @staticmethod
    def _mime_for(extension: str, media_type: str) -> str:
        mime, _ = mimetypes.guess_type(f"file.{extension}")
        if mime:
            return mime
        return {
            "image": "image/jpeg",
            "video": "video/mp4",
            "record": "audio/mpeg",
            "file": "application/octet-stream",
        }[media_type]

    def _content_type_allowed(self, media_type: str, content_type: str | None) -> bool:
        if not content_type:
            return media_type in ("file", "video")
        lowered = content_type.split(";", 1)[0].strip().lower()
        if media_type == "image":
            return lowered.startswith("image/")
        if media_type == "record":
            return lowered.startswith("audio/")
        if media_type == "video":
            return lowered.startswith("video/") or lowered == "application/octet-stream"
        return True

    # ------------------------------------------------------------------
    # HTTP / S3 primitives
    # ------------------------------------------------------------------
    async def _http_client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        return self._http

    async def _s3_client(self) -> Any:
        if self._s3_factory is not None:
            return self._s3_factory()
        bucket = self.settings.str("s3Bucket")
        region = self.settings.str("s3Region")
        if not bucket or not region:
            raise MediaError("S3 is not configured (bucket/region missing)")
        cache_key = (
            bucket,
            region,
            self.settings.str("s3Endpoint"),
            self.settings.str("s3AccessKeyId"),
            self.settings.str("s3SecretAccessKey"),
            self.settings.bool("s3ForcePathStyle"),
        )
        cached = self._s3_clients.get(cache_key)
        if cached is not None:
            return cached
        try:
            import boto3
            from botocore.config import Config as BotoConfig
        except ImportError as exc:  # pragma: no cover - dependency check
            raise MediaError("boto3 is not installed") from exc
        boto_config = (
            BotoConfig(s3={"addressing_style": "path"})
            if self.settings.bool("s3ForcePathStyle")
            else BotoConfig()
        )
        kwargs: dict[str, Any] = {"region_name": region, "config": boto_config}
        endpoint = self.settings.str("s3Endpoint")
        if endpoint:
            kwargs["endpoint_url"] = endpoint
        access_key = self.settings.str("s3AccessKeyId")
        secret_key = self.settings.str("s3SecretAccessKey")
        if access_key and secret_key:
            kwargs["aws_access_key_id"] = access_key
            kwargs["aws_secret_access_key"] = secret_key
        client = boto3.client("s3", **kwargs)
        self._s3_clients[cache_key] = client
        return client

    def _s3_key(self, channel_id: str, media_type: str, filename: str) -> str:
        parts = [
            self.settings.str("s3PathPrefix").strip().strip("/"),
            "v2",
            self.encode_channel(channel_id),
            f"{media_type}s",
            filename,
        ]
        return "/".join(part for part in parts if part)

    @staticmethod
    def _strip_scheme(reference: str) -> tuple[str, str]:
        location = parse_s3_uri(reference)
        if not location:
            raise MediaError(f"invalid S3 URI: {reference}")
        return location

    async def _s3_put(
        self, data: bytes, key: str, content_type: str | None, bucket: str | None = None
    ) -> None:
        client = await self._s3_client()
        bucket_name = bucket or self.settings.str("s3Bucket")
        kwargs: dict[str, Any] = {"Bucket": bucket_name, "Key": key, "Body": data}
        if content_type:
            kwargs["ContentType"] = content_type
        await asyncio.to_thread(client.put_object, **kwargs)

    async def _s3_get(self, reference: str) -> tuple[bytes, str | None]:
        bucket, key = self._strip_scheme(reference)
        client = await self._s3_client()
        response = await asyncio.to_thread(client.get_object, Bucket=bucket, Key=key)
        body = response.get("Body")
        if body is None:
            return b"", response.get("ContentType")
        if hasattr(body, "read"):
            data = await asyncio.to_thread(body.read)
        else:
            data = bytes(body)
        return data, response.get("ContentType")

    async def _s3_delete(self, reference: str) -> None:
        bucket, key = self._strip_scheme(reference)
        client = await self._s3_client()
        await asyncio.to_thread(client.delete_object, Bucket=bucket, Key=key)

    async def _s3_copy(self, source: str, bucket: str, key: str) -> None:
        source_bucket, source_key = self._strip_scheme(source)
        client = await self._s3_client()
        copy_source = f"{source_bucket}/{quote(source_key, safe='/')}"
        await asyncio.to_thread(
            client.copy_object,
            Bucket=bucket,
            Key=key,
            CopySource=copy_source,
        )

    async def presign(self, reference: str) -> str:
        bucket, key = self._strip_scheme(reference)
        client = await self._s3_client()
        expires = max(60, min(604800, self.settings.int("s3PresignExpiresIn")))
        return await asyncio.to_thread(
            client.generate_presigned_url,
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires,
        )

    # ------------------------------------------------------------------
    # Reading / saving
    # ------------------------------------------------------------------
    async def _read_reference(self, reference: str) -> tuple[bytes, str | None]:
        if is_http_url(reference):
            client = await self._http_client()
            response = await client.get(reference)
            if response.status_code < 200 or response.status_code >= 300:
                raise MediaError(f"download failed: HTTP {response.status_code}")
            content_type = response.headers.get("content-type")
            return response.content, content_type
        if is_data_uri(reference):
            header, _, payload = reference.partition(",")
            content_type = header[5:].split(";", 1)[0] or None
            try:
                data = base64.b64decode(payload, validate=False)
            except (ValueError, TypeError) as exc:
                raise MediaError(f"invalid data URI: {exc}") from exc
            return data, content_type
        if is_base64_uri(reference):
            try:
                return base64.b64decode(reference[len("base64://") :], validate=False), None
            except (ValueError, TypeError) as exc:
                raise MediaError(f"invalid base64 URI: {exc}") from exc
        if is_s3_uri(reference):
            return await self._s3_get(reference)
        path = self.ref_to_path(reference)
        if path is not None:
            if not path.is_file():
                raise MediaError(f"local media missing: {path}")
            data = await asyncio.to_thread(path.read_bytes)
            mime, _ = mimetypes.guess_type(path.name)
            return data, mime
        raise MediaError(f"unsupported media reference: {reference}")

    async def _write_local(
        self, data: bytes, channel_id: str, media_type: str, extension: str
    ) -> str:
        directory = self.channel_dir(channel_id, media_type)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{uuid.uuid4().hex}.{extension}"
        await asyncio.to_thread(path.write_bytes, data)
        return path_to_file_uri(path)

    async def save_media(
        self, data: dict[str, Any], media_type: str, channel_id: str
    ) -> str | None:
        """Persist one media segment and return its new reference.

        The existing reference is returned unchanged when saving fails, which
        preserves the message even if the media cannot be archived.
        """
        url_value = data.get("url") if isinstance(data.get("url"), str) else ""
        file_value = data.get("file") if isinstance(data.get("file"), str) else ""
        reference = url_value or file_value
        if not reference:
            return None
        if not (
            is_s3_uri(reference)
            or is_file_uri(reference)
            or is_http_url(reference)
            or is_data_uri(reference)
            or is_base64_uri(reference)
            or is_absolute_path(reference)
        ):
            legacy_candidate = self.legacy_dir(media_type) / reference
            if legacy_candidate.is_file():
                reference = str(legacy_candidate)
            else:
                return None

        if is_s3_uri(reference) and self.settings.str("mediaStorage") != "s3":
            return reference
        if is_file_uri(reference):
            path = Path(file_uri_to_path(reference))
            if self.is_v2_path(path):
                return reference
        if is_absolute_path(reference):
            path = Path(reference)
            if self.is_v2_path(path):
                return reference

        extension = self._file_extension(file_value or reference, media_type)

        try:
            payload, header_type = await self._read_reference(reference)
            if not payload:
                LOG.warning("Downloaded %s buffer is empty: %s", media_type, reference)
                return reference
            if is_http_url(reference) and not self._content_type_allowed(media_type, header_type):
                LOG.warning(
                    "Invalid %s content-type %s for %s",
                    media_type,
                    header_type,
                    reference,
                )
                return reference

            if self.settings.str("mediaStorage") == "s3":
                try:
                    filename = f"{uuid.uuid4().hex}.{extension}"
                    key = self._s3_key(channel_id, media_type, filename)
                    await self._s3_put(
                        payload,
                        key,
                        header_type or self._mime_for(extension, media_type),
                    )
                    return f"s3://{self.settings.str('s3Bucket')}/{key}"
                except Exception as exc:
                    fallback = self.settings.str("s3UploadFailureFallbackMode")
                    LOG.warning(
                        "S3 upload failed for %s (fallback=%s): %s",
                        reference,
                        fallback,
                        exc,
                    )
                    if fallback == "original-link":
                        return reference

            saved = await self._write_local(payload, channel_id, media_type, extension)
            if self.settings.bool("enableSizeLimit"):
                self._schedule_cleanup(channel_id, media_type)
            return saved
        except Exception as exc:
            LOG.error("Failed to save %s %s: %s", media_type, reference, exc)
            return reference

    async def process_content(self, content: str, channel_id: str) -> str:
        """Download/archive all media in a content document."""
        segments = parse_content(content)

        async def walk(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            result: list[dict[str, Any]] = []
            for segment in items:
                segment_type = segment.get("type")
                if segment_type in MEDIA_TYPES:
                    data = dict(segment.get("data") or {})
                    saved = await self.save_media(data, segment_type, channel_id)
                    if saved:
                        data["file"] = saved
                        data.pop("url", None)
                    result.append({**segment, "data": data})
                elif segment_type == "node":
                    data = dict(segment.get("data") or {})
                    children = data.get("content")
                    if isinstance(children, list):
                        data["content"] = await walk(children)
                    result.append({**segment, "data": data})
                else:
                    result.append(segment)
            return result

        return content_mod.dump_content(await walk(segments))

    async def resolve_for_send(self, reference: str, media_type: str) -> dict[str, Any]:
        """Resolve a stored reference into component constructor arguments."""
        if is_s3_uri(reference):
            try:
                return {"kind": "url", "value": await self.presign(reference)}
            except Exception as exc:
                LOG.error("Failed to generate presigned URL for %s: %s", reference, exc)
                return {"kind": "path", "value": reference}
        if is_base64_uri(reference):
            return {"kind": "base64", "value": reference[len("base64://") :]}
        if is_http_url(reference) or is_data_uri(reference):
            return {"kind": "path", "value": reference}
        path = self.ref_to_path(reference)
        if path is None:
            return {"kind": "path", "value": reference}
        if self.settings.bool("useBase64ForMedia") and media_type != "file":
            return await self._as_base64(reference, path, media_type)
        return {"kind": "path", "value": str(path)}

    async def _as_base64(self, reference: str, path: Path, media_type: str) -> dict[str, Any]:
        cached = self._base64_cache.get(reference)
        if cached:
            self._base64_cache.move_to_end(reference)
            return {"kind": "base64", "value": cached}
        try:
            payload = await asyncio.to_thread(path.read_bytes)
            raw = base64.b64encode(payload).decode("ascii")
        except OSError as exc:
            LOG.error("Failed to read media for base64 %s: %s", path, exc)
            return {"kind": "path", "value": str(path)}
        self._base64_cache[reference] = raw
        self._base64_cache.move_to_end(reference)
        while len(self._base64_cache) > self._base64_cache_size:
            self._base64_cache.popitem(last=False)
        return {"kind": "base64", "value": raw}

    async def delete_reference(self, reference: str) -> None:
        """Delete a managed media object; external paths are never touched."""
        if is_s3_uri(reference):
            try:
                await self._s3_delete(reference)
            except Exception as exc:
                LOG.warning("Failed to delete S3 media %s: %s", reference, exc)
            return
        path = self.ref_to_path(reference)
        if path is None:
            return
        if not self.is_managed_path(path):
            LOG.warning("Refusing to delete media outside plugin data dir: %s", path)
            return
        try:
            await asyncio.to_thread(path.unlink)
        except FileNotFoundError:
            pass
        except OSError as exc:
            LOG.warning("Failed to delete media %s: %s", path, exc)
        self._base64_cache.pop(reference, None)
        self._base64_cache.pop(path_to_file_uri(path), None)

    async def delete_content_media(self, content: str) -> None:
        refs = content_mod.collect_media_refs(content, self.legacy_root)
        for reference in refs.file_urls + refs.image_urls + refs.record_urls + refs.video_urls:
            await self.delete_reference(reference)

    # ------------------------------------------------------------------
    # Size limit / cleanup
    # ------------------------------------------------------------------
    def _schedule_cleanup(self, channel_id: str, media_type: str) -> None:
        key = f"{channel_id}:{media_type}"
        previous = self._cleanup_tasks.pop(key, None)
        if previous and not previous.done():
            previous.cancel()

        async def later() -> None:
            try:
                await asyncio.sleep(5)
                if self.cleanup_callback is not None:
                    await self.cleanup_callback(media_type)
                else:
                    await self.files_over_limit(media_type)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                LOG.warning("Media cleanup failed for %s: %s", media_type, exc)
            finally:
                self._cleanup_tasks.pop(key, None)

        task = asyncio.create_task(later())
        self._cleanup_tasks[key] = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _max_size_bytes(self, media_type: str) -> int:
        key = {
            "image": "maxImageSize",
            "video": "maxVideoSize",
            "file": "maxFileSize",
            "record": "maxRecordSize",
        }[media_type]
        value = self.settings.int(key)
        if value <= 0:
            # Treat 0/unset as the documented fallback, not as "delete everything".
            value = {"image": 100, "video": 500, "file": 1000, "record": 200}[media_type]
        return value * MB

    def _collect_type_files(self, media_type: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for root in (self.v2_root, self.legacy_root):
            if not root.exists():
                continue
            for directory in root.rglob(f"{media_type}s"):
                if not directory.is_dir():
                    continue
                for path in directory.iterdir():
                    try:
                        if not path.is_file():
                            continue
                        stat = path.stat()
                    except OSError:
                        continue
                    results.append({"path": path, "size": stat.st_size, "mtime": stat.st_mtime})
        return results

    async def files_over_limit(self, media_type: str) -> list[Path]:
        if self.settings.str("mediaStorage") != "local" or not self.settings.bool(
            "enableSizeLimit"
        ):
            return []
        limit = self._max_size_bytes(media_type)
        files = await asyncio.to_thread(self._collect_type_files, media_type)
        total = sum(file["size"] for file in files)
        if total <= limit:
            return []
        files.sort(key=lambda item: item["mtime"])
        selected: list[Path] = []
        for file in files:
            if total <= limit:
                break
            selected.append(file["path"])
            total -= file["size"]
        return selected

    async def delete_paths(self, paths: list[Path]) -> None:
        for path in paths:
            try:
                await asyncio.to_thread(path.unlink)
            except FileNotFoundError:
                continue
            except OSError as exc:
                LOG.warning("Failed to delete %s: %s", path, exc)
                continue
            LOG.info("Deleted old media: %s", path.name)

    # ------------------------------------------------------------------
    # Migration primitives
    # ------------------------------------------------------------------
    async def _load_ref(self, reference: str) -> tuple[bytes, str | None]:
        return await self._read_reference(reference)

    async def _write_transfer_local(
        self, reference: str, channel_id: str, media_type: str, buffer: bytes
    ) -> str:
        directory = self.channel_dir(channel_id, media_type)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / stable_transfer_name(reference, media_type)
        await asyncio.to_thread(target.write_bytes, buffer)
        return path_to_file_uri(target)

    async def _upload_transfer_s3(
        self,
        reference: str,
        channel_id: str,
        media_type: str,
        buffer: bytes,
        content_type: str | None,
    ) -> str:
        filename = stable_transfer_name(reference, media_type)
        key = self._s3_key(channel_id, media_type, filename)
        await self._s3_put(
            buffer, key, content_type or self._mime_for(filename.rsplit(".", 1)[-1], media_type)
        )
        return f"s3://{self.settings.str('s3Bucket')}/{key}"

    async def transfer_reference(
        self,
        reference: str,
        media_type: str,
        target_channel: str,
        target_mode: str,
        transfer_mode: str,
        stats: MutationStats,
        cache: dict[str, TransferPlan],
    ) -> TransferPlan:
        cache_key = f"{reference}|{target_channel}|{target_mode}|{transfer_mode}"
        if cache_key in cache:
            return cache[cache_key]

        if target_mode == "local":
            if is_file_uri(reference) or is_absolute_path(reference):
                source_path = self.ref_to_path(reference)
                if source_path is None or not source_path.is_file():
                    raise MediaError(f"local source missing: {reference}")
                target_dir = self.channel_dir(target_channel, media_type)
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / stable_transfer_name(reference, media_type)
                source_resolved = str(source_path.resolve()).lower()
                target_resolved = str(target.resolve()).lower()
                if source_resolved == target_resolved:
                    plan = TransferPlan(next_ref=reference)
                    cache[cache_key] = plan
                    return plan
                await asyncio.to_thread(shutil.copyfile, source_path, target)
                next_ref = path_to_file_uri(target)

                async def rollback(target=target) -> None:
                    try:
                        await asyncio.to_thread(target.unlink)
                    except OSError:
                        pass

                plan = TransferPlan(next_ref=next_ref, rollback=rollback)
                if transfer_mode == "move":

                    async def commit(source_path=source_path) -> None:
                        try:
                            await asyncio.to_thread(source_path.unlink)
                        except OSError:
                            pass

                    plan.commit = commit
                    stats.media_moved += 1
                else:
                    stats.media_copied += 1
                cache[cache_key] = plan
                return plan

            buffer, content_type = await self._load_ref(reference)
            next_ref = await self._write_transfer_local(
                reference, target_channel, media_type, buffer
            )
            target = self.ref_to_path(next_ref)

            async def rollback(target=target) -> None:
                if target is not None:
                    try:
                        await asyncio.to_thread(target.unlink)
                    except OSError:
                        pass

            plan = TransferPlan(next_ref=next_ref, rollback=rollback)
            if transfer_mode == "move" and is_s3_uri(reference):

                async def commit(reference=reference) -> None:
                    try:
                        await self._s3_delete(reference)
                    except Exception:
                        pass

                plan.commit = commit
                stats.media_deleted += 1
                stats.media_moved += 1
            else:
                stats.media_copied += 1
            cache[cache_key] = plan
            return plan

        # target S3
        try:
            if is_s3_uri(reference):
                source_bucket = self.settings.str("s3Bucket")
                filename = stable_transfer_name(reference, media_type)
                key = self._s3_key(target_channel, media_type, filename)
                await self._s3_copy(reference, source_bucket, key)
                next_ref = f"s3://{source_bucket}/{key}"

                async def rollback(next_ref=next_ref) -> None:
                    try:
                        await self._s3_delete(next_ref)
                    except Exception:
                        pass

                plan = TransferPlan(next_ref=next_ref, rollback=rollback)
                if transfer_mode == "move":

                    async def commit(reference=reference) -> None:
                        try:
                            await self._s3_delete(reference)
                        except Exception:
                            pass

                    plan.commit = commit
                    stats.media_moved += 1
                    stats.media_deleted += 1
                else:
                    stats.media_copied += 1
                cache[cache_key] = plan
                return plan

            buffer, content_type = await self._load_ref(reference)
            next_ref = await self._upload_transfer_s3(
                reference, target_channel, media_type, buffer, content_type
            )

            async def rollback(next_ref=next_ref) -> None:
                try:
                    await self._s3_delete(next_ref)
                except Exception:
                    pass

            plan = TransferPlan(next_ref=next_ref, rollback=rollback)
            if transfer_mode == "move":
                source_path = self.ref_to_path(reference)

                async def commit(source_path=source_path) -> None:
                    if source_path is not None:
                        try:
                            await asyncio.to_thread(source_path.unlink)
                        except OSError:
                            pass

                plan.commit = commit
                stats.media_deleted += 1
                stats.media_moved += 1
            stats.media_uploaded += 1
            cache[cache_key] = plan
            return plan
        except Exception as exc:
            fallback = self.settings.str("s3UploadFailureFallbackMode")
            LOG.warning(
                "Failed to migrate media %s to S3 (fallback=%s): %s",
                reference,
                fallback,
                exc,
            )
            if fallback == "original-link":
                plan = TransferPlan(next_ref=reference)
                cache[cache_key] = plan
                return plan
            plan = await self.transfer_reference(
                reference,
                media_type,
                target_channel,
                "local",
                transfer_mode,
                stats,
                cache,
            )
            cache[cache_key] = plan
            return plan

    async def rewrite_content(
        self,
        content: str,
        *,
        target_channel: str,
        target_mode: str,
        transfer_mode: str,
        should_rewrite: Callable[[str, str], bool],
        stats: MutationStats,
        cache: dict[str, TransferPlan] | None = None,
    ) -> RewriteResult:
        plans_cache = cache if cache is not None else {}
        plans: list[TransferPlan] = []
        changed = False
        segments = parse_content(content)

        async def walk(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            nonlocal changed
            result: list[dict[str, Any]] = []
            for segment in items:
                segment_type = segment.get("type")
                if segment_type in MEDIA_TYPES:
                    current = content_mod.media_ref_from_element(segment, self.legacy_root)
                    if not current:
                        stats.media_skipped += 1
                        result.append(segment)
                        continue
                    if not should_rewrite(str(current), segment_type):
                        stats.media_skipped += 1
                        result.append(segment)
                        continue
                    try:
                        plan = await self.transfer_reference(
                            str(current),
                            segment_type,
                            target_channel,
                            target_mode,
                            transfer_mode,
                            stats,
                            plans_cache,
                        )
                        if plan not in plans:
                            plans.append(plan)
                        if plan.next_ref == current:
                            stats.media_skipped += 1
                            result.append(segment)
                        else:
                            result.append(content_mod.set_media_ref(segment, plan.next_ref))
                            changed = True
                    except Exception as exc:
                        stats.media_failed += 1
                        LOG.warning("Failed to migrate media %s: %s", current, exc)
                        result.append(segment)
                elif segment_type == "node":
                    data = dict(segment.get("data") or {})
                    children = data.get("content")
                    if isinstance(children, list):
                        data["content"] = await walk(children)
                    result.append({**segment, "data": data})
                else:
                    result.append(segment)
            return result

        rewritten = await walk(segments)
        if changed:
            stats.records_updated += 1
        return RewriteResult(content_mod.dump_content(rewritten), changed, plans)

    @staticmethod
    async def run_plans(plans: list[TransferPlan], phase: str) -> None:
        for plan in plans:
            action = plan.commit if phase == "commit" else plan.rollback
            if action:
                try:
                    await action()
                except Exception as exc:
                    LOG.warning("Media transfer %s action failed: %s", phase, exc)

    def is_legacy_local_ref(self, reference: str, media_type: str) -> bool:
        path = self.ref_to_path(reference)
        if path is None:
            return False
        try:
            legacy = self.legacy_dir(media_type).resolve()
            resolved = path.resolve()
            return legacy == resolved or legacy in resolved.parents
        except (OSError, RuntimeError):
            return False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def close(self) -> None:
        for task in list(self._cleanup_tasks.values()):
            task.cancel()
        self._cleanup_tasks.clear()
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None
