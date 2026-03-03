"""FastAPI сервер Пятница.ai — веб-интерфейс, чат, настройки."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

import structlog

logger = structlog.get_logger()

app = FastAPI(title="Пятница.ai", version="0.1.0")

# ─── Глобальные ссылки (инжектируются из main.py) ───────────
_agent = None
_settings_store = None
_memory_store = None


def inject_dependencies(agent, settings_store, memory_store):
    """Вызывается из main.py после инициализации."""
    global _agent, _settings_store, _memory_store
    _agent = agent
    _settings_store = settings_store
    _memory_store = memory_store


# ─── Health ──────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "pyatnitsa", "version": "0.1.0"}


# ─── Settings API ────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings():
    if not _settings_store:
        return JSONResponse({"error": "Settings store not initialized"}, 503)
    settings = await _settings_store.get_all(mask_secrets=True)
    return settings


class SettingsUpdate(BaseModel):
    settings: dict[str, str]


@app.post("/api/settings")
async def update_settings(body: SettingsUpdate):
    if not _settings_store:
        return JSONResponse({"error": "Settings store not initialized"}, 503)
    await _settings_store.set_many(body.settings)
    return {"status": "saved", "count": len(body.settings)}


# ─── Chat API (WebSocket) ───────────────────────────────────

@app.websocket("/ws/chat")
async def websocket_chat(ws: WebSocket):
    await ws.accept()
    session_id = str(uuid.uuid4())[:8]
    user_id = f"web_{session_id}"
    logger.info("ws_chat_connected", session=session_id)

    try:
        while True:
            data = await ws.receive_text()
            payload = json.loads(data)
            text = payload.get("text", "").strip()

            if not text:
                continue

            if not _agent:
                await ws.send_text(json.dumps({
                    "type": "error",
                    "text": "Агент не инициализирован. Проверьте настройки LLM.",
                }))
                continue

            await ws.send_text(json.dumps({"type": "typing"}))

            try:
                from pyatnitsa.core.models import Message, MessageRole

                msg = Message(
                    id=str(uuid.uuid4()),
                    channel="web",
                    user_id=user_id,
                    chat_id=session_id,
                    text=text,
                    role=MessageRole.USER,
                )

                response = await _agent.handle_message(msg)

                await ws.send_text(json.dumps({
                    "type": "message",
                    "text": response.text or "",
                }))
            except Exception as e:
                logger.error("ws_chat_error", error=str(e))
                await ws.send_text(json.dumps({
                    "type": "error",
                    "text": f"Ошибка: {e}",
                }))

    except WebSocketDisconnect:
        logger.info("ws_chat_disconnected", session=session_id)


# ─── Chat API (REST fallback) ───────────────────────────────

class ChatRequest(BaseModel):
    text: str
    user_id: str = "web_api"


@app.post("/api/chat")
async def rest_chat(body: ChatRequest):
    if not _agent:
        return JSONResponse({"error": "Agent not initialized"}, 503)

    from pyatnitsa.core.models import Message, MessageRole

    msg = Message(
        id=str(uuid.uuid4()),
        channel="web",
        user_id=body.user_id,
        chat_id="api",
        text=body.text,
        role=MessageRole.USER,
    )
    response = await _agent.handle_message(msg)
    return {"text": response.text}


# ─── System Info ─────────────────────────────────────────────

@app.get("/api/status")
async def system_status():
    info: dict[str, Any] = {
        "version": "0.1.0",
        "agent": _agent is not None,
        "llm_providers": [],
        "skills": [],
        "memory": _memory_store is not None,
    }
    if _agent:
        info["llm_providers"] = [p.name for p in _agent.llm.providers]
        info["skills"] = list(_agent.skills.skills.keys())
    return info


# ─── Web UI (single page) ───────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def web_ui():
    html_path = Path(__file__).parent / "web" / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Пятница.ai</h1><p>Web UI not found</p>")
