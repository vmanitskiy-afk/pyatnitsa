"""Хранилище настроек в SQLite — для веб-панели."""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import structlog

logger = structlog.get_logger()

SETTINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Дефолтные настройки
DEFAULTS = {
    # LLM
    "llm.provider": "gigachat",
    "llm.gigachat_credentials": "",
    "llm.gigachat_model": "GigaChat-2-Max",
    "llm.gigachat_scope": "GIGACHAT_API_PERS",
    "llm.gigachat_verify_ssl": "false",
    "llm.gigachat_max_tokens": "4096",
    "llm.claude_api_key": "",
    "llm.claude_model": "claude-sonnet-4-20250514",
    "llm.ollama_base_url": "",
    "llm.ollama_model": "gemma4:31b",
    # Channels
    "channels.max_bot_token": "",
    "channels.max_use_polling": "true",
    "channels.telegram_bot_token": "",
    # Integrations
    "integrations.redmine_url": "",
    "integrations.redmine_api_key": "",
    "integrations.redmine_admin_key": "",
    "integrations.rdm_login": "",
    "integrations.rdm_password": "",
    "integrations.bitrix_webhook_url": "",
    "integrations.onec_base_url": "",
    "integrations.onec_username": "",
    "integrations.onec_password": "",
    # System
    "system.agent_name": "Пятница",
    "system.log_level": "INFO",
    "system.heartbeat_enabled": "true",
    "system.heartbeat_interval": "5",
    # Files
    "files.workspace": "",
}

# Поля с секретами — маскируются при отдаче
SECRET_KEYS = {
    "llm.gigachat_credentials",
    "llm.claude_api_key",
    "integrations.redmine_api_key",
    "integrations.redmine_admin_key",
    "integrations.rdm_password",
    "integrations.onec_password",
}


class SettingsStore:
    """Хранилище настроек в SQLite."""

    def __init__(self, db_path: str = "data/memory.db"):
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self, db: aiosqlite.Connection | None = None):
        """Инициализация. Можно передать существующее соединение."""
        if db:
            self._db = db
        else:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SETTINGS_SCHEMA)
        await self._db.commit()
        # Заполняем дефолты если пусто
        for key, value in DEFAULTS.items():
            await self._db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
        await self._db.commit()
        logger.info("settings_store_initialized")

    async def get(self, key: str) -> str:
        cursor = await self._db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row["value"] if row else DEFAULTS.get(key, "")

    async def get_all(self, mask_secrets: bool = True) -> dict[str, str]:
        cursor = await self._db.execute("SELECT key, value FROM settings ORDER BY key")
        rows = await cursor.fetchall()
        result = {}
        for row in rows:
            k, v = row["key"], row["value"]
            if mask_secrets and k in SECRET_KEYS and v:
                result[k] = v[:4] + "•" * max(0, len(v) - 8) + v[-4:] if len(v) > 8 else "••••"
            else:
                result[k] = v
        return result

    async def set(self, key: str, value: str):
        await self._db.execute(
            """INSERT INTO settings (key, value, updated_at) 
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(key) DO UPDATE SET value=?, updated_at=datetime('now')""",
            (key, value, value),
        )
        await self._db.commit()

    async def set_many(self, updates: dict[str, str]):
        for key, value in updates.items():
            # Не перезаписываем секреты замаскированными значениями
            if key in SECRET_KEYS and "•" in value:
                continue
            await self._db.execute(
                """INSERT INTO settings (key, value, updated_at) 
                   VALUES (?, ?, datetime('now'))
                   ON CONFLICT(key) DO UPDATE SET value=?, updated_at=datetime('now')""",
                (key, value, value),
            )
        await self._db.commit()

    async def close(self):
        if self._db:
            await self._db.close()
