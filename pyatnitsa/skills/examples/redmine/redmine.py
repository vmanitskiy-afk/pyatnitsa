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

import os
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
        self.rdm_login = os.getenv("RDM_LOGIN", "")
        self.rdm_password = os.getenv("RDM_PASSWORD", "")
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

    # ─── File upload ──────────────────────────────────────

    async def _api_upload(self, file_path: str) -> dict:
        """POST /uploads.json → {token, filename, size}."""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")
        filename = os.path.basename(file_path)
        with open(file_path, "rb") as f:
            data = f.read()
        url = f"{self.base_url}/uploads.json?filename={filename}"
        resp = await self._client.post(
            url, content=data,
            headers={"Content-Type": "application/octet-stream",
                     "X-Redmine-API-Key": self.api_key},
        )
        if resp.status_code >= 400:
            raise Exception(f"Upload failed ({resp.status_code}): {resp.text[:200]}")
        result = resp.json()
        return {"token": result["upload"]["token"], "filename": filename, "size": len(data)}

    async def _upload_and_attach(self, issue_id: int, file_path: str,
                                  filename: str | None = None,
                                  description: str | None = None) -> dict:
        """Загружает файл и прикрепляет к задаче."""
        attach_fn = filename or os.path.basename(file_path)
        upload = await self._api_upload(file_path)
        entry: dict[str, Any] = {
            "token": upload["token"], "filename": attach_fn,
            "content_type": "application/octet-stream",
        }
        if description:
            entry["description"] = description
        await self._api("PUT", f"/issues/{issue_id}.json", body={"issue": {"uploads": [entry]}})
        return {"filename": attach_fn, "size": upload["size"], "description": description}

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

    # ─── Playwright: create from template ───────────────────

    async def _resolve_template_id(self, tpl_ident: str) -> int | None:
        """Находит ID шаблона по идентификатору через API."""
        # Прямые эндпоинты
        for ep in [
            f"/project_templates/{tpl_ident}.json",
            f"/easy_project_templates/{tpl_ident}.json",
            f"/projects/{tpl_ident}.json",
        ]:
            try:
                data = await self._api("GET", ep)
                tid = (data.get("project_template") or data.get("easy_project_template") or data.get("project") or {}).get("id")
                if tid:
                    logger.info("template_resolved", ident=tpl_ident, id=tid, via=ep)
                    return tid
            except Exception:
                continue

        # Листинг всех шаблонов
        for ep in ["/project_templates.json", "/easy_project_templates.json"]:
            try:
                data = await self._api("GET", ep)
                items = data.get("project_templates") or data.get("easy_project_templates") or []
                found = next((t for t in items if t.get("identifier") == tpl_ident or t.get("name") == tpl_ident), None)
                if found:
                    logger.info("template_resolved", ident=tpl_ident, id=found["id"], via=f"list {ep}")
                    return found["id"]
            except Exception:
                continue
        return None

    async def _create_from_template(
        self, template_ident: str, name: str, identifier: str, parent_id: int | None = None,
    ) -> dict:
        """Создаёт проект из шаблона EasyRedmine через Playwright.

        EasyRedmine template endpoints защищены CSRF — API не работает,
        нужен браузер для заполнения формы /templates/{ident}/create.

        Returns:
            {"success": True, "id": int, "identifier": str, "name": str} или
            {"success": False, "error": str}
        """
        # Шаг 1: Проверяем шаблон существует
        tpl_id = await self._resolve_template_id(template_ident)
        if not tpl_id:
            return {"success": False, "error": f'Шаблон "{template_ident}" не найден.'}

        # Шаг 2: Проверяем parent
        if parent_id and not str(parent_id).isdigit():
            try:
                pd = await self._api("GET", f"/projects/{parent_id}.json")
                parent_id = pd["project"]["id"]
            except Exception:
                pass

        # Шаг 3: Playwright
        if not self.rdm_login or not self.rdm_password:
            return {"success": False, "error": "RDM_LOGIN и RDM_PASSWORD не заданы — Playwright недоступен."}

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return {"success": False, "error": "playwright не установлен. pip install playwright && playwright install chromium"}

        new_project_id = None
        new_ident = identifier

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                page = await browser.new_page()

                # Login
                await page.goto(f"{self.base_url}/login")
                await page.wait_for_load_state("domcontentloaded")
                await page.fill("#username", self.rdm_login)
                await page.fill("#password", self.rdm_password)
                await page.click('button[type="submit"]')

                # Ждём редиректа с /login
                for _ in range(10):
                    await page.wait_for_timeout(1000)
                    if "/login" not in page.url:
                        break
                if "/login" in page.url:
                    await browser.close()
                    return {"success": False, "error": f"Playwright: не удалось войти (URL={page.url})"}

                logger.info("playwright_logged_in", user=self.rdm_login)

                # Открываем форму создания из шаблона
                create_url = f"{self.base_url}/templates/{template_ident}/create"
                if parent_id:
                    create_url += f"?project[parent_id]={parent_id}"
                logger.info("playwright_opening", url=create_url)

                await page.goto(create_url, wait_until="domcontentloaded")
                await page.wait_for_timeout(3000)

                # Анализируем форму
                form_info = await page.evaluate("""() => {
                    const nameField = document.querySelector(
                        '#project_name, input[name="project[name]"], input[name="template[project][][name]"]'
                    );
                    const identField = document.querySelector(
                        '#project_identifier, input[name="project[identifier]"], input[name="template[project][][identifier]"]'
                    );
                    const submits = Array.from(document.querySelectorAll('input[type="submit"], button[type="submit"]'));
                    const submitLabels = submits.map(s => s.value || s.textContent.trim());
                    const isTemplateForm = !!document.querySelector('input[name="template[project][][identifier]"]');
                    const errorEl = document.querySelector('#errorExplanation');
                    return {
                        hasName: !!nameField,
                        hasIdent: !!identField,
                        isTemplateForm,
                        submitLabels,
                        error: errorEl ? errorEl.textContent.trim().substring(0, 200) : '',
                    };
                }""")

                logger.info("playwright_form", **form_info)

                if not form_info["hasName"]:
                    await browser.close()
                    return {"success": False, "error": f"Форма шаблона не найдена на {create_url}"}

                # Заполняем имя
                if form_info["isTemplateForm"]:
                    await page.fill('input[name="template[project][][name]"]', name)
                else:
                    await page.fill('#project_name, input[name="project[name]"]', name)

                # Заполняем идентификатор
                if form_info["hasIdent"]:
                    if form_info["isTemplateForm"]:
                        await page.fill('input[name="template[project][][identifier]"]', identifier)
                    else:
                        ident_field = page.locator('#project_identifier, input[name="project[identifier]"]')
                        await ident_field.fill("")
                        await ident_field.fill(identifier)

                logger.info("playwright_filled", name=name, identifier=identifier)

                # Нажимаем submit
                if form_info["hasName"] and any("Создать" in l or "Create" in l for l in form_info["submitLabels"]):
                    # EasyRedmine template form
                    if form_info["isTemplateForm"]:
                        # Заполняем через evaluate (надёжнее для template form)
                        await page.evaluate("""({name, identifier}) => {
                            const nameEl = document.querySelector('[name="template[project][][name]"]');
                            if (nameEl) nameEl.value = name;
                            const identEl = document.querySelector('[name="template[project][][identifier]"]');
                            if (identEl) identEl.value = identifier;
                        }""", {"name": name, "identifier": identifier})

                    # Кликаем кнопку (не "Экспорт")
                    submit_btn = page.locator('input[type="submit"]').first
                    # Пробуем найти кнопку "Создать" или первую submit
                    create_btns = page.locator('input[type="submit"]').filter(has_not_text="Экспорт")
                    if await create_btns.count() > 0:
                        submit_btn = create_btns.first
                    await submit_btn.click()
                else:
                    # Стандартная форма — первый submit
                    submit_btn = page.locator('input[type="submit"]').first
                    await submit_btn.click()

                # Ждём редиректа
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                await page.wait_for_timeout(5000)

                final_url = page.url
                logger.info("playwright_after_submit", url=final_url)

                # Проверяем ошибки на странице
                page_errors = await page.evaluate("""() => {
                    const el = document.querySelector('#errorExplanation, .flash.error');
                    return el ? el.textContent.trim().substring(0, 200) : null;
                }""")
                if page_errors:
                    logger.warning("playwright_page_error", error=page_errors)

                # Извлекаем проект из URL редиректа
                import re as _re
                proj_match = _re.search(r'/projects/([^/\?#]+)', final_url)
                if proj_match and '/new' not in final_url and '/create' not in final_url and '/templates/' not in final_url:
                    new_ident = proj_match.group(1)
                    try:
                        p_data = await self._api("GET", f"/projects/{new_ident}.json")
                        new_project_id = p_data["project"]["id"]
                        logger.info("playwright_project_created", id=new_project_id, ident=new_ident)
                    except Exception as e:
                        logger.warning("playwright_fetch_failed", ident=new_ident, error=str(e))

                await browser.close()

        except Exception as e:
            logger.error("playwright_error", error=str(e))
            return {"success": False, "error": f"Playwright: {str(e)[:200]}"}

        if not new_project_id:
            return {"success": False, "error": "Не удалось создать проект из шаблона — нет редиректа."}

        # Обновляем имя (на случай если шаблон переименовал)
        try:
            await self._api("PUT", f"/projects/{new_project_id}.json", body={
                "project": {"name": name, "identifier": identifier}
            })
        except Exception as e:
            logger.warning("playwright_rename_failed", error=str(e)[:80])

        return {
            "success": True,
            "id": new_project_id,
            "identifier": new_ident,
            "name": name,
            "template": template_ident,
            "template_id": tpl_id,
            "parent_id": parent_id,
            "url": f"{self.base_url}/projects/{new_ident}",
        }

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
                "Обязательно: name, description, counterparty. Опционально: ap, rp, manager, budget, curator, sfk.", {
                "type": "object", "properties": {
                    "name": {"type": "string"}, "description": {"type": "string"},
                    "counterparty": {"type": "string"}, "counterparty_id": {"type": "integer"},
                    "ap": {"type": "string"}, "ap_id": {"type": "integer"},
                    "rp": {"type": "string"}, "rp_id": {"type": "integer"},
                    "manager": {"type": "string"}, "manager_id": {"type": "integer"},
                    "stage": {"type": "string", "default": "Проектирование (расчёт)"},
                    "parent_id": {"type": "integer", "default": 25},
                    "finish": {"type": "string", "description": "Дата завершения YYYY-MM-DD"},
                    "template": {"type": "string", "default": "trade_v2"},
                    "budget": {"type": "string", "description": "Бюджет (CF 249)"},
                    "curator": {"type": "string", "description": "Куратор (CF 378)"},
                    "sfk": {"type": "string", "description": "СФК (CF 396 на Паспорте)"},
                    "no_calculation": {"type": "boolean", "default": False},
                    "attach_passport": {"type": "array", "items": {"type": "string"},
                                        "description": "Файлы для Паспорта (path или path|filename)"},
                    "attach_calculation": {"type": "array", "items": {"type": "string"},
                                           "description": "Файлы для Расчёта (path или path|filename)"},
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
            LLMTool("redmine.create_from_template",
                "Создаёт проект из шаблона EasyRedmine через Playwright (browser automation). "
                "Используй когда нужен именно шаблон, а не пустой проект.", {
                "type": "object", "properties": {
                    "template": {"type": "string", "description": "Идентификатор шаблона (напр. trade_v2)"},
                    "name": {"type": "string", "description": "Название нового проекта"},
                    "parent_id": {"type": "integer", "description": "ID родительского проекта"},
                }, "required": ["template", "name"],
            }),
            LLMTool("redmine.attach", "Загрузить файл и прикрепить к задаче/сделке в Redmine. Используй абсолютный путь из контекста файлов [File: ... | path: ...]", {
                "type": "object", "properties": {
                    "issue_id": {"type": "integer", "description": "ID задачи/сделки в Redmine"},
                    "file_path": {"type": "string", "description": "Абсолютный путь к файлу на диске (из поля path в контексте)"},
                    "filename": {"type": "string", "description": "Имя файла (опционально)"},
                    "description": {"type": "string", "description": "Описание вложения"},
                }, "required": ["issue_id", "file_path"],
            }),
            LLMTool("redmine.apply_custom_menu",
                "Применить настраиваемое меню к проекту (Playwright). "
                "Включает чекбокс, удаляет дубликаты, добавляет пункты.", {
                "type": "object", "properties": {
                    "project": {"type": "string", "description": "Идентификатор проекта"},
                    "items": {"type": "array", "description": "Пункты меню [{name, url}]",
                              "items": {"type": "object", "properties": {
                                  "name": {"type": "string"}, "url": {"type": "string"}}}},
                }, "required": ["project"],
            }),
            LLMTool("redmine.inspect_cfs",
                "Инспекция кастомных полей проекта через Playwright (настройки + edit). "
                "Полезно для отладки — показывает все select с custom_field в name.", {
                "type": "object", "properties": {
                    "project": {"type": "string", "description": "Идентификатор проекта"},
                }, "required": ["project"],
            }),
            LLMTool("redmine.statuses", "Список статусов задач в Redmine", {
                "type": "object", "properties": {},
            }),
            LLMTool("redmine.trackers", "Список трекеров (типов задач) в Redmine", {
                "type": "object", "properties": {},
            }),
            LLMTool("redmine.set_group_roles", "Назначить роли группе в проекте (fuzzy поиск группы по имени)", {
                "type": "object", "properties": {
                    "project": {"type": "string", "description": "ID или идентификатор проекта"},
                    "group": {"type": "string", "description": "Название группы (fuzzy поиск)"},
                    "roles": {"type": "string", "description": "Роли через запятую (названия или ID)"},
                }, "required": ["project", "group", "roles"],
            }),
            LLMTool("redmine.update_deal_template", "Обновить шаблон Сделка 2.0 из живого проекта (сохраняет JSON-снапшот настроек)", {
                "type": "object", "properties": {
                    "project": {"type": "string", "description": "ID или идентификатор проекта-шаблона"},
                    "output": {"type": "string", "description": "Путь для сохранения JSON (опционально)"},
                }, "required": ["project"],
            }),
            LLMTool("redmine.create_contact_from_inn",
                "Создать контрагента (Easy Contact) в EasyRedmine по ИНН. "
                "Ищет существующий контакт, при отсутствии — получает данные из Rusprofile и создаёт новый.", {
                "type": "object", "properties": {
                    "inn": {"type": "string", "description": "ИНН организации (10 цифр) или ИП (12 цифр)"},
                    "region": {"type": "string", "description": "Регион для сокращения названия (опционально)"},
                }, "required": ["inn"],
            }),
            LLMTool("redmine.discover_contact_fields",
                "Обнаружить поля формы Easy Contact через Playwright (для диагностики маппинга CF). "
                "Требует RDM_LOGIN и RDM_PASSWORD.", {
                "type": "object", "properties": {},
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
                "create_from_template": self._exec_create_from_template,
                "attach": self._attach,
                "apply_custom_menu": self._apply_custom_menu,
                "inspect_cfs": self._inspect_cfs,
                "statuses": self._list_statuses,
                "trackers": self._list_trackers,
                "set_group_roles": self._set_group_roles,
                "update_deal_template": self._update_deal_template,
                "create_contact_from_inn": self._create_contact_from_inn,
                "discover_contact_fields": self._discover_contact_fields,
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

    async def _exec_create_from_template(self, p):
        """Обёртка для вызова _create_from_template как инструмента."""
        ident = p.get("identifier") or slugify_identifier(p["name"])
        result = await self._create_from_template(
            template_ident=p["template"],
            name=p["name"],
            identifier=ident,
            parent_id=p.get("parent_id"),
        )
        return json.dumps(result, ensure_ascii=False)


    async def _attach(self, p):
        """Загружает файл и прикрепляет к задаче."""
        logger.info("redmine_attach", issue_id=p.get("issue_id"), file_path=p.get("file_path"))
        try:
            result = await self._upload_and_attach(
                issue_id=int(p["issue_id"]),
                file_path=p["file_path"],
                filename=p.get("filename"),
                description=p.get("description"),
            )
            logger.info("redmine_attach_ok", issue_id=p["issue_id"])
            return json.dumps({"success": True, "issue_id": p["issue_id"],
                               "attachment": result}, ensure_ascii=False)
        except Exception as e:
            logger.error("redmine_attach_error", issue_id=p.get("issue_id"),
                         file_path=p.get("file_path"), error=str(e))
            return json.dumps({"success": False, "issue_id": p["issue_id"],
                               "file": p["file_path"], "message": str(e)[:200]}, ensure_ascii=False)

    # ── Playwright: Apply custom menu ─────────────────────

    async def _apply_custom_menu(self, p: dict) -> str:
        """Применяет настраиваемое меню к проекту через Playwright."""
        project = p["project"]
        items = p.get("items") or [
            {"name": "+ Командировка", "url": f"{self.base_url}/templates/trip202022131511-template-5/create"},
        ]

        if not self.rdm_login or not self.rdm_password:
            return json.dumps({"success": False, "error": "RDM_LOGIN/RDM_PASSWORD не заданы"}, ensure_ascii=False)

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return json.dumps({"success": False, "error": "playwright not installed"})

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                page = await browser.new_page()

                # Login
                await page.goto(f"{self.base_url}/login")
                await page.wait_for_load_state("domcontentloaded")
                await page.fill("#username", self.rdm_login)
                await page.fill("#password", self.rdm_password)
                await page.click('button[type="submit"]')
                await page.wait_for_timeout(2000)
                if "/login" in page.url:
                    await browser.close()
                    return json.dumps({"success": False, "error": "Login failed"})

                modules_url = f"{self.base_url}/projects/{project}/settings?tab=modules"
                await page.goto(modules_url, wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)

                body_text = await page.evaluate("() => document.body.innerText")
                if "403" in body_text:
                    await browser.close()
                    return json.dumps({"success": False, "error": "403 Forbidden"})

                # Enable custom menu checkbox
                cb_sel = "#project_easy_has_custom_menu"
                if await page.locator(cb_sel).count() > 0 and not await page.is_checked(cb_sel):
                    await page.check(cb_sel)
                    await page.locator('input[type="submit"][value="Сохранить"]').first.click()
                    await page.wait_for_load_state("domcontentloaded")
                    await page.wait_for_timeout(2000)
                    await page.goto(modules_url, wait_until="domcontentloaded")
                    await page.wait_for_timeout(2000)

                # Delete existing custom items (clean slate)
                await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(500)

                custom_items = await page.evaluate("""() => {
                    return Array.from(document.querySelectorAll('tr')).map(row => {
                        const deleteLink = Array.from(row.querySelectorAll('a')).find(a => a.textContent.trim() === 'Удалить');
                        if (!deleteLink) return null;
                        const name = row.querySelector('td')?.textContent?.trim();
                        return { name, deleteHref: deleteLink.href };
                    }).filter(Boolean);
                }""")

                for item in custom_items:
                    page.once("dialog", lambda d: d.accept())
                    await page.evaluate("""href => {
                        const link = Array.from(document.querySelectorAll('a')).find(a => a.href === href);
                        if (link) link.click();
                    }""", item["deleteHref"])
                    await page.wait_for_timeout(1000)
                    await page.goto(modules_url, wait_until="domcontentloaded")
                    await page.wait_for_timeout(1000)
                    await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(300)

                # Add new items via modal
                created = 0
                for item in items:
                    await page.goto(modules_url, wait_until="domcontentloaded")
                    await page.wait_for_timeout(1500)
                    await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(300)

                    add_btn = page.locator("a").filter(has_text="Добавить пункт настраиваемого меню")
                    if await add_btn.count() > 0:
                        await add_btn.first.click(force=True)
                    else:
                        continue

                    try:
                        await page.wait_for_selector("#easy_custom_project_menu_name", timeout=5000)
                    except Exception:
                        await page.wait_for_timeout(2000)

                    name_field = page.locator("#easy_custom_project_menu_name")
                    if await name_field.count() > 0:
                        await name_field.fill(item["name"])
                        url_field = page.locator("#easy_custom_project_menu_url")
                        if await url_field.count() > 0 and item.get("url"):
                            await url_field.fill(item["url"])

                        await page.evaluate("""() => {
                            const form = document.querySelector('#ajax-modal form, .ui-dialog form, form[action*="easy_custom_project_menu"]');
                            if (form && window.jQuery) window.jQuery(form).trigger('submit');
                            else if (form) form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
                        }""")
                        await page.wait_for_timeout(2000)
                        created += 1

                await browser.close()
                return json.dumps({"success": True, "project": project, "created": created,
                                   "deleted": len(custom_items)}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)[:200]}, ensure_ascii=False)

    # ── Playwright: Inspect custom fields ─────────────────

    async def _inspect_cfs(self, p: dict) -> str:
        """Инспекция кастомных полей проекта через Playwright."""
        project = p["project"]

        if not self.rdm_login or not self.rdm_password:
            return json.dumps({"success": False, "error": "RDM_LOGIN/RDM_PASSWORD не заданы"}, ensure_ascii=False)

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return json.dumps({"success": False, "error": "playwright not installed"})

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                page = await browser.new_page()

                # Login
                await page.goto(f"{self.base_url}/login")
                await page.fill("#username", self.rdm_login)
                await page.fill("#password", self.rdm_password)
                await page.click('button[type="submit"]')
                await page.wait_for_timeout(2000)

                result = {"project": project, "settings": {}, "edit": {}}

                # Settings page
                await page.goto(f"{self.base_url}/projects/{project}/settings", wait_until="domcontentloaded")
                await page.wait_for_timeout(3000)

                result["settings"] = await page.evaluate("""() => {
                    const tabs = Array.from(document.querySelectorAll('a'))
                        .filter(a => a.href && a.href.includes('/settings'))
                        .map(a => a.textContent.trim() + ' -> ' + a.href).slice(0, 10);
                    const selects = Array.from(document.querySelectorAll('select'))
                        .map(s => s.name).filter(n => n.includes('custom'));
                    return { tabs, customSelects: selects, url: location.href };
                }""")

                # Edit page
                await page.goto(f"{self.base_url}/projects/{project}/edit", wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)

                CF_IDS = [241, 242, 243, 247, 249, 378, 152]
                result["edit"] = await page.evaluate(f"""() => {{
                    const cfIds = {CF_IDS};
                    const selects = [];
                    for (const sel of document.querySelectorAll('select')) {{
                        if (sel.name.includes('custom') || cfIds.some(id => sel.name.includes(String(id)))) {{
                            const opts = Array.from(sel.options).slice(0, 8).map(o => o.value + '|' + o.textContent.trim());
                            selects.push({{ name: sel.name, id: sel.id, val: sel.value, opts }});
                        }}
                    }}
                    return {{ url: location.href, selects, totalSelects: document.querySelectorAll('select').length }};
                }}""")

                await browser.close()
                return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)[:200]}, ensure_ascii=False)

    # ═══════════════════════════════════════════════════════
    # CREATE DEAL PROJECT (5 phases)
    # ═══════════════════════════════════════════════════════

    async def _create_deal_project(self, p):
        parent_id = p.get("parent_id", 25)
        stage = p.get("stage", "Проектирование (расчёт)")
        stage_value = STAGE_MAP.get(stage, stage)
        skip_calc = p.get("no_calculation", False)
        finish_date = p.get("finish", f"{dt.date.today().year}-12-31")
        template_ident = p.get("template", "trade_v2")
        log_lines = []
        all_warnings = []

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
                org = next((f["value"] for f in c.get("custom_fields", []) if f.get("id") == 2), "")
                fn = f"{c.get('lastname', '')} {c.get('firstname', '')}".strip()
                inn = next((f["value"] for f in c.get("custom_fields", []) if f.get("id") == 6), None)
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

        # Users — опциональны, HARD STOP только если указано но не резолвится
        fields = [("АП", p.get("ap"), p.get("ap_id"), "ap_id"),
                   ("РП", p.get("rp"), p.get("rp_id"), "rp_id"),
                   ("Менеджер", p.get("manager"), p.get("manager_id"), "manager_id")]
        ru = {}
        for label, nv, idv, idf in fields:
            if idv:
                ru[label] = {"id": int(idv), "name": nv or f"User #{idv}"}
                continue
            if not nv:
                log(f"  · {label}: не указан — пропускаем")
                continue
            cands = await self._resolve_user(nv, parent_id)
            resolved, error = resolve_choice(label, nv, cands, hint=f"Укажите {idf}.")
            if not resolved:
                return json.dumps(error, ensure_ascii=False)
            ru[label] = resolved
        ap_id = ru.get("АП", {}).get("id")
        rp_id = ru.get("РП", {}).get("id")
        mgr_id = ru.get("Менеджер", {}).get("id")
        for l in ("АП", "РП", "Менеджер"):
            if l in ru:
                log(f"  ✓ {l}: {ru[l]['name']} (#{ru[l]['id']})")

        # Пресейл group (SOFT)
        presale_gid = None
        members = await self._get_members(parent_id)
        for m in members:
            if m.get("group") and "пресейл" in m["group"]["name"].lower():
                presale_gid = m["group"]["id"]
                break

        calc_watcher_ids = []
        for wn in CALC_FIXED_WATCHERS:
            u = await self._resolve_user_soft(wn, parent_id)
            if u:
                calc_watcher_ids.append(u["id"])

        log("Phase 0 done.\n")

        # ── PHASE 1: Create project from template ──
        log("[1/5] Creating project from template...")
        ident = slugify_identifier(p["name"])

        tpl_result = await self._create_from_template(
            template_ident=template_ident,
            name=p["name"],
            identifier=ident,
            parent_id=parent_id,
        )

        if tpl_result.get("success"):
            pid = tpl_result["id"]
            pident = tpl_result["identifier"]
            log(f"  ✓ Template #{pid} ({pident})")
        else:
            log(f"  ⚠ Template failed: {tpl_result.get('error', '?')[:80]}")
            log("  Fallback: creating via API...")
            try:
                data = await self._api_safe("POST", "/projects.json", body={"project": {
                    "name": p["name"], "identifier": ident, "is_public": False, "parent_id": parent_id}})
                pid = data["project"]["id"]
                pident = data["project"]["identifier"]
                log(f"  ✓ API #{pid} ({pident})")
            except Exception as e:
                return json.dumps({"success": False, "error": "create_failed",
                    "template_error": tpl_result.get("error"), "api_error": str(e)[:200]}, ensure_ascii=False)

        # ── PHASE 2: Roles ──
        log("[2/5] Roles...")
        mr = {}
        for key, uid, roles in [("rp", rp_id, DEAL_ROLE_MAP["rp"]),
                                  ("ap", ap_id, DEAL_ROLE_MAP["ap"]),
                                  ("manager", mgr_id, DEAL_ROLE_MAP["manager"])]:
            if not uid:
                continue
            r = await self._set_user_roles(pid, uid, roles)
            mr[key] = {"user_id": uid, **r}
            log(f"  ✓ {key} (#{uid}): {r.get('action')}")

        # ── PHASE 3: Custom fields (SOFT — Playwright scrape + fuzzy match) ──
        log("[3/5] Custom fields...")
        cf_inputs = {}
        if p.get("rp"):
            cf_inputs[241] = p["rp"]
        if p.get("manager"):
            cf_inputs[242] = p["manager"]
        if p.get("ap"):
            cf_inputs[243] = p["ap"]
        cf_inputs[247] = stage_value
        if p.get("budget"):
            cf_inputs[249] = p["budget"]
        if p.get("curator"):
            cf_inputs[378] = p["curator"]

        cf_warnings = []
        cf_options = {}
        try:
            from playwright.async_api import async_playwright
            rdm_login = os.getenv("RDM_LOGIN", "aione")
            rdm_password = os.getenv("RDM_PASSWORD", "")

            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                bpage = await browser.new_page()
                await bpage.goto(f"{self.base_url}/login")
                await bpage.wait_for_load_state("domcontentloaded")
                await bpage.fill("#username", rdm_login)
                await bpage.fill("#password", rdm_password)
                await bpage.click('button[type="submit"]')
                try:
                    await bpage.wait_for_load_state("networkidle")
                except Exception:
                    pass
                await bpage.wait_for_timeout(2000)

                await bpage.goto(f"{self.base_url}/projects/{pident}/settings/info",
                                 wait_until="domcontentloaded")
                await bpage.wait_for_timeout(2000)

                cf_ids_list = list(cf_inputs.keys())
                cf_options = await bpage.evaluate(
                    """(cfIds) => {
                    const result = {};
                    for (const cfId of cfIds) {
                        const sel = document.querySelector(
                            'select[name="project[custom_field_values][' + cfId + ']"]');
                        if (sel) {
                            result[cfId] = Array.from(sel.options)
                                .filter(o => o.value !== '')
                                .map(o => ({ value: o.value, text: o.textContent.trim() }));
                        }
                    }
                    return result;
                    }""", cf_ids_list)
                await browser.close()
        except Exception as e:
            log(f"  ⚠ CF scrape failed: {str(e)[:80]}")

        def resolve_cf(cf_id, inp):
            if not inp:
                return None
            opts_list = cf_options.get(str(cf_id)) or cf_options.get(cf_id) or []
            if not opts_list:
                return inp
            exact = next((o for o in opts_list if o["value"] == inp), None)
            if exact:
                return exact["value"]
            exact_text = next((o for o in opts_list if o["text"] == inp), None)
            if exact_text:
                return exact_text["value"]
            lower = inp.lower()
            matches = [o for o in opts_list
                       if lower in o["value"].lower() or lower in o["text"].lower()]
            if len(matches) == 1:
                return matches[0]["value"]
            if len(matches) > 1:
                cf_warnings.append({"cf_id": cf_id, "input": inp, "error": "ambiguous",
                                    "candidates": [m["value"] for m in matches[:5]]})
                return None
            cf_warnings.append({"cf_id": cf_id, "input": inp, "error": "not_found"})
            return None

        cf_updates = []
        for cf_id, inp in cf_inputs.items():
            resolved_val = resolve_cf(cf_id, inp)
            if resolved_val is not None:
                cf_updates.append({"id": cf_id, "value": resolved_val})
                log(f'  ✓ cf_{cf_id}: "{inp}" → "{resolved_val}"')
            else:
                w = next((w for w in cf_warnings if w["cf_id"] == cf_id), None)
                if w and w["error"] == "ambiguous":
                    log(f'  ⚠ cf_{cf_id}: "{inp}" → ambiguous. Field left empty.')
                else:
                    log(f'  ⚠ cf_{cf_id}: "{inp}" → not found. Field left empty.')

        if cf_updates:
            try:
                await self._api("PUT", f"/projects/{pid}.json",
                                body={"project": {"custom_fields": cf_updates}})
                log(f"  ✓ Saved {len(cf_updates)} CFs")
            except Exception as e:
                log(f"  ⚠ CF PUT failed: {str(e)[:80]}")

        # ── PHASE 4: Passport + Calculation ──
        log("[4/5] Passport...")
        pcf = []
        if cp_info:
            if cp_info.get("id"):
                pcf.append({"id": 118, "value": str(cp_info["id"])})
            if cp_info.get("name"):
                pcf.append({"id": 117, "value": cp_info["name"]})
                pcf.append({"id": 399, "value": cp_info["name"]})
        pcf.extend([{"id": 114, "value": stage_value}, {"id": 248, "value": stage}])
        if p.get("sfk"):
            pcf.append({"id": 396, "value": str(p["sfk"])})

        pass_id = None
        try:
            ib = {"project_id": pid, "tracker_id": 41, "subject": "Паспорт проекта",
                   "description": p.get("description", ""), "due_date": finish_date,
                   "custom_fields": pcf}
            if ap_id:
                ib["assigned_to_id"] = ap_id
            d = await self._api_safe("POST", "/issues.json", body={"issue": ib})
            pass_id = d["issue"]["id"]
            log(f"  ✓ #{pass_id}")
        except Exception as e:
            log(f"  ⚠ {str(e)[:80]}")

        if pass_id:
            pw_set = set()
            if rp_id and rp_id != ap_id:
                pw_set.add(rp_id)
            if mgr_id and mgr_id != ap_id:
                pw_set.add(mgr_id)
            for wid in pw_set:
                try:
                    await self._api_safe("POST", f"/issues/{pass_id}/watchers.json",
                                         body={"user_id": wid})
                except Exception:
                    pass
            if p.get("description"):
                try:
                    await self._api_safe("PUT", f"/issues/{pass_id}.json",
                                         body={"issue": {"notes": p["description"]}})
                except Exception:
                    pass

        # Calculation
        calc_id = None
        if not skip_calc:
            log("[5/5] Calculation...")
            calc_due = (dt.date.today() + dt.timedelta(days=2)).isoformat()
            try:
                cb = {"project_id": pid, "tracker_id": 28, "subject": "Расчёт",
                      "description": p.get("description", ""), "due_date": calc_due,
                      "custom_fields": [
                          {"id": 157, "value": "Расчёт"},
                          {"id": 336, "value": (cp_info or {}).get("name", p.get("counterparty", ""))}]}
                if presale_gid:
                    cb["assigned_to_id"] = presale_gid
                elif rp_id:
                    cb["assigned_to_id"] = rp_id
                d = await self._api_safe("POST", "/issues.json", body={"issue": cb})
                calc_id = d["issue"]["id"]
                log(f"  ✓ #{calc_id}")
            except Exception as e:
                log(f"  ⚠ {str(e)[:80]}")
            if calc_id:
                cw = set()
                if rp_id:
                    cw.add(rp_id)
                if presale_gid:
                    cw.add(presale_gid)
                for wid in calc_watcher_ids:
                    cw.add(wid)
                for wid in cw:
                    try:
                        await self._api_safe("POST", f"/issues/{calc_id}/watchers.json",
                                             body={"user_id": wid})
                    except Exception:
                        pass
        else:
            log("[5/5] Calculation skipped")

        # ── PHASE 3.5: Attach files ──
        passport_attachments = []
        calc_attachments = []
        attach_warnings = []

        def parse_attach_arg(raw):
            """Парсит 'path|filename' → (path, filename) или (path, None)."""
            pipe_idx = raw.rfind("|")
            if pipe_idx > 0 and pipe_idx < len(raw) - 1:
                return raw[:pipe_idx], raw[pipe_idx + 1:]
            return raw, None

        for raw in (p.get("attach_passport") or []):
            fp, fn = parse_attach_arg(raw)
            if pass_id:
                try:
                    att = await self._upload_and_attach(pass_id, fp, filename=fn)
                    passport_attachments.append(att)
                    log(f"  ✓ Passport attach: {att['filename']}")
                except Exception as e:
                    attach_warnings.append({"kind": "attachment", "issue_id": pass_id,
                                            "file": fp, "message": str(e)[:120]})
            else:
                attach_warnings.append({"kind": "attachment", "issue_id": None,
                                        "file": fp, "message": "Passport task was not created"})

        for raw in (p.get("attach_calculation") or []):
            fp, fn = parse_attach_arg(raw)
            if skip_calc:
                attach_warnings.append({"kind": "attachment", "issue_id": None,
                                        "file": fp, "message": "Calculation skipped"})
            elif calc_id:
                try:
                    att = await self._upload_and_attach(calc_id, fp, filename=fn)
                    calc_attachments.append(att)
                    log(f"  ✓ Calc attach: {att['filename']}")
                except Exception as e:
                    attach_warnings.append({"kind": "attachment", "issue_id": calc_id,
                                            "file": fp, "message": str(e)[:120]})
            else:
                attach_warnings.append({"kind": "attachment", "issue_id": None,
                                        "file": fp, "message": "Calculation task was not created"})

        all_warnings = cf_warnings + attach_warnings

        # ── Output ──
        result = {
            "success": True,
            "project": {"id": pid, "identifier": pident, "name": p["name"],
                         "url": f"{self.base_url}/projects/{pident}", "parent_id": parent_id},
            "template": template_ident,
            "fields": {}, "counterparty": cp_info or {"note": "N/A"},
            "tasks": {}, "memberships": mr, "log": log_lines,
        }
        if ap_id:
            result["fields"]["ap"] = p.get("ap")
            result["fields"]["ap_id"] = ap_id
        if rp_id:
            result["fields"]["rp"] = p.get("rp")
            result["fields"]["rp_id"] = rp_id
        if mgr_id:
            result["fields"]["manager"] = p.get("manager")
            result["fields"]["manager_id"] = mgr_id
        result["fields"]["stage"] = stage
        result["fields"]["finish"] = finish_date

        if pass_id:
            t = {"id": pass_id, "url": f"{self.base_url}/issues/{pass_id}"}
            if passport_attachments:
                t["attachments"] = passport_attachments
            result["tasks"]["passport"] = t
        else:
            result["tasks"]["passport"] = "failed"

        if not skip_calc:
            if calc_id:
                t = {"id": calc_id, "url": f"{self.base_url}/issues/{calc_id}"}
                if calc_attachments:
                    t["attachments"] = calc_attachments
                result["tasks"]["calculation"] = t
            else:
                result["tasks"]["calculation"] = "failed"

        if all_warnings:
            result["warnings"] = all_warnings

        return json.dumps(result, ensure_ascii=False)


    # ── statuses / trackers ──────────────────────────────

    async def _list_statuses(self, p):
        data = await self._api("GET", "/issue_statuses.json")
        statuses = [{"id": s["id"], "name": s["name"], "is_closed": s.get("is_closed", False)}
                    for s in data.get("issue_statuses", [])]
        return json.dumps(statuses, ensure_ascii=False)

    async def _list_trackers(self, p):
        data = await self._api("GET", "/trackers.json")
        trackers = [{"id": t["id"], "name": t["name"]} for t in data.get("trackers", [])]
        return json.dumps(trackers, ensure_ascii=False)

    # ── set_group_roles ──────────────────────────────────

    async def _set_group_roles(self, p):
        project = p["project"]
        group_name = p["group"]
        roles_str = p["roles"]

        # 1) Resolve project id
        proj_data = await self._api("GET", f"/projects/{project}.json")
        project_id = proj_data["project"]["id"]

        # 2) Resolve group — try /groups.json (admin), fallback to memberships
        all_groups = []
        try:
            gdata = await self._api("GET", "/groups.json")
            all_groups = [{"id": g["id"], "name": g["name"]} for g in gdata.get("groups", [])]
        except Exception:
            pass

        if not all_groups:
            # Scan project memberships
            offset = 0
            while True:
                mdata = await self._api("GET", f"/projects/{project_id}/memberships.json",
                                        params={"limit": 100, "offset": offset})
                batch = mdata.get("memberships", [])
                for m in batch:
                    if m.get("group"):
                        g = m["group"]
                        if not any(x["id"] == g["id"] for x in all_groups):
                            all_groups.append({"id": g["id"], "name": g["name"]})
                if len(batch) < 100:
                    break
                offset += 100

            # Also try parent project
            if not all_groups:
                try:
                    parent_id = proj_data["project"].get("parent", {}).get("id")
                    if parent_id:
                        pmem = await self._api("GET", f"/projects/{parent_id}/memberships.json",
                                               params={"limit": 200})
                        for m in pmem.get("memberships", []):
                            if m.get("group"):
                                g = m["group"]
                                if not any(x["id"] == g["id"] for x in all_groups):
                                    all_groups.append({"id": g["id"], "name": g["name"]})
                except Exception:
                    pass

        needle = group_name.lower()
        exact = [g for g in all_groups if g["name"].lower() == needle]
        if exact:
            group = exact[0]
        else:
            partial = [g for g in all_groups if needle in g["name"].lower()]
            if len(partial) == 0:
                return json.dumps({"error": f"Group '{group_name}' not found. Available: {[g['name'] for g in all_groups]}"}, ensure_ascii=False)
            elif len(partial) > 1:
                return json.dumps({"error": f"Multiple groups match '{group_name}': {[g['name'] for g in partial]}"}, ensure_ascii=False)
            group = partial[0]

        # 3) Resolve role IDs
        all_roles = []
        try:
            rdata = await self._api("GET", "/roles.json")
            all_roles = rdata.get("roles", [])
        except Exception:
            pass

        requested = [r.strip() for r in roles_str.split(",")]
        role_ids = []
        for req in requested:
            if req.isdigit():
                role_ids.append(int(req))
            else:
                match = next((r for r in all_roles if r["name"].lower() == req.lower()), None)
                if not match:
                    match = next((r for r in all_roles if req.lower() in r["name"].lower()), None)
                if match:
                    role_ids.append(match["id"])
                else:
                    return json.dumps({"error": f"Role '{req}' not found. Available: {[r['name'] for r in all_roles]}"}, ensure_ascii=False)

        # 4) Check if group already has membership → update or create
        mdata = await self._api("GET", f"/projects/{project_id}/memberships.json", params={"limit": 200})
        existing_membership = None
        for m in mdata.get("memberships", []):
            if m.get("group") and m["group"]["id"] == group["id"]:
                existing_membership = m
                break

        if existing_membership:
            # Merge with existing roles
            existing_role_ids = [r["id"] for r in existing_membership.get("roles", [])]
            merged = list(set(existing_role_ids + role_ids))
            await self._api("PUT", f"/memberships/{existing_membership['id']}.json",
                            body={"membership": {"role_ids": merged}})
            action_taken = "updated"
        else:
            await self._api("POST", f"/projects/{project_id}/memberships.json",
                            body={"membership": {"group_id": group["id"], "role_ids": role_ids}})
            action_taken = "created"

        return json.dumps({
            "success": True, "action": action_taken,
            "project_id": project_id, "group": group["name"], "group_id": group["id"],
            "role_ids": role_ids,
        }, ensure_ascii=False)

    # ── update_deal_template ─────────────────────────────

    async def _update_deal_template(self, p):
        import os as _os
        project_id = p["project"]
        home = _os.path.expanduser("~")
        output_dir = _os.path.join(home, ".pyatnitsa", "redmine-templates")
        _os.makedirs(output_dir, exist_ok=True)
        json_path = p.get("output") or _os.path.join(output_dir, "DEAL-2.0-PROJECT-SETTINGS.json")

        # Load existing (for merging)
        existing = {}
        try:
            with open(json_path, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            pass

        # Step 1: Project info
        proj_data = await self._api("GET", f"/projects/{project_id}.json",
                                    params={"include": "trackers,enabled_modules,issue_categories,custom_fields"})
        proj = proj_data["project"]
        visible_cfs = [{"id": cf["id"], "name": cf["name"], "value": cf.get("value")}
                       for cf in proj.get("custom_fields", [])]

        # Step 2: Memberships (paginated)
        all_memberships = []
        offset = 0
        while True:
            mdata = await self._api("GET", f"/projects/{project_id}/memberships.json",
                                    params={"limit": 100, "offset": offset})
            batch = mdata.get("memberships", [])
            all_memberships.extend(batch)
            if len(batch) < 100:
                break
            offset += 100

        # Step 3: Versions
        versions = []
        try:
            vdata = await self._api("GET", f"/projects/{project_id}/versions.json")
            versions = vdata.get("versions", [])
        except Exception:
            pass

        # Step 4: Issue custom fields (multi-strategy)
        issue_cfs = []

        # Approach 1: include=issue_custom_fields
        try:
            d = await self._api("GET", f"/projects/{project_id}.json",
                                params={"include": "issue_custom_fields"})
            if d["project"].get("issue_custom_fields"):
                issue_cfs = [{"id": f["id"], "name": f["name"]}
                             for f in d["project"]["issue_custom_fields"]]
        except Exception:
            pass

        # Approach 2: /custom_fields.json (admin only)
        if not issue_cfs:
            try:
                d = await self._api("GET", "/custom_fields.json")
                issue_cfs = [{"id": f["id"], "name": f["name"]}
                             for f in d.get("custom_fields", [])
                             if f.get("customized_type") == "issue"]
            except Exception:
                pass

        # Approach 3: Scan issues in project
        if not issue_cfs:
            cf_map = {}
            try:
                idata = await self._api("GET", "/issues.json",
                                        params={"project_id": project_id, "limit": 10, "status_id": "*"})
                for issue in idata.get("issues", [])[:5]:
                    try:
                        idet = await self._api("GET", f"/issues/{issue['id']}.json",
                                               params={"include": "custom_fields"})
                        for cf in idet.get("issue", {}).get("custom_fields", []):
                            if cf["id"] not in cf_map:
                                cf_map[cf["id"]] = cf["name"]
                    except Exception:
                        continue
            except Exception:
                pass
            if cf_map:
                issue_cfs = [{"id": k, "name": v} for k, v in sorted(cf_map.items())]

        # Fallback: keep existing if we found nothing
        if not issue_cfs and existing.get("issue_custom_fields"):
            issue_cfs = existing["issue_custom_fields"]

        # Step 5: Custom menu
        custom_menu = []
        for ep in [f"/projects/{project_id}/easy_custom_menus.json",
                   f"/easy_custom_menus.json?project_id={project_id}"]:
            try:
                d = await self._api("GET", ep)
                items = d.get("easy_custom_menus") or d.get("custom_menus") or []
                if items:
                    custom_menu = [{"id": m.get("id"), "name": m.get("name") or m.get("label"),
                                    "url": m.get("url"), "position": m.get("position", i + 1)}
                                   for i, m in enumerate(items)]
                    break
            except Exception:
                continue
        if not custom_menu and existing.get("custom_menu"):
            custom_menu = existing["custom_menu"]

        # Step 6: Assemble groups
        group_map = {}
        for m in all_memberships:
            if m.get("group"):
                gid = m["group"]["id"]
                if gid not in group_map:
                    group_map[gid] = {"id": gid, "name": m["group"]["name"],
                                      "type": "group", "role_ids": [], "role_names": []}
                for r in m.get("roles", []):
                    if r["id"] not in group_map[gid]["role_ids"]:
                        group_map[gid]["role_ids"].append(r["id"])
                        group_map[gid]["role_names"].append(r["name"])

        template = {
            "meta": {
                "template_name": "Сделка 2.0",
                "source_project": project_id,
                "source_project_id": proj["id"],
                "exported_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
            },
            "project": {
                "id": proj["id"],
                "identifier": proj["identifier"],
                "name": proj["name"],
                "parent_id": proj.get("parent", {}).get("id"),
                "is_public": proj.get("is_public", False),
                "inherit_members": proj.get("inherit_members", False),
                "visible_custom_fields": [cf["id"] for cf in visible_cfs],
                "visible_custom_fields_detail": visible_cfs,
            },
            "trackers": [{"id": t["id"], "name": t["name"]} for t in proj.get("trackers", [])],
            "modules": [m["name"] for m in proj.get("enabled_modules", [])],
            "issue_custom_fields": issue_cfs,
            "memberships": list(group_map.values()),
            "user_memberships": [
                {"id": m["user"]["id"], "name": m["user"]["name"], "type": "user",
                 "role_ids": [r["id"] for r in m.get("roles", [])],
                 "role_names": [r["name"] for r in m.get("roles", [])]}
                for m in all_memberships if m.get("user")
            ],
            "versions": [{"id": v["id"], "name": v["name"], "status": v.get("status"),
                          "due_date": v.get("due_date"), "sharing": v.get("sharing")}
                         for v in versions],
            "custom_menu": custom_menu,
            "issue_templates": existing.get("issue_templates", {}),
            "defaults": existing.get("defaults", {}),
        }

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(template, f, ensure_ascii=False, indent=2)

        # Generate MD
        md_path = json_path.replace(".json", ".md")
        lines = [
            f"# Настройки проекта «Сделка 2.0»",
            f"> Источник: `{project_id}` ({proj['name']})",
            f"> Дата: {template['meta']['exported_at']}",
            "",
            "## Трекеры",
            *[f"- {t['name']} (id={t['id']})" for t in template["trackers"]],
            "",
            "## Модули",
            *[f"- {m}" for m in template["modules"]],
            "",
            f"## Группы ({len(template['memberships'])})",
            *[f"- **{g['name']}** (id={g['id']}): {', '.join(g['role_names'])}"
              for g in template["memberships"]],
            "",
            f"## Issue Custom Fields ({len(issue_cfs)})",
            *([f"- cf_{f['id']}: {f['name']}" for f in issue_cfs] if issue_cfs
              else ["⚠ Не удалось получить. Нужен админский API-ключ."]),
            "",
            f"## Custom Menu ({len(custom_menu)})",
            *([f"- {m['position']}. {m['name']}{' → ' + m['url'] if m.get('url') else ''}"
               for m in custom_menu] if custom_menu
              else ["Не удалось получить через API."]),
        ]
        with open(md_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        return json.dumps({
            "success": True,
            "output_json": json_path,
            "output_md": md_path,
            "summary": {
                "visible_project_cfs": len(visible_cfs),
                "trackers": len(template["trackers"]),
                "modules": len(template["modules"]),
                "issue_custom_fields": len(issue_cfs),
                "group_memberships": len(template["memberships"]),
                "user_memberships": len(template["user_memberships"]),
                "versions": len(versions),
                "custom_menu": len(custom_menu),
            },
        }, ensure_ascii=False)


    # ── EC_CF / EC_TYPE constants ────────────────────────
    # Easy Contact custom field IDs (update via redmine.discover_contact_fields)
    _EC_CF = {
        "INN": 6, "ORG_NAME": 2, "ADDRESS": 5, "PHONE": 4, "EMAIL": 8,
        "KPP": 29, "OGRN": 34, "BIK": 30, "BANK": 33, "RS": 31, "KS": 32,
        "POSITION": 107,
    }
    _EC_TYPE = {
        "PERSON": 1, "ORGANIZATION": 2, "IP": 3,
        "SUBDIVISION": 4, "EMPLOYEE": 6,
    }

    # ── create_contact_from_inn ──────────────────────────

    async def _create_contact_from_inn(self, p: dict) -> str:
        import re as _re
        raw_inn = _re.sub(r"[^\d]", "", p.get("inn") or "")

        if not _re.match(r"^\d{10}(\d{2})?$", raw_inn):
            return json.dumps({
                "success": False, "error": "invalid_inn",
                "input": p.get("inn", ""),
                "message": "ИНН должен содержать 10 или 12 цифр",
            }, ensure_ascii=False)

        # Step 1: search existing contact by INN
        existing = []
        for params in [
            {"limit": 100, "cf_6": raw_inn},
            {"limit": 100, "easy_query_q": raw_inn},
            {"limit": 100, "search": raw_inn},
        ]:
            try:
                d = await self._api_safe("GET", "/easy_contacts.json", params=params)
                existing = d.get("easy_contacts") or d.get("contacts") or []
                if existing:
                    break
            except Exception:
                continue

        # Filter exact INN match
        inn_cf_id = self._EC_CF["INN"]
        matches = [
            c for c in existing
            if any(
                str(cf.get("value", "")).replace(" ", "") == raw_inn
                for cf in (c.get("custom_fields") or [])
                if cf.get("id") == inn_cf_id
            )
        ]

        if matches:
            c = matches[0]
            def get_cf(cf_id):
                return next((cf["value"] for cf in (c.get("custom_fields") or [])
                             if cf.get("id") == cf_id), None)
            org = get_cf(self._EC_CF["ORG_NAME"]) or c.get("lastname") or c.get("name") or f"#{c['id']}"
            contact = {
                "id": c["id"], "type": c.get("type_name"),
                "name": org, "full_name": org,
                "inn": get_cf(self._EC_CF["INN"]),
                "kpp": get_cf(self._EC_CF["KPP"]),
                "ogrn": get_cf(self._EC_CF["OGRN"]),
                "address": get_cf(self._EC_CF["ADDRESS"]),
                "manager": {"position": get_cf(self._EC_CF["POSITION"]), "name": None},
            }
            return json.dumps({
                "success": True, "source": "existing",
                "contact_url": f"{self.base_url}/easy_contacts/{c['id']}",
                "contact": contact,
                **({"warning": f"Found {len(matches)} contacts with this INN, returning first"} if len(matches) > 1 else {}),
            }, ensure_ascii=False)

        # Step 2: rusprofile lookup via rusprofile skill
        from pyatnitsa.skills.skills import SkillLoader as _SL
        rp_result = None
        try:
            # Try to find rusprofile skill in same loader
            import importlib, pathlib
            skills_dir = pathlib.Path(__file__).parent.parent
            rp_path = skills_dir / "rusprofile" / "rusprofile.py"
            if rp_path.exists():
                spec = importlib.util.spec_from_file_location("rusprofile_skill", str(rp_path))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                rp_skill = mod.RusprofileSkill()
                await rp_skill.on_load()
                rp_raw = await rp_skill.execute("rusprofile.lookup", {"inn": raw_inn})
                rp_result = json.loads(rp_raw)
        except Exception as e:
            return json.dumps({
                "success": False, "error": "rusprofile_error",
                "inn": raw_inn,
                "message": f"Ошибка обращения к Rusprofile: {str(e)[:150]}",
            }, ensure_ascii=False)

        if not rp_result or not rp_result.get("success") or not rp_result.get("company"):
            return json.dumps({
                "success": False, "error": "rusprofile_not_found",
                "inn": raw_inn,
                "message": f"Организация с ИНН {raw_inn} не найдена в Rusprofile",
                "details": rp_result.get("error") if rp_result else None,
            }, ensure_ascii=False)

        company = rp_result["company"]

        # Step 3: shorten org name via shortener skill
        short_name = company.get("short_name") or company.get("full_name", "")
        try:
            sh_path = pathlib.Path(__file__).parent.parent / "shortener" / "shortener.py"
            if sh_path.exists():
                spec = importlib.util.spec_from_file_location("shortener_skill", str(sh_path))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                sh_skill = mod.ShortenerSkill()
                await sh_skill.on_load()
                sh_raw = await sh_skill.execute("shortener.shorten", {
                    "full_name": company.get("full_name", short_name),
                    "region": p.get("region"),
                })
                sh_result = json.loads(sh_raw)
                if sh_result.get("result"):
                    short_name = sh_result["result"]
        except Exception:
            pass

        # Step 4: determine contact type
        full_name = company.get("full_name", "")
        is_ip = (company.get("opf_type") == "ip" or
                 "ИНДИВИДУАЛЬНЫЙ ПРЕДПРИНИМАТЕЛЬ" in full_name.upper())
        type_id = self._EC_TYPE["IP"] if is_ip else self._EC_TYPE["ORGANIZATION"]
        type_name = "ИП" if is_ip else "Организация"

        # Step 5: build custom fields
        custom_fields = []
        filled = []
        skipped = []

        def add_cf(cf_id, value, field_name):
            if value and str(value).strip():
                custom_fields.append({"id": cf_id, "value": str(value).strip()})
                filled.append(field_name)
            else:
                skipped.append(field_name)

        add_cf(self._EC_CF["INN"], raw_inn, "inn")
        add_cf(self._EC_CF["ORG_NAME"], full_name, "full_name")
        add_cf(self._EC_CF["ADDRESS"], company.get("address"), "address")
        add_cf(self._EC_CF["POSITION"], (company.get("manager") or {}).get("position"), "manager.position")
        add_cf(self._EC_CF["PHONE"], ((company.get("contacts") or {}).get("phone") or [None])[0], "phone")
        if not is_ip and company.get("kpp"):
            add_cf(self._EC_CF["KPP"], str(company["kpp"]).replace(r"\D", ""), "kpp")
        else:
            skipped.append("kpp")
        if company.get("ogrn"):
            add_cf(self._EC_CF["OGRN"], str(company["ogrn"]), "ogrn")
        else:
            skipped.append("ogrn")

        # author_note for fields without dedicated CF
        note_lines = []
        if full_name:
            note_lines.append(f"Полное наименование: {full_name}")
        mgr_name = (company.get("manager") or {}).get("name")
        if mgr_name:
            note_lines.append(f"Руководитель: {mgr_name}")
        website = (company.get("contacts") or {}).get("website")
        if website:
            note_lines.append(f"Сайт: {website}")
        if company.get("rusprofile_url"):
            note_lines.append(f"Rusprofile: {company['rusprofile_url']}")

        payload = {
            "easy_contact": {
                "type_id": type_id,
                "firstname": short_name,
                "lastname": " ",
                "custom_fields": custom_fields,
                **({"author_note": "\n".join(note_lines)} if note_lines else {}),
            }
        }

        try:
            new_contact = await self._api_safe("POST", "/easy_contacts.json", payload)
        except Exception as e:
            return json.dumps({
                "success": False, "error": "redmine_api_error",
                "inn": raw_inn, "company_name": full_name,
                "message": f"Ошибка создания контакта: {str(e)[:200]}",
                "hint": "Проверьте маппинг полей через redmine.discover_contact_fields",
            }, ensure_ascii=False)

        contact_id = (
            (new_contact.get("easy_contact") or {}).get("id") or
            (new_contact.get("contact") or {}).get("id") or
            new_contact.get("id")
        )
        if not contact_id:
            return json.dumps({
                "success": False, "error": "no_contact_id",
                "inn": raw_inn,
                "message": "Контакт создан, но ID не получен из ответа API",
                "raw_response": new_contact,
            }, ensure_ascii=False)

        return json.dumps({
            "success": True, "source": "created",
            "contact_url": f"{self.base_url}/easy_contacts/{contact_id}",
            "contact": {
                "id": contact_id, "type": type_name,
                "name": short_name, "full_name": full_name,
                "inn": raw_inn,
                "kpp": company.get("kpp"),
                "ogrn": company.get("ogrn"),
                "address": company.get("address"),
                "manager": {
                    "position": (company.get("manager") or {}).get("position"),
                    "name": (company.get("manager") or {}).get("name"),
                },
                "rusprofile_url": company.get("rusprofile_url"),
            },
            "filled_fields": filled,
            "skipped_fields": skipped,
        }, ensure_ascii=False)

    # ── discover_contact_fields ──────────────────────────

    async def _discover_contact_fields(self, p: dict) -> str:
        """Обнаружить поля формы Easy Contact через Playwright."""
        rdm_login = __import__("os").getenv("RDM_LOGIN", "")
        rdm_password = __import__("os").getenv("RDM_PASSWORD", "")

        if not rdm_login or not rdm_password:
            return json.dumps({
                "success": False, "error": "no_credentials",
                "message": "Требуются RDM_LOGIN и RDM_PASSWORD в .env",
            }, ensure_ascii=False)

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return json.dumps({
                "success": False, "error": "playwright_not_installed",
                "message": "pip install playwright && playwright install chromium",
            }, ensure_ascii=False)

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                page = await browser.new_page()

                await page.goto(f"{self.base_url}/login")
                await page.wait_for_load_state("domcontentloaded")
                await page.fill("#username", rdm_login)
                await page.fill("#password", rdm_password)
                await page.click("button[type='submit']")
                await page.wait_for_load_state("networkidle")
                await page.wait_for_timeout(2000)

                fields = []
                type_options = []
                for url in [f"{self.base_url}/easy_contacts/new"]:
                    await page.goto(url, wait_until="domcontentloaded")
                    await page.wait_for_timeout(2000)

                    fields = await page.evaluate("""() => {
                        const results = [];
                        document.querySelectorAll('input, select, textarea').forEach(el => {
                            const name = el.getAttribute('name') || '';
                            const id = el.getAttribute('id') || '';
                            if (!name || name.includes('[_destroy]') || name === 'utf8' || name === 'authenticity_token') return;
                            const label = el.closest('label, .attribute, p, tr')?.textContent?.trim()?.substring(0, 80) || '';
                            results.push({
                                name, id: id || null,
                                type: el.tagName.toLowerCase() + (el.type ? ':' + el.type : ''),
                                label: label.split('\\n')[0]?.trim()?.substring(0, 80) || null,
                                options: el.tagName === 'SELECT'
                                    ? Array.from(el.options).filter(o => o.value).map(o => ({value: o.value, text: o.textContent.trim()})).slice(0, 20)
                                    : null,
                            });
                        });
                        document.querySelectorAll('[name*=\"custom_field_values\"]').forEach(el => {
                            const m = el.getAttribute('name')?.match(/\\[(\\d+)\\]/);
                            if (m && !results.some(r => r.name === el.getAttribute('name'))) {
                                const label = el.closest('p, div, tr')?.querySelector('label')?.textContent?.trim() || '';
                                results.push({ name: el.getAttribute('name'), cf_id: parseInt(m[1]),
                                    type: el.tagName.toLowerCase() + (el.type ? ':' + el.type : ''),
                                    label: label.substring(0, 80) || 'cf_' + m[1] });
                            }
                        });
                        return results;
                    }""")

                    try:
                        type_options = await page.evaluate("""() => {
                            const sel = document.querySelector('select[name*="type"], select[name*="contact_type"]');
                            if (!sel) return [];
                            return Array.from(sel.options).filter(o => o.value).map(o => ({value: o.value, text: o.textContent.trim()}));
                        }""")
                    except Exception:
                        pass

                    if fields:
                        break

                await browser.close()

                return json.dumps({
                    "success": True,
                    "total_fields": len(fields),
                    "type_options": type_options,
                    "fields": fields,
                    "hint": "Используй cf_id для обновления _EC_CF в redmine.py",
                }, ensure_ascii=False)

        except Exception as e:
            return json.dumps({
                "success": False, "error": "discovery_failed",
                "message": str(e)[:200],
            }, ensure_ascii=False)
