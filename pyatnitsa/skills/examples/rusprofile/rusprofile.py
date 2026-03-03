"""
Навык rusprofile — поиск информации о компаниях на rusprofile.ru.
Playwright headless scraping: ИНН, ОГРН, название → карточка компании.
"""
from __future__ import annotations

import json
import re
import structlog

from pyatnitsa.skills.base import BaseSkill, LLMTool

logger = structlog.get_logger("skill.rusprofile")

BASE = "https://www.rusprofile.ru"
SEARCH_URL = BASE + "/search?query="
TIMEOUT = 30_000


class RusprofileSkill(BaseSkill):
    name = "rusprofile"
    description = "Поиск информации о компаниях на rusprofile.ru по ИНН, ОГРН или названию"
    version = "1.0.0"

    async def on_load(self):
        logger.info("rusprofile_skill_loaded")

    async def on_unload(self):
        pass

    def get_tools(self) -> list[LLMTool]:
        return [
            LLMTool("rusprofile.lookup", "Найти компанию по ИНН, ОГРН или названию на rusprofile.ru", {
                "type": "object", "properties": {
                    "inn": {"type": "string", "description": "ИНН (10 или 12 цифр)"},
                    "ogrn": {"type": "string", "description": "ОГРН (13 или 15 цифр)"},
                    "name": {"type": "string", "description": "Название компании"},
                },
            }),
        ]

    async def execute(self, tool_name: str, params: dict) -> str:
        cmd = tool_name.split(".")[-1]
        if cmd == "lookup":
            return await self._lookup(params)
        return json.dumps({"error": f"unknown tool: {tool_name}"})

    async def _lookup(self, p: dict) -> str:
        inn = _clean_digits(p.get("inn"))
        ogrn = _clean_digits(p.get("ogrn"))
        name = (p.get("name") or "").strip()

        if inn and re.match(r"^\d{10}(\d{2})?$", inn):
            query, query_type = inn, "inn"
        elif ogrn and re.match(r"^\d{13,15}$", ogrn):
            query, query_type = ogrn, "ogrn"
        elif name:
            query, query_type = name, "name"
        else:
            return json.dumps({"success": False, "error": "invalid_input",
                               "message": "Укажи inn, ogrn или name."}, ensure_ascii=False)

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return json.dumps({"success": False, "error": "playwright_missing",
                               "message": "pip install playwright && playwright install chromium"})

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                ctx = await browser.new_context(
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                    viewport={"width": 1280, "height": 900},
                    locale="ru-RU",
                )
                page = await ctx.new_page()
                search_url = SEARCH_URL + _urlencode(query)
                logger.info("rusprofile_search", query=query, type=query_type)

                await page.goto(search_url, wait_until="domcontentloaded", timeout=TIMEOUT)
                await page.wait_for_timeout(2500)
                current_url = page.url

                # Captcha check
                if await page.query_selector(".captcha, .g-recaptcha, #captcha, [data-captcha]"):
                    await browser.close()
                    return json.dumps({"success": False, "error": "captcha",
                                       "message": "Капча. Попробуй позже."}, ensure_ascii=False)

                # Прямой редирект на карточку
                if _is_card_url(current_url):
                    company = await _parse_card(page, query, query_type)
                    company["rusprofile_url"] = current_url
                    await browser.close()
                    return json.dumps({"success": True, "source": "rusprofile",
                                       "query": {query_type: query}, "company": company}, ensure_ascii=False)

                # Парсим поисковую выдачу
                candidates = await _parse_search(page)
                if not candidates:
                    await browser.close()
                    return json.dumps({"success": False, "error": "not_found",
                                       "message": f"Не найдено: {query}"}, ensure_ascii=False)

                # Точное совпадение по ИНН/ОГРН
                if query_type in ("inn", "ogrn"):
                    exact = [c for c in candidates if c.get(query_type) == query]
                    target = exact[0] if len(exact) == 1 else (candidates[0] if len(candidates) == 1 else None)
                    if target and target.get("url"):
                        result = await _go_card(page, target, query, query_type)
                        await browser.close()
                        return json.dumps(result, ensure_ascii=False)

                # Единственный результат по имени
                if query_type == "name" and len(candidates) == 1 and candidates[0].get("url"):
                    result = await _go_card(page, candidates[0], query, query_type)
                    await browser.close()
                    return json.dumps(result, ensure_ascii=False)

                await browser.close()
                return json.dumps({
                    "success": False, "error": "ambiguous",
                    "message": f"Найдено {len(candidates)} результатов для «{query}»",
                    "candidates": [{"index": i + 1, **c} for i, c in enumerate(candidates[:15])],
                    "hint": "Уточни ИНН или ОГРН.",
                }, ensure_ascii=False)

        except Exception as e:
            logger.error("rusprofile_error", error=str(e))
            return json.dumps({"success": False, "error": "network_error",
                               "message": str(e)[:200]}, ensure_ascii=False)


