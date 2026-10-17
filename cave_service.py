"""Core business logic for storing, drawing and managing echo cave records."""

from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import datetime, timedelta, timezone

import astrbot.api.message_components as Comp
from astrbot.core.utils.session_waiter import SessionFilter, session_waiter

from . import content as content_mod
from . import platform_utils as pu
from . import templates
from .db import CaveDatabase, DuplicateCaveError
from .media import MediaManager
from .messages import t
from .models import CaveRecord
from .settings import Settings

LOG = logging.getLogger("astrbot_plugin_echo_cave")

PREDEFINED_PERIODS = ("lday", "lweek", "lmonth", "day", "week", "month", "all")
TIME_UNITS = {
    "m": 30 * 24 * 60 * 60 * 1000,
    "w": 7 * 24 * 60 * 60 * 1000,
    "d": 24 * 60 * 60 * 1000,
    "h": 60 * 60 * 1000,
}
SPECIAL_RECALL_DELAY = 60.0
POKE_COOLDOWN_SECONDS = 5.0


class MaintenanceLock:
    def __init__(self) -> None:
        self.locked = False


class UserSessionFilter(SessionFilter):
    """Scope a waiter to one sender so other members keep normal processing."""

    def filter(self, event) -> str:
        return f"{event.unified_msg_origin}:{event.get_sender_id()}"


