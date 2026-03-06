"""SubAgent -- универсальный специализированный агент.

Настраивается через AgentConfig (YAML / settings_store / веб-панель).
Один класс, без наследования — вся специализация через конфиг.
"""

from __future__ import annotations

from typing import Any

import structlog

from pyatnitsa.core.llm import LLMManager, LLMMessage, LLMTool, LLMResponse
from pyatnitsa.core.models import ToolCall
from pyatnitsa.skills.skills import SkillLoader

logger = structlog.get_logger()


class SubAgent:
    """Универсальный суб-агент, настраиваемый через конфиг."""

    def __init__(
        self,
        name: str,
        description: str,
        system_prompt: str,
        skill_names: list[str],
        skills: SkillLoader,
        llm: LLMManager,
        max_iterations: int = 8,
        temperature: float = 0.5,
        is_fallback: bool = False,
        enabled: bool = True,
    ):
        self.name = name
        self.description = description
        self.system_prompt = system_prompt
        self.skill_names = skill_names
        self.skills = skills
        self.llm = llm
        self.max_iterations = max_iterations
        self.temperature = temperature
        self.is_fallback = is_fallback
        self.enabled = enabled

    def get_tools(self) -> list[LLMTool]:
        """Возвращает только tools из разрешённых скиллов."""
        if not self.skill_names:
            return []
        all_tools = self.skills.get_all_tools()
        allowed = set(self.skill_names)
        return [t for t in all_tools if t.name.split(".")[0] in allowed]

    async def handle(self, task: str, context: str = "") -> str:
        """Выполняет задачу в собственном LLM-цикле.

        Args:
            task: описание задачи от Router'а (текст пользователя + контекст)
            context: дополнительный контекст (память, резюме и т.д.)
        """
        system = self.system_prompt
        if context:
            system = f"{system}\n\nКонтекст:\n{context}"

        tools = self.get_tools()
        messages = [LLMMessage(role="user", content=task)]

        logger.info("sub_agent_start", agent=self.name, task=task[:100],
                     tools=len(tools), max_iter=self.max_iterations)

        for i in range(self.max_iterations):
            llm_response = await self.llm.complete(
                messages=messages,
                system=system,
                tools=tools if tools else None,
                temperature=self.temperature,
            )

            if not llm_response.tool_calls:
                result = llm_response.text or "Не удалось сформулировать ответ."
                logger.info("sub_agent_done", agent=self.name,
                             iterations=i + 1, result_len=len(result))
                return result

            # Собираем assistant content с tool_use блоками
            assistant_content = []
            if llm_response.text:
                assistant_content.append({"type": "text", "text": llm_response.text})
            for tc in llm_response.tool_calls:
                tname = f"{tc.skill_name}.{tc.action}" if tc.action != "execute" else tc.skill_name
                assistant_content.append({
                    "type": "tool_use", "id": tc.id,
                    "name": tname, "input": tc.params,
                })
            messages.append(LLMMessage(role="assistant", content=assistant_content))

            # Выполняем tool calls
            tool_results = []
            for tc in llm_response.tool_calls:
                result = await self.skills.execute_tool(tc.skill_name, tc.action, tc.params)
                tool_results.append({
                    "type": "tool_result", "tool_use_id": tc.id, "content": result,
                })
                logger.debug("sub_agent_tool", agent=self.name,
                              skill=tc.skill_name, action=tc.action)

            messages.append(LLMMessage(role="user", content=tool_results))

        return f"[{self.name}] Достигнут лимит итераций ({self.max_iterations})."

    def to_router_description(self) -> str:
        """Краткое описание для Router prompt."""
        return f"- {self.name}: {self.description}"
