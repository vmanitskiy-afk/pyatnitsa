"""
Навык calendar — интеграция с Mail.ru CalDAV.
CRUD событий через CalDAV HTTP + SMTP-приглашения через smtplib.
"""
from __future__ import annotations

import json
import re
import uuid
import os
from datetime import datetime, timedelta

import httpx
import structlog

from pyatnitsa.skills.base import BaseSkill, LLMTool

logger = structlog.get_logger("skill.calendar")


class CalendarSkill(BaseSkill):
    name = "calendar"
    description = "Управление календарём Mail.ru: события, встречи, приглашения"
    version = "1.0.0"

    def __init__(self):
        super().__init__()
        self._user = ""
        self._password = ""
        self._caldav_url = ""
        self._timezone = "Europe/Moscow"
        self._auth_header = ""

    async def on_load(self):
        self._user = os.getenv("MAILRU_USER", "")
        self._password = os.getenv("MAILRU_APP_PASSWORD", "")
        self._caldav_url = os.getenv(
            "MAILRU_CALDAV_URL",
            "https://calendar.mail.ru/principals/krasnodar.pro/aione/calendars/"
            "54a23a5f-e883-4666-a1e8-b0f36e7775af/",
        )
        self._timezone = os.getenv("MAILRU_TIMEZONE", "Europe/Moscow")

        if self._user and self._password:
            import base64
            creds = base64.b64encode(f"{self._user}:{self._password}".encode()).decode()
            self._auth_header = f"Basic {creds}"
            logger.info("calendar_skill_loaded", user=self._user)
        else:
            logger.warning("calendar_no_config", hint="Set MAILRU_USER and MAILRU_APP_PASSWORD")

    async def on_unload(self):
        pass

    def get_tools(self) -> list[LLMTool]:
        return [
            LLMTool("calendar.list", "Список событий за N дней", {
                "type": "object", "properties": {
                    "days": {"type": "integer", "default": 7, "description": "Количество дней вперёд"},
                },
            }),
            LLMTool("calendar.create", "Создать событие в календаре", {
                "type": "object", "properties": {
                    "title": {"type": "string"},
                    "start": {"type": "string", "description": "ISO datetime (2025-03-15T10:00)"},
                    "end": {"type": "string", "description": "ISO datetime (2025-03-15T11:00)"},
                    "description": {"type": "string"},
                    "location": {"type": "string"},
                    "attendees": {"type": "string", "description": "Email через запятую"},
                }, "required": ["title", "start", "end"],
            }),
            LLMTool("calendar.invite", "Создать событие + отправить email-приглашения (ICS)", {
                "type": "object", "properties": {
                    "title": {"type": "string"},
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "description": {"type": "string"},
                    "location": {"type": "string"},
                    "attendees": {"type": "string", "description": "Email через запятую (обязательно)"},
                }, "required": ["title", "start", "end", "attendees"],
            }),
            LLMTool("calendar.update", "Обновить событие по UID", {
                "type": "object", "properties": {
                    "uid": {"type": "string", "description": "UID события"},
                    "title": {"type": "string"},
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "description": {"type": "string"},
                    "location": {"type": "string"},
                }, "required": ["uid"],
            }),
            LLMTool("calendar.delete", "Удалить событие по UID", {
                "type": "object", "properties": {
                    "uid": {"type": "string", "description": "UID события"},
                }, "required": ["uid"],
            }),
        ]

    async def execute(self, tool_name: str, params: dict) -> str:
        cmd = tool_name.split(".")[-1]
        dispatch = {
            "list": self._list_events,
            "create": self._create_event,
            "invite": self._create_invite,
            "update": self._update_event,
            "delete": self._delete_event,
        }
        fn = dispatch.get(cmd)
        if not fn:
            return json.dumps({"error": f"unknown tool: {tool_name}"})
        if not self._auth_header:
            return json.dumps({"error": "Calendar not configured. Set MAILRU_USER and MAILRU_APP_PASSWORD."})
        return await fn(params)

    # ── CalDAV HTTP helpers ──────────────────────────────

    async def _dav_request(self, method: str, url: str, body: str | None = None,
                           extra_headers: dict | None = None) -> tuple[int, str]:
        headers = {
            "Authorization": self._auth_header,
            "Content-Type": "application/xml; charset=utf-8",
        }
        if extra_headers:
            headers.update(extra_headers)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(method, url, content=body, headers=headers)
            return resp.status_code, resp.text

    # ── iCal helpers ─────────────────────────────────────

    @staticmethod
    def _to_ical_date(iso_str: str) -> str:
        return re.sub(r"[-:]", "", iso_str).split(".")[0]

    @staticmethod
    def _format_ical_date(s: str) -> str:
        clean = re.sub(r"[^0-9T]", "", s)
        if len(clean) >= 15:
            return f"{clean[:4]}-{clean[4:6]}-{clean[6:8]}T{clean[9:11]}:{clean[11:13]}:{clean[13:15]}"
        if len(clean) >= 8:
            return f"{clean[:4]}-{clean[4:6]}-{clean[6:8]}"
        return s

    def _parse_events(self, ics_data: str) -> list[dict]:
        events = []
        blocks = ics_data.split("BEGIN:VEVENT")
        for block in blocks[1:]:
            block = block.split("END:VEVENT")[0]
            ev = {}

            def get_field(name: str) -> str:
                unfolded = re.sub(r"\r?\n[ \t]", "", block)
                for line in unfolded.split("\n"):
                    if line.startswith(name + ":") or line.startswith(name + ";"):
                        idx = line.index(":")
                        return line[idx + 1:]
                return ""

            ev["uid"] = get_field("UID")
            ev["summary"] = get_field("SUMMARY")
            ev["description"] = get_field("DESCRIPTION").replace("\\n", "\n").replace("\\,", ",")
            ev["location"] = get_field("LOCATION").replace("\\,", ",")
            ev["dtstart"] = self._format_ical_date(get_field("DTSTART"))
            ev["dtend"] = self._format_ical_date(get_field("DTEND"))

            attendees = re.findall(r"ATTENDEE[^:]*:mailto:([^\r\n]+)", block, re.IGNORECASE)
            ev["attendees"] = attendees

            if ev["uid"]:
                events.append(ev)
        return events

    def _build_ics(self, uid: str, title: str, start: str, end: str,
                   description: str = "", location: str = "",
                   attendees: list[str] | None = None, method: str = "PUBLISH") -> str:
        now = self._to_ical_date(datetime.utcnow().isoformat())
        dtstart = self._to_ical_date(start + ":00" if len(start) <= 16 else start)
        dtend = self._to_ical_date(end + ":00" if len(end) <= 16 else end)

        lines = [
            "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Pyatnitsa.ai//Calendar//EN",
            "CALSCALE:GREGORIAN", f"METHOD:{method}",
            "BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{now}",
            f"DTSTART;TZID={self._timezone}:{dtstart}",
            f"DTEND;TZID={self._timezone}:{dtend}",
            f"SUMMARY:{title}",
        ]
        if description:
            lines.append(f"DESCRIPTION:{description.replace(chr(10), '\\n')}")
        if location:
            lines.append(f"LOCATION:{location}")
        lines.append(f"ORGANIZER;CN={self._user}:mailto:{self._user}")
        if attendees:
            for email in attendees:
                lines.append(
                    f"ATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{email}"
                )
        lines += ["END:VEVENT", "END:VCALENDAR"]
        return "\r\n".join(lines)

    # ── Commands ─────────────────────────────────────────

    async def _list_events(self, p: dict) -> str:
        days = int(p.get("days") or 7)
        now = datetime.utcnow()
        end = now + timedelta(days=days)
        start_str = now.strftime("%Y%m%dT%H%M%SZ")
        end_str = end.strftime("%Y%m%dT%H%M%SZ")

        body = f"""<?xml version="1.0" encoding="UTF-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:getetag/><c:calendar-data/></d:prop>
  <c:filter>
    <c:comp-filter name="VCALENDAR">
      <c:comp-filter name="VEVENT">
        <c:time-range start="{start_str}" end="{end_str}"/>
      </c:comp-filter>
    </c:comp-filter>
  </c:filter>
</c:calendar-query>"""

        status, text = await self._dav_request("REPORT", self._caldav_url, body, {"Depth": "1"})
        if status == 401:
            return json.dumps({"error": "Invalid credentials"})

        all_events = []
        for m in re.finditer(
            r"<(?:[a-zA-Z0-9]+:)?calendar-data[^>]*>([\s\S]*?)</(?:[a-zA-Z0-9]+:)?calendar-data>",
            text,
        ):
            ics = m.group(1).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
            ics = re.sub(r"<!\[CDATA\[([\s\S]*?)\]\]>", r"\1", ics)
            all_events.extend(self._parse_events(ics))

        all_events.sort(key=lambda e: e.get("dtstart", ""))
        return json.dumps(all_events, ensure_ascii=False)

    async def _create_event(self, p: dict) -> str:
        uid = str(uuid.uuid4())
        ics = self._build_ics(
            uid=uid, title=p["title"], start=p["start"], end=p["end"],
            description=p.get("description", ""), location=p.get("location", ""),
            attendees=[e.strip() for e in p["attendees"].split(",")] if p.get("attendees") else None,
        )
        event_url = f"{self._caldav_url}{uid}.ics"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.put(event_url, content=ics, headers={
                "Authorization": self._auth_header,
                "Content-Type": "text/calendar; charset=utf-8",
                "If-None-Match": "*",
            })
        if resp.status_code >= 400:
            return json.dumps({"error": f"Failed ({resp.status_code}): {resp.text[:200]}"})

        return json.dumps({
            "success": True, "uid": uid, "title": p["title"],
            "start": p["start"], "end": p["end"],
        }, ensure_ascii=False)

    async def _create_invite(self, p: dict) -> str:
        # 1. Create event
        result_str = await self._create_event(p)
        result = json.loads(result_str)
        if not result.get("success"):
            return result_str

        # 2. Send email invitations
        import smtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        from email.mime.base import MIMEBase
        from email import encoders

        emails = [e.strip() for e in p["attendees"].split(",")]
        uid = str(uuid.uuid4())
        ics_content = self._build_ics(
            uid=uid, title=p["title"], start=p["start"], end=p["end"],
            description=p.get("description", ""), location=p.get("location", ""),
            attendees=emails, method="REQUEST",
        )

        text_body = (
            f"Вы приглашены на встречу:\n\n{p['title']}\n"
            f"Начало: {p['start']}\nОкончание: {p['end']}\n"
        )
        if p.get("location"):
            text_body += f"Место: {p['location']}\n"
        if p.get("description"):
            text_body += f"\n{p['description']}"

        try:
            with smtplib.SMTP_SSL("smtp.mail.ru", 465) as server:
                server.login(self._user, self._password)
                for email in emails:
                    msg = MIMEMultipart("mixed")
                    msg["From"] = self._user
                    msg["To"] = email
                    msg["Subject"] = f"Приглашение: {p['title']}"

                    msg.attach(MIMEText(text_body, "plain", "utf-8"))

                    ics_part = MIMEBase("text", "calendar", method="REQUEST", charset="UTF-8")
                    ics_part.set_payload(ics_content.encode("utf-8"))
                    encoders.encode_base64(ics_part)
                    ics_part.add_header("Content-Disposition", "attachment", filename="invite.ics")
                    msg.attach(ics_part)

                    server.sendmail(self._user, email, msg.as_string())

            return json.dumps({
                "success": True, "action": "invite",
                "title": p["title"], "invites_sent": emails,
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"success": True, "event_created": True,
                               "invite_error": str(e)[:200]}, ensure_ascii=False)

    async def _update_event(self, p: dict) -> str:
        uid = p["uid"]
        event_url = f"{self._caldav_url}{uid}.ics"

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(event_url, headers={"Authorization": self._auth_header})
        if resp.status_code >= 400:
            return json.dumps({"error": f"Event not found: {uid}"})

        data = resp.text
        etag = resp.headers.get("etag")

        if p.get("title"):
            data = re.sub(r"SUMMARY:.*", f"SUMMARY:{p['title']}", data)
        if p.get("start"):
            data = re.sub(r"DTSTART[^:]*:.*",
                          f"DTSTART;TZID={self._timezone}:{self._to_ical_date(p['start'] + ':00')}", data)
        if p.get("end"):
            data = re.sub(r"DTEND[^:]*:.*",
                          f"DTEND;TZID={self._timezone}:{self._to_ical_date(p['end'] + ':00')}", data)
        if p.get("description"):
            if "DESCRIPTION:" in data:
                data = re.sub(r"DESCRIPTION:.*", f"DESCRIPTION:{p['description']}", data)
            else:
                data = data.replace("END:VEVENT", f"DESCRIPTION:{p['description']}\r\nEND:VEVENT")
        if p.get("location"):
            if "LOCATION:" in data:
                data = re.sub(r"LOCATION:.*", f"LOCATION:{p['location']}", data)
            else:
                data = data.replace("END:VEVENT", f"LOCATION:{p['location']}\r\nEND:VEVENT")

        now = self._to_ical_date(datetime.utcnow().isoformat())
        data = re.sub(r"DTSTAMP:.*", f"DTSTAMP:{now}", data)

        headers = {"Authorization": self._auth_header, "Content-Type": "text/calendar; charset=utf-8"}
        if etag:
            headers["If-Match"] = etag

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.put(event_url, content=data, headers=headers)
        if resp.status_code >= 400:
            return json.dumps({"error": f"Failed to update ({resp.status_code})"})

        return json.dumps({"success": True, "updated": uid}, ensure_ascii=False)

    async def _delete_event(self, p: dict) -> str:
        uid = p["uid"]

        # Try direct URL first
        event_url = f"{self._caldav_url}{uid}.ics"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.delete(event_url, headers={"Authorization": self._auth_header})
        if resp.status_code < 300:
            return json.dumps({"success": True, "deleted": uid})

        # Fallback: search all events and find by UID in href
        body = """<?xml version="1.0" encoding="UTF-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:getetag/><c:calendar-data/></d:prop>
  <c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT"/></c:comp-filter></c:filter>
</c:calendar-query>"""

        status, text = await self._dav_request("REPORT", self._caldav_url, body, {"Depth": "1"})
        for m in re.finditer(r"<(?:[a-zA-Z0-9]+:)?href>([^<]*?\.ics)</", text):
            href = m.group(1)
            if uid in href or uid in text:
                delete_url = href if href.startswith("http") else f"https://calendar.mail.ru{href}"
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.delete(delete_url, headers={"Authorization": self._auth_header})
                if resp.status_code < 300:
                    return json.dumps({"success": True, "deleted": uid})

        return json.dumps({"error": f"Event not found or delete failed: {uid}"})
