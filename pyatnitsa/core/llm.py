"""Абстракция над LLM провайдерами с failover.

Основной провайдер: GigaChat (Сбер)
Fallback: Claude (Anthropic) — опционально
"""

from __future__ import annotations

import json
import asyncio
from abc import ABC, abstractmethod
from typing import Any

import structlog

from pyatnitsa.core.models import ToolCall

logger = structlog.get_logger()


# ─── Типы ────────────────────────────────────────────────────

class LLMMessage:
    """Сообщение для LLM."""
    def __init__(self, role: str, content: str | list[dict]):
        self.role = role
        self.content = content
    
    def to_dict(self) -> dict:
        return {"role": self.role, "content": self.content}


class LLMTool:
    """Описание инструмента для LLM."""
    def __init__(self, name: str, description: str, parameters: dict[str, Any]):
        self.name = name
        self.description = description
        self.parameters = parameters
    
    def to_gigachat(self) -> dict:
        """Конвертирует в формат GigaChat Function."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }
    
    def to_anthropic(self) -> dict:
        """Конвертирует в формат Anthropic tool."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }


class LLMResponse:
    """Ответ от LLM."""
    def __init__(
        self,
        text: str | None = None,
        tool_calls: list[ToolCall] | None = None,
        usage: dict[str, int] | None = None,
        stop_reason: str | None = None,
    ):
        self.text = text
        self.tool_calls = tool_calls or []
        self.usage = usage or {}
        self.stop_reason = stop_reason


# ─── Базовый провайдер ──────────────────────────────────────

class LLMProvider(ABC):
    """Абстрактный LLM провайдер."""
    
    name: str = "base"
    
    @abstractmethod
    async def complete(
        self,
        messages: list[LLMMessage],
        system: str | None = None,
        tools: list[LLMTool] | None = None,
        temperature: float = 0.7,
    ) -> LLMResponse:
        ...
    
    @abstractmethod
    async def health_check(self) -> bool:
        ...


# ─── GigaChat (основной) ────────────────────────────────────

