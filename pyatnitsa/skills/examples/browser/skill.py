"""
Навык browser — универсальная Playwright автоматизация.
Stateless: каждая команда запускает браузер, действует, сохраняет состояние, закрывает.
Session (cookies/localStorage) персистится через storage state JSON.
"""
from __future__ import annotations

import json
import os
import structlog

from pyatnitsa.skills.base import BaseSkill, LLMTool

logger = structlog.get_logger("skill.browser")


class BrowserSkill(BaseSkill):
    name = "browser"
    description = "Браузерная автоматизация: открытие страниц, скриншоты, клики, заполнение форм, извлечение данных"
    version = "1.0.0"

    def __init__(self):
        super().__init__()
        self._state_file = ""
        self._screenshot_file = ""
        self._last_url_file = ""

    async def on_load(self):
        data_dir = os.getenv("BROWSER_DATA_DIR", os.path.expanduser("~/.pyatnitsa"))
        os.makedirs(data_dir, exist_ok=True)
        self._state_file = os.path.join(data_dir, "browser-state.json")
        self._screenshot_file = os.path.join(data_dir, "browser-screenshot.png")
        self._last_url_file = os.path.join(data_dir, "browser-lasturl.txt")
        logger.info("browser_skill_loaded", data_dir=data_dir)

    async def on_unload(self):
        pass

    def get_tools(self) -> list[LLMTool]:
        return [
            LLMTool("browser.navigate", "Открыть URL в браузере", {
                "type": "object", "properties": {
                    "url": {"type": "string", "description": "URL страницы"},
                }, "required": ["url"],
            }),
            LLMTool("browser.screenshot", "Скриншот страницы или элемента", {
                "type": "object", "properties": {
                    "selector": {"type": "string", "description": "CSS-селектор (опционально, без = вся страница)"},
                },
            }),
            LLMTool("browser.click", "Кликнуть по элементу (CSS-селектор)", {
                "type": "object", "properties": {
                    "selector": {"type": "string"},
                }, "required": ["selector"],
            }),
            LLMTool("browser.click_text", "Кликнуть по тексту (ссылка/кнопка)", {
                "type": "object", "properties": {
                    "text": {"type": "string"},
                }, "required": ["text"],
            }),
            LLMTool("browser.fill", "Заполнить поле (очистить + ввести)", {
                "type": "object", "properties": {
                    "selector": {"type": "string"},
                    "text": {"type": "string"},
                }, "required": ["selector", "text"],
            }),
            LLMTool("browser.type", "Напечатать текст в поле (добавить к существующему)", {
                "type": "object", "properties": {
                    "selector": {"type": "string"},
                    "text": {"type": "string"},
                }, "required": ["selector", "text"],
            }),
            LLMTool("browser.select", "Выбрать значение в <select>", {
                "type": "object", "properties": {
                    "selector": {"type": "string"},
                    "value": {"type": "string"},
                }, "required": ["selector", "value"],
            }),
            LLMTool("browser.extract", "Извлечь текст из элементов (до 50)", {
                "type": "object", "properties": {
                    "selector": {"type": "string"},
                }, "required": ["selector"],
            }),
            LLMTool("browser.html", "Получить HTML элемента или всей страницы", {
                "type": "object", "properties": {
                    "selector": {"type": "string"},
                },
            }),
            LLMTool("browser.links", "Все ссылки на странице (до 100)", {
                "type": "object", "properties": {},
            }),
            LLMTool("browser.inputs", "Все input/textarea/select/button на странице", {
                "type": "object", "properties": {},
            }),
            LLMTool("browser.scroll", "Прокрутить страницу (up/down или к селектору)", {
                "type": "object", "properties": {
                    "direction": {"type": "string", "description": "up, down, или CSS-селектор"},
                },
            }),
            LLMTool("browser.press", "Нажать клавишу (Enter, Tab, Escape...)", {
                "type": "object", "properties": {
                    "key": {"type": "string"},
                }, "required": ["key"],
            }),
            LLMTool("browser.eval", "Выполнить JavaScript-выражение на странице", {
                "type": "object", "properties": {
                    "expression": {"type": "string"},
                }, "required": ["expression"],
            }),
            LLMTool("browser.login", "Combo: открыть URL → заполнить логин/пароль → submit → скриншот", {
                "type": "object", "properties": {
                    "url": {"type": "string"},
                    "user_selector": {"type": "string", "default": "#username"},
                    "pass_selector": {"type": "string", "default": "#password"},
                    "username": {"type": "string"},
                    "password": {"type": "string"},
                    "submit_selector": {"type": "string", "description": "Кнопка submit (опционально, без = Enter)"},
                }, "required": ["url", "username", "password"],
            }),
            LLMTool("browser.nav_shot", "Combo: открыть URL + скриншот", {
                "type": "object", "properties": {
                    "url": {"type": "string"},
                }, "required": ["url"],
            }),
        ]

    async def execute(self, tool_name: str, params: dict) -> str:
        cmd = tool_name.split(".")[-1]
        dispatch = {
            "navigate": self._navigate, "screenshot": self._screenshot,
            "click": self._click, "click_text": self._click_text,
            "fill": self._fill, "type": self._type_text,
            "select": self._select, "extract": self._extract,
            "html": self._html, "links": self._links,
            "inputs": self._inputs, "scroll": self._scroll,
            "press": self._press, "eval": self._eval,
            "login": self._login, "nav_shot": self._nav_shot,
        }
        fn = dispatch.get(cmd)
        if not fn:
            return json.dumps({"error": f"unknown: {tool_name}"})
        return await fn(params)

    # ── Playwright context manager ───────────────────────

    async def _with_page(self, fn, needs_last_url: bool = False):
        """Запускает браузер, вызывает fn(page, context), сохраняет state."""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return {"success": False, "error": "playwright not installed"}

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
                )
                ctx_opts = {
                    "viewport": {"width": 1280, "height": 900},
                    "locale": "ru-RU",
                    "timezone_id": "Europe/Moscow",
                    "ignore_https_errors": True,
                }
                # Restore saved state
                if os.path.exists(self._state_file):
                    try:
                        ctx_opts["storage_state"] = self._state_file
                    except Exception:
                        pass

                context = await browser.new_context(**ctx_opts)
                page = await context.new_page()

                # Restore last URL if needed
                if needs_last_url:
                    last_url = self._get_last_url()
                    if last_url and last_url != "about:blank":
                        await page.goto(last_url, wait_until="domcontentloaded", timeout=30000)

                result = await fn(page, context)

                # Save state
                await context.storage_state(path=self._state_file)
                await browser.close()
                return result

        except Exception as e:
            logger.error("browser_error", error=str(e))
            return {"success": False, "error": str(e)[:300]}

    def _save_last_url(self, url: str):
        try:
            with open(self._last_url_file, "w") as f:
                f.write(url)
        except Exception:
            pass

    def _get_last_url(self) -> str | None:
        try:
            with open(self._last_url_file) as f:
                return f.read().strip()
        except Exception:
            return None

    # ── Commands ─────────────────────────────────────────

    async def _navigate(self, p: dict) -> str:
        async def action(page, ctx):
            await page.goto(p["url"], wait_until="domcontentloaded", timeout=30000)
            self._save_last_url(page.url)
            return {"success": True, "url": page.url, "title": await page.title()}
        return json.dumps(await self._with_page(action), ensure_ascii=False)

    async def _screenshot(self, p: dict) -> str:
        async def action(page, ctx):
            sel = p.get("selector")
            if sel:
                el = await page.query_selector(sel)
                if not el:
                    return {"success": False, "error": f"Element not found: {sel}"}
                await el.screenshot(path=self._screenshot_file, type="png")
            else:
                await page.screenshot(path=self._screenshot_file, type="png", full_page=False)
            return {"success": True, "path": self._screenshot_file, "url": page.url}
        return json.dumps(await self._with_page(action, needs_last_url=True), ensure_ascii=False)

    async def _click(self, p: dict) -> str:
        async def action(page, ctx):
            await page.click(p["selector"], timeout=10000)
            await page.wait_for_load_state("domcontentloaded")
            self._save_last_url(page.url)
            return {"success": True, "url": page.url, "title": await page.title()}
        return json.dumps(await self._with_page(action, needs_last_url=True), ensure_ascii=False)

    async def _click_text(self, p: dict) -> str:
        async def action(page, ctx):
            text = p["text"]
            locator = page.get_by_role("link", name=text).or_(
                page.get_by_role("button", name=text)
            ).or_(page.locator(f'text="{text}"'))
            await locator.first.click(timeout=10000)
            await page.wait_for_load_state("domcontentloaded")
            self._save_last_url(page.url)
            return {"success": True, "url": page.url, "title": await page.title()}
        return json.dumps(await self._with_page(action, needs_last_url=True), ensure_ascii=False)

    async def _fill(self, p: dict) -> str:
        async def action(page, ctx):
            await page.fill(p["selector"], p["text"], timeout=10000)
            self._save_last_url(page.url)
            return {"success": True}
        return json.dumps(await self._with_page(action, needs_last_url=True), ensure_ascii=False)

    async def _type_text(self, p: dict) -> str:
        async def action(page, ctx):
            await page.type(p["selector"], p["text"], timeout=10000)
            self._save_last_url(page.url)
            return {"success": True}
        return json.dumps(await self._with_page(action, needs_last_url=True), ensure_ascii=False)

    async def _select(self, p: dict) -> str:
        async def action(page, ctx):
            await page.select_option(p["selector"], p["value"], timeout=10000)
            self._save_last_url(page.url)
            return {"success": True}
        return json.dumps(await self._with_page(action, needs_last_url=True), ensure_ascii=False)

    async def _extract(self, p: dict) -> str:
        async def action(page, ctx):
            elements = await page.query_selector_all(p["selector"])
            texts = []
            for el in elements[:50]:
                text = await el.text_content()
                if text and text.strip():
                    texts.append(text.strip())
            return {"success": True, "count": len(texts), "texts": texts}
        return json.dumps(await self._with_page(action, needs_last_url=True), ensure_ascii=False)

    async def _html(self, p: dict) -> str:
        async def action(page, ctx):
            sel = p.get("selector")
            if sel:
                el = await page.query_selector(sel)
                if not el:
                    return {"success": False, "error": f"Not found: {sel}"}
                html = await el.inner_html()
            else:
                html = await page.content()
            if len(html) > 50000:
                html = html[:50000] + "\n... (truncated)"
            return {"success": True, "html": html, "url": page.url}
        return json.dumps(await self._with_page(action, needs_last_url=True), ensure_ascii=False)

    async def _links(self, p: dict) -> str:
        async def action(page, ctx):
            links = await page.eval_on_selector_all("a[href]", """
                els => els.slice(0, 100).map(a => ({
                    text: (a.textContent || '').trim().substring(0, 100),
                    href: a.href,
                })).filter(l => l.text && l.href)
            """)
            return {"success": True, "count": len(links), "links": links}
        return json.dumps(await self._with_page(action, needs_last_url=True), ensure_ascii=False)

    async def _inputs(self, p: dict) -> str:
        async def action(page, ctx):
            inputs = await page.eval_on_selector_all(
                'input, textarea, select, button, [role="button"]',
                """els => els.slice(0, 100).map(el => ({
                    tag: el.tagName.toLowerCase(),
                    type: el.type || '',
                    name: el.name || '',
                    id: el.id || '',
                    placeholder: el.placeholder || '',
                    value: el.value ? el.value.substring(0, 50) : '',
                    text: (el.textContent || '').trim().substring(0, 50),
                    selector: el.id ? '#' + el.id : el.name ? '[name=\"' + el.name + '\"]' : '',
                }))""",
            )
            return {"success": True, "count": len(inputs), "inputs": inputs}
        return json.dumps(await self._with_page(action, needs_last_url=True), ensure_ascii=False)

    async def _scroll(self, p: dict) -> str:
        async def action(page, ctx):
            direction = p.get("direction", "down")
            if direction == "up":
                await page.evaluate("window.scrollBy(0, -500)")
            elif direction == "down":
                await page.evaluate("window.scrollBy(0, 500)")
            else:
                el = await page.query_selector(direction)
                if el:
                    await el.scroll_into_view_if_needed()
            self._save_last_url(page.url)
            return {"success": True}
        return json.dumps(await self._with_page(action, needs_last_url=True), ensure_ascii=False)

    async def _press(self, p: dict) -> str:
        async def action(page, ctx):
            await page.keyboard.press(p["key"])
            self._save_last_url(page.url)
            return {"success": True}
        return json.dumps(await self._with_page(action, needs_last_url=True), ensure_ascii=False)

    async def _eval(self, p: dict) -> str:
        async def action(page, ctx):
            result = await page.evaluate(p["expression"])
            return {"success": True, "result": result}
        return json.dumps(await self._with_page(action, needs_last_url=True), ensure_ascii=False)

    async def _login(self, p: dict) -> str:
        async def action(page, ctx):
            await page.goto(p["url"], wait_until="domcontentloaded", timeout=30000)
            user_sel = p.get("user_selector", "#username")
            pass_sel = p.get("pass_selector", "#password")
            await page.fill(user_sel, p["username"], timeout=10000)
            await page.fill(pass_sel, p["password"], timeout=10000)

            submit = p.get("submit_selector")
            if submit:
                await page.click(submit, timeout=10000)
            else:
                await page.keyboard.press("Enter")

            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(2000)
            self._save_last_url(page.url)
            await page.screenshot(path=self._screenshot_file, type="png", full_page=False)
            return {
                "success": True, "url": page.url,
                "title": await page.title(), "screenshot": self._screenshot_file,
            }
        return json.dumps(await self._with_page(action), ensure_ascii=False)

    async def _nav_shot(self, p: dict) -> str:
        async def action(page, ctx):
            await page.goto(p["url"], wait_until="domcontentloaded", timeout=30000)
            self._save_last_url(page.url)
            await page.screenshot(path=self._screenshot_file, type="png", full_page=False)
            return {
                "success": True, "url": page.url,
                "title": await page.title(), "screenshot": self._screenshot_file,
            }
        return json.dumps(await self._with_page(action), ensure_ascii=False)
