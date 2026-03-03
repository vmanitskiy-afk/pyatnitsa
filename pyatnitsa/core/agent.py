"""Агент Пятница — главный оркестратор."""

from __future__ import annotations

from typing import Any

import structlog

from pyatnitsa.core.models import Message, Response, Event, ToolCall, ToolResult
from pyatnitsa.core.llm import LLMManager, LLMMessage, LLMTool
from pyatnitsa.skills.skills import SkillLoader
from pyatnitsa.memory.store import MemoryStore

logger = structlog.get_logger()

SYSTEM_PROMPT = """Ты — Пятница, персональный AI-ассистент для бизнеса. 
Ты помогаешь пользователю управлять задачами, проектами и бизнес-процессами.

Правила:
- Отвечай на русском языке
- Будь кратким и по делу
- Если нужно выполнить действие — используй доступные инструменты
- Если не знаешь ответ — скажи об этом, не выдумывай
- Запоминай важные факты о пользователе для будущих разговоров

{memory_context}

Доступные навыки:
{skills_context}
"""


class Agent:
    """Главный агент Пятница.ai."""
    
    def __init__(
        self,
        llm: LLMManager,
        skills: SkillLoader,
        memory: MemoryStore,
    ):
        self.llm = llm
        self.skills = skills
        self.memory = memory
        
        # Текущие разговоры (in-memory, per user)
        self._conversations: dict[str, list[LLMMessage]] = {}
        self._max_history = 20
    
    async def handle_message(self, message: Message) -> Response:
        """Обрабатывает входящее сообщение."""
        
        user_id = message.user_id
        logger.info("agent_message_received", user_id=user_id, channel=message.channel, text=message.text[:100] if message.text else "")
        
        # 1. Загружаем контекст памяти
        memory_context = await self.memory.build_context(user_id)
        
        # 2. Собираем инструменты навыков
        tools = self.skills.get_all_tools()
        skills_descriptions = "\n".join(
            f"- {s.name}: {s.description}" for s in self.skills.skills.values()
        )
        
        # 3. Строим system prompt
        system = SYSTEM_PROMPT.format(
            memory_context=memory_context or "Пока ничего не известно.",
            skills_context=skills_descriptions or "Навыки не загружены.",
        )
        
        # 4. Получаем историю разговора
        history = self._get_history(user_id)
        history.append(LLMMessage(role="user", content=message.text or ""))
        
        # 5. Вызываем LLM (с циклом tool use)
        response_text = await self._run_agent_loop(user_id, system, history, tools)
        
        # 6. Сохраняем в историю
        history.append(LLMMessage(role="assistant", content=response_text))
        self._trim_history(user_id)
        
        # 7. Извлекаем факты (async, в фоне)
        # TODO: await self._extract_facts(user_id, message.text, response_text)
        
        return Response(text=response_text)
    
    async def _run_agent_loop(
        self,
        user_id: str,
        system: str,
        messages: list[LLMMessage],
        tools: list[LLMTool],
        max_iterations: int = 5,
    ) -> str:
        """Цикл агента: LLM → tool call → result → LLM → ..."""
        
        for i in range(max_iterations):
            llm_response = await self.llm.complete(
                messages=messages,
                system=system,
                tools=tools if tools else None,
            )
            
            # Если нет tool calls — возвращаем текст
            if not llm_response.tool_calls:
                return llm_response.text or "🤔 Не могу сформулировать ответ."
            
            # Обрабатываем tool calls
            # Сначала добавляем ответ ассистента с tool_use
            assistant_content = []
            if llm_response.text:
                assistant_content.append({"type": "text", "text": llm_response.text})
            
            for tc in llm_response.tool_calls:
                assistant_content.append({
                    "type": "tool_use",
                    "id": tc.id,
                    "name": f"{tc.skill_name}.{tc.action}" if tc.action != "execute" else tc.skill_name,
                    "input": tc.params,
                })
            
            messages.append(LLMMessage(role="assistant", content=assistant_content))
            
            # Выполняем tool calls и добавляем результаты
            tool_results = []
            for tc in llm_response.tool_calls:
                result = await self.skills.execute_tool(tc.skill_name, tc.action, tc.params)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": result,
                })
            
            messages.append(LLMMessage(role="user", content=tool_results))
            
            logger.debug("agent_loop_iteration", iteration=i + 1, tool_calls=len(llm_response.tool_calls))
        
        return "⚠️ Достигнут лимит итераций. Попробуйте упростить запрос."
    
    async def handle_event(self, event: Event) -> Response | None:
        """Обрабатывает событие от heartbeat/scheduler."""
        
        logger.info("agent_event", type=event.type, source=event.source, title=event.title)
        
        # Формируем сообщение для LLM
        prompt = f"""Произошло событие, которое может быть важно для пользователя:
        
Тип: {event.type.value}
Источник: {event.source}
Заголовок: {event.title}
Описание: {event.description}

Если это важно — сформулируй краткое уведомление для пользователя.
Если не важно — ответь "SKIP"."""

        llm_response = await self.llm.complete(
            messages=[LLMMessage(role="user", content=prompt)],
        )
        
        if llm_response.text and "SKIP" not in llm_response.text.upper():
            return Response(text=f"🔔 {llm_response.text}")
        
        return None
    
    def _get_history(self, user_id: str) -> list[LLMMessage]:
        """Получает историю разговора."""
        if user_id not in self._conversations:
            self._conversations[user_id] = []
        return self._conversations[user_id]
    
    def _trim_history(self, user_id: str):
        """Обрезает историю до max_history сообщений."""
        history = self._conversations.get(user_id, [])
        if len(history) > self._max_history:
            self._conversations[user_id] = history[-self._max_history:]