class GigaChatProvider(LLMProvider):
    """Сбер GigaChat API — основной провайдер.
    
    Модели 2-го поколения:
      - GigaChat-2       (Lite, бесплатный лимит)
      - GigaChat-2-Pro   (повседневные задачи)
      - GigaChat-2-Max   (сложные задачи, топовая)
    
    Авторизация: OAuth credentials (base64 client_id:client_secret)
    Scope: GIGACHAT_API_PERS (физлица) / GIGACHAT_API_B2B (бизнес) / GIGACHAT_API_CORP (корп)
    """
    
    name = "gigachat"
    
    def __init__(
        self,
        credentials: str,
        model: str = "GigaChat-2-Max",
        scope: str = "GIGACHAT_API_PERS",
        verify_ssl: bool = False,
        ca_bundle_file: str | None = None,
        max_tokens: int = 4096,
    ):
        from gigachat import GigaChat
        
        kwargs: dict[str, Any] = {
            "credentials": credentials,
            "model": model,
            "scope": scope,
            "verify_ssl_certs": verify_ssl,
            "timeout": 60,
        }
        if ca_bundle_file:
            kwargs["ca_bundle_file"] = ca_bundle_file
        
        self.client = GigaChat(**kwargs)
        self.model = model
        self.max_tokens = max_tokens
        self.scope = scope
        
        logger.info("gigachat_provider_init", model=model, scope=scope)
    
    async def complete(
        self,
        messages: list[LLMMessage],
        system: str | None = None,
        tools: list[LLMTool] | None = None,
        temperature: float = 0.7,
    ) -> LLMResponse:
        from gigachat.models import (
            Chat, Messages, MessagesRole,
            Function, FunctionParameters,
        )
        
        # Собираем сообщения
        gc_messages = []
        
        # System prompt → первое сообщение с ролью system
        if system:
            gc_messages.append(Messages(role=MessagesRole.SYSTEM, content=system))
        
        # Остальные сообщения
        for msg in messages:
            role = {
                "user": MessagesRole.USER,
                "assistant": MessagesRole.ASSISTANT,
                "system": MessagesRole.SYSTEM,
            }.get(msg.role, MessagesRole.USER)
            
            # GigaChat принимает content только как строку
            content = msg.content
            gc_attachments = None
            if isinstance(content, list):
                text_parts = []
                image_ids = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(block["text"])
                        elif block.get("type") == "tool_result":
                            text_parts.append(f"Результат: {block.get('content', '')}")
                        elif block.get("type") == "image":
                            try:
                                import base64 as b64mod
                                src = block.get("source", {})
                                img_data = b64mod.b64decode(src.get("data", ""))
                                mime = src.get("media_type", "image/jpeg")
                                logger.info("gigachat_image_prep", original_mime=mime, data_len=len(img_data))
                                # GigaChat капризен к формату — прогоняем через PIL для валидности
                                converted = False
                                try:
                                    from PIL import Image as PILImage
                                    import io
                                    pil_img = PILImage.open(io.BytesIO(img_data))
                                    logger.info("pil_image_opened", format=pil_img.format, mode=pil_img.mode, size=pil_img.size)
                                    if pil_img.mode in ("RGBA", "LA", "P"):
                                        pil_img = pil_img.convert("RGB")
                                    buf = io.BytesIO()
                                    pil_img.save(buf, format="JPEG", quality=85)
                                    img_data = buf.getvalue()
                                    mime = "image/jpeg"
                                    converted = True
                                except ImportError:
                                    logger.warning("pillow_not_installed", hint="pip install Pillow")
                                except Exception as conv_err:
                                    logger.warning("image_convert_err", error=str(conv_err)[:200], original_mime=mime)
                                if not converted and mime not in ("image/jpeg", "image/png", "image/gif"):
                                    text_parts.append("[Картинка — формат не поддерживается GigaChat]")
                                    continue
                                uploaded = None
                                for _upl_attempt in range(3):
                                    try:
                                        if _upl_attempt > 0:
                                            await asyncio.sleep(2 ** _upl_attempt)
                                        uploaded = await asyncio.get_event_loop().run_in_executor(
                                            None, self.client.upload_file,
                                            (f"image.jpg", img_data, mime)
                                        )
                                        break
                                    except Exception as ue:
                                        if "429" in str(ue) and _upl_attempt < 2:
                                            continue
                                        raise
                                image_ids.append(uploaded.id_)
                                logger.info("gigachat_image_uploaded", file_id=uploaded.id_)
                            except Exception as e:
                                logger.warning("gigachat_image_upload_err", error=str(e)[:200])
                                text_parts.append("[Картинка — не удалось загрузить]")
                        elif block.get("type") == "tool_use":
                            pass
                content = "\n".join(text_parts) if text_parts else str(content)
                if image_ids:
                    gc_attachments = image_ids
            
            if content:  # не добавляем пустые сообщения
                m_kwargs = {"role": role, "content": content}
                if gc_attachments:
                    m_kwargs["attachments"] = gc_attachments
                gc_messages.append(Messages(**m_kwargs))
        
        # Собираем функции (tools)
        gc_functions = None
        if tools:
            gc_functions = []
            for tool in tools:
                gc_functions.append(Function(
                    name=tool.name,
                    description=tool.description,
                    parameters=FunctionParameters(
                        type=tool.parameters.get("type", "object"),
                        properties=tool.parameters.get("properties", {}),
                        required=tool.parameters.get("required", []),
                    ),
                ))
        
        # Формируем запрос
        chat = Chat(
            messages=gc_messages,
            functions=gc_functions,
            temperature=temperature,
            max_tokens=self.max_tokens,
        )
        
        # Пауза после загрузки картинок чтобы не словить 429
        if any(getattr(m, 'attachments', None) for m in gc_messages):
            await asyncio.sleep(1)
        
        # GigaChat SDK синхронный — оборачиваем в asyncio с retry для 429
        loop = asyncio.get_event_loop()
        last_err = None
        for attempt in range(3):
            try:
                if attempt > 0:
                    await asyncio.sleep(2 ** attempt)  # 2s, 4s
                    logger.info("gigachat_retry", attempt=attempt + 1)
                response = await loop.run_in_executor(None, self.client.chat, chat)
                break
            except Exception as e:
                last_err = e
                if "429" in str(e):
                    continue
                raise
        else:
            raise last_err
        
        # Парсим ответ
        choice = response.choices[0]
        message = choice.message
        finish_reason = choice.finish_reason
        
        text = message.content if message.content else None
        tool_calls = []
        
        # Обработка function_call
        if finish_reason == "function_call" and message.function_call:
            fc = message.function_call
            # Парсим имя: "redmine.my_tasks" → skill="redmine", action="my_tasks"
            parts = fc.name.split(".", 1)
            skill_name = parts[0]
            action = parts[1] if len(parts) > 1 else "execute"
            
            # Парсим аргументы
            params = {}
            if fc.arguments:
                if isinstance(fc.arguments, str):
                    try:
                        params = json.loads(fc.arguments)
                    except json.JSONDecodeError:
                        params = {"raw": fc.arguments}
                elif isinstance(fc.arguments, dict):
                    params = fc.arguments
            
            tool_calls.append(ToolCall(
                id=f"gc_{fc.name}_{id(fc)}",
                skill_name=skill_name,
                action=action,
                params=params,
            ))
        
        # Usage
        usage = {}
        if response.usage:
            usage = {
                "input_tokens": response.usage.prompt_tokens or 0,
                "output_tokens": response.usage.completion_tokens or 0,
                "total_tokens": response.usage.total_tokens or 0,
            }
        
        return LLMResponse(
            text=text,
            tool_calls=tool_calls,
            usage=usage,
            stop_reason=finish_reason,
        )
    
    async def health_check(self) -> bool:
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None, self.client.chat, "ping"
            )
            return bool(response.choices)
        except Exception as e:
            logger.error("gigachat_health_check_failed", error=str(e))
            return False