# ── Helpers ──────────────────────────────────────────────

def _clean_digits(v) -> str | None:
    if not v:
        return None
    return re.sub(r"[\s\-]", "", str(v))


def _urlencode(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")


def _is_card_url(url: str) -> bool:
    return bool(re.search(r"/id/\d+", url) or re.search(r"/ip/\d+", url))


async def _go_card(page, target: dict, query: str, query_type: str) -> dict:
    url = target["url"] if target["url"].startswith("http") else BASE + target["url"]
    logger.info("rusprofile_card", name=target.get("name"), url=url)
    await page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT)
    await page.wait_for_timeout(2000)
    company = await _parse_card(page, query, query_type)
    company["rusprofile_url"] = page.url
    return {"success": True, "source": "rusprofile", "query": {query_type: query}, "company": company}


# ── DOM Parsers (выполняются в контексте браузера) ───────

PARSE_CARD_JS = r"""
(args) => {
    const qv = args.qv, qt = args.qt;
    const T = s => (s || '').replace(/\s+/g, ' ').trim();
    const body = document.body.innerText;

    const c = {
        full_name: null, short_name: null, inn: null, kpp: null, ogrn: null,
        opf_type: 'org', status: null, registration_date: null, address: null,
        okved_main: null, okved_extra: [], manager: null,
        contacts: { phone: [], website: null },
    };

    // Names
    const lines = body.split('\n').map(l => l.trim()).filter(l => l.length > 10);
    for (const line of lines) {
        if (/^[\u0410-\u042F\u0401\s""\u00AB\u00BB\u2018\u2019\-\u2014\u2013().,\u21160-9]{15,}$/.test(line) && line.length < 300) {
            c.full_name = line; break;
        }
    }
    const h1 = document.querySelector('h1');
    if (h1) { const t = T(h1.textContent); if (!c.full_name) c.full_name = t; else c.short_name = t; }

    // IP detection
    if (c.full_name && /^ИНДИВИДУАЛЬНЫЙ\s+ПРЕДПРИНИМАТЕЛЬ/i.test(c.full_name)) c.opf_type = 'ip';

    // dt/dd map
    const rmap = {};
    document.querySelectorAll('dt').forEach(dt => {
        const dd = dt.nextElementSibling;
        if (dd && dd.tagName === 'DD') rmap[T(dt.textContent).toLowerCase()] = T(dd.textContent);
    });
    const clips = [...document.querySelectorAll('[data-clipboard-text]')];

    // INN (10 digits = org, 12 digits = ip)
    for (const el of clips) { const v = el.getAttribute('data-clipboard-text'); if (/^\d{10}$/.test(v)) { c.inn = v; break; } }
    if (!c.inn) {
        for (const [k, v] of Object.entries(rmap)) {
            if (k.includes('инн') && !k.includes('огрн')) {
                const m = v.match(/\b(\d{10})\b/); if (m) { c.inn = m[1]; break; }
            }
        }
    }
    if (!c.inn) { const m = body.match(/ИНН\s*[:\s]*(\d{10})\b/); if (m) c.inn = m[1]; }
    if (!c.inn && qt === 'inn' && /^\d{10}$/.test(qv)) c.inn = qv;
    if (!c.inn) {
        for (const el of clips) { const v = el.getAttribute('data-clipboard-text'); if (/^\d{12}$/.test(v)) { c.inn = v; c.opf_type = 'ip'; break; } }
    }
    if (!c.inn) { const m = body.match(/ИНН\s*[:\s]*(\d{12})\b/); if (m) { c.inn = m[1]; c.opf_type = 'ip'; } }
    if (!c.inn && qt === 'inn' && /^\d{12}$/.test(qv)) { c.inn = qv; c.opf_type = 'ip'; }

    // KPP
    const okKpp = v => v && /^\d{9}$/.test(v) && (!c.inn || !c.inn.startsWith(v));
    for (const el of clips) { const v = el.getAttribute('data-clipboard-text'); if (okKpp(v)) { c.kpp = v; break; } }
    if (!c.kpp) {
        for (const [k, v] of Object.entries(rmap)) {
            if (k.includes('инн') && k.includes('кпп')) {
                const m = v.match(/\d{10,12}\D+(\d{9})\b/); if (m && okKpp(m[1])) { c.kpp = m[1]; break; }
            }
        }
    }
    if (!c.kpp) {
        for (const [k, v] of Object.entries(rmap)) {
            if (k.includes('кпп') && !k.includes('инн')) {
                const m = v.match(/(\d{9})/); if (m && okKpp(m[1])) { c.kpp = m[1]; break; }
            }
        }
    }
    if (!c.kpp) { const m = body.match(/КПП\s*[:\s]+(\d{9})\b/); if (m && okKpp(m[1])) c.kpp = m[1]; }
    if (!c.kpp && c.inn) { const m = body.match(new RegExp(c.inn + '\\D+(\\d{9})\\b')); if (m && okKpp(m[1])) c.kpp = m[1]; }

    // OGRN
    for (const el of clips) { const v = el.getAttribute('data-clipboard-text'); if (/^\d{13,15}$/.test(v)) { c.ogrn = v; break; } }
    if (!c.ogrn) {
        for (const [k, v] of Object.entries(rmap)) {
            if (k.includes('огрн')) { const m = v.match(/(\d{13,15})/); if (m) { c.ogrn = m[1]; break; } }
        }
    }
    if (!c.ogrn) { const m = body.match(/ОГРН[ИП]?\s*[:\s]*(\d{13,15})/); if (m) c.ogrn = m[1]; }
    if (!c.ogrn && qt === 'ogrn') c.ogrn = qv;

    // Status
    const statusBlock = document.querySelector('.company-status, [class*="status"]');
    if (statusBlock) c.status = T(statusBlock.textContent).substring(0, 100);
    if (!c.status) {
        for (const [k, v] of Object.entries(rmap)) {
            if (k.includes('статус') || k.includes('состояние')) { c.status = v.substring(0, 100); break; }
        }
    }
    if (!c.status) { const m = body.match(/(Действующ[а-яё]*|Ликвидирован[а-яё]*|В стадии ликвидации|Банкротство)/i); if (m) c.status = m[1]; }

    // Registration date
    for (const [k, v] of Object.entries(rmap)) {
        if (k.includes('дата регистрации') || k.includes('дата создания')) {
            const m = v.match(/(\d{2}\.\d{2}\.\d{4})/); if (m) { c.registration_date = m[1]; break; }
        }
    }

    // Address
    for (const [k, v] of Object.entries(rmap)) {
        if (k.includes('адрес') && v.length > 10 && v.length < 300) { c.address = v; break; }
    }
    if (!c.address) {
        for (const el of clips) {
            const v = el.getAttribute('data-clipboard-text');
            if (v && v.length > 15 && v.length < 300 && /\d{6}/.test(v)) { c.address = v; break; }
        }
    }

    // OKVED
    const okveds = [];
    document.querySelectorAll('[class*="okved"], [class*="activity"]').forEach(el => {
        const t = T(el.textContent);
        const m = t.match(/^(\d{2}\.\d{1,2}(?:\.\d{1,2})?)/);
        if (!m) return;
        const code = m[1];
        let desc = t.substring(m[0].length).trim();
        desc = desc.split('\n')[0].trim().substring(0, 200);
        if (!okveds.some(o => o.code === code)) okveds.push({ code, name: desc });
    });
    if (okveds.length === 0) {
        const re = /(\d{2}\.\d{1,2}(?:\.\d{1,2})?)\s+([\u0410-\u042F\u0430-\u044F\u0401\u0451A-Za-z][^\n]{5,150})/g;
        let m;
        while ((m = re.exec(body)) !== null) {
            const nm = m[2].trim().replace(/\s+/g, ' ');
            if (nm.length > 5 && !okveds.some(o => o.code === m[1])) okveds.push({ code: m[1], name: nm });
            if (okveds.length >= 30) break;
        }
    }
    if (okveds.length > 0) { c.okved_main = okveds[0]; c.okved_extra = okveds.slice(1); }

    // Manager
    const fioRe = /([\u0410-\u042f\u0401][\u0430-\u044f\u0451]+\s+[\u0410-\u042f\u0401][\u0430-\u044f\u0451]+(?:\s+[\u0410-\u042f\u0401][\u0430-\u044f\u0451]+)?)/;
    const skipFio = /^(Руководитель|Учредител|Основной|Дополнит|История|Налоговый|Финансов)/i;

    try {
        const mgrCandidates = [];
        document.querySelectorAll('span, div, p, dt, td, h3, h4, a, li').forEach(el => {
            const t = T(el.textContent);
            if (!/^Руководитель/i.test(t)) return;
            if (t.length > 500) return;
            mgrCandidates.push(el);
        });
        for (const el of mgrCandidates) {
            const container = el.closest('div, section, li, tr, dl') || el.parentElement;
            if (!container) continue;
            const ct = T(container.textContent);
            if (ct.length > 600) continue;
            const rawLines = container.innerText.split('\n').map(l => l.trim()).filter(l => l.length > 1);
            let fioLine = null, posLine = null;
            for (const line of rawLines) {
                if (/^Руководитель/i.test(line)) continue;
                if (/^ИНН|^ОГРН|^КПП|^Дата|^Адрес|^Устав|^Налог/i.test(line)) continue;
                const fm = line.match(fioRe);
                if (fm && !skipFio.test(fm[1])) {
                    if (!fioLine) {
                        fioLine = fm[1].trim();
                        const before = line.substring(0, line.indexOf(fm[1])).replace(/[\s:,\-\u2013\u2014]+$/, '').trim();
                        if (before.length > 2 && !/^Руководитель$/i.test(before)) posLine = before;
                    }
                } else if (!posLine && !fioLine && line.length > 3 && line.length < 150) {
                    if (!/^Руководитель/i.test(line) && !/^\d/.test(line))
                        posLine = line.replace(/[\s:]+$/, '').trim();
                }
            }
            if (fioLine) { c.manager = { position: posLine || null, name: fioLine }; break; }
        }
    } catch (_) {}

    if (!c.manager) {
        const mgrBlock = body.match(/Руководитель[\s\S]{0,300}?([\u0410-\u042f\u0401][\u0430-\u044f\u0451]+\s+[\u0410-\u042f\u0401][\u0430-\u044f\u0451]+(?:\s+[\u0410-\u042f\u0401][\u0430-\u044f\u0451]+)?)/);
        if (mgrBlock) {
            const fio = mgrBlock[1].trim();
            if (!skipFio.test(fio)) {
                const fullMatch = mgrBlock[0];
                const fioIdx = fullMatch.lastIndexOf(fio);
                const between = fullMatch.substring(0, fioIdx).replace(/^Руководитель[\s:,\-\u2013\u2014]*/i, '').trim();
                const pos = between.replace(/[\s:,\-\u2013\u2014]+$/, '').replace(/^\s*\n+/, '').trim();
                c.manager = { position: pos.length > 2 ? pos : null, name: fio };
            }
        }
    }

    // Phones
    const pset = new Set();
    document.querySelectorAll('a[href^="tel:"]').forEach(a => {
        const ph = T(a.textContent);
        if (/^\+?\d[\d\s\-()]{6,}/.test(ph) && ph.length < 30) pset.add(ph);
    });
    for (const m of body.matchAll(/(\+7\s*\(\d{3,4}\)\s*[\d\-\s]{7,12})/g)) {
        const ph = m[1].replace(/\s+/g, ' ').trim();
        if (ph.length < 30) pset.add(ph);
    }
    c.contacts.phone = [...pset].slice(0, 5);

    // Website
    const blocked = ['rusprofile.ru', 'google', 'yandex', 'facebook', 'vk.com', 't.me', 'instagram', 'ok.ru', 'youtube'];
    for (const a of document.querySelectorAll('a[href^="http"]')) {
        const href = a.getAttribute('href') || '';
        if (blocked.some(b => href.includes(b))) continue;
        if (href.includes('?rp')) continue;
        if (/^https?:\/\/[\w\-]+\.[\w]{2,}/i.test(href) && href.length < 100) {
            c.contacts.website = href; break;
        }
    }

    return c;
}
"""

PARSE_SEARCH_JS = """
() => {
    const T = s => (s || '').replace(/\\s+/g, ' ').trim();
    const r = [];
    const seen = {};
    document.querySelectorAll('a[href*="/id/"], a[href*="/ip/"]').forEach(a => {
        const href = a.getAttribute('href') || '';
        const name = T(a.textContent);
        if (!href || !name || name.length < 2 || name.length > 200 || seen[href]) return;
        seen[href] = 1;
        const ctx = (a.closest('div, li, tr') || a.parentElement);
        const ct = ctx ? ctx.textContent : '';
        r.push({
            name,
            url: href.startsWith('http') ? href : 'https://www.rusprofile.ru' + href,
            inn: (ct.match(/ИНН[:\\s]*(\\d{10,12})/) || [])[1] || null,
            ogrn: (ct.match(/ОГРН[:\\s]*(\\d{13,15})/) || [])[1] || null,
        });
    });
    return r;
}
"""


async def _parse_card(page, query: str, query_type: str) -> dict:
    return await page.evaluate(PARSE_CARD_JS, {"qv": query, "qt": query_type})


async def _parse_search(page) -> list[dict]:
    return await page.evaluate(PARSE_SEARCH_JS)
