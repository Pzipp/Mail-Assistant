#!/usr/bin/env python3
"""
Mail Assistant Web API
FastAPI REST service til styring af filterregler og folders.json.
Kører side-om-side med scheduler.sh via entrypoint.sh.
"""

import json
import os
import sqlite3
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

DB_PATH         = "/data/seen.db"
FOLDERS_PATH    = "/data/folders.json"
PID_FILE        = "/data/assistant.pid"
STATIC_DIR      = os.path.join(os.path.dirname(__file__), "web")


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS filter_rules (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            name                  TEXT NOT NULL,
            enabled               INTEGER NOT NULL DEFAULT 1,
            crit_from             TEXT,
            crit_subject          TEXT,
            crit_body             TEXT,
            crit_has_attachment   INTEGER,
            action_move_to        TEXT,
            action_skip_llm       INTEGER NOT NULL DEFAULT 0,
            action_skip_llm_move  INTEGER NOT NULL DEFAULT 0,
            action_skip_task      INTEGER NOT NULL DEFAULT 0,
            sort_order            INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
            updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        )
    """)
    conn.commit()
    conn.close()
    yield


app = FastAPI(title="Mail Assistant API", lifespan=lifespan)


def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
    finally:
        conn.close()


class RuleIn(BaseModel):
    name:                 str
    enabled:              bool              = True
    crit_from:            Optional[str]     = None
    crit_subject:         Optional[str]     = None
    crit_body:            Optional[str]     = None
    crit_has_attachment:  Optional[int]     = None
    action_move_to:       Optional[str]     = None
    action_skip_llm:      bool              = False
    action_skip_llm_move: bool              = False
    action_skip_task:     bool              = False
    sort_order:           int               = 0


class ReorderBody(BaseModel):
    order: list[int]


@app.get("/api/rules")
def list_rules(db: sqlite3.Connection = Depends(get_db)):
    rows = db.execute(
        "SELECT * FROM filter_rules ORDER BY sort_order ASC, id ASC"
    ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/rules", status_code=201)
def create_rule(body: RuleIn, db: sqlite3.Connection = Depends(get_db)):
    cur = db.execute("""
        INSERT INTO filter_rules
            (name, enabled, crit_from, crit_subject, crit_body, crit_has_attachment,
             action_move_to, action_skip_llm, action_skip_llm_move, action_skip_task, sort_order)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        body.name, int(body.enabled), body.crit_from, body.crit_subject,
        body.crit_body, body.crit_has_attachment, body.action_move_to,
        int(body.action_skip_llm), int(body.action_skip_llm_move),
        int(body.action_skip_task), body.sort_order,
    ))
    db.commit()
    row = db.execute("SELECT * FROM filter_rules WHERE id=?", (cur.lastrowid,)).fetchone()
    return dict(row)


@app.get("/api/rules/{rule_id}")
def get_rule(rule_id: int, db: sqlite3.Connection = Depends(get_db)):
    row = db.execute("SELECT * FROM filter_rules WHERE id=?", (rule_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Regel ikke fundet")
    return dict(row)


@app.put("/api/rules/{rule_id}")
def update_rule(rule_id: int, body: RuleIn, db: sqlite3.Connection = Depends(get_db)):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute("""
        UPDATE filter_rules SET
            name=?, enabled=?, crit_from=?, crit_subject=?, crit_body=?,
            crit_has_attachment=?, action_move_to=?, action_skip_llm=?,
            action_skip_llm_move=?, action_skip_task=?, sort_order=?, updated_at=?
        WHERE id=?
    """, (
        body.name, int(body.enabled), body.crit_from, body.crit_subject,
        body.crit_body, body.crit_has_attachment, body.action_move_to,
        int(body.action_skip_llm), int(body.action_skip_llm_move),
        int(body.action_skip_task), body.sort_order, now, rule_id,
    ))
    db.commit()
    row = db.execute("SELECT * FROM filter_rules WHERE id=?", (rule_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Regel ikke fundet")
    return dict(row)


@app.delete("/api/rules/{rule_id}")
def delete_rule(rule_id: int, db: sqlite3.Connection = Depends(get_db)):
    db.execute("DELETE FROM filter_rules WHERE id=?", (rule_id,))
    db.commit()
    return {"ok": True}


@app.patch("/api/rules/{rule_id}/toggle")
def toggle_rule(rule_id: int, db: sqlite3.Connection = Depends(get_db)):
    row = db.execute("SELECT enabled FROM filter_rules WHERE id=?", (rule_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Regel ikke fundet")
    new_val = 0 if row["enabled"] else 1
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute("UPDATE filter_rules SET enabled=?, updated_at=? WHERE id=?", (new_val, now, rule_id))
    db.commit()
    return {"id": rule_id, "enabled": bool(new_val)}


@app.post("/api/rules/reorder")
def reorder_rules(body: ReorderBody, db: sqlite3.Connection = Depends(get_db)):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for idx, rule_id in enumerate(body.order):
        db.execute(
            "UPDATE filter_rules SET sort_order=?, updated_at=? WHERE id=?",
            (idx * 10, now, rule_id),
        )
    db.commit()
    return {"ok": True}


def _load_folders_json() -> dict[str, str]:
    try:
        with open(FOLDERS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if not k.startswith("_")}
    except FileNotFoundError:
        return {}


def _save_folders_json(data: dict[str, str]):
    os.makedirs("/data", exist_ok=True)
    with open(FOLDERS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


@app.get("/api/folders")
def list_folder_names():
    return sorted(_load_folders_json().keys())


@app.get("/api/folders-json")
def get_folders_json():
    return _load_folders_json()


@app.put("/api/folders-json")
def save_folders_json(data: dict[str, str]):
    _save_folders_json(data)
    return {"ok": True, "count": len(data)}


@app.post("/api/folders/sync")
def sync_folders_from_imap():
    try:
        from assistant import IMAPClient  # type: ignore[import]
    except ImportError as e:
        raise HTTPException(500, f"Kan ikke importere IMAPClient: {e}")

    try:
        client = IMAPClient()
        client.connect()
        imap_folders = client.fetch_folders()
        client.disconnect()
    except Exception as e:
        raise HTTPException(500, f"IMAP-fejl: {e}")

    existing = _load_folders_json()
    merged = dict(existing)
    added = 0
    for folder in imap_folders:
        if folder not in merged:
            merged[folder] = ""
            added += 1

    _save_folders_json(merged)
    return {"ok": True, "total": len(merged), "added": added, "folders": merged}


@app.get("/api/mails")
def list_mails(limit: int = 50, db: sqlite3.Connection = Depends(get_db)):
    rows = db.execute("""
        SELECT message_id, subject, sender, received_at, moved_to,
               needs_action, processed_at, pending_folder
        FROM seen_mails
        ORDER BY processed_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def _is_running() -> bool:
    if not os.path.exists(PID_FILE):
        return False
    try:
        pid = int(open(PID_FILE).read().strip())
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, OSError, ValueError):
        return False


@app.get("/api/run/status")
def run_status():
    return {"running": _is_running()}


@app.post("/api/run")
def run_assistant():
    if _is_running():
        return {"status": "already_running"}
    subprocess.Popen(["python", "/app/assistant.py"])
    return {"status": "started"}


@app.get("/api/health")
def health():
    return {"status": "ok"}