# ─── Claude (опциональный fallback) ─────────────────────────

class ClaudeProvider(LLMProvider):
    """Anthropic Claude API — опциональный fallback."""
    
    name = "claude"
    
    def __init__(self, api_key: str, model: str = "claude-sonnet-4-20250514", max_tokens: int = 4096):
        import anthropic
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model
        self.max_tokens = max_tokens
    
    async def complete(
        self,
        messages: list[LLMMessage],
        system: str | None = None,
        tools: list[LLMTool] | None = None,
        temperature: float = 0.7,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [m.to_dict() for m in messages],
            "temperature": temperature,
        }
        
        if system:
            kwargs["system"] = system
        
        if tools:
            kwargs["tools"] = [t.to_anthropic() for t in tools]
        
        response = await self.client.messages.create(**kwargs)
        
        text_parts = []
        tool_calls = []
        
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                parts = block.name.split(".", 1)
                tool_calls.append(ToolCall(
                    id=block.id,
                    skill_name=parts[0],
                    action=parts[1] if len(parts) > 1 else "execute",
                    params=block.input,
                ))
        
        return LLMResponse(
            text="\n".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            usage={
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
            stop_reason=response.stop_reason,
        )
    
    async def health_check(self) -> bool:
        try:
            await self.client.messages.create(
                model=self.model,
                max_tokens=10,
                messages=[{"role": "user", "content": "ping"}],
            )
            return True
        except Exception as e:
            logger.error("claude_health_check_failed", error=str(e))
            return False


# ─── LLM Manager (failover) ─────────────────────────────────

class LLMManager:
    """Менеджер LLM с failover между провайдерами."""
    
    def __init__(self):
        self.providers: list[LLMProvider] = []
    
    def add_provider(self, provider: LLMProvider):
        self.providers.append(provider)
        logger.info("llm_provider_added", provider=provider.name)
    
    async def complete(
        self,
        messages: list[LLMMessage],
        system: str | None = None,
        tools: list[LLMTool] | None = None,
        temperature: float = 0.7,
    ) -> LLMResponse:
        """Пробует провайдеров по очереди с failover."""
        
        last_error = None
        
        for provider in self.providers:
            try:
                logger.debug("llm_trying_provider", provider=provider.name)
                response = await provider.complete(messages, system, tools, temperature)
                logger.info(
                    "llm_complete",
                    provider=provider.name,
                    usage=response.usage,
                    has_tools=len(response.tool_calls) > 0,
                )
                return response
            except Exception as e:
                logger.warning(
                    "llm_provider_failed",
                    provider=provider.name,
                    error=str(e),
                )
                last_error = e
                continue
        
        raise RuntimeError(f"Все LLM провайдеры недоступны. Последняя ошибка: {last_error}")
