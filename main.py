"""Echo cave plugin for AstrBot."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

from . import platform_utils as pu
from .admin_service import AdminService
from .cave_service import CaveService, MaintenanceLock
from .db import CaveDatabase
from .media import MediaManager
from .scheduler import DailyScheduler
from .settings import Settings

PLUGIN_NAME = "astrbot_plugin_echo_cave"
POKE_COOLDOWN_SECONDS = 5.0
POKE_MAP_LIMIT = 256


def resolve_data_dir() -> Path:
    """Resolve the plugin data directory.

    ``ECHO_CAVE_DATA_DIR`` is honoured for tests and unusual deployments.
    """
    override = os.environ.get("ECHO_CAVE_DATA_DIR")
    if override:
        return Path(override).expanduser()
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        return Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
    except Exception:
        return Path.cwd() / "data" / "plugin_data" / PLUGIN_NAME


class EchoCavePlugin(Star):
    """回声洞：群聊消息存储、随机回溯、排行榜与维护。"""

    def __init__(self, context: Context, config=None) -> None:
        super().__init__(context)
        self.config = config
        self.settings = Settings(config)
        self.data_dir = resolve_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = CaveDatabase(self.data_dir / "echo_cave.sqlite3")
        self.media = MediaManager(self.data_dir, self.settings)
        self.maintenance = MaintenanceLock()
        self.tasks: set[asyncio.Task] = set()
        self.service = CaveService(
            self.db,
            self.media,
            self.settings,
            self.maintenance,
            task_set=self.tasks,
        )
        self.media.cleanup_callback = self.service.enforce_media_limit
        self.admin = AdminService(
            self.db,
            self.media,
            self.settings,
            self.maintenance,
            self.data_dir,
            send_private=self._send_private,
        )
        self.scheduler = DailyScheduler()
        self._initialized = False
        self._poke_cooldown: dict[str, float] = {}

    async def initialize(self) -> None:
        if self._initialized:
            return
        if self.scheduler.stopped:
            self.scheduler = DailyScheduler()
        await self.db.initialize()
        self._initialized = True
        if self.settings.str("sendFailureHandlingMode") == "daily-report":
            self.scheduler.add(
                "send-failure-summary",
                self.settings.str("sendFailureSummaryTime") or "09:00",
                self.admin.flush_send_failure_summary,
            )
        if self.settings.bool("enableAutoReindex"):
            self.scheduler.add(
                "auto-reindex",
                self.settings.str("autoReindexTime") or "00:00",
                self._scheduled_reindex,
            )
        self.logger.info(
            "Echo cave initialized (data dir: %s, storage: %s).",
            self.data_dir,
            self.settings.str("mediaStorage"),
        )

    async def terminate(self) -> None:
        await self.scheduler.stop()
        for task in list(self.tasks):
            task.cancel()
        for task in list(self.tasks):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self.tasks.clear()
        await self.media.close()
        await self.db.close()
        self._initialized = False
        self.logger.info("Echo cave terminated.")

    async def _scheduled_reindex(self) -> None:
        await self.admin.reindex(None, scheduled=True)

    async def _send_private(self, platform_id: str, admin_id: str, text: str) -> bool:
        umo = f"{platform_id}:FriendMessage:{admin_id}"
        try:
            return await self.context.send_message(umo, MessageChain().message(text))
        except Exception as exc:
            self.logger.error("Failed to send private summary: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Argument helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _args(event: AstrMessageEvent, command: str) -> list[str]:
        return pu.command_arguments(event, command)

    # ------------------------------------------------------------------
    # User commands
    # ------------------------------------------------------------------
    @filter.command("cave")
    async def random_cave(self, event: AstrMessageEvent) -> None:
        """随机发送回声洞里面的内容。"""
        await self.service.get_cave(event)

    @filter.command("查看")
    async def view_cave(self, event: AstrMessageEvent) -> None:
        """查看指定 ID 的回声洞内容。"""
        args = self._args(event, "查看")
        await self.service.view_cave_by_id(event, args[0] if args else None)

    @filter.command("查询")
    async def query_caves(self, event: AstrMessageEvent) -> None:
        """查询自己已投稿的全部回声洞 ID（仅 ID）。"""
        await self.service.list_contributed(event, include_header=False)

    @filter.command("cave.pic")
    async def cave_pic(self, event: AstrMessageEvent) -> None:
        """随机获取一条包含图片的回声洞消息。"""
        await self.service.get_cave(event, None, picture_only=True)

    @filter.command("cave.listen")
    async def cave_listen(self, event: AstrMessageEvent) -> None:
        """获取自己投稿的回声洞列表。"""
        await self.service.list_contributed(event)

    @filter.command("cave.trace")
    async def cave_trace(self, event: AstrMessageEvent) -> None:
        """获取自己发言被投稿的回声洞列表。"""
        await self.service.list_traced(event)

    @filter.command("投稿")
    async def submit_cave(self, event: AstrMessageEvent) -> None:
        """引用一条消息并投稿到回声洞。"""
        await self.service.add_cave(event, self._args(event, "投稿"))

    @filter.command("cave.drop")
    async def cave_drop(self, event: AstrMessageEvent) -> None:
        """删除指定 ID 的回声洞消息。"""
        await self.service.drop(event, self._args(event, "cave.drop"))

    @filter.command("cave.purge")
    async def cave_purge(self, event: AstrMessageEvent) -> None:
        """批量删除多个 ID 的回声洞消息。"""
        await self.service.purge(event, self._args(event, "cave.purge"))

    @filter.command("cave.search")
    async def cave_search(self, event: AstrMessageEvent) -> None:
        """搜索与指定用户相关的回声洞。"""
        await self.service.search(event, self._args(event, "cave.search"))

    @filter.command("cave.bind")
    async def cave_bind(self, event: AstrMessageEvent) -> None:
        """将用户绑定到指定回声洞（管理员）。"""
        await self.service.bind(event, self._args(event, "cave.bind"))

    @filter.command("cave.rank")
    async def cave_rank(self, event: AstrMessageEvent) -> None:
        """查看回声洞排行榜。"""
        await self.service.rank(event, self._args(event, "cave.rank"))

    # ------------------------------------------------------------------
    # Admin commands
    # ------------------------------------------------------------------
    @filter.command("cave.admin.merge")
    async def cave_admin_merge(self, event: AstrMessageEvent) -> None:
        """将一个群聊的回声洞记录合并到另一个群聊。"""
        await self.admin.merge(event, self._args(event, "cave.admin.merge"))

    @filter.command("cave.admin.migrate-local-v2")
    async def cave_admin_migrate_local(self, event: AstrMessageEvent) -> None:
        """迁移旧本地媒体目录到 V2 分群存储格式。"""
        await self.admin.migrate_local_v2(event)

    @filter.command("cave.admin.migrate-s3")
    async def cave_admin_migrate_s3(self, event: AstrMessageEvent) -> None:
        """将本地 V2 媒体资源迁移到 S3 存储。"""
        await self.admin.migrate_s3(event, self._args(event, "cave.admin.migrate-s3"))

    @filter.command("cave.admin.inspect-media")
    async def cave_admin_inspect(self, event: AstrMessageEvent) -> None:
        """检测回声洞中的媒体链接，不执行实际迁移。"""
        await self.admin.inspect_media(event, self._args(event, "cave.admin.inspect-media"))

    @filter.command("cave.admin.backfill-media-urls")
    async def cave_admin_backfill(self, event: AstrMessageEvent) -> None:
        """为历史回声洞回填媒体 URL 列。"""
        await self.admin.backfill_media_urls(
            event, self._args(event, "cave.admin.backfill-media-urls")
        )

    @filter.command("cave.admin.reindex")
    async def cave_admin_reindex(self, event: AstrMessageEvent) -> None:
        """重排所有回声洞 public ID（私聊管理员）。"""
        await self.admin.reindex(event)

    @filter.command("cave.admin.restore-reindex")
    async def cave_admin_restore(self, event: AstrMessageEvent) -> None:
        """从重排生成的 JSON 备份恢复数据库（私聊管理员）。"""
        await self.admin.restore_reindex(event, self._args(event, "cave.admin.restore-reindex"))

    # ------------------------------------------------------------------
    # OneBot poke
    # ------------------------------------------------------------------
    @filter.event_message_type(filter.EventMessageType.OTHER_MESSAGE)
    async def on_poke(self, event: AstrMessageEvent) -> None:
        """拍一拍机器人时随机抽取一条回声洞。"""
        if not self.settings.bool("enablePokeTrigger"):
            return
        if not pu.is_group(event):
            return
        messages = pu.event_messages(event)
        poke = next((item for item in messages if isinstance(item, Comp.Poke)), None)
        if poke is None:
            return
        target = poke.target_id()
        if not target or str(target) != pu.self_id(event):
            return
        if pu.sender_id(event) == pu.self_id(event):
            return
        key = f"{pu.group_id(event)}:{pu.sender_id(event)}"
        now = time.monotonic()
        last = self._poke_cooldown.get(key, 0.0)
        if now - last < POKE_COOLDOWN_SECONDS:
            return
        if len(self._poke_cooldown) >= POKE_MAP_LIMIT:
            expired = [
                item
                for item, timestamp in self._poke_cooldown.items()
                if now - timestamp >= POKE_COOLDOWN_SECONDS
            ]
            for item in expired:
                self._poke_cooldown.pop(item, None)
        self._poke_cooldown[key] = now
        await self.service.get_cave(event)
