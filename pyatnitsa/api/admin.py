"""Админ-панель Пятница.ai — REST API."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

import structlog

logger = structlog.get_logger()

router = APIRouter(prefix="/admin/api", tags=["admin"])

# ─── Dependencies (инжектируются из main.py) ─────────────────
_event_tracker = None
_settings_store = None
_conversation_store = None
_skill_loader = None
_llm_manager = None
_agent = None
_agent_registry = None
_admin_secret = None  # ADMIN_PASSWORD hash


def inject_admin_deps(event_tracker, settings_store, conversation_store,
                      skill_loader, llm_manager, agent, admin_password: str,
                      agent_registry=None):
    """Вызывается из main.py."""
    global _event_tracker, _settings_store, _conversation_store
    global _skill_loader, _llm_manager, _agent, _agent_registry, _admin_secret
    _event_tracker = event_tracker
    _settings_store = settings_store
    _conversation_store = conversation_store
    _skill_loader = skill_loader
    _llm_manager = llm_manager
    _agent = agent
    _agent_registry = agent_registry
    # Храним hash пароля
    _admin_secret = hashlib.sha256(admin_password.encode()).hexdigest() if admin_password else None


# ─── Auth ────────────────────────────────────────────────────

def _make_token(password_hash: str) -> str:
    """Простой HMAC-токен: hash(password + timestamp_day)."""
    day = str(int(time.time()) // 86400)
    return hmac.new(password_hash.encode(), day.encode(), hashlib.sha256).hexdigest()


class LoginRequest(BaseModel):
    password: str


@router.post("/auth")
async def admin_login(body: LoginRequest):
    """Авторизация — возвращает токен на сутки."""
    if not _admin_secret:
        raise HTTPException(503, "Админ-пароль не настроен. Установите ADMIN_PASSWORD в .env")
    pwd_hash = hashlib.sha256(body.password.encode()).hexdigest()
    if pwd_hash != _admin_secret:
        raise HTTPException(401, "Неверный пароль")
    token = _make_token(_admin_secret)
    return {"token": token, "expires_in": 86400}


async def require_admin(request: Request):
    """Dependency — проверка токена."""
    if not _admin_secret:
        # Если пароль не задан — пускаем всех (dev-режим)
        return True
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "").strip()
    expected = _make_token(_admin_secret)
    if not token or token != expected:
        raise HTTPException(401, "Требуется авторизация")
    return True


# ─── Dashboard / Stats ──────────────────────────────────────

@router.get("/stats")
async def get_stats(hours: float = 24, _=Depends(require_admin)):
    """Сводная статистика."""
    if not _event_tracker:
        raise HTTPException(503, "EventTracker не инициализирован")
    return await _event_tracker.get_stats(hours)


# ─── Skills ──────────────────────────────────────────────────

# Required env vars per skill name
_SKILL_ENV_VARS: dict[str, list[dict]] = {
    "mail": [
        {"key": "MAILRU_USER", "label": "Email адрес", "placeholder": "user@mail.ru"},
        {"key": "MAILRU_APP_PASSWORD", "label": "Пароль приложения", "secret": True,
         "hint": "Mail.ru → Настройки → Безопасность → Пароли приложений"},
    ],
    "calendar": [
        {"key": "MAILRU_USER", "label": "Email адрес", "placeholder": "user@mail.ru"},
        {"key": "MAILRU_APP_PASSWORD", "label": "Пароль приложения", "secret": True},
        {"key": "MAILRU_CALDAV_URL", "label": "CalDAV URL", "placeholder": "https://calendar.mail.ru/principals/..."},
        {"key": "MAILRU_TIMEZONE", "label": "Часовой пояс", "placeholder": "Europe/Moscow"},
    ],
    "redmine": [
        {"key": "REDMINE_URL", "label": "Адрес EasyRedmine", "placeholder": "https://rdm.example.com"},
        {"key": "REDMINE_API_KEY", "label": "API-ключ пользователя", "secret": True},
        {"key": "REDMINE_ADMIN_KEY", "label": "API-ключ администратора", "secret": True, "required": False},
        {"key": "RDM_LOGIN", "label": "Логин (Playwright)", "required": False},
        {"key": "RDM_PASSWORD", "label": "Пароль (Playwright)", "secret": True, "required": False},
    ],
    "rusprofile": [],
    "browser": [],
    "shortener": [],
    "files": [],
}


@router.get("/skills")
async def list_skills(_=Depends(require_admin)):
    """Список скиллов с их статусом, инструментами и требуемыми переменными окружения."""
    if not _skill_loader:
        return []
    skills = []
    for name, skill in _skill_loader.skills.items():
        enabled = True
        if _settings_store:
            val = await _settings_store.get(f"skill.{name}.enabled")
            if val == "false":
                enabled = False

        tools = [{"name": t.name, "description": t.description}
                 for t in skill.get_tools()]

        env_vars = _SKILL_ENV_VARS.get(name, [])

        skills.append({
            "name": name,
            "description": skill.description,
            "version": skill.version,
            "enabled": enabled,
            "tools_count": len(tools),
            "tools": tools,
            "env_vars": env_vars,
        })
    return skills


class SkillToggle(BaseModel):
    enabled: bool


@router.post("/skills/{name}/toggle")
async def toggle_skill(name: str, body: SkillToggle, _=Depends(require_admin)):
    """Включить/выключить скилл."""
    if not _skill_loader or name not in _skill_loader.skills:
        raise HTTPException(404, f"Скилл '{name}' не найден")
    if _settings_store:
        await _settings_store.set(f"skill.{name}.enabled", str(body.enabled).lower())
    return {"name": name, "enabled": body.enabled}


@router.get("/skills/{name}/config")
async def get_skill_config(name: str, _=Depends(require_admin)):
    """Настройки скилла."""
    if not _skill_loader or name not in _skill_loader.skills:
        raise HTTPException(404, f"Скилл '{name}' не найден")
    skill = _skill_loader.skills[name]
    config = {}
    if _settings_store:
        # Ищем все ключи skill.{name}.*
        all_settings = await _settings_store.get_all(mask_secrets=False)
        prefix = f"skill.{name}."
        config = {k[len(prefix):]: v for k, v in all_settings.items() if k.startswith(prefix)}
    return {"name": name, "config": config}


class SkillConfig(BaseModel):
    config: dict[str, str]


@router.put("/skills/{name}/config")
async def update_skill_config(name: str, body: SkillConfig, _=Depends(require_admin)):
    """Обновить настройки скилла."""
    if not _skill_loader or name not in _skill_loader.skills:
        raise HTTPException(404, f"Скилл '{name}' не найден")
    if _settings_store:
        for k, v in body.config.items():
            await _settings_store.set(f"skill.{name}.{k}", v)
    return {"name": name, "updated": list(body.config.keys())}


# ─── Users ───────────────────────────────────────────────────

@router.get("/users")
async def list_users(include_blocked: bool = False, _=Depends(require_admin)):
    """Список пользователей."""
    if not _event_tracker:
        return []
    return await _event_tracker.get_users(include_blocked)


@router.get("/users/{user_id}")
async def get_user(user_id: str, _=Depends(require_admin)):
    """Профиль пользователя."""
    if not _event_tracker:
        raise HTTPException(503)
    user = await _event_tracker.get_user(user_id)
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    return user


class UserRole(BaseModel):
    role: str


@router.post("/users/{user_id}/role")
async def set_user_role(user_id: str, body: UserRole, _=Depends(require_admin)):
    """Назначить роль."""
    if body.role not in ("admin", "user"):
        raise HTTPException(400, "Роль должна быть 'admin' или 'user'")
    await _event_tracker.set_user_role(user_id, body.role)
    return {"user_id": user_id, "role": body.role}


class UserBlock(BaseModel):
    blocked: bool = True


@router.post("/users/{user_id}/block")
async def block_user(user_id: str, body: UserBlock, _=Depends(require_admin)):
    """Заблокировать/разблокировать."""
    await _event_tracker.block_user(user_id, body.blocked)
    return {"user_id": user_id, "blocked": body.blocked}


# ─── LLM Settings ───────────────────────────────────────────

@router.get("/llm")
async def get_llm_settings(_=Depends(require_admin)):
    """Текущие настройки LLM."""
    if not _settings_store:
        return {}
    all_s = await _settings_store.get_all(mask_secrets=True)
    llm_settings = {k: v for k, v in all_s.items() if k.startswith("llm.")}
    # Добавляем статус провайдеров
    providers = []
    if _llm_manager:
        for p in _llm_manager.providers:
            providers.append({
                "name": p.name,
                "model": getattr(p, "model", "unknown"),
                "available": True,
            })
    return {"settings": llm_settings, "providers": providers}


class LLMUpdate(BaseModel):
    settings: dict[str, str]


@router.put("/llm")
async def update_llm_settings(body: LLMUpdate, _=Depends(require_admin)):
    """Обновить настройки LLM."""
    if not _settings_store:
        raise HTTPException(503)
    await _settings_store.set_many(body.settings)
    return {"updated": list(body.settings.keys()), "restart_required": True}


@router.post("/llm/test")
async def test_llm(_=Depends(require_admin)):
    """Тест-запрос к LLM."""
    if not _llm_manager:
        raise HTTPException(503, "LLM не инициализирован")
    try:
        from pyatnitsa.core.llm import LLMMessage
        t0 = time.time()
        result = await _llm_manager.complete(
            messages=[LLMMessage(role="user", content="Скажи 'привет' одним словом.")],
            tools=[],
        )
        latency = round((time.time() - t0) * 1000)
        return {
            "success": True,
            "response": result.content[:200],
            "provider": result.provider,
            "latency_ms": latency,
        }
    except Exception as e:
        return {"success": False, "error": str(e)[:300]}


# ─── Logs ────────────────────────────────────────────────────

@router.get("/logs")
async def get_logs(limit: int = 50, event_type: str | None = None,
                   _=Depends(require_admin)):
    """Последние события."""
    if not _event_tracker:
        return []
    return await _event_tracker.get_recent_events(limit, event_type)


# ─── Conversations ───────────────────────────────────────────

@router.get("/conversations")
async def list_conversations(user_id: str | None = None, limit: int = 20,
                             _=Depends(require_admin)):
    """Список диалогов."""
    if not _conversation_store:
        return []
    if user_id:
        chats = await _conversation_store.get_user_chats(user_id, limit)
    else:
        chats = await _conversation_store.get_all_chats(limit)
    return chats


@router.get("/conversations/{chat_id}")
async def get_conversation(chat_id: int, _=Depends(require_admin)):
    """Содержимое диалога."""
    if not _conversation_store:
        raise HTTPException(503)
    messages = await _conversation_store.get_messages(chat_id, limit=200)
    return {"chat_id": chat_id, "messages": messages}


# ─── Agents (суб-агенты) ──────────────────────────────────

@router.get("/agents")
async def list_agents(_=Depends(require_admin)):
    """Список всех суб-агентов."""
    available_skills = list(_skill_loader.skills.keys()) if _skill_loader else []

    # Системный агент «Пятница» — всегда присутствует, редактируемый
    pyatnitsa_defaults = {
        "id": "__pyatnitsa__",
        "name": "Пятница",
        "description": "Основной агент — прямые tool calls (legacy-режим)",
        "system_prompt": "",
        "skills": available_skills,
        "max_iterations": 5,
        "temperature": 0.5,
        "is_fallback": False,
        "enabled": True,
        "system": True,  # маркер — нельзя удалить
    }
    # Загружаем сохранённый конфиг
    if _settings_store:
        saved = await _settings_store.get("agent.pyatnitsa")
        if saved:
            try:
                import json as _json
                saved_data = _json.loads(saved)
                pyatnitsa_defaults.update(saved_data)
                pyatnitsa_defaults["id"] = "__pyatnitsa__"
                pyatnitsa_defaults["system"] = True
            except (ValueError, TypeError):
                pass
    pyatnitsa_agent = pyatnitsa_defaults

    configs = []
    mode = "legacy"
    if _agent_registry:
        configs = _agent_registry.list_configs()
        if _agent_registry.list_active():
            mode = "router"

    return {
        "agents": [pyatnitsa_agent] + configs,
        "mode": mode,
        "available_skills": available_skills,
    }


class AgentCreate(BaseModel):
    id: str
    name: str
    description: str
    system_prompt: str
    skills: list[str] = []
    max_iterations: int = 8
    temperature: float = 0.5
    is_fallback: bool = False
    enabled: bool = True


@router.post("/agents")
async def create_agent(body: AgentCreate, _=Depends(require_admin)):
    """Создать нового суб-агента."""
    if not _agent_registry:
        raise HTTPException(503, "AgentRegistry не инициализирован")
    if _agent_registry.get(body.id):
        raise HTTPException(409, f"Агент '{body.id}' уже существует")

    from pyatnitsa.core.agent_registry import AgentConfig
    config = AgentConfig(body.model_dump())
    _agent_registry.register(config)

    # Persist to settings_store
    if _settings_store:
        await _agent_registry.save_to_settings(_settings_store)

    return config.to_dict()


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str, _=Depends(require_admin)):
    """Получить конфигурацию агента."""
    if agent_id == "__pyatnitsa__":
        # Возвращаем из settings_store
        available_skills = list(_skill_loader.skills.keys()) if _skill_loader else []
        result = {
            "id": "__pyatnitsa__", "name": "Пятница",
            "description": "Основной агент — прямые tool calls (legacy-режим)",
            "system_prompt": "", "skills": available_skills,
            "max_iterations": 5, "temperature": 0.5,
            "is_fallback": False, "enabled": True, "system": True,
        }
        if _settings_store:
            raw = await _settings_store.get("agent.pyatnitsa")
            if raw:
                try:
                    import json as _json
                    result.update(_json.loads(raw))
                    result["id"] = "__pyatnitsa__"
                    result["system"] = True
                except (ValueError, TypeError):
                    pass
        return result

    if not _agent_registry:
        raise HTTPException(503, "AgentRegistry не инициализирован")
    configs = {c["id"]: c for c in _agent_registry.list_configs()}
    if agent_id not in configs:
        raise HTTPException(404, f"Агент '{agent_id}' не найден")
    return configs[agent_id]


class AgentUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    skills: list[str] | None = None
    max_iterations: int | None = None
    temperature: float | None = None
    is_fallback: bool | None = None
    enabled: bool | None = None


@router.put("/agents/{agent_id}")
async def update_agent(agent_id: str, body: AgentUpdate, _=Depends(require_admin)):
    """Обновить конфигурацию агента."""
    # Системный агент Пятница — сохраняем в settings_store
    if agent_id == "__pyatnitsa__":
        if not _settings_store:
            raise HTTPException(503, "SettingsStore не инициализирован")
        updates = {k: v for k, v in body.model_dump().items() if v is not None}
        # Загружаем текущий конфиг и мержим
        import json as _json
        raw = await _settings_store.get("agent.pyatnitsa")
        current = _json.loads(raw) if raw else {}
        current.update(updates)
        await _settings_store.set("agent.pyatnitsa", _json.dumps(current, ensure_ascii=False))
        # Применяем фильтр скиллов к агенту, если он запущен
        if _agent and "skills" in updates:
            _agent._pyatnitsa_skills = updates["skills"]
        current["id"] = "__pyatnitsa__"
        current["system"] = True
        return current

    if not _agent_registry:
        raise HTTPException(503, "AgentRegistry не инициализирован")
    if agent_id not in _agent_registry._configs:
        raise HTTPException(404, f"Агент '{agent_id}' не найден")

    from pyatnitsa.core.agent_registry import AgentConfig
    old = _agent_registry._configs[agent_id].to_dict()
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    old.update(updates)
    config = AgentConfig(old)
    _agent_registry.register(config)  # перезаписывает

    if _settings_store:
        await _agent_registry.save_to_settings(_settings_store)

    return config.to_dict()


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str, _=Depends(require_admin)):
    """Удалить суб-агента."""
    if agent_id == "__pyatnitsa__":
        raise HTTPException(400, "Системного агента нельзя удалить")
    if not _agent_registry:
        raise HTTPException(503, "AgentRegistry не инициализирован")
    if agent_id not in _agent_registry._configs:
        raise HTTPException(404, f"Агент '{agent_id}' не найден")

    _agent_registry.unregister(agent_id)

    if _settings_store:
        await _agent_registry.save_to_settings(_settings_store)

    return {"deleted": agent_id}

