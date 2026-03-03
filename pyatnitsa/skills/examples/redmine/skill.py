"""Навык EasyRedmine для Пятница.ai."""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from pyatnitsa.skills.skills import BaseSkill
from pyatnitsa.core.llm import LLMTool

logger = structlog.get_logger()


class RedmineSkill(BaseSkill):
    """Интеграция с EasyRedmine."""
    
    name = "redmine"
    description = "Управление проектами и задачами в EasyRedmine"
    version = "0.1.0"
    
    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.base_url = ""
        self.api_key = ""
        self._client: httpx.AsyncClient | None = None
    
    async def on_load(self):
        """Инициализация при загрузке навыка."""
        import os
        self.base_url = os.getenv("REDMINE_URL", "").rstrip("/")
        self.api_key = os.getenv("REDMINE_API_KEY", "")
        
        if self.base_url and self.api_key:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"X-Redmine-API-Key": self.api_key},
                timeout=30.0,
            )
            logger.info("redmine_skill_loaded", url=self.base_url)
        else:
            logger.warning("redmine_skill_no_config", hint="Set REDMINE_URL and REDMINE_API_KEY")
    
    async def on_unload(self):
        if self._client:
            await self._client.aclose()
    
    def get_tools(self) -> list[LLMTool]:
        return [
            LLMTool(
                name="redmine.my_tasks",
                description="Показывает задачи пользователя в EasyRedmine",
                parameters={
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": ["open", "closed", "all"],
                            "description": "Фильтр по статусу",
                            "default": "open",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Количество задач",
                            "default": 10,
                        },
                    },
                },
            ),
            LLMTool(
                name="redmine.project_status",
                description="Показывает краткий отчёт по проекту в EasyRedmine",
                parameters={
                    "type": "object",
                    "properties": {
                        "project": {
                            "type": "string",
                            "description": "Название или идентификатор проекта",
                        },
                    },
                    "required": ["project"],
                },
            ),
            LLMTool(
                name="redmine.create_task",
                description="Создаёт задачу в проекте EasyRedmine",
                parameters={
                    "type": "object",
                    "properties": {
                        "project": {
                            "type": "string",
                            "description": "Название или ID проекта",
                        },
                        "subject": {
                            "type": "string",
                            "description": "Тема задачи",
                        },
                        "description": {
                            "type": "string",
                            "description": "Описание задачи",
                            "default": "",
                        },
                        "assigned_to": {
                            "type": "string",
                            "description": "Кому назначить (ФИО или часть фамилии)",
                            "default": "",
                        },
                        "priority": {
                            "type": "string",
                            "enum": ["low", "normal", "high", "urgent"],
                            "default": "normal",
                        },
                    },
                    "required": ["project", "subject"],
                },
            ),
        ]
    
    async def execute(self, action: str, params: dict[str, Any]) -> str:
        if not self._client:
            return "❌ Redmine не настроен. Укажите REDMINE_URL и REDMINE_API_KEY."
        
        match action:
            case "my_tasks":
                return await self._get_my_tasks(params)
            case "project_status":
                return await self._get_project_status(params)
            case "create_task":
                return await self._create_task(params)
            case _:
                return f"❌ Неизвестное действие: {action}"
    
    async def _get_my_tasks(self, params: dict) -> str:
        """Получает задачи текущего пользователя."""
        status = params.get("status", "open")
        limit = params.get("limit", 10)
        
        try:
            query = f"/issues.json?assigned_to_id=me&limit={limit}"
            if status == "open":
                query += "&status_id=open"
            elif status == "closed":
                query += "&status_id=closed"
            
            resp = await self._client.get(query)
            resp.raise_for_status()
            data = resp.json()
            
            issues = data.get("issues", [])
            if not issues:
                return "✅ У вас нет открытых задач."
            
            lines = [f"📋 Ваши задачи ({len(issues)}):\n"]
            for issue in issues:
                priority = issue.get("priority", {}).get("name", "")
                project = issue.get("project", {}).get("name", "")
                status_name = issue.get("status", {}).get("name", "")
                lines.append(
                    f"• #{issue['id']} [{project}] {issue['subject']} "
                    f"({status_name}, {priority})"
                )
            
            return "\n".join(lines)
            
        except Exception as e:
            logger.error("redmine_my_tasks_error", error=str(e))
            return f"❌ Ошибка получения задач: {e}"
    
    async def _get_project_status(self, params: dict) -> str:
        """Краткий отчёт по проекту."""
        project = params.get("project", "")
        
        try:
            # Ищем проект
            resp = await self._client.get(f"/projects.json?limit=100")
            resp.raise_for_status()
            projects = resp.json().get("projects", [])
            
            # Fuzzy match по названию
            matched = None
            project_lower = project.lower()
            for p in projects:
                if project_lower in p["name"].lower() or project_lower == str(p["id"]):
                    matched = p
                    break
            
            if not matched:
                return f"❌ Проект '{project}' не найден."
            
            # Получаем задачи проекта
            resp = await self._client.get(
                f"/issues.json?project_id={matched['id']}&status_id=open&limit=100"
            )
            resp.raise_for_status()
            issues = resp.json().get("issues", [])
            
            total = len(issues)
            by_priority = {}
            for issue in issues:
                p = issue.get("priority", {}).get("name", "Normal")
                by_priority[p] = by_priority.get(p, 0) + 1
            
            report = [
                f"📊 Проект: {matched['name']}",
                f"Открытых задач: {total}",
            ]
            
            if by_priority:
                report.append("По приоритету:")
                for priority, count in sorted(by_priority.items()):
                    report.append(f"  • {priority}: {count}")
            
            return "\n".join(report)
            
        except Exception as e:
            logger.error("redmine_project_status_error", error=str(e))
            return f"❌ Ошибка: {e}"
    
    async def _create_task(self, params: dict) -> str:
        """Создаёт задачу в проекте."""
        # TODO: реализовать с fuzzy matching пользователей (как в нашем Redmine-скрипте)
        return "🚧 Создание задач через бот в разработке."
