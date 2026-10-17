"""Configuration defaults and typed accessors."""

from __future__ import annotations

from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "enableChannelWhitelist": False,
    "channelWhitelist": [],
    "adminMessageProtection": False,
    "adminIds": [],
    "allowContributorDelete": True,
    "allowSenderDelete": True,
    "deleteMediaWhenDeletingMsg": True,
    "enableSizeLimit": False,
    "maxImageSize": 20480,
    "maxVideoSize": 5120,
    "maxFileSize": 5120,
    "maxRecordSize": 5120,
    "useBase64ForMedia": False,
    "sendAllAsForwardMsg": False,
    "rankingTopCount": 10,
    "forwardSelectTimeout": 20,
    "enableForwardUserSelection": True,
    "autoBindSingleForwardUser": False,
    "forwardSpecialUserHandlingMode": "ignore",
    "enablePokeTrigger": True,
    "alpha": 0.2,
    "mediaStorage": "local",
    "s3UploadFailureFallbackMode": "local",
    "s3Bucket": "",
    "s3Region": "",
    "s3Endpoint": "",
    "s3AccessKeyId": "",
    "s3SecretAccessKey": "",
    "s3ForcePathStyle": False,
    "s3PathPrefix": "",
    "s3PresignExpiresIn": 3600,
    "sendFailureHandlingMode": "ignore",
    "sendFailureSummaryAdminId": "",
    "sendFailureSummaryTime": "09:00",
    "oversizedMediaCleanupMode": "keep-cave",
    "enableAutoReindex": False,
    "autoReindexTime": "00:00",
    "enableDuplicateDetection": True,
}


class Settings:
    """Read-only view over an AstrBotConfig-like mapping."""

    def __init__(self, config: Any) -> None:
        self._config = config

    def get(self, key: str, default: Any = None) -> Any:
        if self._config is not None:
            try:
                value = self._config.get(key, None)
            except AttributeError:
                value = None
            if value is not None:
                return value
        if default is not None:
            return default
        return DEFAULT_CONFIG.get(key)

    def bool(self, key: str) -> bool:
        return bool(self.get(key))

    def int(self, key: str) -> int:
        try:
            return int(self.get(key))
        except (TypeError, ValueError):
            return int(DEFAULT_CONFIG.get(key, 0))

    def float(self, key: str) -> float:
        try:
            value = float(self.get(key))
        except (TypeError, ValueError):
            value = float(DEFAULT_CONFIG.get(key, 0))
        if key == "alpha":
            value = max(0.0, min(2.0, value))
        return value

    def str(self, key: str) -> str:
        value = self.get(key)
        return "" if value is None else str(value)

    def str_list(self, key: str) -> list[str]:
        value = self.get(key)
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(item) for item in value if str(item).strip()]
        return []

    def admin_ids(self) -> set[str]:
        return set(self.str_list("adminIds"))

    def host_admin_ids(self) -> set[str]:
        """Best-effort access to AstrBot's global admin list."""
        values: Any = []
        try:
            from astrbot.core import astrbot_config

            values = astrbot_config.get("admins_id", [])
        except Exception:
            try:
                host = getattr(self._config, "astrbot_config", None) or self._config
                values = host.get("admins_id", [])
            except (AttributeError, TypeError):
                values = []
        if isinstance(values, str):
            return {values.strip()} if values.strip() else set()
        if isinstance(values, (list, tuple, set)):
            return {str(value) for value in values}
        return set()

    def all_admin_ids(self) -> set[str]:
        return self.admin_ids() | self.host_admin_ids()
