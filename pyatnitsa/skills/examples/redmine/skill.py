"""Навык EasyRedmine для Пятница.ai.

Полная интеграция с EasyRedmine/Redmine:
- CRUD задачи и проекты
- Поиск пользователей (fuzzy по фамилии)
- Поиск контрагентов (Easy Contacts API)
- Создание проектов сделок (5-фазная архитектура)
- Назначение ролей в проекте
- Логирование времени
"""

from __future__ import annotations

import re
import time
import json
import datetime as dt
from typing import Any

import httpx
import structlog

from pyatnitsa.skills.skills import BaseSkill
from pyatnitsa.core.llm import LLMTool

logger = structlog.get_logger()


# ─── Транслитерация кириллицы ─────────────────────────────

_CYRILLIC_MAP = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo',
    'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
    'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
    'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'shch',
    'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
}


def slugify_identifier(name: str) -> str:
    result = name.lower()
    result = ''.join(_CYRILLIC_MAP.get(c, c) for c in result)
    result = re.sub(r'[^a-z0-9]+', '-', result).strip('-')
    if result and result[0].isdigit():
        result = 'p-' + result
    ts = format(int(time.time()), 'x')
    return result[:88] + '-' + ts


# ─── resolveChoice ────────────────────────────────────────

def resolve_choice(label, user_input, options, hint=''):
    if not options:
        return None, {
            'success': False, 'error': 'not_found', 'field': label, 'input': user_input,
            'message': f'Не найдено ни одного значения для «{label}» по строке «{user_input}».',
            'hint': hint or 'Проверьте написание или укажите ID.',
        }
    if len(options) == 1:
        return options[0], None
    return None, {
        'success': False, 'error': 'ambiguous_choice', 'field': label, 'input': user_input,
        'message': f'Найдено {len(options)} вариантов для «{label}» по строке «{user_input}». Уточните.',
        'candidates': options[:15],
        'hint': hint or 'Повторите с более точным именем или укажите ID.',
    }


# ─── Маппинги ─────────────────────────────────────────────

STATUS_MAP = {'new': 1, 'новая': 1, 'новый': 1, 'in progress': 2, 'в работе': 2,
    'resolved': 3, 'решена': 3, 'feedback': 4, 'обратная связь': 4,
    'closed': 5, 'закрыта': 5, 'закрыт': 5, 'rejected': 6, 'отклонена': 6}

TRACKER_MAP = {'bug': 1, 'баг': 1, 'ошибка': 1, 'feature': 2, 'фича': 2, 'функционал': 2,
    'support': 3, 'поддержка': 3, 'task': 4, 'задача': 4}

PRIORITY_MAP = {'low': 1, 'низкий': 1, 'normal': 2, 'нормальный': 2,
    'high': 3, 'высокий': 3, 'urgent': 4, 'срочный': 4, 'immediate': 5, 'немедленный': 5}

ACTIVITY_MAP = {'development': 9, 'разработка': 9, 'design': 10, 'дизайн': 10,
    'testing': 11, 'тестирование': 11, 'support': 12, 'поддержка': 12,
    'other': 13, 'прочее': 13, 'management': 14, 'управление': 14}

ROLE_IDS = {'Ответственный': 9, 'Исполнитель': 12, 'Диспетчер': 11, 'Контролёр': 13}

DEAL_ROLE_MAP = {
    'rp':      ['Ответственный', 'Исполнитель', 'Диспетчер', 'Контролёр'],
    'ap':      ['Ответственный', 'Исполнитель', 'Диспетчер', 'Контролёр'],
    'manager': ['Исполнитель', 'Контролёр', 'Диспетчер'],
}

STAGE_MAP = {
    'start': 'Пресейл > Проектирование (расчёт)',
    'Проектирование (расчёт)': 'Пресейл > Проектирование (расчёт)',
    'Сбор входных данных': 'Пресейл > Сбор входных данных',
    'Утверждение спецификации': 'Пресейл > Утверждение спецификации',
    'Подготовка КП': 'Пресейл > Подготовка КП',
    'Пресейл': 'Пресейл',
    'Закладка в бюджет': 'Закладка в бюджет',
    'Закупка (аукцион)': 'Закупка (аукцион)',
    'Подписание контракта': 'Подписание контракта',
    'Выполнение работ': 'Выполнение работ',
    'Документальное закрытие': 'Документальное закрытие',
}

CALC_FIXED_WATCHERS = [
    'Дубянский Александр Александрович',
    'Поздеев Денис Александрович',
    'Прокопенко Илья Александрович',
]


def _resolve_name(name, mapping):
    lower = (name or '').lower()
    val = mapping.get(lower)
    if val:
        return val
    try:
        return int(name)
    except (ValueError, TypeError):
        return name


