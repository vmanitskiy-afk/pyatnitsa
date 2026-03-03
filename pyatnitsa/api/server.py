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


_conversation_store = None


def inject_dependencies(agent, settings_store, memory_store, conversation_store=None):
    """Вызывается из main.py после инициализации."""
    global _agent, _settings_store, _memory_store, _conversation_store
    _agent = agent
    _settings_store = settings_store
    _memory_store = memory_store
    _conversation_store = conversation_store


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
    user_id = None
    logger.info("ws_chat_connected")

    try:
        while True:
            data = await ws.receive_text()
            payload = json.loads(data)

            if payload.get("type") == "init":
                user_id = payload.get("user_id", f"web_{uuid.uuid4().hex[:8]}")
                logger.info("ws_chat_init", user_id=user_id)
                if _conversation_store and _agent:
                    try:
                        chat = await _conversation_store.get_or_create_active_chat(user_id, "web")
                        msgs = await _conversation_store.get_messages(chat.id)
                        history = []
                        for m in msgs:
                            if m.role in ("user", "assistant"):
                                ct = m.content
                                try:
                                    parsed = json.loads(ct)
                                    if isinstance(parsed, list):
                                        texts = [b.get("text", "") for b in parsed if isinstance(b, dict) and b.get("type") == "text"]
                                        ct = " ".join(texts) if texts else ""
                                    elif isinstance(parsed, dict):
                                        ct = ""
                                except (json.JSONDecodeError, TypeError):
                                    pass
                                if ct and ct.strip():
                                    history.append({"role": m.role, "text": ct})
                        await ws.send_text(json.dumps({"type": "history", "messages": history, "chat_title": chat.title}))
                    except Exception as e:
                        logger.error("ws_history_error", error=str(e))
                continue

            text = payload.get("text", "").strip()
            if not text:
                continue
            if not user_id:
                user_id = f"web_{uuid.uuid4().hex[:8]}" 

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
                    chat_id="web",
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
        logger.info("ws_chat_disconnected", user_id=user_id)


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



# Chats API
@app.get("/api/chats")
async def list_chats(user_id: str = "web_default", limit: int = 10):
    if not _conversation_store:
        return JSONResponse({"error": "Conversations not initialized"}, 503)
    chats = await _conversation_store.list_chats(user_id, limit=limit)
    return [c.to_dict() for c in chats]


@app.post("/api/chats/new")
async def new_chat_api(user_id: str = "web_default"):
    if not _conversation_store:
        return JSONResponse({"error": "Conversations not initialized"}, 503)
    chat = await _conversation_store.create_chat(user_id, "web")
    return chat.to_dict()


# ─── System Info ─────────────────────────────────────────────

@app.post("/api/restart")
async def restart_server():
    """Перезапуск процесса сервера."""
    import os
    import sys

    logger.info("restart_requested_via_web")

    async def _restart():
        await asyncio.sleep(0.5)
        os.execv(sys.executable, [sys.executable, "-m", "pyatnitsa.main"])

    asyncio.create_task(_restart())
    return {"status": "restarting"}


@app.post("/api/apply")
async def apply_settings():
    """Сохраняет настройки и перезапускает сервер для полного применения."""
    import os
    import sys

    if not _settings_store:
        return JSONResponse({"error": "Settings store not initialized"}, 503)

    logger.info("apply_settings_restart")

    async def _restart():
        await asyncio.sleep(0.5)
        os.execv(sys.executable, [sys.executable, "-m", "pyatnitsa.main"])

    asyncio.create_task(_restart())
    return {"status": "restarting"}


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
