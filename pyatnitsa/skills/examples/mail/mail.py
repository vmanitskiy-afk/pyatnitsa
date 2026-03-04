"""
Навык mail — интеграция с почтой Mail.ru через IMAP (чтение/поиск) и SMTP (отправка).
Зависимости: pip install aioimaplib aiosmtplib mail-parser
"""
from __future__ import annotations

import asyncio
import email as email_lib
import email.mime.multipart
import email.mime.text
import email.utils
import json
import os
import re
from datetime import datetime, timezone
from email.header import decode_header as _decode_header

import structlog

from pyatnitsa.skills.base import BaseSkill, LLMTool

logger = structlog.get_logger("skill.mail")

IMAP_HOST = "imap.mail.ru"
IMAP_PORT = 993
SMTP_HOST = "smtp.mail.ru"
SMTP_PORT = 465


def _decode_str(s: str | bytes | None) -> str:
    """Decode RFC2047-encoded header string."""
    if s is None:
        return ""
    if isinstance(s, bytes):
        try:
            return s.decode("utf-8", errors="replace")
        except Exception:
            return repr(s)
    parts = _decode_header(s)
    result = []
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            result.append(chunk.decode(enc or "utf-8", errors="replace"))
        else:
            result.append(chunk)
    return "".join(result)


def _addr_str(addr: str | None) -> str:
    if not addr:
        return ""
    name, email_addr = email.utils.parseaddr(addr)
    name = _decode_str(name)
    return f"{name} <{email_addr}>".strip() if name else email_addr


def _parse_message(raw: bytes) -> dict:
    """Parse raw email bytes into a dict."""
    msg = email_lib.message_from_bytes(raw)
    text_body = ""
    has_html = False

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                text_body = payload.decode(charset, errors="replace")
            elif ct == "text/html" and "attachment" not in cd:
                has_html = True
    else:
        payload = msg.get_payload(decode=True) or b""
        charset = msg.get_content_charset() or "utf-8"
        text_body = payload.decode(charset, errors="replace")

    attachments = []
    if msg.is_multipart():
        for part in msg.walk():
            cd = str(part.get("Content-Disposition", ""))
            if "attachment" in cd:
                fn = part.get_filename()
                attachments.append({
                    "filename": _decode_str(fn) if fn else "unnamed",
                    "content_type": part.get_content_type(),
                })

    date_str = msg.get("Date", "")
    try:
        parsed_date = email.utils.parsedate_to_datetime(date_str).isoformat()
    except Exception:
        parsed_date = date_str

    return {
        "from": _addr_str(msg.get("From")),
        "to": _addr_str(msg.get("To")),
        "cc": _addr_str(msg.get("Cc")),
        "reply_to": _addr_str(msg.get("Reply-To")),
        "subject": _decode_str(msg.get("Subject")),
        "date": parsed_date,
        "message_id": msg.get("Message-ID", ""),
        "references": msg.get("References", ""),
        "text": text_body,
        "html": "(HTML content available)" if has_html else "",
        "attachments": attachments,
    }


async def _imap_run(coro):
    """Run an IMAP coroutine in executor to avoid blocking (aioimaplib is sync-ish)."""
    return await asyncio.get_event_loop().run_in_executor(None, coro)


