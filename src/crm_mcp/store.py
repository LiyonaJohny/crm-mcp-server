"""SQLite storage for leads and notes.

Design choices worth knowing:

* Cursor pagination, not offset. Results stay stable while other writes happen.
* Every write takes an idempotency key. Agents retry, so a repeated call with the
  same key returns the first result instead of writing twice.
* Every write can be previewed with dry_run. The write runs for real inside a
  transaction that is then rolled back, so the preview is exactly what would be saved.
* Every write is recorded in an audit log, so a person can see what an agent changed.
* Errors are written for the caller: they say what was wrong and what to send instead.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from crm_mcp.models import LEAD_STATUSES, Lead, LeadDetail, LeadPage, LeadWrite, Note, NoteWrite

MAX_PAGE_SIZE = 50

# Ids returned by a dry run. Nothing is stored under them, so get_lead rejects them.
PREVIEW_LEAD_ID = "lead_preview"
PREVIEW_NOTE_ID = "note_preview"

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    id         TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    email      TEXT,
    company    TEXT,
    status     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads (status, seq);

CREATE TABLE IF NOT EXISTS notes (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    id         TEXT NOT NULL UNIQUE,
    lead_id    TEXT NOT NULL REFERENCES leads (id),
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_lead ON notes (lead_id, seq);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key        TEXT PRIMARY KEY,
    operation  TEXT NOT NULL,
    result     TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    operation  TEXT NOT NULL,
    lead_id    TEXT NOT NULL,
    detail     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class CRMError(Exception):
    """An error whose message tells the caller how to fix the request."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _encode_cursor(seq: int) -> str:
    return base64.urlsafe_b64encode(str(seq).encode()).decode()


def _decode_cursor(cursor: str) -> int:
    try:
        return int(base64.urlsafe_b64decode(cursor.encode()).decode())
    except (ValueError, UnicodeDecodeError) as exc:
        raise CRMError(
            "cursor is not valid. Use the next_cursor value from the previous search_leads result, "
            "or leave cursor empty to start from the first page."
        ) from exc


