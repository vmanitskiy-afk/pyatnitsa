"""Каналы связи — абстракция над мессенджерами."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Callable, Awaitable

import structlog

from pyatnitsa.core.models import Message, Response, MessageRole

if TYPE_CHECKING:
    pass

logger = structlog.get_logger()

# Тип обработчика сообщений
MessageHandler = Callable[[Message], Awaitable[Response | None]]


class BaseChannel(ABC):
    """Абстрактный канал связи."""
    
    name: str = "base"
    
    def __init__(self):
        self._handler: MessageHandler | None = None
    
    def on_message(self, handler: MessageHandler):
        """Регистрирует обработчик входящих сообщений."""
        self._handler = handler
    
    @abstractmethod
    async def start(self):
        """Запускает канал (polling/webhook)."""
        ...
    
    @abstractmethod
    async def stop(self):
        """Останавливает канал."""
        ...
    
    @abstractmethod
    async def send(self, chat_id: str, response: Response):
        """Отправляет ответ в чат."""
        ...
    
    async def _dispatch(self, message: Message):
        """Передаёт сообщение обработчику."""
        if self._handler:
            try:
                response = await self._handler(message)
                if response and (response.text or response.attachments):
                    await self.send(message.chat_id, response)
            except Exception as e:
                logger.error("channel_dispatch_error", channel=self.name, error=str(e))
                await self.send(message.chat_id, Response(text="⚠️ Произошла ошибка. Попробуйте позже."))


# ─── MAX Messenger ───────────────────────────────────────────

class MaxChannel(BaseChannel):
    """Канал MAX мессенджер (через max-botapi-python).
    
    ВАЖНО: В MAX SDK баг — get_updates не передаёт marker обратно в API,
    из-за чего каждый poll возвращает одни и те же события.
    Фикс: monkey-patch GetUpdates.fetch для передачи marker.
    """
    
    name = "max"
    
    def __init__(self, token: str, use_polling: bool = True):
        super().__init__()
        self.token = token
        self.use_polling = use_polling
        self._bot = None
        self._dp = None
    
    async def start(self):
        """Запускает MAX бот — свой polling без SDK Dispatcher.
        
        MAX SDK (maxapi) имеет баги в polling:
        - Не передаёт marker в get_updates → дубли между poll-циклами
        - Dispatcher вызывает handler несколько раз на 1 event
        Поэтому реализуем polling напрямую через API.
        """
        import asyncio
        import aiohttp
        
        try:
            from maxapi import Bot
        except ImportError:
            logger.error("maxapi_not_installed", hint="pip install git+https://github.com/max-messenger/max-botapi-python.git")
            return
        
        API_URL = "https://botapi.max.ru"
        self._bot = Bot(self.token)
        self._bot_username = None
        self._bot_id = None
        
        # Получаем инфо бота
        try:
            me = await self._bot.get_me()
            self._bot_username = (me.username or "").lower()
            self._bot_id = me.user_id
            logger.info("max_bot_info", username=self._bot_username, bot_id=self._bot_id)
        except Exception as e:
            logger.error("max_bot_info_error", error=str(e)[:100])
            self._bot_username = ""
            self._bot_id = 0
        
        logger.info("max_channel_starting", polling=True)
        
        marker = None
        seen_mids: set[str] = set()
        
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    params = {"access_token": self.token, "limit": 100}
                    if marker is not None:
                        params["marker"] = marker
                    
                    async with session.get(f"{API_URL}/updates", params=params, timeout=aiohttp.ClientTimeout(total=35)) as resp:
                        if resp.status != 200:
                            logger.warning("max_poll_error", status=resp.status)
                            await asyncio.sleep(5)
                            continue
                        data = await resp.json()
                    
                    new_marker = data.get("marker")
                    updates = data.get("updates", [])
                    
                    if new_marker:
                        marker = new_marker
                    
                    for event in updates:
                        try:
                            if event.get("update_type") != "message_created":
                                continue
                            msg_data = event.get("message", {})
                            body = msg_data.get("body", {})
                            mid = body.get("mid")
                            
                            # Дедупликация
                            if mid and mid in seen_mids:
                                continue
                            if mid:
                                seen_mids.add(mid)
                            if len(seen_mids) > 2000:
                                seen_mids.clear()
                            
                            await self._handle_raw_event(msg_data, mid)
                        except Exception as e:
                            logger.error("max_event_error", error=str(e)[:200])
                
                except asyncio.TimeoutError:
                    continue
                except aiohttp.ClientConnectorError:
                    logger.warning("max_connection_error")
                    await asyncio.sleep(10)
                except Exception as e:
                    logger.error("max_poll_loop_error", error=str(e)[:200])
                    await asyncio.sleep(5)
    
    async def _handle_raw_event(self, msg_data: dict, mid: str):
        """Обрабатывает сырой event из MAX API."""
        body = msg_data.get("body", {})
        text = body.get("text")
        
        sender = msg_data.get("sender", {})
        user_id = str(sender.get("user_id", "unknown"))
        sender_name = (sender.get("first_name", "") + " " + sender.get("last_name", "")).strip()
        
        recipient = msg_data.get("recipient", {})
        chat_id = str(recipient.get("chat_id", "0"))
        chat_type = recipient.get("chat_type", "dialog")
        
        # Фильтрация в групповых чатах
        addressed = True
        if chat_type == "chat":
            txt = text or ""
            is_command = txt.startswith("/")
            is_mention = self._bot_username and f"@{self._bot_username}" in txt.lower()
            addressed = is_command or is_mention
        
        # Убираем @mention
        clean_text = text or ""
        if self._bot_username and f"@{self._bot_username}" in clean_text.lower():
            import re
            clean_text = re.sub(f"@{re.escape(self._bot_username)}", "", clean_text, flags=re.IGNORECASE).strip()
        
        # Скачиваем вложения
        from pyatnitsa.core.models import Attachment
        attachments = []
        for att in body.get("attachments", []):
            try:
                att_type = att.get("type", "")
                payload = att.get("payload", {})
                url = payload.get("url")
                token = payload.get("token")
                if not url:
                    continue
                
                import httpx
                download_url = f"{url}?token={token}" if token else url
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(download_url)
                    if resp.status_code != 200:
                        logger.warning("max_download_failed", status=resp.status_code)
                        continue
                    data = resp.content
                    content_type = resp.headers.get("content-type", "")
                
                fname = payload.get("file_name") or att.get("filename") or "file"
                if att_type in ("image", "IMAGE"):
                    mime = content_type.split(";")[0].strip() if content_type else "image/jpeg"
                    if fname == "file":
                        ext = mime.split("/")[-1] if "/" in mime else "jpg"
                        fname = f"image.{ext}"
                    attachments.append(Attachment(type="image", data=data, filename=fname, mime_type=mime))
                else:
                    mime = content_type.split(";")[0].strip() if content_type else None
                    attachments.append(Attachment(type="file", data=data, filename=fname, mime_type=mime))
            except Exception as e:
                logger.warning("max_attachment_error", error=str(e)[:120])
        
        msg = Message(
            id=mid or str(uuid.uuid4()),
            channel=self.name,
            user_id=user_id,
            chat_id=chat_id,
            text=clean_text,
            attachments=attachments,
            listen_only=not addressed,
            role=MessageRole.USER,
            raw={"sender_name": sender_name},
        )
        await self._dispatch(msg)
        
        logger.info("max_channel_starting", polling=self.use_polling)
        
        if self.use_polling:
            await self._dp.start_polling(self._bot)
    
    async def stop(self):
        if self._dp:
            await self._dp.stop_polling()
    
    async def send(self, chat_id: str, response: Response):
        if not self._bot:
            return
        if response.text:
            await self._bot.send_message(chat_id=int(chat_id), text=response.text)
        for att in response.attachments:
            try:
                if att.data:
                    from io import BytesIO
                    buf = BytesIO(att.data)
                    buf.name = att.filename or "file"
                    await self._bot.send_file(chat_id=int(chat_id), file=buf)
                elif att.url and att.url.startswith("/") and hasattr(self, '_file_store') and self._file_store:
                    url_parts = att.url.split("/")
                    if len(url_parts) >= 4:
                        result = await self._file_store.get_file_data(url_parts[3])
                        if result:
                            from io import BytesIO
                            buf = BytesIO(result[0])
                            buf.name = result[2]
                            await self._bot.send_file(chat_id=int(chat_id), file=buf)
            except Exception as e:
                logger.error("max_send_file_error", error=str(e))


# ─── Telegram ────────────────────────────────────────────────

class TelegramChannel(BaseChannel):
    """Канал Telegram (через aiogram 3)."""
    
    name = "telegram"
    
    def __init__(self, token: str, use_polling: bool = True):
        super().__init__()
        self.token = token
        self.use_polling = use_polling
        self._bot = None
        self._dp = None
    
    async def start(self):
        from aiogram import Bot, Dispatcher, types
        
        self._bot = Bot(token=self.token)
        self._dp = Dispatcher()
        
        # Кеш username бота
        self._bot_username = None
        self._bot_id = None

        @self._dp.message()
        async def on_message(tg_message: types.Message):
            # Кешируем info бота (один раз)
            if self._bot_username is None:
                me = await self._bot.me()
                self._bot_username = (me.username or "").lower()
                self._bot_id = me.id

            # В группах отвечаем только на команды, @упоминания и реплаи
            if tg_message.chat.type in ("group", "supergroup"):
                txt = tg_message.text or ""
                is_command = txt.startswith("/")
                is_reply_to_bot = (
                    tg_message.reply_to_message
                    and tg_message.reply_to_message.from_user
                    and tg_message.reply_to_message.from_user.id == self._bot_id
                )
                is_mention = self._bot_username and f"@{self._bot_username}" in txt.lower()
                if not is_mention and tg_message.entities:
                    for ent in tg_message.entities:
                        if ent.type == "mention":
                            m_text = txt[ent.offset:ent.offset + ent.length].lower()
                            if m_text == f"@{self._bot_username}":
                                is_mention = True
                                break
                if not (is_command or is_reply_to_bot or is_mention):
                    addressed = False
                else:
                    addressed = True
            else:
                addressed = True

            # Убираем @mention из текста
            raw_text = tg_message.text or tg_message.caption or ""
            clean_text = raw_text.replace(f"@{self._bot_username}", "").strip() if self._bot_username else raw_text

            from pyatnitsa.core.models import Attachment
            attachments = []
            tg_file = tg_message.document or (tg_message.photo[-1] if tg_message.photo else None)
            if tg_file:
                try:
                    file_info = await self._bot.get_file(tg_file.file_id)
                    result = await self._bot.download_file(file_info.file_path)
                    data = result.read() if hasattr(result, "read") else result
                    fname = getattr(tg_file, "file_name", None)
                    mime = getattr(tg_file, "mime_type", None)
                    if tg_message.photo:
                        # PhotoSize не имеет mime_type/file_name — определяем из file_path
                        ext = (file_info.file_path or "jpg").rsplit(".", 1)[-1] or "jpg"
                        if not mime:
                            mime = "image/jpeg" if ext == "jpg" else f"image/{ext}"
                        fname = fname or f"photo_{tg_file.file_id}.{ext}"
                    else:
                        fname = fname or f"file_{tg_file.file_id}"
                    att_type = "image" if tg_message.photo else "file"
                    attachments.append(Attachment(type=att_type, data=data, filename=fname, mime_type=mime))
                except Exception as e:
                    logger.warning("tg_download_error", error=str(e))
            msg = Message(
                id=str(tg_message.message_id),
                channel=self.name,
                user_id=str(tg_message.from_user.id) if tg_message.from_user else "unknown",
                chat_id=str(tg_message.chat.id),
                text=clean_text,
                attachments=attachments,
                listen_only=not addressed,
                raw={"sender_name": ((tg_message.from_user.first_name or "") + " " + (tg_message.from_user.last_name or "")).strip() if tg_message.from_user else ""},
                role=MessageRole.USER,
            )
            await self._dispatch(msg)
        
        logger.info("telegram_channel_starting", polling=self.use_polling)
        
        if self.use_polling:
            await self._dp.start_polling(self._bot)
    
    async def stop(self):
        if self._dp:
            await self._dp.stop_polling()
        if self._bot:
            await self._bot.session.close()
    
    async def send(self, chat_id: str, response: Response):
        if not self._bot:
            return
        if response.text:
            text = response.text
            while text:
                chunk, text = text[:4096], text[4096:]
                await self._bot.send_message(chat_id=int(chat_id), text=chunk)
        # Отправка вложений
        for att in response.attachments:
            try:
                from aiogram.types import FSInputFile, BufferedInputFile
                if att.data:
                    f = BufferedInputFile(att.data, filename=att.filename or "file")
                elif att.url and att.url.startswith("/"):
                    # Локальный файл через file_store
                    from pathlib import Path
                    path = Path(att.url.lstrip("/"))
                    if not path.exists() and hasattr(self, '_file_store') and self._file_store:
                        url_parts = att.url.split("/")
                        if len(url_parts) >= 4:
                            result = await self._file_store.get_file_data(url_parts[3])
                            if result:
                                f = BufferedInputFile(result[0], filename=result[2])
                            else:
                                continue
                    elif path.exists():
                        f = FSInputFile(str(path), filename=att.filename)
                    else:
                        continue
                else:
                    continue
                if att.type == "image" or (att.mime_type or "").startswith("image/"):
                    await self._bot.send_photo(chat_id=int(chat_id), photo=f)
                else:
                    await self._bot.send_document(chat_id=int(chat_id), document=f)
            except Exception as e:
                logger.error("tg_send_file_error", error=str(e))