class CaveService:
    def __init__(
        self,
        db: CaveDatabase,
        media: MediaManager,
        settings: Settings,
        maintenance: MaintenanceLock,
        *,
        rng: random.Random | None = None,
        task_set: set[asyncio.Task] | None = None,
    ) -> None:
        self.db = db
        self.media = media
        self.settings = settings
        self.maintenance = maintenance
        self.rng = rng or random.Random()
        self.tasks = task_set if task_set is not None else set()

    # ------------------------------------------------------------------
    # Common validation
    # ------------------------------------------------------------------
    async def _group_access(self, event) -> bool:
        if not pu.is_group(event):
            await pu.send_text(event, t("privateChatReminder"))
            return False
        if self.settings.bool("enableChannelWhitelist"):
            allowed = self.settings.str_list("channelWhitelist")
            if pu.group_id(event) not in allowed:
                await pu.send_text(event, t("whitelistDenied"))
                return False
        if self.maintenance.locked:
            await pu.send_text(event, t("maintenanceLocked"))
            return False
        return True

    def _is_admin_id(self, user_id: str) -> bool:
        return str(user_id) in self.settings.all_admin_ids()

    def _actor_is_admin(self, event) -> bool:
        try:
            if event.is_admin():
                return True
        except Exception:
            pass
        return self._is_admin_id(pu.sender_id(event))

    # ------------------------------------------------------------------
    # submission
    # ------------------------------------------------------------------
    async def add_cave(self, event, args: list[str]) -> None:
        if not await self._group_access(event):
            return
        reply = pu.get_reply(event)
        if reply is None:
            await pu.send_text(event, t("noMsgQuoted"))
            return

        quoted = await pu.get_quoted_segments(event, reply)
        if not quoted:
            await pu.send_text(event, t("noMsgQuoted"))
            return

        explicit_ids, error = pu.parse_user_ids(event, args)
        if error == "invalid_all_mention":
            await pu.send_text(event, t("invalidAllMention"))
            return
        if explicit_ids and not await pu.check_users_in_group(event, explicit_ids):
            await pu.send_text(event, t("userNotInGroup"))
            return

        cave_type = "msg"
        forward_users: list[dict[str, str]] = []
        content_segments = quoted

        if quoted and quoted[0].get("type") == "forward":
            cave_type = "forward"
            expanded = await pu.expand_forward_segments(event, quoted)
            content_segments = expanded
            forward_users = self._collect_forward_users(expanded)
            if pu.contains_special_user(expanded):
                if not await self._handle_special_forward(event, expanded):
                    return
        else:
            first_type = quoted[0].get("type") if quoted else ""
            if first_type in ("video", "file", "record") or any(
                segment.get("type") == "reply" for segment in quoted
            ):
                cave_type = "forward"

        content = content_mod.dump_content(content_segments)

        selected_users: list[dict[str, str]] = []
        if cave_type == "forward" and forward_users and not explicit_ids:
            if self.settings.bool("autoBindSingleForwardUser") and len(forward_users) == 1:
                selected_users = list(forward_users)
            elif self.settings.bool("enableForwardUserSelection"):
                selection, completed = await self._select_related_users(event, forward_users)
                selected_users = selection
                if completed:
                    await self.db.set_forward_selection_done(pu.sender_id(event))

        content = await self._process_media_with_progress(event, content)
        related_users = (
            [user["user_id"] for user in selected_users] if selected_users else explicit_ids
        )

        origin_user_id = str(getattr(reply, "sender_id", "") or "")
        origin_name = await pu.get_user_name(event, origin_user_id)
        media_refs = content_mod.collect_media_refs(content, self.media.legacy_root)

        source_key = None
        if self.settings.bool("enableDuplicateDetection"):
            source_key = content_mod.content_fingerprint(
                cave_type,
                origin_user_id,
                content,
                origin_message_id=str(getattr(reply, "id", "") or ""),
            )

        record = CaveRecord(
            public_id=0,
            channel_id=pu.group_id(event),
            create_time=datetime.now(timezone.utc),
            user_id=pu.sender_id(event),
            origin_user_id=origin_user_id,
            type=cave_type,
            content=content,
            related_users=related_users,
            draw_count=0,
            pic_draw_count=0,
            file_urls=media_refs.file_urls,
            image_urls=media_refs.image_urls,
            record_urls=media_refs.record_urls,
            video_urls=media_refs.video_urls,
            source_key=source_key,
        )
        try:
            saved = await self.db.create_cave(record)
        except DuplicateCaveError as exc:
            await pu.send_text(event, t("duplicate", id=exc.public_id))
            return
        except Exception as exc:
            LOG.error("Failed to save cave: %s", exc)
            await pu.send_text(event, t("msgFailedToSave"))
            return

        if selected_users:
            related_names = ", ".join(user["nickname"] for user in selected_users)
        elif related_users:
            names = [await pu.get_user_name(event, uid) for uid in related_users]
            related_names = ", ".join(names)
        else:
            related_names = origin_name
        LOG.info(
            "Stored cave #%s in %s by %s (related users: %s)",
            saved.public_id,
            saved.channel_id,
            saved.user_id,
            related_names,
        )
        await pu.send_text(event, t("msgSaved", id=saved.public_id))

    @staticmethod
    def _collect_forward_users(nodes: list[dict]) -> list[dict[str, str]]:
        seen: dict[str, str] = {}
        for node in nodes:
            if node.get("type") != "node":
                continue
            data = node.get("data") or {}
            user_id = str(data.get("user_id") or "")
            nickname = str(data.get("nickname") or "")
            if user_id and nickname and user_id not in seen:
                seen[user_id] = nickname
        return [{"user_id": user_id, "nickname": nickname} for user_id, nickname in seen.items()]

    async def enforce_media_limit(self, media_type: str) -> None:
        """Delete oldest media (and optionally caves) when a type exceeds its cap."""
        paths = await self.media.files_over_limit(media_type)
        if not paths:
            return
        if self.settings.str("oversizedMediaCleanupMode") == "delete-cave":
            from .models import MEDIA_FIELDS

            targets = {self._normalized_path(path) for path in paths}
            for cave in await self.db.get_all_caves():
                refs = content_mod.collect_media_refs(cave.content, self.media.legacy_root)
                matched = False
                for field in MEDIA_FIELDS:
                    for reference in getattr(refs, field):
                        path = self.media.ref_to_path(reference)
                        if path is not None and self._normalized_path(path) in targets:
                            matched = True
                            break
                    if matched:
                        break
                if matched:
                    try:
                        await self._delete_stored_cave(cave)
                        LOG.info(
                            "Deleted cave #%s because its media exceeded the size limit.",
                            cave.public_id,
                        )
                    except Exception as exc:
                        LOG.warning(
                            "Failed to delete cave #%s during media cleanup: %s",
                            cave.public_id,
                            exc,
                        )
        await self.media.delete_paths(paths)

    @staticmethod
    def _normalized_path(path) -> str:
        try:
            return str(path.resolve()).replace("\\", "/").lower()
        except (OSError, RuntimeError):
            return str(path).replace("\\", "/").lower()

    async def _process_media_with_progress(self, event, content: str) -> str:
        show_progress = self.settings.str(
            "mediaStorage"
        ) == "s3" and content_mod.message_contains_media(content)
        progress_id = None
        if show_progress:
            progress_id = await pu.send_text_with_id(event, t("mediaSaving"))
        try:
            return await self.media.process_content(content, pu.group_id(event))
        finally:
            if progress_id:
                await pu.delete_messages(event, [progress_id])

    async def _select_related_users(
        self, event, forward_users: list[dict[str, str]]
    ) -> tuple[list[dict[str, str]], bool]:
        user_id = pu.sender_id(event)
        channel = pu.group_id(event)
        has_completed = await self.db.get_forward_selection_done(user_id)
        header = t("selectRelatedUsersSimple" if has_completed else "selectRelatedUsersDetailed")
        footer = t("selectInstructionBrief" if has_completed else "selectInstructionDetailed")
        lines = [header]
        lines.extend(
            f"{index}: {user['nickname']}" for index, user in enumerate(forward_users, start=1)
        )
        lines.append(footer)
        prompt = "\n".join(lines)

        sent_ids: list[str | None] = []
        outcome: dict[str, object] = {"users": [], "completed": False}
        timeout = max(1, self.settings.int("forwardSelectTimeout"))

        @session_waiter(timeout=timeout, record_history_chains=False)
        async def waiter(controller, msg_event) -> None:
            if pu.sender_id(msg_event) != user_id or pu.group_id(msg_event) != channel:
                return
            text = str(msg_event.message_str or "").strip()
            lowered = text.lower()
            if lowered == "all":
                outcome["users"] = list(forward_users)
                outcome["completed"] = True
                controller.stop()
                return
            if lowered == "skip":
                outcome["users"] = []
                outcome["completed"] = True
                controller.stop()
                return
            indices = []
            for token in text.split():
                try:
                    indices.append(int(token) - 1)
                except ValueError:
                    continue
            valid = [index for index in indices if 0 <= index < len(forward_users)]
            if not valid:
                sent_ids.append(await pu.send_text_with_id(msg_event, t("invalidSelection")))
                return
            outcome["users"] = [forward_users[index] for index in valid]
            outcome["completed"] = True
            controller.stop()

        sent_ids.append(await pu.send_text_with_id(event, prompt))
        try:
            await waiter(event, session_filter=UserSessionFilter())
        except TimeoutError:
            pass
        except Exception as exc:
            LOG.warning("Forward user selection failed: %s", exc)
        finally:
            await pu.delete_messages(event, sent_ids)
        return list(outcome["users"]), bool(outcome["completed"])

    async def _handle_special_forward(self, event, expanded: list[dict]) -> bool:
        mode = self.settings.str("forwardSpecialUserHandlingMode") or "ignore"
        if mode == "ignore":
            return True
        if mode == "reject":
            message_id = await pu.send_text_with_id(event, t("specialForwardUserBlocked"))
            pu.schedule_message_delete(event, [message_id], SPECIAL_RECALL_DELAY, self.tasks)
            return False
        return await self._confirm_special_forward(event, expanded)

    async def _confirm_special_forward(self, event, expanded: list[dict]) -> bool:
        sent_ids: list[str | None] = []
        sent_ids.append(await pu.send_text_with_id(event, t("specialForwardUserConfirmNotice")))
        try:
            components = await pu.cq_to_components(event, expanded, self.media.resolve_for_send)
            nodes = [item for item in components if isinstance(item, Comp.Node)]
            if nodes:
                await pu.send_forward_nodes(event, nodes)
        except Exception as exc:
            LOG.warning("Failed to preview special forward: %s", exc)

        outcome: dict[str, bool] = {"value": False}

        @session_waiter(timeout=60, record_history_chains=False)
        async def waiter(controller, msg_event) -> None:
            if pu.sender_id(msg_event) != pu.sender_id(event):
                return
            normalized = str(msg_event.message_str or "").strip().lower()
            if normalized in ("存储", "store"):
                outcome["value"] = True
                controller.stop()
                return
            if normalized in ("取消", "不存", "cancel", "skip"):
                outcome["value"] = False
                controller.stop()
                return
            sent_ids.append(
                await pu.send_text_with_id(msg_event, t("specialForwardUserConfirmRetry"))
            )

        sent_ids.append(await pu.send_text_with_id(event, t("specialForwardUserConfirmPrompt")))
        try:
            await waiter(event, session_filter=UserSessionFilter())
        except TimeoutError:
            outcome["value"] = False
        except Exception as exc:
            LOG.warning("Special forward confirmation failed: %s", exc)
        finally:
            pu.schedule_message_delete(event, sent_ids, SPECIAL_RECALL_DELAY, self.tasks)
        return outcome["value"]

    # ------------------------------------------------------------------
    # cave / cave.pic
    # ------------------------------------------------------------------
    async def get_cave(
        self, event, target: str | None = None, *, picture_only: bool = False
    ) -> None:
        if not await self._group_access(event):
            return
        if picture_only and target:
            await pu.send_text(event, t("noMsgWithId"))
            return

        channel_id = pu.group_id(event)
        draw_field = "pic_draw_count" if picture_only else "draw_count"
        caves = await self.db.get_caves_by_channel(channel_id)

        selected: CaveRecord | None = None
        count_draw = True
        if not target:
            selectable = (
                [cave for cave in caves if content_mod.is_picture_message(cave.type, cave.content)]
                if picture_only
                else caves
            )
            if not selectable:
                await pu.send_text(
                    event,
                    t("noPicMsgInCave" if picture_only else "noMsgInCave"),
                )
                return
            alpha = self.settings.float("alpha")
            selected = self._weighted_pick(selectable, alpha, draw_field)
        else:
            trimmed = target.strip()
            if trimmed.isdigit():
                selected = await self.db.get_cave(int(trimmed), channel_id)
                if selected is not None:
                    success = await self._send_cave_safe(event, selected)
                    if success:
                        await self.db.increment_draw(selected.entry_id, draw_field)
                    return

            user_ids, error = pu.parse_user_ids(event, [trimmed])
            if error == "invalid_all_mention":
                await pu.send_text(event, t("invalidAllMention"))
                return
            if not user_ids:
                await pu.send_text(event, t("noMsgWithId"))
                return
            if not await pu.check_users_in_group(event, user_ids[:1]):
                await pu.send_text(event, t("userNotInGroup"))
                return
            matching = [
                cave
                for cave in caves
                if (
                    cave.origin_user_id == user_ids[0]
                    if not cave.related_users
                    else user_ids[0] in cave.related_users
                )
            ]
            if not matching:
                await pu.send_text(event, t("noMatchingUserCave"))
                return
            selected = self.rng.choice(matching)
            count_draw = False

        if selected is None:
            await pu.send_text(event, t("noMsgInCave"))
            return
        success = await self._send_cave_safe(event, selected)
        if success and count_draw:
            await self.db.increment_draw(selected.entry_id, draw_field)

    def _weighted_pick(self, caves: list[CaveRecord], alpha: float, field: str) -> CaveRecord:
        weights = [1.0 / (1.0 + getattr(cave, field, 0) * alpha) for cave in caves]
        total = sum(weights)
        if total <= 0:
            return self.rng.choice(caves)
        point = self.rng.random() * total
        for cave, weight in zip(caves, weights, strict=False):
            if point <= weight:
                return cave
            point -= weight
        return caves[-1]

    async def _send_cave_safe(self, event, cave: CaveRecord) -> bool:
        try:
            await self.send_cave(event, cave)
            return True
        except Exception as exc:
            LOG.warning(
                "Failed to send cave #%s in %s: %s",
                cave.public_id,
                cave.channel_id,
                exc,
            )
            await self._handle_send_failure(event, cave, exc)
            return False

    async def send_cave(self, event, cave: CaveRecord) -> None:
        segments = content_mod.parse_content(cave.content)
        if any(segment.get("type") == "forward" for segment in segments):
            segments = await pu.expand_forward_segments(event, segments)

        starts_with_text, ends_with_text = content_mod.first_and_last_are_text(segments)
        origin_name = await pu.get_user_name(event, cave.origin_user_id)
        user_name = await pu.get_user_name(event, cave.user_id)
        related_names = origin_name
        if cave.related_users:
            names = [await pu.get_user_name(event, uid) for uid in cave.related_users]
            related_names = ", ".join(names)
        data = templates.template_data(
            cave_id=cave.public_id,
            create_time=cave.create_time,
            origin_name=origin_name,
            user_name=user_name,
            related_users=related_names,
            starts_with_text=starts_with_text,
            ends_with_text=ends_with_text,
        )

        should_forward = self.settings.bool("sendAllAsForwardMsg") or cave.type == "forward"
        if should_forward:
            template = templates.render(templates.pick_forward_template(self.rng), data)
            await pu.send_text(event, template)
            try:
                components = await pu.cq_to_components(event, segments, self.media.resolve_for_send)
                is_actual_forward = any(
                    isinstance(component, Comp.Node) for component in components
                )
                if not is_actual_forward:
                    if segments and segments[0].get("type") == "record":
                        await pu.send_chain(event, components)
                        return
                    node = Comp.Node(
                        content=components,
                        uin=str(cave.origin_user_id),
                        name=origin_name,
                    )
                    if not await pu.send_forward_nodes(event, [node]):
                        await self._send_forward_text_fallback(event, segments)
                    return
                nodes = [item for item in components if isinstance(item, Comp.Node)]
                if not await pu.send_forward_nodes(event, nodes):
                    await self._send_forward_text_fallback(event, segments)
            except Exception as exc:
                raise RuntimeError(t("sendBodyFailed")) from exc
            return

        available = list(templates.MSG_TEMPLATES)
        if not available:
            await pu.send_text(event, t("noTemplatesConfigured"))
            return
        prefix_template, suffix_template = templates.pick_msg_template(self.rng)
        prefix = templates.render(prefix_template, data)
        suffix = templates.render(suffix_template, data)
        components = await pu.cq_to_components(event, segments, self.media.resolve_for_send)
        components = [Comp.Plain(prefix), *components, Comp.Plain(suffix)]
        await pu.send_chain(event, components)

    @staticmethod
    async def _send_forward_text_fallback(event, segments: list[dict]) -> None:
        lines = []
        for segment in segments:
            if segment.get("type") == "node":
                lines.append(content_mod.plain_text_of_node(segment))
            else:
                lines.append(content_mod.flatten_text([segment]))
        await pu.send_text(event, "\n".join(line for line in lines if line))

    async def _handle_send_failure(self, event, cave: CaveRecord, error: Exception) -> None:
        mode = self.settings.str("sendFailureHandlingMode") or "ignore"
        error_message = str(error)[:120] or t("unknownError")
        if mode == "auto-delete":
            try:
                await self._delete_stored_cave(cave)
                await pu.send_text(event, t("sendFailedAutoDeleted", id=cave.public_id))
            except Exception as delete_error:
                LOG.error("Failed to auto-delete cave #%s: %s", cave.public_id, delete_error)
                await pu.send_text(event, t("sendFailedAutoDeleteAlsoFailed", id=cave.public_id))
            return
        if mode == "daily-report":
            try:
                await self.db.add_send_failure(
                    cave_id=cave.public_id,
                    channel_id=cave.channel_id,
                    guild_id=pu.group_id(event),
                    platform_id=pu.platform_id(event),
                    self_id=pu.self_id(event),
                    error_message=error_message,
                )
            except Exception as record_error:
                LOG.error(
                    "Failed to record send failure for #%s: %s",
                    cave.public_id,
                    record_error,
                )
                await pu.send_text(event, t("sendFailedReportRecordFailed", id=cave.public_id))
                return
            admin_id = self.settings.str("sendFailureSummaryAdminId").strip()
            summary_time = self.settings.str("sendFailureSummaryTime") or "09:00"
            if admin_id:
                await pu.send_text(
                    event,
                    t(
                        "sendFailedRecordedForDailyReport",
                        id=cave.public_id,
                        summaryTime=summary_time,
                    ),
                )
            else:
                await pu.send_text(
                    event,
                    t("sendFailedRecordedWithoutAdmin", id=cave.public_id),
                )
            return
        await pu.send_text(event, t("sendFailedIgnored", id=cave.public_id))

    # ------------------------------------------------------------------
    # Lists / search
    # ------------------------------------------------------------------
    async def _cave_id_list(
        self,
        event,
        field: str,
        empty_key: str,
        header_key: str,
        *,
        include_header: bool = True,
    ) -> None:
        if not await self._group_access(event):
            return
        caves = await self.db.get_caves_by_user(field, pu.sender_id(event), pu.group_id(event))
        if not caves:
            await pu.send_text(event, t(empty_key))
            return
        ids = " | ".join(str(cave.public_id) for cave in caves)
        await pu.send_text(event, f"{t(header_key)}\n{ids}" if include_header else ids)

    async def list_contributed(self, event, *, include_header: bool = True) -> None:
        await self._cave_id_list(
            event,
            "user_id",
            "noMsgContributed",
            "msgListHeaderContributed",
            include_header=include_header,
        )

    async def list_traced(self, event) -> None:
        await self._cave_id_list(event, "origin_user_id", "noMsgTraced", "msgListHeaderTraced")

    async def view_cave_by_id(self, event, value: str | None) -> None:
        """View exactly one cave by public ID (no user-target fallback)."""
        if not await self._group_access(event):
            return
        text = (value or "").strip()
        if not text.isdigit():
            await pu.send_text(event, t("noMsgWithId"))
            return
        cave = await self.db.get_cave(int(text), pu.group_id(event))
        if cave is None:
            await pu.send_text(event, t("noMsgWithId"))
            return
        success = await self._send_cave_safe(event, cave)
        if success:
            await self.db.increment_draw(cave.entry_id, "draw_count")

    async def search(self, event, args: list[str]) -> None:
        if not await self._group_access(event):
            return
        if not args:
            mention_ids, _ = pu.parse_user_ids(event, [])
            if not mention_ids:
                await pu.send_text(event, t("noUserIdProvided"))
                return
        user_ids, error = pu.parse_user_ids(event, args)
        if error == "invalid_all_mention":
            await pu.send_text(event, t("invalidAllMention"))
            return
        if not user_ids:
            await pu.send_text(event, t("noValidUserIdProvided"))
            return
        target = user_ids[0]
        if not await pu.check_users_in_group(event, [target]):
            await pu.send_text(event, t("userNotInGroup"))
            return
        caves = await self.db.get_caves_by_channel(pu.group_id(event))
        matching = [
            cave
            for cave in caves
            if (
                cave.origin_user_id == target
                if not cave.related_users
                else target in cave.related_users
            )
        ]
        if not matching:
            name = await pu.get_user_name(event, target)
            await pu.send_text(event, t("noMatchingCaves", user=name))
            return
        ids = ", ".join(str(cave.public_id) for cave in matching)
        await pu.send_text(event, t("searchResult", count=len(matching), ids=ids))

    # ------------------------------------------------------------------
    # Delete / bind
    # ------------------------------------------------------------------
    async def _delete_stored_cave(self, cave: CaveRecord) -> None:
        if self.settings.bool("deleteMediaWhenDeletingMsg"):
            await self.media.delete_content_media(cave.content)
        if cave.entry_id is None:
            raise RuntimeError(f"missing entry_id for cave {cave.public_id}")
        await self.db.delete_cave(cave.entry_id)

    def _delete_permission_failure(self, event, cave: CaveRecord, actor_admin: bool) -> str | None:
        if self.settings.bool("adminMessageProtection"):
            if self._is_admin_id(cave.user_id) and not actor_admin:
                return t("adminOnly")
        if actor_admin:
            return None
        actor = pu.sender_id(event)
        if actor == cave.user_id:
            if self.settings.bool("allowContributorDelete"):
                return None
            return t("contributorDeleteDenied")
        if actor == cave.origin_user_id:
            if self.settings.bool("allowSenderDelete"):
                return None
            return t("senderDeleteDenied")
        return t("permissionDenied")

    async def drop(self, event, args: list[str]) -> None:
        if not await self._group_access(event):
            return
        if not args or not args[0].isdigit() or int(args[0]) <= 0:
            await pu.send_text(event, t("noIdProvided"))
            return
        public_id = int(args[0])
        cave = await self.db.get_cave(public_id, pu.group_id(event))
        if cave is None:
            await pu.send_text(event, t("noMsgWithId"))
            return
        actor_admin = self._actor_is_admin(event)
        failure = self._delete_permission_failure(event, cave, actor_admin)
        if failure:
            await pu.send_text(event, failure)
            return
        try:
            await self._delete_stored_cave(cave)
        except Exception as exc:
            LOG.error("Failed to delete cave #%s: %s", public_id, exc)
            await pu.send_text(event, t("permissionDenied"))
            return
        await pu.send_text(event, t("msgDeleted", id=public_id))

    async def purge(self, event, args: list[str]) -> None:
        if not await self._group_access(event):
            return
        ids: list[int] = []
        for token in args:
            if token.isdigit() and int(token) > 0:
                ids.append(int(token))
        if not ids:
            await pu.send_text(event, t("noIdProvided"))
            return
        actor_admin = self._actor_is_admin(event)
        caves = await self.db.get_caves_by_public_ids(ids, pu.group_id(event))
        found_ids = {cave.public_id for cave in caves}
        failed = [public_id for public_id in ids if public_id not in found_ids]
        for cave in caves:
            failure = self._delete_permission_failure(event, cave, actor_admin)
            if failure:
                failed.append(cave.public_id)
                continue
            try:
                await self._delete_stored_cave(cave)
            except Exception as exc:
                LOG.error("Failed to delete cave #%s: %s", cave.public_id, exc)
                failed.append(cave.public_id)
        failed = sorted(set(failed))
        if not failed:
            await pu.send_text(event, t("msgDeletedMultiple", count=len(ids)))
        elif len(failed) == len(ids):
            await pu.send_text(event, t("msgDeleteFailedAll", ids=", ".join(map(str, failed))))
        else:
            await pu.send_text(event, t("msgDeletePartial", ids=", ".join(map(str, failed))))

    async def bind(self, event, args: list[str]) -> None:
        if not await self._group_access(event):
            return
        if not self._actor_is_admin(event):
            await pu.send_text(event, t("permissionDenied"))
            return
        if not args:
            await pu.send_text(event, t("noBindIdProvided"))
            return
        if not args[0].isdigit() or int(args[0]) <= 0:
            await pu.send_text(event, t("noBindIdProvided"))
            return
        public_id = int(args[0])
        user_ids, error = pu.parse_user_ids(event, args[1:])
        if error == "invalid_all_mention":
            await pu.send_text(event, t("invalidAllMention"))
            return
        if not user_ids:
            await pu.send_text(event, t("noBindUserIdProvided"))
            return
        cave = await self.db.get_cave(public_id, pu.group_id(event))
        if cave is None:
            await pu.send_text(event, t("noMsgWithId"))
            return
        if not await pu.check_users_in_group(event, user_ids):
            await pu.send_text(event, t("userNotInGroup"))
            return
        if cave.entry_id is None:
            await pu.send_text(event, t("noMsgWithId"))
            return
        await self.db.update_cave(cave.entry_id, related_users=user_ids)
        await pu.send_text(event, t("userBoundSuccess", id=public_id))

    # ------------------------------------------------------------------
    # Rank
    # ------------------------------------------------------------------
    @staticmethod
    def parse_custom_time(value: str) -> int | None:
        text = (value or "").lower()
        if not re.fullmatch(r"(\d+(m|w|d|h))+", text):
            return None
        order = {"m": 0, "w": 1, "d": 2, "h": 3}
        last_index = -1
        total = 0
        for match in re.finditer(r"(\d+)([mwdh])", text):
            amount = int(match.group(1))
            unit = match.group(2)
            index = order[unit]
            if last_index != -1 and index < last_index:
                return None
            total += amount * TIME_UNITS[unit]
            last_index = index
        return total

    @staticmethod
    def start_time(period: str, custom_ms: int | None = None) -> datetime:
        now = datetime.now().astimezone()
        if custom_ms is not None:
            return (now - timedelta(milliseconds=custom_ms)).astimezone(timezone.utc)
        if period == "lday":
            start = now - timedelta(days=1)
        elif period == "lweek":
            start = now - timedelta(days=7)
        elif period == "lmonth":
            start = now - timedelta(days=30)
        elif period == "day":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "week":
            start = (now - timedelta(days=now.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        elif period == "month":
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            start = datetime(1970, 1, 1, tzinfo=timezone.utc)
        return start.astimezone(timezone.utc)

    async def rank(self, event, args: list[str]) -> None:
        if not await self._group_access(event):
            return
        period = (args[0] if args else "all").lower()
        custom_ms = self.parse_custom_time(period)
        if period not in PREDEFINED_PERIODS and custom_ms is None:
            await pu.send_text(event, t("invalidPeriod", periods=", ".join(PREDEFINED_PERIODS)))
            return
        start = self.start_time(period, custom_ms)
        caves = [
            cave
            for cave in await self.db.get_caves_by_channel(pu.group_id(event))
            if cave.create_time >= start
        ]
        counts: dict[str, int] = {}
        for cave in caves:
            if cave.related_users:
                for user_id in cave.related_users:
                    counts[user_id] = counts.get(user_id, 0) + 1
            else:
                counts[cave.origin_user_id] = counts.get(cave.origin_user_id, 0) + 1

        top_count = max(1, self.settings.int("rankingTopCount") or 10)
        ordered = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:top_count]
        if ordered:
            lines = []
            for index, (user_id, count) in enumerate(ordered, start=1):
                name = await pu.get_user_name(event, user_id)
                emoji = {1: "🥇", 2: "🥈", 3: "🥉"}.get(index, f"{index}.")
                lines.append(t("rankFormat", rankEmoji=emoji, userName=name, count=count))
            ranking_text = "\n".join(lines)
        else:
            ranking_text = t("noData")

        period_text = period if custom_ms is not None else t(f"period_{period}")
        title = t("rankingTitle", period=period_text)
        bot_id = pu.self_id(event) or pu.sender_id(event)
        bot_name = await pu.get_user_name(event, bot_id) or "Bot"
        nodes = [
            Comp.Node(content=[Comp.Plain(title)], uin=bot_id, name=bot_name),
            Comp.Node(content=[Comp.Plain(ranking_text)], uin=bot_id, name=bot_name),
        ]
        if not await pu.send_forward_nodes(event, nodes):
            await pu.send_text(event, f"{title}\n{ranking_text}")

    # ------------------------------------------------------------------
    # Poke
    # ------------------------------------------------------------------
    async def handle_poke(self, event) -> None:
        if self.settings.bool("enablePokeTrigger") is False:
            return
        if not pu.is_onebot(event):
            return
        # Pokes outside the whitelist are silently ignored.
        if self.settings.bool("enableChannelWhitelist"):
            if pu.group_id(event) not in self.settings.str_list("channelWhitelist"):
                return
        if not await self._group_access(event):
            return
        await self.get_cave(event)
