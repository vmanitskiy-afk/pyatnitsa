"""Пятница.ai — точка входа."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

import structlog
import uvicorn
from dotenv import load_dotenv

# Загружаем .env до импорта настроек
load_dotenv()

from pyatnitsa.config.settings import get_settings
from pyatnitsa.config.settings_store import SettingsStore
from pyatnitsa.core.agent import Agent
from pyatnitsa.core.llm import LLMManager, GigaChatProvider, ClaudeProvider
from pyatnitsa.channels.channels import MaxChannel, TelegramChannel
from pyatnitsa.skills.skills import SkillLoader
from pyatnitsa.memory.store import MemoryStore
from pyatnitsa.scheduler.heartbeat import Heartbeat
from pyatnitsa.api.server import app as fastapi_app, inject_dependencies

logger = structlog.get_logger()


async def run():
    """Запускает Пятница.ai."""

    settings = get_settings()

    # ─── Logging ─────────────────────────────────────────────
    import logging
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
    )

    logger.info("pyatnitsa_starting", version="0.1.0", debug=settings.debug)

    # ─── Memory ──────────────────────────────────────────────
    memory = MemoryStore(db_path=settings.memory.db_path)
    await memory.init()

    # ─── Settings Store (для веб-панели) ─────────────────────
    settings_store = SettingsStore(db_path=settings.memory.db_path)
    await settings_store.init(db=memory._db)

    # Читаем credentials из settings_store (веб-панель может их обновить)
    gc_creds = settings.llm.gigachat_credentials or await settings_store.get("llm.gigachat_credentials")
    gc_model = settings.llm.gigachat_model or await settings_store.get("llm.gigachat_model")
    gc_scope = settings.llm.gigachat_scope or await settings_store.get("llm.gigachat_scope")
    claude_key = settings.llm.claude_api_key or await settings_store.get("llm.claude_api_key")

    # ─── LLM ─────────────────────────────────────────────────
    llm = LLMManager()

    if gc_creds:
        try:
            llm.add_provider(GigaChatProvider(
                credentials=gc_creds,
                model=gc_model or "GigaChat-2-Max",
                scope=gc_scope or "GIGACHAT_API_PERS",
                verify_ssl=settings.llm.gigachat_verify_ssl,
                ca_bundle_file=settings.llm.gigachat_ca_bundle_file or None,
                max_tokens=settings.llm.gigachat_max_tokens,
            ))
        except Exception as e:
            logger.error("gigachat_init_failed", error=str(e))

    if claude_key:
        try:
            llm.add_provider(ClaudeProvider(
                api_key=claude_key,
                model=settings.llm.claude_model,
                max_tokens=settings.llm.claude_max_tokens,
            ))
        except Exception as e:
            logger.error("claude_init_failed", error=str(e))

    if not llm.providers:
        logger.warning(
            "no_llm_providers",
            hint="Откройте веб-панель и укажите GigaChat credentials в настройках",
        )

    # ─── Skills ──────────────────────────────────────────────
    # Определяем путь к навыкам: если указан абсолютный — используем его,
    # иначе ищем относительно пакета pyatnitsa/skills/examples/
    skills_path = settings.skills_dir
    if not os.path.isabs(skills_path):
        pkg_dir = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            os.path.join(pkg_dir, "skills", "examples"),  # pyatnitsa/skills/examples/
            os.path.join(pkg_dir, "skills"),               # pyatnitsa/skills/
            os.path.join(os.getcwd(), skills_path),        # cwd/skills/
        ]
        skills_path = next((c for c in candidates if os.path.isdir(c)), skills_path)

    skills = SkillLoader(skills_dir=skills_path)
    await skills.load_all()

    # ─── Agent ───────────────────────────────────────────────
    agent = Agent(llm=llm, skills=skills, memory=memory) if llm.providers else None

    # ─── Heartbeat ───────────────────────────────────────────
    heartbeat = Heartbeat(interval_minutes=settings.scheduler.heartbeat_interval_minutes)
    if settings.scheduler.enabled and agent:
        heartbeat.start()

    # ─── Inject dependencies в FastAPI ───────────────────────
    inject_dependencies(
        agent=agent,
        settings_store=settings_store,
        memory_store=memory,
    )

    # ─── Channels (опционально) ──────────────────────────────
    channels = []
    tasks = []

    max_token = settings.channels.max_bot_token or await settings_store.get("channels.max_bot_token")
    if max_token:
        try:
            max_channel = MaxChannel(
                token=max_token,
                use_polling=settings.channels.max_use_polling,
            )
            max_channel.on_message(agent.handle_message)
            channels.append(max_channel)
            tasks.append(asyncio.create_task(max_channel.start()))
            logger.info("channel_enabled", channel="max")
        except Exception as e:
            logger.warning("max_channel_failed", error=str(e))

    tg_token = settings.channels.telegram_bot_token or await settings_store.get("channels.telegram_bot_token")
    if tg_token and agent:
        try:
            tg_channel = TelegramChannel(
                token=tg_token,
                use_polling=settings.channels.telegram_use_polling,
            )
            tg_channel.on_message(agent.handle_message)
            channels.append(tg_channel)
            tasks.append(asyncio.create_task(tg_channel.start()))
            logger.info("channel_enabled", channel="telegram")
        except Exception as e:
            logger.warning("telegram_channel_failed", error=str(e))

    # ─── FastAPI (веб-интерфейс — всегда) ────────────────────
    api_config = uvicorn.Config(
        fastapi_app,
        host=settings.api_host,
        port=settings.api_port,
        log_level="warning",
    )
    api_server = uvicorn.Server(api_config)
    tasks.append(asyncio.create_task(api_server.serve()))

    web_url = f"http://{settings.api_host}:{settings.api_port}"
    if settings.api_host == "0.0.0.0":
        web_url = f"http://localhost:{settings.api_port}"

    logger.info(
        "pyatnitsa_ready",
        web=web_url,
        channels=[c.name for c in channels] or ["web"],
        skills=list(skills.skills.keys()),
        llm=[p.name for p in llm.providers] or ["not configured"],
    )

    if not llm.providers:
        logger.info("open_web_panel", url=f"{web_url}", hint="Откройте в браузере для настройки")

    # ─── Graceful shutdown ───────────────────────────────────
    stop_event = asyncio.Event()

    def handle_signal(*_):
        logger.info("shutdown_signal_received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_event_loop().add_signal_handler(sig, handle_signal)
        except NotImplementedError:
            signal.signal(sig, handle_signal)

    await stop_event.wait()

    # Cleanup
    logger.info("pyatnitsa_stopping")
    heartbeat.stop()
    api_server.should_exit = True
    for channel in channels:
        await channel.stop()
    await skills.unload_all()
    await memory.close()

    for task in tasks:
        task.cancel()

    logger.info("pyatnitsa_stopped")


def main():
    """CLI entry point."""
    print("""
    ╔═══════════════════════════════════════╗
    ║         🤖 Пятница.ai v0.1.0         ║
    ║   AI-агент для российского бизнеса    ║
    ╚═══════════════════════════════════════╝
    """)
    asyncio.run(run())


if __name__ == "__main__":
    main()
