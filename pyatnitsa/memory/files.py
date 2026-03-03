"""Хранилище файлов Пятница.ai -- метаданные в SQLite, файлы на диске."""

from __future__ import annotations

import mimetypes
import uuid
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS uploaded_files (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    channel TEXT DEFAULT 'web',
    original_name TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    mime_type TEXT,
    size_bytes INTEGER DEFAULT 0,
    chat_id INTEGER,
    text_content TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_files_user ON uploaded_files(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_files_chat ON uploaded_files(chat_id);
"""

MAX_FILE_SIZE = 20 * 1024 * 1024

ALLOWED_MIME_PREFIXES = (
    "image/", "text/", "application/pdf",
    "application/msword", "application/vnd.openxmlformats",
    "application/vnd.ms-excel", "application/vnd.ms-powerpoint",
    "application/json", "application/xml", "application/zip",
    "application/x-tar", "application/gzip",
    "audio/", "video/",
)


class FileStore:

    def __init__(self, db: aiosqlite.Connection, upload_dir: str = "data/uploads"):
        self._db = db
        self._upload_dir = Path(upload_dir)

    async def init(self):
        self._upload_dir.mkdir(parents=True, exist_ok=True)
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def save_file(
        self, data: bytes, original_name: str, user_id: str,
        channel: str = "web", chat_id: int | None = None, mime_type: str | None = None,
    ) -> dict:
        if len(data) > MAX_FILE_SIZE:
            raise ValueError(f"File too big: {len(data)}")
        if not mime_type:
            mime_type = mimetypes.guess_type(original_name)[0] or "application/octet-stream"
        if not any(mime_type.startswith(p) for p in ALLOWED_MIME_PREFIXES):
            raise ValueError(f"Unsupported type: {mime_type}")

        file_id = uuid.uuid4().hex[:16]
        ext = Path(original_name).suffix or ""
        stored_path = self._upload_dir / f"{file_id}{ext}"
        stored_path.write_bytes(data)

        await self._db.execute(
            "INSERT INTO uploaded_files (id,user_id,channel,original_name,stored_path,mime_type,size_bytes,chat_id) VALUES (?,?,?,?,?,?,?,?)",
            (file_id, user_id, channel, original_name, str(stored_path), mime_type, len(data), chat_id),
        )
        await self._db.commit()
        return {"id": file_id, "name": original_name, "mime_type": mime_type, "size": len(data), "url": f"/api/files/{file_id}/{original_name}"}

    async def get_file(self, file_id: str) -> dict | None:
        cursor = await self._db.execute("SELECT * FROM uploaded_files WHERE id = ?", (file_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return {"id": row["id"], "user_id": row["user_id"], "original_name": row["original_name"],
                "stored_path": row["stored_path"], "mime_type": row["mime_type"], "size": row["size_bytes"],
                "chat_id": row["chat_id"], "text_content": row["text_content"],
                "url": f"/api/files/{row['id']}/{row['original_name']}"}

    async def get_file_data(self, file_id: str) -> tuple[bytes, str, str] | None:
        meta = await self.get_file(file_id)
        if not meta:
            return None
        path = Path(meta["stored_path"])
        if not path.exists():
            return None
        return path.read_bytes(), meta["mime_type"], meta["original_name"]

    async def set_text_content(self, file_id: str, text: str):
        await self._db.execute("UPDATE uploaded_files SET text_content = ? WHERE id = ?", (text, file_id))
        await self._db.commit()

    async def list_files(self, user_id: str | None = None, chat_id: int | None = None, limit: int = 20) -> list[dict]:
        if chat_id:
            cursor = await self._db.execute("SELECT * FROM uploaded_files WHERE chat_id = ? ORDER BY created_at DESC LIMIT ?", (chat_id, limit))
        elif user_id:
            cursor = await self._db.execute("SELECT * FROM uploaded_files WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (user_id, limit))
        else:
            return []
        rows = await cursor.fetchall()
        return [{"id": r["id"], "name": r["original_name"], "mime_type": r["mime_type"],
                 "size": r["size_bytes"], "url": f"/api/files/{r['id']}/{r['original_name']}",
                 "created_at": r["created_at"]} for r in rows]

    async def delete_file(self, file_id: str) -> bool:
        meta = await self.get_file(file_id)
        if not meta:
            return False
        path = Path(meta["stored_path"])
        if path.exists():
            path.unlink()
        await self._db.execute("DELETE FROM uploaded_files WHERE id = ?", (file_id,))
        await self._db.commit()
        return True
