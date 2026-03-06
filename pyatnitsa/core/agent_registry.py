"""AgentRegistry -- реестр суб-агентов.

Загружает конфигурации агентов из YAML-файла и/или settings_store.
Поддерживает hot-reload без перезапуска.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from pyatnitsa.core.sub_agent import SubAgent
from pyatnitsa.core.llm import LLMManager
from pyatnitsa.skills.skills import SkillLoader

logger = structlog.get_logger()


class AgentConfig:
    """Конфигурация одного агента (десериализация из YAML/dict)."""

    def __init__(self, data: dict[str, Any]):
        self.id = data.get("id", data.get("name", "unnamed"))
        self.name = data.get("name", self.id)
        self.description = data.get("description", "")
        self.system_prompt = data.get("system_prompt", "")
        self.skills = data.get("skills", [])
        self.max_iterations = data.get("max_iterations", 8)
        self.temperature = data.get("temperature", 0.5)
        self.is_fallback = data.get("is_fallback", False)
        self.enabled = data.get("enabled", True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "system_prompt": self.system_prompt,
            "skills": self.skills,
            "max_iterations": self.max_iterations,
            "temperature": self.temperature,
            "is_fallback": self.is_fallback,
            "enabled": self.enabled,
        }


class AgentRegistry:
    """Реестр суб-агентов с загрузкой из YAML и settings_store."""

    def __init__(self, skills: SkillLoader, llm: LLMManager):
        self.skills = skills
        self.llm = llm
        self.agents: dict[str, SubAgent] = {}
        self._configs: dict[str, AgentConfig] = {}

    def _build_agent(self, config: AgentConfig) -> SubAgent:
        return SubAgent(
            name=config.id,
            description=config.description,
            system_prompt=config.system_prompt,
            skill_names=config.skills,
            skills=self.skills,
            llm=self.llm,
            max_iterations=config.max_iterations,
            temperature=config.temperature,
            is_fallback=config.is_fallback,
            enabled=config.enabled,
        )

    def register(self, config: AgentConfig) -> SubAgent:
        """Регистрирует агента из конфига."""
        agent = self._build_agent(config)
        self.agents[config.id] = agent
        self._configs[config.id] = config
        logger.info("agent_registered", agent=config.id,
                     skills=config.skills, fallback=config.is_fallback)
        return agent

    def unregister(self, agent_id: str):
        self.agents.pop(agent_id, None)
        self._configs.pop(agent_id, None)

    def get(self, agent_id: str) -> SubAgent | None:
        agent = self.agents.get(agent_id)
        if agent and agent.enabled:
            return agent
        return None

    def get_fallback(self) -> SubAgent | None:
        for agent in self.agents.values():
            if agent.is_fallback and agent.enabled:
                return agent
        return None

    def list_active(self) -> list[SubAgent]:
        return [a for a in self.agents.values() if a.enabled]

    def list_configs(self) -> list[dict[str, Any]]:
        return [c.to_dict() for c in self._configs.values()]

    def router_descriptions(self) -> str:
        """Текст для system prompt Router'а — список доступных агентов."""
        lines = []
        for agent in self.list_active():
            lines.append(f"- {agent.name}: {agent.description}")
        return "\n".join(lines)

    # ── Загрузка из YAML ──

    def load_from_yaml(self, path: str | Path):
        """Загружает агентов из YAML-файла."""
        import yaml

        path = Path(path)
        if not path.exists():
            logger.warning("agents_yaml_not_found", path=str(path))
            return

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        agents_data = data.get("agents", {})
        count = 0
        for agent_id, agent_data in agents_data.items():
            agent_data["id"] = agent_id
            config = AgentConfig(agent_data)
            if config.enabled:
                self.register(config)
                count += 1

        logger.info("agents_loaded_from_yaml", path=str(path), count=count)

    # ── Загрузка из settings_store (веб-панель) ──

    async def load_from_settings(self, store):
        """Загружает агентов из settings_store (ключи agents.*)."""
        import json

        raw = await store.get("agents.registry")
        if not raw:
            return

        try:
            agents_data = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            logger.warning("agents_settings_parse_error")
            return

        if isinstance(agents_data, dict):
            agents_data = agents_data.get("agents", agents_data)

        count = 0
        for agent_id, agent_data in agents_data.items():
            agent_data["id"] = agent_id
            config = AgentConfig(agent_data)
            if config.enabled:
                self.register(config)
                count += 1

        logger.info("agents_loaded_from_settings", count=count)

    async def save_to_settings(self, store):
        """Сохраняет текущие конфиги в settings_store."""
        import json

        data = {cid: c.to_dict() for cid, c in self._configs.items()}
        await store.set("agents.registry", json.dumps(data, ensure_ascii=False))

    # ── Hot reload ──

    def reload_from_yaml(self, path: str | Path):
        """Перезагружает агентов из YAML без перезапуска."""
        self.agents.clear()
        self._configs.clear()
        self.load_from_yaml(path)