class RedmineSkill(BaseSkill):
    """Полная интеграция с EasyRedmine."""

    name = "redmine"
    description = "Управление проектами и задачами в EasyRedmine: задачи, проекты, сделки, пользователи, контрагенты"
    version = "1.0.0"

    def __init__(self, config=None):
        super().__init__(config)
        self.base_url = ""
        self.api_key = ""
        self.admin_key = ""
        self._client = None

    async def on_load(self):
        import os
        self.base_url = os.getenv("REDMINE_URL", "").rstrip("/")
        self.api_key = os.getenv("REDMINE_API_KEY", "")
        self.admin_key = os.getenv("REDMINE_ADMIN_KEY", "")
        if self.base_url and self.api_key:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"X-Redmine-API-Key": self.api_key, "Content-Type": "application/json"},
                timeout=30.0,
            )
            logger.info("redmine_skill_loaded", url=self.base_url)
        else:
            logger.warning("redmine_skill_no_config", hint="Set REDMINE_URL and REDMINE_API_KEY")

    async def on_unload(self):
        if self._client:
            await self._client.aclose()

    # ─── API ──────────────────────────────────────────────

    async def _api(self, method, path, body=None, params=None, api_key=None):
        headers = {}
        if api_key:
            headers["X-Redmine-API-Key"] = api_key
        kw = {"params": params, "headers": headers}
        if method == "GET":
            resp = await self._client.get(path, **kw)
        elif method == "POST":
            resp = await self._client.post(path, json=body, **kw)
        elif method == "PUT":
            resp = await self._client.put(path, json=body, **kw)
        elif method == "DELETE":
            resp = await self._client.delete(path, **kw)
        else:
            raise ValueError(f"Unknown method: {method}")
        if resp.status_code >= 400:
            raise Exception(f"Redmine API ({resp.status_code}): {resp.text[:200]}")
        text = resp.text.strip()
        if not text:
            return {"status": resp.status_code}
        try:
            return resp.json()
        except Exception:
            return {"raw": text, "status": resp.status_code}

    async def _api_safe(self, method, path, body=None, params=None):
        try:
            return await self._api(method, path, body, params)
        except Exception as e:
            if ('403' in str(e) or '401' in str(e)) and self.admin_key:
                return await self._api(method, path, body, params, api_key=self.admin_key)
            raise

    # ─── User resolution ──────────────────────────────────

    async def _get_members(self, project_id):
        try:
            d = await self._api("GET", f"/projects/{project_id}/memberships.json", params={"limit": 200})
            return d.get("memberships", [])
        except Exception:
            return []

    async def _resolve_user(self, name_or_id, project_id=25):
        name = (name_or_id or "").strip()
        if not name:
            return []
        if name.isdigit():
            return [{"id": int(name), "name": f"User #{name}"}]
        members = await self._get_members(project_id)
        parts = name.split()
        ln_lower = parts[0].lower() if len(parts) == 1 else name.lower()

        if len(parts) == 1:
            # Strict last-name match
            matches = [{"id": m["user"]["id"], "name": m["user"]["name"]}
                       for m in members if m.get("user") and m["user"]["name"].split()[0].lower() == ln_lower]
            if matches:
                return matches
        # Includes match
        matches = [{"id": m["user"]["id"], "name": m["user"]["name"]}
                   for m in members if m.get("user") and ln_lower in m["user"]["name"].lower()]
        if matches:
            return matches
        # Fallback admin API
        if self.admin_key:
            try:
                d = await self._api("GET", "/users.json", params={"name": name, "limit": 20}, api_key=self.admin_key)
                return [{"id": u["id"], "name": f"{u.get('lastname', '')} {u.get('firstname', '')}".strip()}
                        for u in d.get("users", [])]
            except Exception:
                pass
        return []

    async def _resolve_user_soft(self, name_or_id, project_id=25):
        c = await self._resolve_user(name_or_id, project_id)
        return c[0] if len(c) == 1 else None

    # ─── Roles ────────────────────────────────────────────

    async def _set_user_roles(self, proj_id, user_id, role_names):
        desired = [ROLE_IDS[n] for n in role_names if n in ROLE_IDS]
        if not desired:
            return {"action": "skip"}
        try:
            d = await self._api_safe("GET", f"/projects/{proj_id}/memberships.json", params={"limit": 200})
            memberships = d.get("memberships", [])
        except Exception:
            memberships = []
        existing = next((m for m in memberships if m.get("user") and m["user"]["id"] == user_id), None)
        if not existing:
            try:
                await self._api_safe("POST", f"/projects/{proj_id}/memberships.json",
                    body={"membership": {"user_id": user_id, "role_ids": desired}})
                return {"action": "created", "role_ids": desired}
            except Exception as e:
                return {"action": "error", "error": str(e)[:80]}
        else:
            old_ids = [r["id"] for r in existing.get("roles", [])]
            merged = list(set(old_ids + desired))
            if set(merged) == set(old_ids):
                return {"action": "unchanged"}
            try:
                await self._api_safe("PUT", f"/memberships/{existing['id']}.json",
                    body={"membership": {"role_ids": merged}})
                return {"action": "updated", "role_ids": merged}
            except Exception as e:
                return {"action": "error", "error": str(e)[:80]}

    # ═══════════════════════════════════════════════════════
    # TOOLS
    # ═══════════════════════════════════════════════════════

    def get_tools(self):
        return [
            LLMTool("redmine.my_tasks", "Показывает задачи пользователя в EasyRedmine", {
                "type": "object", "properties": {
                    "status": {"type": "string", "enum": ["open", "closed", "all"], "default": "open"},
                    "limit": {"type": "integer", "default": 10},
                },
            }),
            LLMTool("redmine.list_issues", "Список задач в проекте с фильтрами", {
                "type": "object", "properties": {
                    "project": {"type": "string", "description": "ID или идентификатор проекта"},
                    "assigned_to": {"type": "string", "description": "me или ID"},
                    "status": {"type": "string", "enum": ["open", "closed", "all"], "default": "open"},
                    "tracker": {"type": "string"}, "priority": {"type": "string"},
                    "limit": {"type": "integer", "default": 25},
                },
            }),
            LLMTool("redmine.get_issue", "Детали задачи по ID (история, вложения)", {
                "type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"],
            }),
            LLMTool("redmine.create_task", "Создаёт задачу в проекте", {
                "type": "object", "properties": {
                    "project": {"type": "string"}, "subject": {"type": "string"},
                    "description": {"type": "string", "default": ""},
                    "assigned_to": {"type": "string"}, "tracker": {"type": "string", "default": "task"},
                    "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"], "default": "normal"},
                    "due_date": {"type": "string"}, "parent_id": {"type": "integer"},
                }, "required": ["project", "subject"],
            }),
            LLMTool("redmine.update_task", "Обновляет задачу: статус, приоритет, назначение", {
                "type": "object", "properties": {
                    "id": {"type": "integer"}, "status": {"type": "string"},
                    "priority": {"type": "string"}, "subject": {"type": "string"},
                    "assigned_to": {"type": "string"}, "due_date": {"type": "string"},
                    "done_ratio": {"type": "integer"}, "notes": {"type": "string"},
                }, "required": ["id"],
            }),
            LLMTool("redmine.comment", "Комментарий к задаче", {
                "type": "object", "properties": {
                    "id": {"type": "integer"}, "text": {"type": "string"},
                }, "required": ["id", "text"],
            }),
            LLMTool("redmine.log_time", "Списание времени на задачу", {
                "type": "object", "properties": {
                    "id": {"type": "integer"}, "hours": {"type": "number"},
                    "activity": {"type": "string", "default": "development"},
                    "comment": {"type": "string"}, "date": {"type": "string"},
                }, "required": ["id", "hours"],
            }),
            LLMTool("redmine.project_status", "Отчёт по проекту: задачи, приоритеты", {
                "type": "object", "properties": {"project": {"type": "string"}}, "required": ["project"],
            }),
            LLMTool("redmine.list_projects", "Список проектов", {
                "type": "object", "properties": {"limit": {"type": "integer", "default": 100}},
            }),
            LLMTool("redmine.find_user", "Поиск пользователя по ФИО/фамилии", {
                "type": "object", "properties": {
                    "name": {"type": "string"}, "project": {"type": "string", "default": "ds4_ps"},
                }, "required": ["name"],
            }),
            LLMTool("redmine.find_counterparty", "Поиск контрагента по названию/ИНН", {
                "type": "object", "properties": {
                    "name": {"type": "string"}, "limit": {"type": "integer", "default": 10},
                }, "required": ["name"],
            }),
            LLMTool("redmine.create_deal_project",
                "Создаёт проект сделки (5 фаз): проект, роли, CF, Паспорт, Расчёт. "
                "Обязательно: name, description, counterparty. Опционально: ap, rp, manager (ФИО).", {
                "type": "object", "properties": {
                    "name": {"type": "string"}, "description": {"type": "string"},
                    "counterparty": {"type": "string"}, "counterparty_id": {"type": "integer"},
                    "ap": {"type": "string"}, "ap_id": {"type": "integer"},
                    "rp": {"type": "string"}, "rp_id": {"type": "integer"},
                    "manager": {"type": "string"}, "manager_id": {"type": "integer"},
                    "stage": {"type": "string", "default": "Проектирование (расчёт)"},
                    "parent_id": {"type": "integer", "default": 25},
                    "no_calculation": {"type": "boolean", "default": False},
                }, "required": ["name", "description", "counterparty"],
            }),
            LLMTool("redmine.members", "Участники проекта с ролями", {
                "type": "object", "properties": {"project": {"type": "string"}}, "required": ["project"],
            }),
            LLMTool("redmine.me", "Текущий пользователь Redmine", {
                "type": "object", "properties": {},
            }),
            LLMTool("redmine.time_entries", "Записи учёта времени", {
                "type": "object", "properties": {
                    "project": {"type": "string"}, "user": {"type": "string"},
                    "from_date": {"type": "string"}, "to_date": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
            }),
        ]

    # ═══════════════════════════════════════════════════════
    # EXECUTE
    # ═══════════════════════════════════════════════════════

    async def execute(self, action, params):
        if not self._client:
            return json.dumps({"error": "Redmine не настроен. Укажите REDMINE_URL и REDMINE_API_KEY."}, ensure_ascii=False)
        try:
            handler = {
                "my_tasks": self._my_tasks, "list_issues": self._list_issues,
                "get_issue": self._get_issue, "create_task": self._create_task,
                "update_task": self._update_task, "comment": self._comment,
                "log_time": self._log_time, "project_status": self._project_status,
                "list_projects": self._list_projects, "find_user": self._find_user,
                "find_counterparty": self._find_counterparty,
                "create_deal_project": self._create_deal_project,
                "members": self._members, "me": self._me, "time_entries": self._time_entries,
            }.get(action)
            if not handler:
                return json.dumps({"error": f"Неизвестное действие: {action}"}, ensure_ascii=False)
            return await handler(params)
        except Exception as e:
            logger.error("redmine_error", action=action, error=str(e))
            return json.dumps({"error": str(e)[:300]}, ensure_ascii=False)

    # ═══════════════════════════════════════════════════════
    # ACTIONS
    # ═══════════════════════════════════════════════════════

    async def _my_tasks(self, p):
        params = {"assigned_to_id": "me", "limit": p.get("limit", 10), "sort": "updated_on:desc"}
        s = p.get("status", "open")
        if s == "open": params["status_id"] = "open"
        elif s == "closed": params["status_id"] = "closed"
        data = await self._api("GET", "/issues.json", params=params)
        issues = [{"id": i["id"], "project": i.get("project",{}).get("name"), "subject": i["subject"],
                    "status": i.get("status",{}).get("name"), "priority": i.get("priority",{}).get("name"),
                    "updated": i.get("updated_on")} for i in data.get("issues", [])]
        return json.dumps({"total": data.get("total_count", len(issues)), "issues": issues}, ensure_ascii=False)

    async def _list_issues(self, p):
        params = {"limit": p.get("limit", 25), "sort": "updated_on:desc"}
        if p.get("project"): params["project_id"] = p["project"]
        if p.get("assigned_to"): params["assigned_to_id"] = p["assigned_to"]
        s = p.get("status", "open")
        if s == "closed": params["status_id"] = "closed"
        elif s == "all": params["status_id"] = "*"
        if p.get("tracker"): params["tracker_id"] = _resolve_name(p["tracker"], TRACKER_MAP)
        if p.get("priority"): params["priority_id"] = _resolve_name(p["priority"], PRIORITY_MAP)
        data = await self._api("GET", "/issues.json", params=params)
        issues = [{"id": i["id"], "project": i.get("project",{}).get("name"),
                    "tracker": i.get("tracker",{}).get("name"), "subject": i["subject"],
                    "status": i.get("status",{}).get("name"), "priority": i.get("priority",{}).get("name"),
                    "assigned_to": (i.get("assigned_to") or {}).get("name"),
                    "done_ratio": i.get("done_ratio"), "updated": i.get("updated_on")}
                   for i in data.get("issues", [])]
        return json.dumps({"total": data.get("total_count"), "issues": issues}, ensure_ascii=False)

    async def _get_issue(self, p):
        iid = p["id"]
        data = await self._api("GET", f"/issues/{iid}.json", params={"include": "journals,attachments,children"})
        i = data["issue"]
        return json.dumps({
            "id": i["id"], "project": i.get("project",{}).get("name"),
            "tracker": i.get("tracker",{}).get("name"), "status": i.get("status",{}).get("name"),
            "priority": i.get("priority",{}).get("name"), "subject": i.get("subject"),
            "description": (i.get("description") or "")[:500],
            "assigned_to": (i.get("assigned_to") or {}).get("name"),
            "author": (i.get("author") or {}).get("name"),
            "done_ratio": i.get("done_ratio"), "due_date": i.get("due_date"),
            "created": i.get("created_on"), "updated": i.get("updated_on"),
            "parent": (i.get("parent") or {}).get("id"),
            "children": [{"id": c["id"], "subject": c.get("subject")} for c in i.get("children", [])],
            "journals": [{"user": j.get("user",{}).get("name"), "date": j.get("created_on"), "notes": j.get("notes")}
                         for j in (i.get("journals") or [])[-10:] if j.get("notes")],
            "url": f"{self.base_url}/issues/{iid}",
        }, ensure_ascii=False)

    async def _create_task(self, p):
        issue = {"project_id": p["project"], "subject": p["subject"]}
        if p.get("description"): issue["description"] = p["description"]
        if p.get("tracker"): issue["tracker_id"] = _resolve_name(p["tracker"], TRACKER_MAP)
        if p.get("priority"): issue["priority_id"] = _resolve_name(p["priority"], PRIORITY_MAP)
        if p.get("assigned_to"):
            v = p["assigned_to"]
            if v == "me": issue["assigned_to_id"] = "me"
            elif str(v).isdigit(): issue["assigned_to_id"] = int(v)
            else:
                u = await self._resolve_user_soft(v)
                if u: issue["assigned_to_id"] = u["id"]
        if p.get("due_date"): issue["due_date"] = p["due_date"]
        if p.get("parent_id"): issue["parent_issue_id"] = p["parent_id"]
        data = await self._api("POST", "/issues.json", body={"issue": issue})
        i = data["issue"]
        return json.dumps({"success": True, "id": i["id"], "subject": i["subject"],
            "project": i.get("project",{}).get("name"), "url": f"{self.base_url}/issues/{i['id']}"}, ensure_ascii=False)

    async def _update_task(self, p):
        iid = p["id"]
        issue = {}
        for k in ("subject", "description", "notes"):
            if p.get(k): issue[k] = p[k]
        if p.get("status"): issue["status_id"] = _resolve_name(p["status"], STATUS_MAP)
        if p.get("priority"): issue["priority_id"] = _resolve_name(p["priority"], PRIORITY_MAP)
        if p.get("assigned_to"):
            v = p["assigned_to"]
            if v == "none": issue["assigned_to_id"] = ""
            elif v == "me": issue["assigned_to_id"] = "me"
            elif str(v).isdigit(): issue["assigned_to_id"] = int(v)
            else:
                u = await self._resolve_user_soft(v)
                if u: issue["assigned_to_id"] = u["id"]
        if p.get("due_date"): issue["due_date"] = p["due_date"]
        if p.get("done_ratio") is not None: issue["done_ratio"] = p["done_ratio"]
        await self._api("PUT", f"/issues/{iid}.json", body={"issue": issue})
        return json.dumps({"success": True, "updated": iid, "url": f"{self.base_url}/issues/{iid}"}, ensure_ascii=False)

    async def _comment(self, p):
        iid, text = p["id"], p["text"]
        await self._api("PUT", f"/issues/{iid}.json", body={"issue": {"notes": text}})
        return json.dumps({"success": True, "commented": iid}, ensure_ascii=False)

    async def _log_time(self, p):
        entry = {"issue_id": p["id"], "hours": p["hours"]}
        if p.get("activity"): entry["activity_id"] = _resolve_name(p["activity"], ACTIVITY_MAP)
        if p.get("comment"): entry["comments"] = p["comment"]
        if p.get("date"): entry["spent_on"] = p["date"]
        data = await self._api("POST", "/time_entries.json", body={"time_entry": entry})
        return json.dumps({"success": True, "id": data.get("time_entry",{}).get("id"),
            "hours": p["hours"]}, ensure_ascii=False)

    async def _project_status(self, p):
        project = p["project"]
        resp = await self._api("GET", "/projects.json", params={"limit": 100})
        matched = None
        pl = project.lower()
        for pr in resp.get("projects", []):
            if pl in pr["name"].lower() or pl == str(pr["id"]) or pl == pr.get("identifier", ""):
                matched = pr; break
        if not matched:
            return json.dumps({"error": f"Проект «{project}» не найден."}, ensure_ascii=False)
        ir = await self._api("GET", "/issues.json", params={"project_id": matched["id"], "status_id": "open", "limit": 100})
        issues = ir.get("issues", [])
        by_pri, by_asgn = {}, {}
        for iss in issues:
            pri = iss.get("priority",{}).get("name", "Normal")
            by_pri[pri] = by_pri.get(pri, 0) + 1
            a = (iss.get("assigned_to") or {}).get("name", "Не назначено")
            by_asgn[a] = by_asgn.get(a, 0) + 1
        return json.dumps({"project": matched["name"], "id": matched["id"],
            "identifier": matched.get("identifier"), "open_issues": len(issues),
            "by_priority": by_pri, "by_assignee": dict(sorted(by_asgn.items(), key=lambda x: -x[1])[:10]),
            "url": f"{self.base_url}/projects/{matched.get('identifier', matched['id'])}"}, ensure_ascii=False)

    async def _list_projects(self, p):
        data = await self._api("GET", "/projects.json", params={"limit": p.get("limit", 100)})
        projects = [{"id": pr["id"], "identifier": pr.get("identifier"), "name": pr["name"],
                      "status": "active" if pr.get("status") == 1 else "closed"} for pr in data.get("projects", [])]
        return json.dumps({"total": data.get("total_count"), "projects": projects}, ensure_ascii=False)

    async def _find_user(self, p):
        candidates = await self._resolve_user(p["name"], p.get("project", "ds4_ps"))
        return json.dumps({"total": len(candidates), "query": p["name"], "users": candidates}, ensure_ascii=False)

    async def _find_counterparty(self, p):
        name, limit = p["name"], p.get("limit", 10)
        is_inn = bool(re.match(r'^\d{10}(\d{2})?$', name.replace(' ', '')))
        items, source = [], None
        endpoints = [
            ("/easy_contacts.json", {"limit": limit, "easy_query_q": name}),
            ("/easy_contacts.json", {"limit": limit, "search": name}),
            ("/easy_contacts.json", {"limit": limit, "name": name}),
        ]
        if is_inn:
            inn = name.replace(' ', '')
            endpoints = [("/easy_contacts.json", {"limit": 100, "cf_6": inn}),
                         ("/easy_contacts.json", {"limit": 100, "easy_query_q": inn})] + endpoints
        for path, ep in endpoints:
            try:
                data = await self._api("GET", path, params=ep)
                items = data.get("easy_contacts") or data.get("contacts") or []
                if items: source = path; break
            except Exception: continue
        if is_inn and items:
            inn = name.replace(' ', '')
            items = [c for c in items if any(f.get("id") == 6 and str(f.get("value","")).replace(' ','') == inn
                     for f in c.get("custom_fields", []))]
        if items:
            contacts = []
            for c in items[:limit]:
                org = next((f["value"] for f in c.get("custom_fields",[]) if f.get("id")==2), "")
                fn = f"{c.get('lastname','')} {c.get('firstname','')}".strip()
                inn_v = next((f.get("value") for f in c.get("custom_fields",[]) if f.get("id") in (6,117)), None)
                contacts.append({"id": c["id"], "name": c.get("name") or org or fn or f"#{c['id']}", "inn": inn_v})
            return json.dumps({"total": len(contacts), "source": source, "contacts": contacts}, ensure_ascii=False)
        # Fallback projects
        try:
            pd = await self._api("GET", "/projects.json", params={"limit": 100})
            sl = name.lower()
            m = [{"id": pr["id"], "name": pr["name"], "kind": "project"}
                 for pr in pd.get("projects",[]) if sl in pr["name"].lower()][:limit]
            return json.dumps({"total": len(m), "source": "projects-fallback", "contacts": m}, ensure_ascii=False)
        except Exception:
            return json.dumps({"total": 0, "error": "Контрагент не найден"}, ensure_ascii=False)

    async def _members(self, p):
        data = await self._api("GET", f"/projects/{p['project']}/memberships.json", params={"limit": 100})
        members = [{"id": m.get("user", m.get("group",{})).get("id"),
                     "name": m.get("user", m.get("group",{})).get("name"),
                     "type": "user" if m.get("user") else "group",
                     "roles": [r["name"] for r in m.get("roles",[])]} for m in data.get("memberships",[])]
        return json.dumps({"total": data.get("total_count"), "members": members}, ensure_ascii=False)

    async def _me(self, _):
        data = await self._api("GET", "/users/current.json")
        u = data["user"]
        return json.dumps({"id": u["id"], "login": u.get("login"),
            "name": f"{u.get('firstname','')} {u.get('lastname','')}".strip(),
            "email": u.get("mail"), "admin": u.get("admin")}, ensure_ascii=False)

    async def _time_entries(self, p):
        params = {"limit": p.get("limit", 50)}
        if p.get("user"): params["user_id"] = p["user"]
        if p.get("project"): params["project_id"] = p["project"]
        if p.get("from_date"): params["from"] = p["from_date"]
        if p.get("to_date"): params["to"] = p["to_date"]
        data = await self._api("GET", "/time_entries.json", params=params)
        entries = [{"id": e["id"], "project": e.get("project",{}).get("name"),
                    "issue": (e.get("issue") or {}).get("id"), "user": e.get("user",{}).get("name"),
                    "hours": e.get("hours"), "date": e.get("spent_on")} for e in data.get("time_entries",[])]
        total = sum(e["hours"] or 0 for e in entries)
        return json.dumps({"total_hours": round(total, 2), "entries": entries}, ensure_ascii=False)

    # ═══════════════════════════════════════════════════════
    # CREATE DEAL PROJECT (5 phases)
    # ═══════════════════════════════════════════════════════

    async def _create_deal_project(self, p):
        parent_id = p.get("parent_id", 25)
        stage = p.get("stage", "Проектирование (расчёт)")
        stage_value = STAGE_MAP.get(stage, stage)
        skip_calc = p.get("no_calculation", False)
        finish_date = p.get("finish", f"{dt.date.today().year}-12-31")
        log_lines = []
        def log(msg):
            log_lines.append(msg)
            logger.info("deal_project", msg=msg)

        # ── PHASE 0: Pre-flight ──
        log("Phase 0: Resolving lookups...")

        # Counterparty (HARD STOP)
        cp_info = None
        if p.get("counterparty_id"):
            cid = p["counterparty_id"]
            try:
                cd = await self._api("GET", f"/easy_contacts/{cid}.json")
                c = cd.get("easy_contact") or cd
                org = next((f["value"] for f in c.get("custom_fields",[]) if f.get("id")==2), "")
                fn = f"{c.get('lastname','')} {c.get('firstname','')}".strip()
                inn = next((f["value"] for f in c.get("custom_fields",[]) if f.get("id")==6), None)
                cp_info = {"id": cid, "name": org or fn or p.get("counterparty", f"#{cid}"), "inn": inn}
            except Exception:
                cp_info = {"id": cid, "name": p.get("counterparty", f"#{cid}"), "inn": None}
        elif p.get("counterparty"):
            sr = json.loads(await self._find_counterparty({"name": p["counterparty"], "limit": 10}))
            contacts = sr.get("contacts", [])
            resolved, error = resolve_choice("Контрагент", p["counterparty"], contacts,
                hint="Повторите с counterparty_id или уточните.")
            if not resolved:
                return json.dumps(error, ensure_ascii=False)
            cp_info = resolved
        if cp_info:
            log(f"  ✓ Контрагент: {cp_info.get('name')} (#{cp_info.get('id')})")

        # Users (HARD STOP per field)
        fields = [("АП", p.get("ap"), p.get("ap_id"), "ap_id"),
                   ("РП", p.get("rp"), p.get("rp_id"), "rp_id"),
                   ("Менеджер", p.get("manager"), p.get("manager_id"), "manager_id")]
        ru = {}
        for label, nv, idv, idf in fields:
            if idv:
                ru[label] = {"id": int(idv), "name": nv or f"User #{idv}"}; continue
            if not nv:
                log(f"  · {label}: пропущен"); continue
            cands = await self._resolve_user(nv, parent_id)
            resolved, error = resolve_choice(label, nv, cands, hint=f"Укажите {idf}.")
            if not resolved:
                return json.dumps(error, ensure_ascii=False)
            ru[label] = resolved
        ap_id = ru.get("АП",{}).get("id")
        rp_id = ru.get("РП",{}).get("id")
        mgr_id = ru.get("Менеджер",{}).get("id")
        for l in ("АП","РП","Менеджер"):
            if l in ru: log(f"  ✓ {l}: {ru[l]['name']} (#{ru[l]['id']})")

        # Пресейл group (SOFT)
        presale_gid = None
        members = await self._get_members(parent_id)
        for m in members:
            if m.get("group") and "пресейл" in m["group"]["name"].lower():
                presale_gid = m["group"]["id"]; break

        calc_watcher_ids = []
        for wn in CALC_FIXED_WATCHERS:
            u = await self._resolve_user_soft(wn, parent_id)
            if u: calc_watcher_ids.append(u["id"])

        log("Phase 0 done.\n")

        # ── PHASE 1: Create project ──
        log("[1/5] Creating project...")
        ident = slugify_identifier(p["name"])
        try:
            data = await self._api_safe("POST", "/projects.json", body={"project": {
                "name": p["name"], "identifier": ident, "is_public": False, "parent_id": parent_id}})
            pid = data["project"]["id"]
            pident = data["project"]["identifier"]
            log(f"  ✓ #{pid} ({pident})")
        except Exception as e:
            return json.dumps({"success": False, "error": "create_failed", "message": str(e)[:200]}, ensure_ascii=False)

        # ── PHASE 2: Roles ──
        log("[2/5] Roles...")
        mr = {}
        for key, uid, roles in [("rp", rp_id, DEAL_ROLE_MAP["rp"]),
                                  ("ap", ap_id, DEAL_ROLE_MAP["ap"]),
                                  ("manager", mgr_id, DEAL_ROLE_MAP["manager"])]:
            if not uid: continue
            r = await self._set_user_roles(pid, uid, roles)
            mr[key] = {"user_id": uid, **r}
            log(f"  ✓ {key} (#{uid}): {r.get('action')}")

        # ── PHASE 3: Custom fields ──
        log("[3/5] Custom fields...")
        cfs = []
        if p.get("rp"): cfs.append({"id": 241, "value": p["rp"]})
        if p.get("manager"): cfs.append({"id": 242, "value": p["manager"]})
        if p.get("ap"): cfs.append({"id": 243, "value": p["ap"]})
        cfs.append({"id": 247, "value": stage_value})
        if cfs:
            try:
                await self._api("PUT", f"/projects/{pid}.json", body={"project": {"custom_fields": cfs}})
                log(f"  ✓ {len(cfs)} CFs saved")
            except Exception as e:
                log(f"  ⚠ CF: {str(e)[:80]}")

        # ── PHASE 4: Passport ──
        log("[4/5] Passport...")
        pcf = []
        if cp_info:
            if cp_info.get("id"): pcf.append({"id": 118, "value": str(cp_info["id"])})
            if cp_info.get("name"):
                pcf.append({"id": 117, "value": cp_info["name"]})
                pcf.append({"id": 399, "value": cp_info["name"]})
        pcf.extend([{"id": 114, "value": stage_value}, {"id": 248, "value": stage}])
        pass_id = None
        try:
            ib = {"project_id": pid, "tracker_id": 41, "subject": "Паспорт проекта",
                   "description": p.get("description",""), "due_date": finish_date, "custom_fields": pcf}
            if ap_id: ib["assigned_to_id"] = ap_id
            d = await self._api_safe("POST", "/issues.json", body={"issue": ib})
            pass_id = d["issue"]["id"]
            log(f"  ✓ #{pass_id}")
        except Exception as e:
            log(f"  ⚠ {str(e)[:80]}")
        if pass_id:
            pw = set()
            if rp_id and rp_id != ap_id: pw.add(rp_id)
            if mgr_id and mgr_id != ap_id: pw.add(mgr_id)
            for wid in pw:
                try: await self._api_safe("POST", f"/issues/{pass_id}/watchers.json", body={"user_id": wid})
                except Exception: pass
            if p.get("description"):
                try: await self._api_safe("PUT", f"/issues/{pass_id}.json", body={"issue": {"notes": p["description"]}})
                except Exception: pass

        # ── PHASE 5: Calculation ──
        calc_id = None
        if not skip_calc:
            log("[5/5] Calculation...")
            calc_due = (dt.date.today() + dt.timedelta(days=2)).isoformat()
            try:
                cb = {"project_id": pid, "tracker_id": 28, "subject": "Расчёт",
                      "description": p.get("description",""), "due_date": calc_due,
                      "custom_fields": [{"id": 157, "value": "Расчёт"},
                                         {"id": 336, "value": (cp_info or {}).get("name", p.get("counterparty",""))}]}
                if presale_gid: cb["assigned_to_id"] = presale_gid
                elif rp_id: cb["assigned_to_id"] = rp_id
                d = await self._api_safe("POST", "/issues.json", body={"issue": cb})
                calc_id = d["issue"]["id"]
                log(f"  ✓ #{calc_id}")
            except Exception as e:
                log(f"  ⚠ {str(e)[:80]}")
            if calc_id:
                cw = set()
                if rp_id: cw.add(rp_id)
                if presale_gid: cw.add(presale_gid)
                for wid in calc_watcher_ids: cw.add(wid)
                for wid in cw:
                    try: await self._api_safe("POST", f"/issues/{calc_id}/watchers.json", body={"user_id": wid})
                    except Exception: pass
        else:
            log("[5/5] Calculation skipped")

        # ── Output ──
        result = {
            "success": True,
            "project": {"id": pid, "identifier": pident, "name": p["name"],
                         "url": f"{self.base_url}/projects/{pident}", "parent_id": parent_id},
            "fields": {}, "counterparty": cp_info or {"note": "N/A"},
            "tasks": {"passport": {"id": pass_id, "url": f"{self.base_url}/issues/{pass_id}"} if pass_id else "failed"},
            "memberships": mr, "log": log_lines,
        }
        if ap_id: result["fields"]["ap"] = p.get("ap"); result["fields"]["ap_id"] = ap_id
        if rp_id: result["fields"]["rp"] = p.get("rp"); result["fields"]["rp_id"] = rp_id
        if mgr_id: result["fields"]["manager"] = p.get("manager"); result["fields"]["manager_id"] = mgr_id
        result["fields"]["stage"] = stage
        if not skip_calc:
            result["tasks"]["calculation"] = {"id": calc_id, "url": f"{self.base_url}/issues/{calc_id}"} if calc_id else "failed"
        return json.dumps(result, ensure_ascii=False)
