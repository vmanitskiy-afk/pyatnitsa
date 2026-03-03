"""Система навыков Пятница.ai."""

from __future__ import annotations

import importlib.util
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import structlog

from pyatnitsa.core.llm import LLMTool

logger = structlog.get_logger()


class BaseSkill(ABC):
    """Базовый класс навыка."""
    
    name: str = "base"
    description: str = ""
    version: str = "0.1.0"
    
    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
    
    @abstractmethod
    def get_tools(self) -> list[LLMTool]:
        """Возвращает список инструментов для LLM."""
        ...
    
    @abstractmethod
    async def execute(self, action: str, params: dict[str, Any]) -> str:
        """Выполняет действие навыка."""
        ...
    
    async def on_load(self):
        """Вызывается при загрузке навыка."""
        pass
    
    async def on_unload(self):
        """Вызывается при выгрузке навыка."""
        pass


class SkillLoader:
    """Загрузчик навыков из файловой системы."""
    
    def __init__(self, skills_dir: str = "skills"):
        self.skills_dir = Path(skills_dir)
        self.skills: dict[str, BaseSkill] = {}
    
    async def load_all(self) -> dict[str, BaseSkill]:
        """Загружает все навыки из директории skills/ (рекурсивно).

        Структура:
            skills/
            ├── browser/
            │   ├── browser.py   ← BrowserSkill
            │   └── SKILL.md
            ├── examples/
            │   ├── redmine/
            │   │   ├── redmine.py   ← RedmineSkill
            │   │   └── SKILL.md
        """
        if not self.skills_dir.exists():
            logger.warning("skills_dir_not_found", path=str(self.skills_dir))
            return self.skills

        await self._scan_dir(self.skills_dir)
        logger.info("skills_loaded", count=len(self.skills), names=list(self.skills.keys()))
        return self.skills

    async def _scan_dir(self, directory: Path):
        """Рекурсивно сканирует директорию на наличие навыков."""
        for entry in sorted(directory.iterdir()):
            if not entry.is_dir() or entry.name.startswith(("_", ".")):
                continue

            # Проверяем есть ли .py файлы с BaseSkill в этой папке
            py_files = list(entry.glob("*.py"))
            has_skill_files = any(
                not f.name.startswith("_") for f in py_files
            )

            if has_skill_files:
                await self._load_skill(entry)
            else:
                # Это промежуточная папка (examples/, custom/...) — идём глубже
                await self._scan_dir(entry)
    
    async def _load_skill(self, skill_dir: Path):
        """Загружает один навык из директории.

        Сканирует все .py файлы в папке навыка и ищет первый класс-
        наследник BaseSkill. Файл может называться как угодно:
        redmine.py, browser.py, и т.д.
        """
        # Собираем все .py файлы в папке
        py_files = sorted(skill_dir.glob("*.py"))
        if not py_files:
            logger.warning("no_py_files", skill_dir=str(skill_dir))
            return

        skill_md = skill_dir / "SKILL.md"

        for skill_py in py_files:
            # Пропускаем __init__.py, тесты и т.п.
            if skill_py.name.startswith("_"):
                continue

            try:
                spec = importlib.util.spec_from_file_location(
                    f"skills.{skill_dir.name}.{skill_py.stem}", str(skill_py)
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                # Ищем класс-наследник BaseSkill
                skill_class = None
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (
                        isinstance(attr, type)
                        and issubclass(attr, BaseSkill)
                        and attr is not BaseSkill
                    ):
                        skill_class = attr
                        break

                if skill_class is None:
                    continue  # этот .py не содержит навык — пробуем следующий

                # Читаем SKILL.md если есть
                skill_description = ""
                if skill_md.exists():
                    skill_description = skill_md.read_text(encoding="utf-8")

                # Создаём экземпляр
                skill = skill_class(config={"description_md": skill_description})
                await skill.on_load()

                self.skills[skill.name] = skill
                logger.info("skill_loaded", name=skill.name, version=skill.version,
                            file=skill_py.name)
                return  # нашли навык — выходим

            except Exception as e:
                logger.error("skill_load_error", file=str(skill_py), error=str(e))

        logger.warning("no_skill_class_found", skill_dir=str(skill_dir))
    
    def get_all_tools(self) -> list[LLMTool]:
        """Собирает инструменты со всех навыков."""
        tools = []
        for skill in self.skills.values():
            tools.extend(skill.get_tools())
        return tools
    
    async def execute_tool(self, skill_name: str, action: str, params: dict) -> str:
        """Выполняет инструмент навыка."""
        skill = self.skills.get(skill_name)
        if not skill:
            return f"❌ Навык '{skill_name}' не найден"
        
        try:
            result = await skill.execute(action, params)
            logger.info("skill_executed", skill=skill_name, action=action)
            return result
        except Exception as e:
            logger.error("skill_execution_error", skill=skill_name, action=action, error=str(e))
            return f"❌ Ошибка выполнения {skill_name}.{action}: {e}"
    
    async def unload_all(self):
        """Выгружает все навыки."""
        for skill in self.skills.values():
            await skill.on_unload()
        self.skills.clear()