class MailSkill(BaseSkill):
    name = "mail"
    description = "Почта Mail.ru: чтение, поиск, отправка, ответ, пересылка писем (IMAP/SMTP)"
    version = "1.0.0"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._user = ""
        self._password = ""

    async def on_load(self):
        self._user = os.getenv("MAILRU_USER", "")
        self._password = os.getenv("MAILRU_APP_PASSWORD", "")
        if self._user and self._password:
            logger.info("mail_skill_loaded", user=self._user)
        else:
            logger.warning("mail_no_config", hint="Set MAILRU_USER and MAILRU_APP_PASSWORD")

    async def on_unload(self):
        pass

    def get_tools(self) -> list[LLMTool]:
        return [
            LLMTool("mail.inbox", "Получить входящие письма", {
                "type": "object", "properties": {
                    "limit": {"type": "integer", "default": 10, "description": "Количество писем"},
                    "unseen": {"type": "boolean", "default": False, "description": "Только непрочитанные"},
                    "folder": {"type": "string", "default": "INBOX", "description": "Папка (INBOX, Sent, Drafts, Spam, Trash)"},
                },
            }),
            LLMTool("mail.read", "Прочитать письмо по UID (полный текст)", {
                "type": "object", "properties": {
                    "uid": {"type": "integer", "description": "UID письма из inbox/search"},
                    "folder": {"type": "string", "default": "INBOX"},
                }, "required": ["uid"],
            }),
            LLMTool("mail.search", "Поиск писем по теме, отправителю, дате", {
                "type": "object", "properties": {
                    "query": {"type": "string", "description": "Поиск по теме письма"},
                    "from_addr": {"type": "string", "description": "Фильтр по отправителю"},
                    "to_addr": {"type": "string", "description": "Фильтр по получателю"},
                    "since": {"type": "string", "description": "Дата с (YYYY-MM-DD)"},
                    "limit": {"type": "integer", "default": 20},
                    "folder": {"type": "string", "default": "INBOX"},
                },
            }),
            LLMTool("mail.send", "Отправить письмо", {
                "type": "object", "properties": {
                    "to": {"type": "string", "description": "Email получателя"},
                    "subject": {"type": "string", "description": "Тема письма"},
                    "body": {"type": "string", "description": "Текст письма"},
                    "cc": {"type": "string", "description": "Копия (email)"},
                    "html": {"type": "string", "description": "HTML-тело (опционально)"},
                }, "required": ["to", "subject", "body"],
            }),
            LLMTool("mail.reply", "Ответить на письмо по UID", {
                "type": "object", "properties": {
                    "uid": {"type": "integer", "description": "UID письма для ответа"},
                    "body": {"type": "string", "description": "Текст ответа"},
                    "folder": {"type": "string", "default": "INBOX"},
                }, "required": ["uid", "body"],
            }),
            LLMTool("mail.forward", "Переслать письмо по UID другому получателю", {
                "type": "object", "properties": {
                    "uid": {"type": "integer", "description": "UID письма"},
                    "to": {"type": "string", "description": "Email для пересылки"},
                    "folder": {"type": "string", "default": "INBOX"},
                }, "required": ["uid", "to"],
            }),
            LLMTool("mail.flag", "Отметить/удалить письмо", {
                "type": "object", "properties": {
                    "uid": {"type": "integer", "description": "UID письма"},
                    "action": {"type": "string", "enum": ["seen", "unseen", "flagged", "unflagged", "delete"],
                               "description": "Действие"},
                    "folder": {"type": "string", "default": "INBOX"},
                }, "required": ["uid", "action"],
            }),
        ]

    async def execute(self, tool_name: str, params: dict) -> str:
        cmd = tool_name.split(".")[-1]
        dispatch = {
            "inbox": self._inbox,
            "read": self._read,
            "search": self._search,
            "send": self._send,
            "reply": self._reply,
            "forward": self._forward,
            "flag": self._flag,
        }
        fn = dispatch.get(cmd)
        if not fn:
            return json.dumps({"error": f"unknown tool: {tool_name}"})
        if not self._user or not self._password:
            return json.dumps({"error": "Mail not configured. Set MAILRU_USER and MAILRU_APP_PASSWORD."})
        try:
            return await fn(params)
        except Exception as e:
            logger.error("mail_skill_error", cmd=cmd, error=str(e))
            return json.dumps({"error": str(e)})

    # ── IMAP helper ───────────────────────────────────────

    async def _imap_fetch(self, folder: str, search_criteria: list[str], limit: int,
                          full: bool = False) -> list[dict]:
        """Connect to IMAP, search, fetch messages, return list of dicts."""
        import imaplib

        def _run():
            conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
            conn.login(self._user, self._password)
            try:
                conn.select(folder, readonly=True)
                status, data = conn.uid("SEARCH", None, *search_criteria)
                if status != "OK" or not data[0]:
                    return []
                uid_list = data[0].split()
                selected = uid_list[-limit:]  # newest last → take tail
                messages = []
                for uid in selected:
                    fetch_parts = "(RFC822)" if full else "(RFC822.HEADER FLAGS)"
                    status2, msg_data = conn.uid("FETCH", uid, fetch_parts)
                    if status2 != "OK" or not msg_data or not msg_data[0]:
                        continue
                    # msg_data[0] is tuple (header, data)
                    raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else None
                    if not raw:
                        continue
                    parsed = _parse_message(raw)
                    parsed["uid"] = int(uid)
                    if not full:
                        parsed.pop("text", None)
                        parsed.pop("html", None)
                        parsed.pop("attachments", None)
                    # Get flags
                    flags_part = msg_data[1] if len(msg_data) > 1 else b""
                    if isinstance(flags_part, bytes):
                        parsed["seen"] = b"\\Seen" in flags_part
                        parsed["flagged"] = b"\\Flagged" in flags_part
                    messages.append(parsed)
                return list(reversed(messages))  # newest first
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass

        return await asyncio.get_event_loop().run_in_executor(None, _run)

    async def _smtp_send(self, to: str, subject: str, body: str,
                         cc: str = "", html: str = "",
                         in_reply_to: str = "", references: str = "") -> dict:
        """Send email via SMTP."""
        import smtplib

        def _run():
            if html:
                msg = email.mime.multipart.MIMEMultipart("alternative")
                msg.attach(email.mime.text.MIMEText(body, "plain", "utf-8"))
                msg.attach(email.mime.text.MIMEText(html, "html", "utf-8"))
            else:
                msg = email.mime.text.MIMEText(body, "plain", "utf-8")

            msg["From"] = self._user
            msg["To"] = to
            msg["Subject"] = subject
            msg["Date"] = email.utils.formatdate(localtime=True)
            if cc:
                msg["Cc"] = cc
            if in_reply_to:
                msg["In-Reply-To"] = in_reply_to
                msg["References"] = references or in_reply_to

            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
                server.login(self._user, self._password)
                recipients = [to] + ([cc] if cc else [])
                server.sendmail(self._user, recipients, msg.as_bytes())

            return {"success": True, "to": to, "subject": subject}

        return await asyncio.get_event_loop().run_in_executor(None, _run)

    # ── Commands ──────────────────────────────────────────

    async def _inbox(self, p: dict) -> str:
        folder = p.get("folder") or "INBOX"
        limit = int(p.get("limit") or 10)
        criteria = ["UNSEEN"] if p.get("unseen") else ["ALL"]
        messages = await self._imap_fetch(folder, criteria, limit)
        return json.dumps(messages, ensure_ascii=False)

    async def _read(self, p: dict) -> str:
        uid = int(p["uid"])
        folder = p.get("folder") or "INBOX"
        import imaplib

        def _run():
            conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
            conn.login(self._user, self._password)
            try:
                conn.select(folder, readonly=True)
                status, data = conn.uid("FETCH", str(uid), "(RFC822 FLAGS)")
                if status != "OK" or not data or not data[0]:
                    return {"error": f"Message UID {uid} not found"}
                raw = data[0][1] if isinstance(data[0], tuple) else None
                if not raw:
                    return {"error": "Empty message"}
                result = _parse_message(raw)
                result["uid"] = uid
                flags_part = data[1] if len(data) > 1 else b""
                if isinstance(flags_part, bytes):
                    result["seen"] = b"\\Seen" in flags_part
                return result
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass

        result = await asyncio.get_event_loop().run_in_executor(None, _run)
        return json.dumps(result, ensure_ascii=False)

    async def _search(self, p: dict) -> str:
        folder = p.get("folder") or "INBOX"
        limit = int(p.get("limit") or 20)
        criteria = []
        if p.get("query"):
            criteria += ["SUBJECT", p["query"]]
        if p.get("from_addr"):
            criteria += ["FROM", p["from_addr"]]
        if p.get("to_addr"):
            criteria += ["TO", p["to_addr"]]
        if p.get("since"):
            # IMAP date format: DD-Mon-YYYY
            try:
                dt = datetime.strptime(p["since"], "%Y-%m-%d")
                imap_date = dt.strftime("%d-%b-%Y")
                criteria += ["SINCE", imap_date]
            except Exception:
                pass
        if not criteria:
            criteria = ["ALL"]

        messages = await self._imap_fetch(folder, criteria, limit)
        return json.dumps(messages, ensure_ascii=False)

    async def _send(self, p: dict) -> str:
        result = await self._smtp_send(
            to=p["to"],
            subject=p.get("subject") or "(no subject)",
            body=p.get("body") or "",
            cc=p.get("cc") or "",
            html=p.get("html") or "",
        )
        return json.dumps(result, ensure_ascii=False)

    async def _reply(self, p: dict) -> str:
        uid = int(p["uid"])
        folder = p.get("folder") or "INBOX"
        # Fetch original to get headers
        original = json.loads(await self._read({"uid": uid, "folder": folder}))
        if "error" in original:
            return json.dumps(original)

        reply_to = original.get("reply_to") or original.get("from") or ""
        subject = original.get("subject") or ""
        if not subject.startswith("Re:"):
            subject = f"Re: {subject}"

        # Quote original
        orig_text = original.get("text") or ""
        quoted = "\n".join(f"> {line}" for line in orig_text.splitlines()[:20])
        body = f"{p['body']}\n\n--- Исходное письмо ---\nОт: {original.get('from', '')}\nДата: {original.get('date', '')}\nТема: {original.get('subject', '')}\n\n{quoted}"

        result = await self._smtp_send(
            to=reply_to,
            subject=subject,
            body=body,
            in_reply_to=original.get("message_id") or "",
            references=original.get("references") or original.get("message_id") or "",
        )
        return json.dumps(result, ensure_ascii=False)

    async def _forward(self, p: dict) -> str:
        uid = int(p["uid"])
        folder = p.get("folder") or "INBOX"
        original = json.loads(await self._read({"uid": uid, "folder": folder}))
        if "error" in original:
            return json.dumps(original)

        subject = f"Fwd: {original.get('subject') or ''}"
        body = (
            f"--- Пересланное письмо ---\n"
            f"От: {original.get('from', '')}\n"
            f"Дата: {original.get('date', '')}\n"
            f"Кому: {original.get('to', '')}\n"
            f"Тема: {original.get('subject', '')}\n\n"
            f"{original.get('text') or ''}"
        )
        result = await self._smtp_send(to=p["to"], subject=subject, body=body)
        return json.dumps(result, ensure_ascii=False)

    async def _flag(self, p: dict) -> str:
        uid = int(p["uid"])
        action = p["action"]
        folder = p.get("folder") or "INBOX"
        import imaplib

        def _run():
            conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
            conn.login(self._user, self._password)
            try:
                conn.select(folder)
                if action == "seen":
                    conn.uid("STORE", str(uid), "+FLAGS", "\\Seen")
                elif action == "unseen":
                    conn.uid("STORE", str(uid), "-FLAGS", "\\Seen")
                elif action == "flagged":
                    conn.uid("STORE", str(uid), "+FLAGS", "\\Flagged")
                elif action == "unflagged":
                    conn.uid("STORE", str(uid), "-FLAGS", "\\Flagged")
                elif action == "delete":
                    conn.uid("STORE", str(uid), "+FLAGS", "\\Deleted")
                    conn.expunge()
                else:
                    return {"error": f"Unknown action: {action}. Use: seen|unseen|flagged|unflagged|delete"}
                return {"success": True, "uid": uid, "action": action}
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass

        result = await asyncio.get_event_loop().run_in_executor(None, _run)
        return json.dumps(result, ensure_ascii=False)
