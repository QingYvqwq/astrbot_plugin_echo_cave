"""Administrative operations: maintenance, media migration and failure reports."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from astrbot.core.utils.session_waiter import session_waiter

from . import content as content_mod
from . import platform_utils as pu
from .db import (
    CaveDatabase,
    build_reindex_mapping,
    caves_are_sequential,
    parse_reindex_backup,
)
from .media import MediaManager, MutationStats
from .messages import t
from .models import CaveRecord, SendFailure
from .settings import Settings

LOG = logging.getLogger("astrbot_plugin_echo_cave")
PROGRESS_INTERVAL = 50
CONFIRM_TIMEOUT = 30


class AdminService:
    def __init__(
        self,
        db: CaveDatabase,
        media: MediaManager,
        settings: Settings,
        maintenance,
        data_dir: str | Path,
        *,
        send_private: Callable[[str, str, str], Awaitable[bool]] | None = None,
    ) -> None:
        self.db = db
        self.media = media
        self.settings = settings
        self.maintenance = maintenance
        self.backup_dir = Path(data_dir) / "backups"
        self.send_private = send_private

    # ------------------------------------------------------------------
    # Access helpers
    # ------------------------------------------------------------------
    async def _admin_access(self, event) -> bool:
        if self.maintenance.locked:
            await pu.send_text(event, t("maintenanceLocked"))
            return False
        if pu.is_group(event):
            await pu.send_text(event, t("adminPrivateOnly"))
            return False
        if pu.sender_id(event) not in self.settings.admin_ids():
            await pu.send_text(event, t("adminPermissionDenied"))
            return False
        return True

    async def _confirm(self, event, summary: str, cancelled: str) -> bool:
        await pu.send_text_with_id(event, summary)
        outcome: dict[str, bool] = {"value": False}

        @session_waiter(timeout=CONFIRM_TIMEOUT, record_history_chains=False)
        async def waiter(controller, msg_event) -> None:
            if pu.sender_id(msg_event) != pu.sender_id(event):
                return
            normalized = str(msg_event.message_str or "").strip()
            if normalized == t("confirmInput"):
                outcome["value"] = True
                controller.stop()
                return
            if normalized == t("cancelInput"):
                await pu.send_text(msg_event, cancelled)
                controller.stop()
                return
            await pu.send_text(msg_event, t("confirmRetry"))

        try:
            await waiter(event)
        except TimeoutError:
            await pu.send_text(event, t("confirmTimeout"))
            outcome["value"] = False
        except Exception as exc:
            LOG.warning("Admin confirmation failed: %s", exc)
            outcome["value"] = False
        return outcome["value"]

    @staticmethod
    def parse_boolean(value: str | None, default: bool) -> bool | None:
        if not value:
            return default
        normalized = value.strip().lower()
        if normalized in (
            "true",
            "1",
            "yes",
            "y",
            "keep",
            "保留",
            "是",
        ):
            return True
        if normalized in (
            "false",
            "0",
            "no",
            "n",
            "drop",
            "remove",
            "删除",
            "否",
        ):
            return False
        return None

    @staticmethod
    def parse_id_ranges(value: str | None) -> list[tuple[int, int]] | None:
        if not value or not value.strip():
            return []
        ranges: list[tuple[int, int]] = []
        for segment in value.split(","):
            segment = segment.strip()
            if not segment:
                continue
            if segment.isdigit():
                ranges.append((int(segment), int(segment)))
                continue
            if "-" not in segment:
                return None
            start_text, end_text = segment.split("-", 1)
            if not start_text.isdigit() or not end_text.isdigit():
                return None
            start, end = int(start_text), int(end_text)
            if start > end:
                return None
            ranges.append((start, end))
        return ranges

    @staticmethod
    def _in_ranges(public_id: int, ranges: list[tuple[int, int]]) -> bool:
        if not ranges:
            return True
        return any(start <= public_id <= end for start, end in ranges)

    @staticmethod
    def _append_failed(message: str, failed_ids: list[int]) -> str:
        if not failed_ids:
            return message
        return t(
            "failedRecordSummary",
            message=message,
            failedRecordIds=", ".join(str(value) for value in failed_ids),
        )

    async def _progress(self, event, key: str, processed: int, total: int) -> None:
        if processed and processed % PROGRESS_INTERVAL == 0:
            await pu.send_text(event, t(key, processedCount=processed, totalCount=total))

    # ------------------------------------------------------------------
    # Merge / migrations
    # ------------------------------------------------------------------
    async def merge(self, event, args: list[str]) -> None:
        if not await self._admin_access(event):
            return
        if len(args) < 2:
            await pu.send_text(event, t("noIdProvided"))
            return
        source, target = args[0], args[1]
        keep_source = self.parse_boolean(args[2] if len(args) > 2 else None, True)
        if keep_source is None:
            await pu.send_text(event, t("invalidBoolean"))
            return
        if source == target:
            await pu.send_text(event, t("mergeSameChannel"))
            return
        source_caves = await self.db.get_caves_by_channel(source)
        if not source_caves:
            await pu.send_text(event, t("mergeNoSourceCaves"))
            return
        target_count = len(await self.db.get_caves_by_channel(target))
        if not await self._confirm(
            event,
            t(
                "mergeConfirm",
                sourceChannelId=source,
                targetChannelId=target,
                sourceCount=len(source_caves),
                targetCount=target_count,
                keepSource=t("keep" if keep_source else "drop"),
            ),
            t("confirmCancelled"),
        ):
            return

        stats = MutationStats()
        cache: dict = {}
        failed: list[int] = []
        merged = 0
        processed = 0
        target_mode = "s3" if self.settings.str("mediaStorage") == "s3" else "local"
        transfer_mode = "copy" if keep_source else "move"
        for cave in source_caves:
            plans = []
            try:
                rewritten = await self.media.rewrite_content(
                    cave.content,
                    target_channel=target,
                    target_mode=target_mode,
                    transfer_mode=transfer_mode,
                    should_rewrite=lambda reference, _type: True,
                    stats=stats,
                    cache=cache,
                )
                plans = rewritten.plans
                media = content_mod.collect_media_refs(rewritten.content, self.media.legacy_root)
                if keep_source:
                    clone = CaveRecord(
                        public_id=0,
                        channel_id=target,
                        create_time=cave.create_time,
                        user_id=cave.user_id,
                        origin_user_id=cave.origin_user_id,
                        type=cave.type,
                        content=rewritten.content,
                        related_users=list(cave.related_users),
                        draw_count=cave.draw_count,
                        pic_draw_count=cave.pic_draw_count,
                        file_urls=media.file_urls,
                        image_urls=media.image_urls,
                        record_urls=media.record_urls,
                        video_urls=media.video_urls,
                        source_key=None,
                    )
                    await self.db.create_cave(clone, allow_duplicate=True)
                else:
                    await self.db.update_cave(
                        cave.entry_id,
                        channel_id=target,
                        content=rewritten.content,
                        file_urls=media.file_urls,
                        image_urls=media.image_urls,
                        record_urls=media.record_urls,
                        video_urls=media.video_urls,
                    )
                await self.media.run_plans(plans, "commit")
                merged += 1
            except Exception as exc:
                await self.media.run_plans(plans, "rollback")
                failed.append(cave.public_id)
                LOG.warning("Failed to merge cave #%s: %s", cave.public_id, exc)
            processed += 1
            await self._progress(event, "mergeProgress", processed, len(source_caves))

        message = t(
            "mergeDone",
            mergedRecords=merged,
            mediaCopied=stats.media_copied,
            mediaMoved=stats.media_moved,
            mediaUploaded=stats.media_uploaded,
            mediaFailed=stats.media_failed,
        )
        await pu.send_text(event, self._append_failed(message, failed))

    async def _migrate_content_for_cave(
        self, cave: CaveRecord, target_mode: str, transfer_mode: str, stats, cache
    ):
        rewritten = await self.media.rewrite_content(
            cave.content,
            target_channel=cave.channel_id,
            target_mode=target_mode,
            transfer_mode=transfer_mode,
            should_rewrite=lambda reference, media_type: (
                self.media.is_legacy_local_ref(reference, media_type)
                if target_mode == "local"
                else (
                    content_mod.is_file_uri(reference)
                    or content_mod.is_absolute_path(reference)
                    or self.media.is_legacy_local_ref(reference, media_type)
                )
            ),
            stats=stats,
            cache=cache,
        )
        media = content_mod.collect_media_refs(rewritten.content, self.media.legacy_root)
        changed_fields = (
            rewritten.changed
            or cave.file_urls != media.file_urls
            or cave.image_urls != media.image_urls
            or cave.record_urls != media.record_urls
            or cave.video_urls != media.video_urls
        )
        return rewritten, media, changed_fields

    async def migrate_local_v2(self, event) -> None:
        if not await self._admin_access(event):
            return
        caves = await self.db.get_all_caves()
        stats = MutationStats()
        cache: dict = {}
        failed: list[int] = []
        processed = 0
        for cave in caves:
            plans = []
            try:
                rewritten, media, changed = await self._migrate_content_for_cave(
                    cave, "local", "move", stats, cache
                )
                plans = rewritten.plans
                if changed:
                    if cave.entry_id is None:
                        raise RuntimeError(f"missing entry_id for cave {cave.public_id}")
                    await self.db.update_cave(
                        cave.entry_id,
                        content=rewritten.content,
                        file_urls=media.file_urls,
                        image_urls=media.image_urls,
                        record_urls=media.record_urls,
                        video_urls=media.video_urls,
                    )
                    await self.media.run_plans(plans, "commit")
                    stats.records_updated += 1
            except Exception as exc:
                await self.media.run_plans(plans, "rollback")
                failed.append(cave.public_id)
                LOG.warning(
                    "Failed to migrate legacy media for cave #%s: %s",
                    cave.public_id,
                    exc,
                )
            processed += 1
            await self._progress(event, "migrateLocalProgress", processed, len(caves))
        message = t(
            "migrateLocalDone",
            scannedRecords=len(caves),
            recordsUpdated=stats.records_updated,
            mediaCopied=stats.media_copied,
            mediaMoved=stats.media_moved,
            mediaFailed=stats.media_failed,
        )
        await pu.send_text(event, self._append_failed(message, failed))

    async def migrate_s3(self, event, args: list[str]) -> None:
        if not await self._admin_access(event):
            return
        if not self.settings.str("s3Bucket") or not self.settings.str("s3Region"):
            await pu.send_text(event, t("s3NotConfigured"))
            return
        keep_local = self.parse_boolean(args[0] if args else None, True)
        if keep_local is None:
            await pu.send_text(event, t("invalidBoolean"))
            return
        if not await self._confirm(
            event,
            t(
                "migrateS3Confirm",
                mediaStorage=self.settings.str("mediaStorage") or "local",
                bucket=self.settings.str("s3Bucket"),
                prefix=self.settings.str("s3PathPrefix") or t("emptyValue"),
                keepLocal=t("keep" if keep_local else "drop"),
            ),
            t("confirmCancelled"),
        ):
            return

        caves = await self.db.get_all_caves()
        stats = MutationStats()
        cache: dict = {}
        failed: list[int] = []
        processed = 0
        for cave in caves:
            plans = []
            try:
                rewritten, media, changed = await self._migrate_content_for_cave(
                    cave, "s3", "copy" if keep_local else "move", stats, cache
                )
                plans = rewritten.plans
                if changed:
                    if cave.entry_id is None:
                        raise RuntimeError(f"missing entry_id for cave {cave.public_id}")
                    await self.db.update_cave(
                        cave.entry_id,
                        content=rewritten.content,
                        file_urls=media.file_urls,
                        image_urls=media.image_urls,
                        record_urls=media.record_urls,
                        video_urls=media.video_urls,
                    )
                    await self.media.run_plans(plans, "commit")
                    stats.records_updated += 1
            except Exception as exc:
                await self.media.run_plans(plans, "rollback")
                failed.append(cave.public_id)
                LOG.warning("Failed to migrate cave #%s to S3: %s", cave.public_id, exc)
            processed += 1
            await self._progress(event, "migrateS3Progress", processed, len(caves))
        message = t(
            "migrateS3Done",
            scannedRecords=len(caves),
            recordsUpdated=stats.records_updated,
            mediaUploaded=stats.media_uploaded,
            mediaCopied=stats.media_copied,
            mediaMoved=stats.media_moved,
            mediaDeleted=stats.media_deleted,
            mediaFailed=stats.media_failed,
        )
        await pu.send_text(event, self._append_failed(message, failed))

    # ------------------------------------------------------------------
    # Inspect / backfill
    # ------------------------------------------------------------------
    async def inspect_media(self, event, args: list[str]) -> None:
        if not await self._admin_access(event):
            return
        ranges = self.parse_id_ranges(args[0] if args else None)
        if ranges is None:
            await pu.send_text(event, t("invalidRange"))
            return
        display = (args[0] if args else "") or t("allRanges")
        hits: list[tuple[int, list[str]]] = []
        failed: list[int] = []
        scanned = 0
        for cave in await self.db.get_all_caves():
            if not self._in_ranges(cave.public_id, ranges):
                continue
            scanned += 1
            try:
                refs = content_mod.collect_media_refs(cave.content, self.media.legacy_root)
                values = refs.file_urls + refs.image_urls + refs.record_urls + refs.video_urls
                if values:
                    hits.append((cave.public_id, values))
            except Exception as exc:
                failed.append(cave.public_id)
                LOG.warning(
                    "Failed to inspect media refs for cave #%s: %s",
                    cave.public_id,
                    exc,
                )
        if not hits and not failed:
            await pu.send_text(event, t("noMediaFound", idRanges=display))
            return
        if not await self._confirm(
            event,
            t("inspectConfirm", idRanges=display, matchedCount=len(hits)),
            t("inspectCancelled"),
        ):
            return

        processed = 0
        batch: list[str] = []
        for public_id, refs in hits:
            batch.append(t("inspectResultItem", id=public_id, refs="\n".join(refs)))
            processed += 1
            if len(batch) >= PROGRESS_INTERVAL:
                await pu.send_text(event, "\n\n".join(batch))
                batch.clear()
                await pu.send_text(
                    event,
                    t(
                        "inspectProgress",
                        processedCount=processed,
                        totalCount=len(hits),
                    ),
                )
        if batch:
            await pu.send_text(event, "\n\n".join(batch))
        message = t("inspectDone", scannedRecords=scanned, matchedCount=len(hits))
        await pu.send_text(event, self._append_failed(message, failed))

    async def backfill_media_urls(self, event, args: list[str]) -> None:
        if not await self._admin_access(event):
            return
        ranges = self.parse_id_ranges(args[0] if args else None)
        if ranges is None:
            await pu.send_text(event, t("invalidRange"))
            return
        display = (args[0] if args else "") or t("allRanges")
        candidates = [
            cave
            for cave in await self.db.get_all_caves()
            if self._in_ranges(cave.public_id, ranges)
            and not cave.file_urls
            and not cave.image_urls
            and not cave.record_urls
            and not cave.video_urls
        ]
        if not candidates:
            await pu.send_text(event, t("backfillNoCandidates", idRanges=display))
            return
        if not await self._confirm(
            event,
            t("backfillConfirm", idRanges=display, candidateCount=len(candidates)),
            t("backfillCancelled"),
        ):
            return

        updated = 0
        skipped = 0
        failed: list[int] = []
        for index, cave in enumerate(candidates, start=1):
            try:
                media = content_mod.collect_media_refs(cave.content, self.media.legacy_root)
                if media.is_empty():
                    skipped += 1
                else:
                    if cave.entry_id is None:
                        raise RuntimeError(f"missing entry_id for cave {cave.public_id}")
                    await self.db.update_cave(
                        cave.entry_id,
                        file_urls=media.file_urls,
                        image_urls=media.image_urls,
                        record_urls=media.record_urls,
                        video_urls=media.video_urls,
                    )
                    updated += 1
            except Exception as exc:
                failed.append(cave.public_id)
                LOG.warning(
                    "Failed to backfill media URLs for cave #%s: %s",
                    cave.public_id,
                    exc,
                )
            await self._progress(event, "backfillProgress", index, len(candidates))
        message = t(
            "backfillDone",
            scannedRecords=len(candidates),
            updatedCount=updated,
            skippedWithoutMedia=skipped,
        )
        await pu.send_text(event, self._append_failed(message, failed))

    # ------------------------------------------------------------------
    # Reindex / restore
    # ------------------------------------------------------------------
    async def reindex(self, event, *, scheduled: bool = False) -> str | None:
        if self.maintenance.locked:
            if scheduled:
                LOG.warning(
                    "Skipped scheduled cave reindex because another maintenance task is running."
                )
            else:
                await pu.send_text(event, t("maintenanceLocked"))
            return "locked"
        if not scheduled:
            if pu.is_group(event):
                await pu.send_text(event, t("adminPrivateOnly"))
                return "private_only"
            if pu.sender_id(event) not in self.settings.admin_ids():
                await pu.send_text(event, t("adminPermissionDenied"))
                return "permission_denied"

        caves = sorted(await self.db.get_all_caves(), key=lambda item: item.public_id)
        if not caves:
            if scheduled:
                LOG.info("Skipped scheduled cave reindex: no records.")
            else:
                await pu.send_text(event, t("reindexNoCaves"))
            return "no_caves"
        if caves_are_sequential(caves):
            if scheduled:
                LOG.info("Skipped scheduled cave reindex: IDs are already sequential.")
            else:
                await pu.send_text(event, t("reindexAlreadySequential"))
            return "sequential"

        mapping = build_reindex_mapping(caves)
        try:
            backup_path = await self.db.write_reindex_backup(self.backup_dir, caves, mapping)
        except Exception as exc:
            if not scheduled:
                await pu.send_text(event, t("reindexBackupFailed", error=str(exc)))
            LOG.error("Failed to write reindex backup: %s", exc)
            return "backup_failed"

        if not scheduled:
            if not await self._confirm(
                event,
                t(
                    "reindexConfirm",
                    caveCount=len(caves),
                    currentMaxId=caves[-1].public_id,
                    nextMaxId=len(caves),
                    backupPath=str(backup_path),
                ),
                t("reindexCancelled"),
            ):
                return "cancelled"

        self.maintenance.locked = True
        try:
            if not scheduled:
                for index, _cave in enumerate(caves, start=1):
                    await self._progress(event, "reindexProgress", index, len(caves))
            await self.db.apply_reindex(caves)
            if scheduled:
                LOG.info(
                    "Scheduled cave reindex completed. Processed %s records. Backup: %s",
                    len(caves),
                    backup_path,
                )
                return str(backup_path)
            await pu.send_text(
                event,
                t(
                    "reindexDone",
                    caveCount=len(caves),
                    backupPath=str(backup_path),
                ),
            )
            return str(backup_path)
        except Exception as exc:
            LOG.error("Failed to reindex cave ids: %s", exc)
            if not scheduled:
                await pu.send_text(
                    event,
                    t(
                        "reindexFailed",
                        backupPath=str(backup_path),
                        error=str(exc),
                    ),
                )
            return "failed"
        finally:
            self.maintenance.locked = False

    async def restore_reindex(self, event, args: list[str]) -> None:
        if not await self._admin_access(event):
            return
        if not args or not args[0].strip():
            await pu.send_text(event, t("restoreMissingPath"))
            return
        raw_path = Path(args[0].strip())
        backup_path = raw_path if raw_path.is_absolute() else Path.cwd() / raw_path
        try:
            content = await asyncio.to_thread(backup_path.read_text, "utf-8")
            created_at, records, _mapping = parse_reindex_backup(content)
        except Exception as exc:
            LOG.error("Failed to read cave reindex backup: %s", exc)
            await pu.send_text(
                event,
                t(
                    "restoreReadFailed",
                    backupPath=str(backup_path),
                    error=str(exc),
                ),
            )
            return

        current = await self.db.get_all_caves()
        if not await self._confirm(
            event,
            t(
                "restoreConfirm",
                backupPath=str(backup_path),
                backupCreatedAt=created_at,
                backupCount=len(records),
                currentCount=len(current),
            ),
            t("restoreCancelled"),
        ):
            return

        self.maintenance.locked = True
        try:
            for index, _record in enumerate(records, start=1):
                await self._progress(event, "restoreProgress", index, len(records))
            await self.db.replace_with_snapshot(records)
            await pu.send_text(
                event,
                t(
                    "restoreDone",
                    caveCount=len(records),
                    backupPath=str(backup_path),
                ),
            )
        except Exception as exc:
            LOG.error("Failed to restore cave backup: %s", exc)
            await pu.send_text(
                event,
                t(
                    "restoreFailed",
                    backupPath=str(backup_path),
                    error=str(exc),
                ),
            )
        finally:
            self.maintenance.locked = False

    # ------------------------------------------------------------------
    # Daily failure report
    # ------------------------------------------------------------------
    @staticmethod
    def _format_datetime(value: datetime) -> str:
        return value.astimezone().strftime("%Y/%m/%d %H:%M:%S")

    def _build_summary_messages(
        self, entries: list[SendFailure], max_length: int = 1500
    ) -> list[str]:
        ordered = sorted(entries, key=lambda item: item.fail_time)
        summary: dict[tuple[str, int], dict] = {}
        for entry in ordered:
            key = (entry.channel_id, entry.cave_id)
            item = summary.get(key)
            if item is None:
                summary[key] = {
                    "caveId": entry.cave_id,
                    "channelId": entry.channel_id,
                    "count": 1,
                    "lastFailTime": entry.fail_time,
                    "errorMessage": entry.error_message,
                }
            else:
                item["count"] += 1
                item["lastFailTime"] = entry.fail_time
                item["errorMessage"] = entry.error_message

        details = sorted(
            summary.values(),
            key=lambda item: (item["count"], item["lastFailTime"]),
            reverse=True,
        )
        header = [
            t("failureSummaryTitle"),
            t(
                "failureSummaryRange",
                start=self._format_datetime(ordered[0].fail_time),
                end=self._format_datetime(ordered[-1].fail_time),
            ),
            t("failureSummaryCount", count=len(ordered)),
            t("failureSummaryCaveCount", count=len(details)),
            "",
        ]
        detail_lines = [
            t(
                "failureSummaryDetail",
                caveId=item["caveId"],
                channelId=item["channelId"],
                count=item["count"],
                lastFailTime=self._format_datetime(item["lastFailTime"]),
                errorMessage=item["errorMessage"],
            )
            for item in details
        ]
        messages: list[str] = []
        current = "\n".join(header)
        for line in detail_lines:
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) <= max_length:
                current = candidate
            else:
                messages.append(current)
                current = f"{t('failureSummaryContinuationTitle')}\n{line}"
        if current:
            messages.append(current)
        return messages

    async def flush_send_failure_summary(self) -> int:
        admin_id = self.settings.str("sendFailureSummaryAdminId").strip()
        if not admin_id:
            return 0
        failures = await self.db.list_send_failures()
        if not failures:
            return 0
        grouped: dict[str, list[SendFailure]] = {}
        for failure in failures:
            grouped.setdefault(failure.platform_id, []).append(failure)

        sent = 0
        for platform_id, entries in grouped.items():
            if self.send_private is None:
                continue
            messages = self._build_summary_messages(entries)
            delivered = True
            for message in messages:
                try:
                    ok = await self.send_private(platform_id, admin_id, message)
                except Exception as exc:
                    LOG.error(
                        "Failed to deliver cave failure summary via %s: %s",
                        platform_id,
                        exc,
                    )
                    ok = False
                if not ok:
                    delivered = False
                    break
            if delivered:
                await self.db.delete_send_failures([entry.id for entry in entries])
                sent += len(entries)
        return sent