def _like_pattern(text: str) -> str:
    # % and _ are wildcards in LIKE; escape them so a search for "50%" matches that text only.
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class LeadStore:
    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._conn.close()

    # ---------- reads ----------

    def search_leads(
        self,
        query: str | None = None,
        status: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> LeadPage:
        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise CRMError(f"limit must be between 1 and {MAX_PAGE_SIZE}; you sent {limit}.")
        if status is not None and status not in LEAD_STATUSES:
            raise CRMError(f"status '{status}' is not valid. Use one of: {', '.join(LEAD_STATUSES)}.")

        sql = "SELECT * FROM leads WHERE 1 = 1"
        params: list[object] = []
        if query:
            sql += (
                " AND (name LIKE ? ESCAPE '\\' OR email LIKE ? ESCAPE '\\' OR company LIKE ? ESCAPE '\\')"
            )
            like = _like_pattern(query)
            params += [like, like, like]
        if status:
            sql += " AND status = ?"
            params.append(status)
        if cursor:
            sql += " AND seq > ?"
            params.append(_decode_cursor(cursor))
        sql += " ORDER BY seq LIMIT ?"
        params.append(limit + 1)

        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()

        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = _encode_cursor(rows[-1]["seq"]) if has_more and rows else None
        return LeadPage(leads=[self._lead(r) for r in rows], next_cursor=next_cursor)

    def get_lead(self, lead_id: str) -> LeadDetail:
        with self._lock:
            row = self._conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
            if row is None:
                raise CRMError(
                    f"No lead with id '{lead_id}'. Call search_leads to find the right id first."
                )
            note_rows = self._conn.execute(
                "SELECT * FROM notes WHERE lead_id = ? ORDER BY seq", (lead_id,)
            ).fetchall()
        return LeadDetail(
            **self._lead(row).model_dump(),
            notes=[Note(id=n["id"], lead_id=n["lead_id"], body=n["body"], created_at=n["created_at"]) for n in note_rows],
        )

    # ---------- writes ----------

    def create_lead(
        self,
        name: str,
        idempotency_key: str,
        email: str | None = None,
        company: str | None = None,
        dry_run: bool = False,
    ) -> LeadWrite:
        name = name.strip()
        if not name:
            raise CRMError("name cannot be empty. Send the person's or company's name.")
        if email is not None and "@" not in email:
            raise CRMError(f"email '{email}' does not look valid. Send an address like name@example.com, or leave it out.")

        def write() -> dict:
            now = _now()
            lead_id = PREVIEW_LEAD_ID if dry_run else _new_id("lead")
            self._conn.execute(
                "INSERT INTO leads (id, name, email, company, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'new', ?, ?)",
                (lead_id, name, email, company, now, now),
            )
            self._audit("create_lead", lead_id, {"name": name, "email": email, "company": company})
            row = self._conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
            return self._lead(row).model_dump()

        return LeadWrite(**self._idempotent("create_lead", idempotency_key, write, dry_run), dry_run=dry_run)

    def update_lead_status(
        self, lead_id: str, status: str, idempotency_key: str, dry_run: bool = False
    ) -> LeadWrite:
        if status not in LEAD_STATUSES:
            raise CRMError(f"status '{status}' is not valid. Use one of: {', '.join(LEAD_STATUSES)}.")

        def write() -> dict:
            row = self._conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
            if row is None:
                raise CRMError(f"No lead with id '{lead_id}'. Call search_leads to find the right id first.")
            now = _now()
            self._conn.execute(
                "UPDATE leads SET status = ?, updated_at = ? WHERE id = ?", (status, now, lead_id)
            )
            self._audit("update_lead_status", lead_id, {"from": row["status"], "to": status})
            row = self._conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
            return self._lead(row).model_dump()

        return LeadWrite(
            **self._idempotent("update_lead_status", idempotency_key, write, dry_run), dry_run=dry_run
        )

    def add_note(self, lead_id: str, body: str, idempotency_key: str, dry_run: bool = False) -> NoteWrite:
        body = body.strip()
        if not body:
            raise CRMError("body cannot be empty. Send the text of the note.")

        def write() -> dict:
            exists = self._conn.execute("SELECT 1 FROM leads WHERE id = ?", (lead_id,)).fetchone()
            if exists is None:
                raise CRMError(f"No lead with id '{lead_id}'. Call search_leads to find the right id first.")
            note_id = PREVIEW_NOTE_ID if dry_run else _new_id("note")
            note = Note(id=note_id, lead_id=lead_id, body=body, created_at=_now())
            self._conn.execute(
                "INSERT INTO notes (id, lead_id, body, created_at) VALUES (?, ?, ?, ?)",
                (note.id, note.lead_id, note.body, note.created_at),
            )
            self._audit("add_note", lead_id, {"note_id": note.id})
            return note.model_dump()

        return NoteWrite(**self._idempotent("add_note", idempotency_key, write, dry_run), dry_run=dry_run)

    def audit_entries(self, lead_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT operation, detail, created_at FROM audit_log WHERE lead_id = ? ORDER BY seq",
                (lead_id,),
            ).fetchall()
        return [{"operation": r["operation"], "detail": json.loads(r["detail"]), "created_at": r["created_at"]} for r in rows]

    # ---------- helpers ----------

    def _idempotent(self, operation: str, key: str, write, dry_run: bool = False) -> dict:
        key = key.strip()
        if not key:
            raise CRMError(
                "idempotency_key cannot be empty. Send a unique string per intended change "
                "(for example a UUID), and reuse it only when retrying the same change."
            )
        with self._lock:
            seen = self._conn.execute(
                "SELECT operation, result FROM idempotency_keys WHERE key = ?", (key,)
            ).fetchone()
            if seen is not None:
                if seen["operation"] != operation:
                    raise CRMError(
                        f"idempotency_key '{key}' was already used for {seen['operation']}. "
                        "Use a new key for a different change."
                    )
                return json.loads(seen["result"])
            try:
                result = write()
                if dry_run:
                    # Undo the write and leave the key unused, so the same key can apply the change later.
                    self._conn.rollback()
                    return result
                self._conn.execute(
                    "INSERT INTO idempotency_keys (key, operation, result, created_at) VALUES (?, ?, ?, ?)",
                    (key, operation, json.dumps(result), _now()),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            return result

    def _audit(self, operation: str, lead_id: str, detail: dict) -> None:
        self._conn.execute(
            "INSERT INTO audit_log (operation, lead_id, detail, created_at) VALUES (?, ?, ?, ?)",
            (operation, lead_id, json.dumps(detail), _now()),
        )

    @staticmethod
    def _lead(row: sqlite3.Row) -> Lead:
        return Lead(
            id=row["id"],
            name=row["name"],
            email=row["email"],
            company=row["company"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
