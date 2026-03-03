"""Heartbeat — планировщик проактивных действий Пятница.ai."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Callable, Awaitable

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from pyatnitsa.core.models import Event, EventType

if TYPE_CHECKING:
    from pyatnitsa.core.agent import Agent

logger = structlog.get_logger()


class Heartbeat:
    """Периодически проверяет внешние системы и генерирует события."""
    
    def __init__(self, interval_minutes: int = 5):
        self.interval_minutes = interval_minutes
        self._scheduler = AsyncIOScheduler()
        self._checks: list[Callable[[], Awaitable[list[Event]]]] = []
        self._event_handler: Callable[[Event], Awaitable[None]] | None = None
    
    def add_check(self, check: Callable[[], Awaitable[list[Event]]]):
        """Добавляет проверку, выполняемую по расписанию."""
        self._checks.append(check)
    
    def on_event(self, handler: Callable[[Event], Awaitable[None]]):
        """Регистрирует обработчик событий."""
        self._event_handler = handler
    
    async def _tick(self):
        """Один тик heartbeat — запускает все проверки."""
        logger.debug("heartbeat_tick", checks=len(self._checks))
        
        for check in self._checks:
            try:
                events = await check()
                for event in events:
                    if self._event_handler:
                        await self._event_handler(event)
            except Exception as e:
                logger.error("heartbeat_check_error", error=str(e))
    
    def start(self):
        """Запускает планировщик."""
        self._scheduler.add_job(
            self._tick,
            "interval",
            minutes=self.interval_minutes,
            id="heartbeat",
        )
        self._scheduler.start()
        logger.info("heartbeat_started", interval_minutes=self.interval_minutes)
    
    def stop(self):
        """Останавливает планировщик."""
        self._scheduler.shutdown(wait=False)
        logger.info("heartbeat_stopped")


# ─── Примеры проверок ────────────────────────────────────────

async def check_redmine_deadlines() -> list[Event]:
    """Проверяет приближающиеся дедлайны в Redmine."""
    # TODO: реализовать
    return []


async def check_new_emails() -> list[Event]:
    """Проверяет новые письма."""
    # TODO: реализовать
    return []
