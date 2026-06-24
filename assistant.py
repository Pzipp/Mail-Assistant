#!/usr/bin/env python3
"""
V: 0.2R18 — 2026-06-16
mail-assistant — IMAP → LLM → Google Tasks
Tilladt på mailserver: COPY/MOVE (flytning), STORE (kun \\Flagged). Aldrig: EXPUNGE, DELETE, APPEND
Aldrig: sletning, markering som læst, svar, afsendelse
"""

import imaplib
import email
import email.header
import html
import re
import sqlite3
import json
import os
import sys
import logging
import uuid
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from email.utils import parsedate_to_datetime
from google.oauth2.credentials import Credentials # pyright: ignore[reportMissingImports]
from google_auth_oauthlib.flow import InstalledAppFlow # pyright: ignore[reportMissingImports]
from google.auth.transport.requests import Request # pyright: ignore[reportMissingImports]
from googleapiclient.discovery import build # pyright: ignore[reportMissingImports]
import pickle

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Konfiguration fra miljøvariabler ──────────────────────────────────────────
IMAP_HOST       = os.environ["IMAP_HOST"]
IMAP_PORT       = int(os.environ.get("IMAP_PORT", "993"))
IMAP_USER       = os.environ["IMAP_USER"]
IMAP_PASSWORD   = os.environ["IMAP_PASSWORD"]
IMAP_FOLDER     = os.environ.get("IMAP_FOLDER", "INBOX")

# Præfiks på IMAP-mappenavne (f.eks. "" eller "INBOX.") — bruges til at strippe ved visning
IMAP_FOLDER_PREFIX  = os.environ.get("IMAP_FOLDER_PREFIX", "")

# Arkivmappe til auto-completion-tjek (præfiks tilføjes automatisk)
IMAP_ARCHIVE_FOLDER = os.environ.get("IMAP_ARCHIVE_FOLDER", "Archive")

# ── LLM provider ──────────────────────────────────────────────────────────────
# LLM_PROVIDER: "ollama" | "anthropic" | "mistral" | "claude-code"
LLM_PROVIDER    = os.environ.get("LLM_PROVIDER", "ollama")
LLM_TIMEOUT     = int(os.environ.get("LLM_TIMEOUT", "600"))         # sekunder — gælder alle providers

# Ollama (lokal)
OLLAMA_HOST       = os.environ.get("OLLAMA_HOST", "http://ollama:11434")
OLLAMA_MODEL      = os.environ.get("OLLAMA_MODEL", "qwen2.5:0.5b")
OLLAMA_NUM_CTX    = int(os.environ.get("OLLAMA_NUM_CTX", "2048"))   # context window
OLLAMA_NUM_THREAD = int(os.environ.get("OLLAMA_NUM_THREAD", "2"))   # CPU tråde

# Anthropic (Claude API)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL   = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

# Mistral API
MISTRAL_API_KEY   = os.environ.get("MISTRAL_API_KEY", "")
MISTRAL_MODEL     = os.environ.get("MISTRAL_MODEL", "mistral-small-latest")

# Claude Code (lokal container)
CLAUDE_CODE_HOST    = os.environ.get("CLAUDE_CODE_HOST", "http://claude-code:11578")
CLAUDE_CODE_TIMEOUT = int(os.environ.get("CLAUDE_CODE_TIMEOUT", "300"))
CLAUDE_CODE_DIR     = Path(os.environ.get("CLAUDE_CODE_DIR", "/data/claudecli"))

GOOGLE_TASKLIST = os.environ.get("GOOGLE_TASKLIST", "@default")

# Startdato: hvis sat i .env bruges den, ellers bruges DB-gemt dato (= første kørsel = i dag)
SCAN_FROM_DATE  = os.environ.get("SCAN_FROM_DATE", "")   # Format: YYYY-MM-DD

# Maks mails per kørsel (fordel lasten over 6 daglige kørsler)
MAX_MAILS       = int(os.environ.get("MAX_MAILS", "30"))

# Fallback deadline: antal dage fra modtagelse hvis LLM ikke finder en dato
DEADLINE_FALLBACK_DAYS = int(os.environ.get("DEADLINE_FALLBACK_DAYS", "0"))  # 0 = i dag

# Antal mails per LLM-kald — 1 = single (gammel adfærd), >1 = batch
LLM_BATCH_SIZE = int(os.environ.get("LLM_BATCH_SIZE", "1"))

# Debug-tilstand: logger LLM-prompts og råsvar
LLM_DEBUG = os.environ.get("LLM_DEBUG", "").lower() in ("1", "true", "yes")
if LLM_DEBUG:
    logging.getLogger().setLevel(logging.DEBUG)
    log.debug("Debug-tilstand aktiveret (LLM_DEBUG=1)")

# ── Auto-completion af tasks ───────────────────────────────────────────────────
# Marker task udført hvis mailen har \Answered flag (besvaret)
COMPLETE_ON_ANSWERED = os.environ.get("COMPLETE_ON_ANSWERED", "true").lower() not in ("false", "0", "no")
# Marker task udført hvis mail er forsvundet fra INBOX og findes i arkivmappen
COMPLETE_ON_ARCHIVED = os.environ.get("COMPLETE_ON_ARCHIVED", "true").lower() not in ("false", "0", "no")

# Kommasepareret liste af prioriteter der sætter \Flagged på mailen i INBOX
# Eksempel: "high" eller "medium,high" — tom streng = aldrig flag
FLAG_ON_PRIORITY = {p.strip().lower() for p in os.environ.get("FLAG_ON_PRIORITY", "").split(",") if p.strip()}

DB_PATH    = "/data/seen.db"
TOKEN_PATH = "/data/google_token.pickle"
CREDS_PATH = "/data/google_credentials.json"
PID_FILE   = "/data/assistant.pid"

SCOPES = ["https://www.googleapis.com/auth/tasks"]


# ── PID-fil beskyttelse mod dobbelt-kørsel ─────────────────────────────────────
def is_already_running() -> bool:
    """
    Tjekker om en instans allerede kører via PID-fil.
    Håndterer gracefully hvis scriptet crashede uden at slette filen.
    """
    if not os.path.exists(PID_FILE):
        return False
    try:
        pid = int(open(PID_FILE).read().strip())
        os.kill(pid, 0)   # Signal 0 = tjek eksistens, sender intet
        return True        # Processen kører stadig
    except (ProcessLookupError, OSError, ValueError):
        # PID eksisterer ikke eller filen er korrupt — ignorer
        log.warning(f"Gammel PID-fil fundet ({PID_FILE}) — forrige kørsel afsluttede ikke rent. Fortsætter.")
        return False


def write_pid():
    """Skriver aktuel PID til fil."""
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def remove_pid():
    """Sletter PID-fil ved afslutning."""
    try:
        os.remove(PID_FILE)
    except FileNotFoundError:
        pass


# ── IMAP modified UTF-7 encoding/decoding ─────────────────────────────────────
def decode_imap_name(name: str) -> str:
    """
    Decoder modified UTF-7 IMAP mappenavn til Unicode.
    Eksempel: &ANg-konomi → Økonomi
    IMAP bruger modified UTF-7 (RFC 3501) hvor & starter en encoded sekvens.
    """
    import base64
    res = []
    i = 0
    while i < len(name):
        if name[i] == "&":
            j = name.index("-", i + 1)
            encoded = name[i + 1:j]
            if encoded == "":
                res.append("&")   # &- er literal &
            else:
                # Pad base64 og decode som UTF-16BE
                pad = encoded + "=" * (-len(encoded) % 4)
                res.append(base64.b64decode(pad).decode("utf-16-be"))
            i = j + 1
        else:
            res.append(name[i])
            i += 1
    return "".join(res)


def encode_imap_name(name: str) -> str:
    """
    Encoder Unicode mappenavn til modified UTF-7 til IMAP-kommandoer.
    Eksempel: Økonomi → &ANg-konomi
    """
    import base64
    res = []
    buf = []
    for c in name:
        if ord(c) < 128 and c != "&":
            if buf:
                encoded = base64.b64encode("".join(buf).encode("utf-16-be")).decode("ascii").rstrip("=")
                res.append(f"&{encoded}-")
                buf = []
            res.append(c)
        elif c == "&":
            if buf:
                encoded = base64.b64encode("".join(buf).encode("utf-16-be")).decode("ascii").rstrip("=")
                res.append(f"&{encoded}-")
                buf = []
            res.append("&-")
        else:
            buf.append(c)
    if buf:
        encoded = base64.b64encode("".join(buf).encode("utf-16-be")).decode("ascii").rstrip("=")
        res.append(f"&{encoded}-")
    return "".join(res)


# ── Database ───────────────────────────────────────────────────────────────────
def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_mails (
            message_id     TEXT PRIMARY KEY,
            subject        TEXT,
            sender         TEXT,
            received_at    TEXT,
            task_id        TEXT,
            needs_action   INTEGER,
            moved_to       TEXT,
            processed_at   TEXT,
            thread_id      TEXT,
            pending_folder TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            task_id     TEXT PRIMARY KEY,
            message_id  TEXT,
            subject     TEXT,
            due_date    TEXT,
            created_at  TEXT,
            completed   INTEGER DEFAULT 0,
            thread_id   TEXT
        )
    """)
    # Gem startdato — sættes én gang og ændres ikke automatisk herefter
    conn.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()

    # Migration: tilføj nye kolonner til eksisterende databaser
    for col, definition in [
        ("thread_id",      "TEXT"),
        ("pending_folder", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE seen_mails ADD COLUMN {col} {definition}")
            conn.commit()
            log.info(f"DB migration: tilføjede seen_mails.{col}")
        except sqlite3.OperationalError:
            pass  # kolonnen eksisterer allerede

    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN thread_id TEXT")
        conn.commit()
        log.info("DB migration: tilføjede tasks.thread_id")
    except sqlite3.OperationalError:
        pass

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

    return conn


def get_scan_start_date(conn) -> str:
    """
    Returnerer den dato (SINCE-format til IMAP) der scannes fra.
    Prioritet: SCAN_FROM_DATE env → gemt i DB → i dag.
    Gemmer datoen i DB ved første kørsel så den ikke ændrer sig.
    """
    # Env-variabel overskriver altid
    if SCAN_FROM_DATE:
        try:
            dt = datetime.strptime(SCAN_FROM_DATE, "%Y-%m-%d")
            return dt.strftime("%d-%b-%Y")
        except ValueError:
            log.warning(f"Ugyldig SCAN_FROM_DATE '{SCAN_FROM_DATE}', bruger i dag")

    # Tjek DB
    row = conn.execute("SELECT value FROM config WHERE key='scan_start'").fetchone()
    if row:
        return row[0]

    # Første kørsel — brug i dag og gem
    today = datetime.now(timezone.utc).strftime("%d-%b-%Y")
    conn.execute(
        "INSERT INTO config (key, value) VALUES ('scan_start', ?)", (today,)
    )
    conn.commit()
    log.info(f"Startdato sat til i dag: {today}")
    return today


def load_filter_rules(conn) -> list[dict]:
    """Henter aktive filterregler sorteret efter sort_order."""
    rows = conn.execute("""
        SELECT id, name, crit_from, crit_subject, crit_body, crit_has_attachment,
               action_move_to, action_skip_llm, action_skip_llm_move, action_skip_task
        FROM filter_rules WHERE enabled = 1 ORDER BY sort_order ASC, id ASC
    """).fetchall()
    cols = ["id", "name", "crit_from", "crit_subject", "crit_body", "crit_has_attachment",
            "action_move_to", "action_skip_llm", "action_skip_llm_move", "action_skip_task"]
    return [dict(zip(cols, row)) for row in rows]


def match_rule(rule: dict, mail: dict) -> bool:
    """Returnerer True hvis mailen opfylder ALLE specificerede kriterier (AND-logik)."""
    if rule["crit_from"] is not None and rule["crit_from"].lower() not in mail["sender"].lower():
        return False
    if rule["crit_subject"] is not None and rule["crit_subject"].lower() not in mail["subject"].lower():
        return False
    if rule["crit_body"] is not None and rule["crit_body"].lower() not in mail["body"].lower():
        return False
    if rule["crit_has_attachment"] is not None:
        has = len(mail.get("attachments", [])) > 0
        if bool(rule["crit_has_attachment"]) != has:
            return False
    return True


def apply_filter_rules(rules: list[dict], mail: dict) -> dict:
    """Returnerer overrides fra første matchende regel (stop-on-first-match)."""
    result = {
        "matched_rule_name": None,
        "move_to":           None,
        "skip_llm":          False,
        "skip_llm_move":     False,
        "skip_task":         False,
    }
    for rule in rules:
        if match_rule(rule, mail):
            result.update({
                "matched_rule_name": rule["name"],
                "move_to":           rule["action_move_to"],
                "skip_llm":          bool(rule["action_skip_llm"]),
                "skip_llm_move":     bool(rule["action_skip_llm_move"]),
                "skip_task":         bool(rule["action_skip_task"]),
            })
            log.info(f"Filterregel '{rule['name']}' matcher: {mail['subject'][:60]}")
            break
    return result


def is_seen(conn, message_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM seen_mails WHERE message_id = ?", (message_id,)
    ).fetchone() is not None


def mark_seen(conn, message_id: str, subject: str, sender: str,
              received_at: str, task_id: str | None,
              needs_action: bool, moved_to: str | None,
              thread_id: str | None = None,
              pending_folder: str | None = None):
    conn.execute("""
        INSERT OR REPLACE INTO seen_mails
            (message_id, subject, sender, received_at, task_id,
             needs_action, moved_to, processed_at, thread_id, pending_folder)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        message_id, subject, sender, received_at, task_id,
        1 if needs_action else 0, moved_to,
        datetime.now(timezone.utc).isoformat(),
        thread_id, pending_folder
    ))
    conn.commit()


# ── IMAP ───────────────────────────────────────────────────────────────────────
class IMAPClient:
    """
    IMAP-klient med eksplicit kontrol over hvad der er tilladt.
    Tilladt:  SELECT, FETCH, LIST, MOVE (RFC 6851), COPY, STORE (kun \\Flagged)
    Forbudt:  EXPUNGE, DELETE, APPEND, CREATE, STORE \\Seen/\\Deleted
    Mappeliste hentes én gang ved opstart og sendes med til LLM.
    """

    def __init__(self):
        self.conn = None

    def connect(self):
        log.info(f"Forbinder til IMAP: {IMAP_HOST}:{IMAP_PORT}")
        self.conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        self.conn.login(IMAP_USER, IMAP_PASSWORD)

    def disconnect(self):
        if self.conn:
            try:
                self.conn.logout()
            except Exception:
                pass

    def fetch_folders(self) -> list[str]:
        """
        Henter alle IMAP-mapper ved container-start.
        Returnerer liste af mappenavne uden præfiks, sorteret.
        Bruges til at bygge system-prompt så LLM kun vælger eksisterende mapper.
        """
        _, folder_list = self.conn.list()
        folders = []
        for item in folder_list:
            if not item:
                continue
            decoded = item.decode("utf-8", errors="replace")
            # IMAP LIST format: (\Flags) "separator" "name"
            # Del ved separator (typisk "/") og tag sidste del
            parts = decoded.split('"/"')
            if len(parts) >= 2:
                name = parts[-1].strip().strip('"')
            else:
                # Prøv split ved mellemrum som fallback
                name = decoded.split()[-1].strip('"')

            # Decode modified UTF-7 så LLM ser Unicode (Økonomi i stedet for &ANg-konomi)
            name = decode_imap_name(name)

            # Fjern præfiks så LLM ser rene mappenavne
            if IMAP_FOLDER_PREFIX and name.startswith(IMAP_FOLDER_PREFIX):
                name = name[len(IMAP_FOLDER_PREFIX):]

            # Filtrer systemmapper og INBOX selv fra listen (case-insensitiv)
            skip_lower = {s.lower() for s in {"INBOX", "Trash", "Junk", "Spam", "Drafts",
                                               "Sent", "Deleted Items", "Sent Items", "[Gmail]"}}
            if name and name.lower() not in skip_lower and not name.startswith("["):
                folders.append(name)

        folders.sort()
        log.info(f"Fandt {len(folders)} IMAP-mapper: {', '.join(folders)}")
        return folders

    def fetch_new_mails(self, db_conn, since_date: str) -> list[dict]:
        """Henter og returnerer nye mails fra INBOX. Ændrer intet."""
        # Åbn INBOX — ikke readonly da vi skal kunne MOVE/COPY/STORE \\Flagged
        self.conn.select(IMAP_FOLDER, readonly=False)

        _, uids = self.conn.uid("SEARCH", None, f"SINCE {since_date}")
        uid_list = uids[0].split() if uids[0] else []
        log.info(f"Fandt {len(uid_list)} mails siden {since_date}")

        # Nyeste først, begræns til MAX_MAILS
        uid_list = uid_list[-MAX_MAILS:]

        mails = []
        for uid in uid_list:
            try:
                _, data = self.conn.uid("FETCH", uid, "(RFC822)")
                raw = data[0][1]
                msg = email.message_from_bytes(raw)

                message_id = msg.get("Message-ID", "").strip()
                if not message_id or is_seen(db_conn, message_id):
                    continue

                subject     = decode_header_value(msg.get("Subject", "(intet emne)"))
                sender      = decode_header_value(msg.get("From", "ukendt"))
                date_str    = msg.get("Date", "")
                try:
                    received_at = parsedate_to_datetime(date_str).isoformat()
                except Exception:
                    received_at = datetime.now(timezone.utc).isoformat()

                body        = extract_text_body(msg)[:2500]
                attachments = extract_attachments(msg)
                in_reply_to = msg.get("In-Reply-To", "").strip()
                references  = msg.get("References", "").strip()

                mails.append({
                    "uid":         uid,
                    "message_id":  message_id,
                    "subject":     subject,
                    "sender":      sender,
                    "received_at": received_at,
                    "body":        body,
                    "attachments": attachments,
                    "in_reply_to": in_reply_to,
                    "references":  references,
                })
            except Exception as e:
                log.warning(f"Fejl ved hentning af UID {uid}: {e}")

        log.info(f"{len(mails)} nye mails til behandling")
        return mails

    def move_mail(self, uid: bytes, target_folder: str) -> bool:
        """
        Flytter mail til målmappe.
        Strategi:
          1. RFC 6851 MOVE hvis serveren understøtter det — atomisk, ingen rester.
          2. Fallback: COPY uden efterfølgende sletning.
             Originalen bliver i INBOX — ryddes op manuelt når alt virker.
        Ingen STORE \\Deleted, ingen EXPUNGE, ingen sletning i fallback.
        """
        # Encode Unicode mappenavn til modified UTF-7 som IMAP-serveren forventer
        encoded_target = encode_imap_name(f"{IMAP_FOLDER_PREFIX}{target_folder}")

        try:
            # Forsøg MOVE (RFC 6851) — understøttes af Dovecot, Cyrus, Exchange m.fl.
            capabilities = self.conn.capability()[1][0].decode().upper()
            if "MOVE" in capabilities:
                result = self.conn.uid("MOVE", uid, encoded_target)
                if result[0] == "OK":
                    log.info(f"Flyttede mail (MOVE) til: {target_folder}")
                    return True
                log.warning(f"MOVE svarede {result[0]}, prøver COPY-fallback")

            # Fallback: COPY — original bliver i INBOX, ingen sletning
            copy_result = self.conn.uid("COPY", uid, encoded_target)
            if copy_result[0] != "OK":
                log.warning(f"COPY fejlede for UID {uid}: {copy_result}")
                return False

            log.info(f"Kopierede mail (COPY-fallback, original beholdt) til: {target_folder}")
            return True

        except Exception as e:
            log.warning(f"Kunne ikke flytte mail UID {uid} til {encoded_target}: {e}")
            return False

    def set_flagged(self, uid: bytes) -> bool:
        """Sætter \\Flagged på mail i INBOX (markerer som vigtig)."""
        try:
            self.conn.select(IMAP_FOLDER, readonly=False)
            result = self.conn.uid("STORE", uid, "+FLAGS", "\\Flagged")
            if result[0] == "OK":
                log.debug(f"Satte \\Flagged på UID {uid}")
                return True
            log.warning(f"Kunne ikke sætte \\Flagged på UID {uid}: {result}")
            return False
        except Exception as e:
            log.warning(f"set_flagged fejl for UID {uid}: {e}")
            return False

    def check_mail_status(self, message_id: str) -> dict:
        """
        Tjekker mailens aktuelle IMAP-status via Message-ID header-søgning.
        Returnerer: {"answered": bool, "in_inbox": bool, "in_archive": bool}
        Søger i INBOX og IMAP_ARCHIVE_FOLDER via SEARCH HEADER Message-ID.
        """
        result = {"answered": False, "in_inbox": False, "in_archive": False}

        # Tjek INBOX — hent UID og flags
        try:
            self.conn.select(IMAP_FOLDER, readonly=True)
            _, uids = self.conn.uid("SEARCH", None, "HEADER", "Message-ID", message_id)
            uid_list = uids[0].split() if uids[0] else []
            if uid_list:
                result["in_inbox"] = True
                _, flags_data = self.conn.uid("FETCH", uid_list[0], "(FLAGS)")
                if flags_data and flags_data[0]:
                    raw = flags_data[0]
                    flags_str = raw.decode() if isinstance(raw, bytes) else str(raw)
                    result["answered"] = "\\Answered" in flags_str
        except Exception as e:
            log.warning(f"check_mail_status INBOX fejl for '{message_id[:40]}': {e}")

        # Tjek arkivmappe
        archive_encoded = encode_imap_name(f"{IMAP_FOLDER_PREFIX}{IMAP_ARCHIVE_FOLDER}")
        try:
            status, _ = self.conn.select(archive_encoded, readonly=True)
            if status == "OK":
                _, uids = self.conn.uid("SEARCH", None, "HEADER", "Message-ID", message_id)
                uid_list = uids[0].split() if uids[0] else []
                result["in_archive"] = bool(uid_list)
        except Exception as e:
            log.debug(f"check_mail_status archive fejl for '{message_id[:40]}': {e}")

        log.debug(
            f"check_mail_status '{message_id[:40]}': "
            f"inbox={result['in_inbox']} answered={result['answered']} archive={result['in_archive']}"
        )
        return result

    def find_uid_by_message_id(self, message_id: str) -> bytes | None:
        """Søger i INBOX efter mail med givet Message-ID, returnerer UID eller None."""
        try:
            self.conn.select(IMAP_FOLDER, readonly=False)
            _, uids = self.conn.uid("SEARCH", None, "HEADER", "Message-ID", message_id)
            uid_list = uids[0].split() if uids[0] else []
            return uid_list[0] if uid_list else None
        except Exception as e:
            log.warning(f"find_uid_by_message_id fejl for '{message_id[:40]}': {e}")
            return None


# ── Hjælpefunktioner ───────────────────────────────────────────────────────────
def decode_header_value(value: str) -> str:
    parts = email.header.decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(str(part))
    return " ".join(decoded)


def _clean_body(text: str) -> str:
    text = re.sub(r"<https?://[^>]+>", "", text)
    text = re.sub(r"https?://\S+", "", text)
    text = html.unescape(text)
    text = text.replace(" ", " ")
    text = re.sub(r"(\r\n|\r)", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"^ $", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_text_body(msg) -> str:
    """
    Udtrækker plain-text body fra mail.
    Hvis kun HTML findes, bruges den med entities decoded.
    """
    plain = None
    html_body = None

    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if "attachment" in cd:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                decoded = part.get_payload(decode=True).decode(charset, errors="replace")
            except Exception:
                continue
            if ct == "text/plain" and plain is None:
                plain = decoded
            elif ct == "text/html" and html_body is None:
                html_body = decoded
    else:
        charset = msg.get_content_charset() or "utf-8"
        try:
            decoded = msg.get_payload(decode=True).decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                html_body = decoded
            else:
                plain = decoded
        except Exception:
            pass

    if plain:
        return _clean_body(plain)

    if html_body:
        # Fjern HTML-tags og decode entities til plain text
        text = re.sub(r"<[^>]+>", " ", html_body)
        text = html.unescape(text)
        return _clean_body(text)

    return ""


def extract_attachments(msg) -> list[str]:
    """
    Returnerer liste af vedhæftede filers navne og MIME-type.
    Selve filindholdet sendes ikke — kun metadata til LLM.
    Eksempel: ["rapport.pdf (application/pdf)", "billede.jpg (image/jpeg)"]
    """
    attachments = []
    if msg.is_multipart():
        for part in msg.walk():
            cd = str(part.get("Content-Disposition", ""))
            if "attachment" in cd:
                filename = part.get_filename()
                if filename:
                    filename = decode_header_value(filename)
                    mime     = part.get_content_type()
                    attachments.append(f"{filename} ({mime})")
    return attachments


# ── LLM klassificering ─────────────────────────────────────────────────────────
FOLDERS_JSON_PATH  = "/data/folders.json"
# Bruger-override: /data/system_prompt.txt (rettes uden rebuild)
# Fallback: den bagt-ind skabelon i imagen
_PROMPT_FILE_USER    = "/data/system_prompt.txt"
_PROMPT_FILE_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts", "system_prompt.txt")


def load_folder_descriptions() -> dict[str, str]:
    """
    Indlæser mapbeskrivelser fra folders.json.
    Format: {"Mappenavn": "Beskrivelse af hvad der hører til her"}
    Bruges til at hjælpe LLM med at vælge korrekt mappe.
    """
    try:
        with open(FOLDERS_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Filtrer _comment og andre meta-nøgler fra — sendes ikke til LLM
        return {k: v for k, v in data.items() if not k.startswith("_")}
    except FileNotFoundError:
        log.warning(f"folders.json ikke fundet: {FOLDERS_JSON_PATH} — bruger kun mappenavne")
        return {}
    except json.JSONDecodeError as e:
        log.warning(f"Ugyldig folders.json: {e} — bruger kun mappenavne")
        return {}


def _build_folder_list(folders: list[str]) -> str:
    """Bygger mappestreng med beskrivelser til indsættelse i system-prompt."""
    descriptions = load_folder_descriptions()
    lines = []
    for f in folders:
        desc = descriptions.get(f)
        if desc:
            lines.append(f"{f}: {desc}")
        else:
            lines.append(f)
    return " | ".join(lines) if lines else "none"


_DEFAULT_PROMPT_TEMPLATE = (
    'Analyze the emails in the JSON array and return ONLY a JSON array — one result object per email, in any order.\n'
    'Each result object must have exactly these fields:\n'
    '{"id": <same as input>, "needs_action": true/false, "priority": "high/medium/low", '
    '"summary": "brief description", "action": "what to do", "folder": "foldername", "deadline": "YYYY-MM-DD"}\n\n'
    'All fields must be present. Use null when a field is not applicable.\n\n'
    'Rules:\n'
    '- id: copy the id value from the input email\n'
    '- needs_action: true if the email requires action (reply, payment, pickup, confirmation, signature), otherwise false\n'
    '- priority: high=time-sensitive/has deadline, medium=normal, low=newsletter/automated/notification\n'
    '- folder: choose from the list below. Each entry is "FolderName: what belongs here". Return ONLY the folder name — not the description. Use null if the email does not clearly fit any folder or you are uncertain: {folder_list}\n'
    '- deadline: date in YYYY-MM-DD format if a deadline is mentioned in the email, otherwise null\n\n'
    'Return ONLY the JSON array, no other text.'
)


def build_system_prompt(folders: list[str]) -> str:
    """
    Bygger system-prompt med mappeliste indsat.
    Søger i prioriteret rækkefølge:
      1. /data/system_prompt.txt  — bruger-override i data-mount, ingen rebuild
      2. /app/prompts/system_prompt.txt  — bagt-ind standard i imagen
      3. Indbygget _DEFAULT_PROMPT_TEMPLATE som absolut fallback
    """
    folder_list = _build_folder_list(folders)

    for path in (_PROMPT_FILE_USER, _PROMPT_FILE_DEFAULT):
        try:
            with open(path, encoding="utf-8") as fh:
                template = fh.read()
            log.debug(f"System-prompt indlæst fra {path}")
            return template.replace("{folder_list}", folder_list)
        except FileNotFoundError:
            continue

    log.warning("Ingen system_prompt.txt fundet — bruger indbygget standard")
    return _DEFAULT_PROMPT_TEMPLATE.replace("{folder_list}", folder_list)


def build_mail_prompt(mail: dict) -> str:
    """Bygger bruger-prompt med mail-indhold og vedhæftede filer (single-mail format)."""
    attachment_lines = ""
    if mail.get("attachments"):
        attachment_lines = "\nVedhæftede filer:\n" + "\n".join(
            f"- {a}" for a in mail["attachments"]
        )
    return f"""Fra: {mail['sender']}
Emne: {mail['subject']}
Dato: {mail['received_at'][:10]}{attachment_lines}

Indhold:
{mail['body']}"""


def build_batch_prompt(mails: list[dict]) -> str:
    """Bygger JSON array prompt med id-felter til batch-klassificering."""
    items = []
    for i, mail in enumerate(mails, start=1):
        items.append({
            "id":          i,
            "from":        mail["sender"],
            "subject":     mail["subject"],
            "date":        mail["received_at"][:10],
            "attachments": mail.get("attachments", []),
            "body":        mail["body"],
        })
    return json.dumps(items, ensure_ascii=False)


def parse_llm_response(raw: str, subject: str) -> dict | None:
    """Parser LLM-svar til JSON — håndterer markdown code fences."""
    raw = raw.strip()
    if "```" in raw:
        for part in raw.split("```"):
            part = part.strip().lstrip("json").strip()
            try:
                return json.loads(part)
            except json.JSONDecodeError:
                continue
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning(f"Ugyldigt JSON fra LLM for '{subject[:50]}': {e}")
        return None


def parse_llm_batch_response(raw: str) -> list[dict] | None:
    """Parser LLM-svar til JSON array — håndterer markdown code fences."""
    raw = raw.strip()
    if "```" in raw:
        for part in raw.split("```"):
            part = part.strip().lstrip("json").strip()
            try:
                result = json.loads(part)
                if isinstance(result, list):
                    return result
            except json.JSONDecodeError:
                continue
    try:
        result = json.loads(raw)
        if isinstance(result, list):
            return result
        return None
    except json.JSONDecodeError as e:
        log.warning(f"Ugyldigt JSON array fra LLM: {e}")
        return None


def classify_ollama(prompt: str, system_prompt: str, max_tokens: int = 300) -> str:
    """Sender til lokal Ollama og returnerer rå tekst-svar."""
    response = requests.post(
        f"{OLLAMA_HOST}/api/generate",
        json={
            "model":  OLLAMA_MODEL,
            "prompt": prompt,
            "system": system_prompt,
            "stream": False,
            "options": {
                "temperature":  0.1,
                "num_predict":  max_tokens,
                "num_ctx":      OLLAMA_NUM_CTX,
                "num_thread":   OLLAMA_NUM_THREAD,
            },
        },
        timeout=LLM_TIMEOUT,
    )
    response.raise_for_status()
    return response.json().get("response", "")


def classify_anthropic(prompt: str, system_prompt: str, max_tokens: int = 300) -> str:
    """Sender til Anthropic Claude API og returnerer rå tekst-svar."""
    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key":         ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        },
        json={
            "model":      ANTHROPIC_MODEL,
            "max_tokens": max_tokens,
            "system":     system_prompt,
            "messages":   [{"role": "user", "content": prompt}],
        },
        timeout=LLM_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()["content"][0]["text"]


def classify_mistral(prompt: str, system_prompt: str, max_tokens: int = 300) -> str:
    """Sender til Mistral API og returnerer rå tekst-svar."""
    response = requests.post(
        "https://api.mistral.ai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {MISTRAL_API_KEY}",
            "Content-Type":  "application/json",
        },
        json={
            "model": MISTRAL_MODEL,
            "messages": [
                {"role": "system",  "content": system_prompt},
                {"role": "user",    "content": prompt},
            ],
            "max_tokens":  max_tokens,
            "temperature": 0.1,
        },
        timeout=LLM_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


def classify_claude_code(prompt: str, system_prompt: str, max_tokens: int = 300) -> str:
    """Sender mails til Claude Code container via delt volumen og HTTP-trigger.

    Protokol:
    1. Sletter evt. gammel response.json — forhindrer forældet svar.
    2. Skriver mails.json med unik request_id, system prompt og mail-data.
    3. POST /run/mail/ blokerer indtil run.sh er færdig (synkront i server.py).
    4. Læser response.json og verificerer request_id inden retur.
    """
    mails_file    = CLAUDE_CODE_DIR / "mails.json"
    response_file = CLAUDE_CODE_DIR / "response.json"

    request_id = str(uuid.uuid4())

    response_file.unlink(missing_ok=True)

    CLAUDE_CODE_DIR.mkdir(parents=True, exist_ok=True)
    with open(mails_file, "w", encoding="utf-8") as f:
        json.dump({
            "request_id": request_id,
            "system":     system_prompt,
            "prompt":     prompt,
        }, f, ensure_ascii=False)

    resp = requests.post(f"{CLAUDE_CODE_HOST}/run/mail/", timeout=CLAUDE_CODE_TIMEOUT)
    resp.raise_for_status()

    run_result = resp.json()
    if run_result.get("exit_code", 1) != 0:
        raise RuntimeError(f"Claude Code run.sh fejlede: {run_result.get('stderr', '')}")

    if not response_file.exists():
        raise FileNotFoundError("response.json ikke skrevet af run.sh")

    with open(response_file, encoding="utf-8") as f:
        data = json.load(f)

    if data.get("request_id") != request_id:
        raise ValueError("response.json request_id stemmer ikke overens — forældet svar forkastet")

    return data["response"]


def _call_llm(prompt: str, system_prompt: str, max_tokens: int = 300) -> str:
    """Ruter LLM-kald til konfigureret provider. Kaster ValueError ved manglende API-nøgle."""
    if LLM_PROVIDER == "anthropic":
        if not ANTHROPIC_API_KEY:
            raise ValueError("ANTHROPIC_API_KEY ikke sat i .env")
        return classify_anthropic(prompt, system_prompt, max_tokens)
    elif LLM_PROVIDER == "mistral":
        if not MISTRAL_API_KEY:
            raise ValueError("MISTRAL_API_KEY ikke sat i .env")
        return classify_mistral(prompt, system_prompt, max_tokens)
    elif LLM_PROVIDER == "claude-code":
        return classify_claude_code(prompt, system_prompt, max_tokens)
    else:
        return classify_ollama(prompt, system_prompt, max_tokens)


def classify_mail(mail: dict, system_prompt: str) -> dict | None:
    """
    Klassificerer én mail via konfigureret LLM provider (single-mail format).
    LLM_PROVIDER: "ollama" | "anthropic" | "mistral"
    Returnerer parsed JSON eller None ved fejl.
    """
    prompt = build_mail_prompt(mail)
    log.debug(f"LLM prompt for '{mail['subject'][:50]}':\n{prompt}")

    try:
        raw = _call_llm(prompt, system_prompt)
        log.debug(f"LLM svar for '{mail['subject'][:50]}':\n{raw}")
        return parse_llm_response(raw, mail["subject"])
    except (requests.RequestException, ValueError) as e:
        log.error(f"LLM ({LLM_PROVIDER}) ikke tilgængelig: {e}")
        return None


def _classify_single_fallback(mail: dict, system_prompt: str) -> dict | None:
    """Single-mail fallback ved batch-fejl: sender 1-elements array og forventer 1-elements array."""
    prompt = build_batch_prompt([mail])
    log.debug(f"Single-fallback prompt for '{mail['subject'][:50]}':\n{prompt}")

    try:
        raw = _call_llm(prompt, system_prompt, max_tokens=300)
        log.debug(f"Single-fallback svar for '{mail['subject'][:50]}':\n{raw}")
    except (requests.RequestException, ValueError) as e:
        log.error(f"LLM fejlede for '{mail['subject'][:50]}': {e}")
        return None

    result = parse_llm_batch_response(raw)
    if result and isinstance(result[0], dict):
        return result[0]

    log.warning(f"Single-fallback svar ugyldigt for '{mail['subject'][:50]}'")
    return None


def classify_mail_batch(mails: list[dict], system_prompt: str) -> list[dict | None]:
    """
    Klassificerer en batch af mails via konfigureret LLM provider.
    Sender JSON array og forventer JSON array retur med id-felter.
    Falder tilbage til single-kald per mail hvis svar ikke er gyldigt JSON array.
    Returnerer liste med samme længde som mails — None for fejlede mails.
    """
    results: list[dict | None] = [None] * len(mails)
    prompt = build_batch_prompt(mails)
    log.debug(f"Batch LLM prompt ({len(mails)} mails):\n{prompt}")

    try:
        raw = _call_llm(prompt, system_prompt, max_tokens=300 * len(mails))
        log.debug(f"Batch LLM svar ({len(mails)} mails):\n{raw}")
    except (requests.RequestException, ValueError) as e:
        log.error(f"LLM ({LLM_PROVIDER}) ikke tilgængelig: {e}")
        return results

    batch_result = parse_llm_batch_response(raw)

    if batch_result is None:
        log.warning(
            f"Batch svar ikke gyldigt JSON array — falder tilbage til single-kald for {len(mails)} mails"
        )
        for i, mail in enumerate(mails):
            results[i] = _classify_single_fallback(mail, system_prompt)
        return results

    id_map = {item.get("id"): item for item in batch_result if isinstance(item, dict)}
    log.debug(f"Batch svar indeholder IDs: {sorted(id_map.keys())}")

    for i in range(len(mails)):
        mail_id = i + 1
        if mail_id in id_map:
            results[i] = id_map[mail_id]
        else:
            log.warning(
                f"ID {mail_id} mangler i batch-svar — "
                f"'{mails[i]['subject'][:50]}' behandles som fejl"
            )

    return results


def resolve_due_date(classification: dict, received_at: str) -> str:
    """
    Finder due date til Google Task.
    1. Brug LLM-fundet deadline hvis gyldig fremtidig dato
    2. Fallback: modtagelsesdato + DEADLINE_FALLBACK_DAYS (standard = i dag)
    """
    deadline_str = classification.get("deadline")

    if deadline_str:
        try:
            dt = datetime.strptime(deadline_str, "%Y-%m-%d")
            # Acceptér kun fremtidige datoer (eller i dag)
            if dt.date() >= datetime.now(timezone.utc).date():
                log.info(f"Deadline fra mail-indhold: {deadline_str}")
                return deadline_str
        except ValueError:
            pass

    # Fallback: brug i dag + DEADLINE_FALLBACK_DAYS
    fallback = (
        datetime.now(timezone.utc) + timedelta(days=DEADLINE_FALLBACK_DAYS)
    ).strftime("%Y-%m-%d")
    log.info(f"Fallback deadline: {fallback}")
    return fallback


# ── Google Tasks ───────────────────────────────────────────────────────────────
def get_google_tasks_service():
    creds = None

    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH, "rb") as f:
            creds = pickle.load(f)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_PATH, "wb") as f:
            pickle.dump(creds, f)

    if not creds or not creds.valid:
        log.error(
            "Google token mangler. Kør: "
            "docker run --rm -it -v ./vol/data:/data "
            "mail-assistant python assistant.py --auth"
        )
        sys.exit(1)

    return build("tasks", "v1", credentials=creds)


def run_google_auth():
    """Engangs OAuth2 flow — køres manuelt én gang."""
    if not os.path.exists(CREDS_PATH):
        log.error(f"google_credentials.json ikke fundet: {CREDS_PATH}")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(CREDS_PATH, SCOPES)

    # Generer auth URL manuelt — åbn linket i en browser, godkend,
    # og indsæt den returnerede URL eller kode herunder
    flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
    auth_url, _ = flow.authorization_url(prompt="consent")

    print("\n" + "="*60)
    print("Åbn dette link i din browser:")
    print(auth_url)
    print("="*60)
    print("Efter godkendelse får du en kode.")
    code = input("Indsæt koden her: ").strip()

    flow.fetch_token(code=code)
    creds = flow.credentials

    os.makedirs("/data", exist_ok=True)
    with open(TOKEN_PATH, "wb") as f:
        pickle.dump(creds, f)

    log.info(f"Google token gemt: {TOKEN_PATH}")


def create_task(service, mail: dict, classification: dict, due_date: str) -> str | None:
    """Opretter Google Task med due date."""
    priority_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(
        classification.get("priority", "low"), "⚪"
    )

    title = f"{priority_emoji} {mail['subject'][:80]}"

    notes_lines = [
        f"Fra: {mail['sender']}",
        f"Modtaget: {mail['received_at'][:10]}",
        f"Mappe: {classification.get('folder') or 'INBOX (ikke sorteret)'}",
        "",
        f"Resumé: {classification.get('summary', '')}",
    ]
    if classification.get("action"):
        notes_lines.append(f"Handling: {classification['action']}")
    if classification.get("deadline"):
        notes_lines.append(f"Frist i mail: {classification['deadline']}")
    if mail.get("attachments"):
        notes_lines.append(f"Vedhæftede filer: {', '.join(mail['attachments'])}")

    # Google Tasks due format: RFC 3339, tid skal være 00:00:00Z
    due_rfc3339 = f"{due_date}T00:00:00.000Z"

    try:
        task = service.tasks().insert(
            tasklist=GOOGLE_TASKLIST,
            body={
                "title":  title,
                "notes":  "\n".join(notes_lines),
                "status": "needsAction",
                "due":    due_rfc3339,
            }
        ).execute()

        log.info(f"Task oprettet: {title} (due: {due_date})")
        return task["id"]

    except Exception as e:
        log.error(f"Fejl ved oprettelse af Google Task: {e}")
        return None


def sync_completed_tasks(service, conn):
    """Markerer afsluttede Google Tasks i lokal DB."""
    try:
        result = service.tasks().list(
            tasklist=GOOGLE_TASKLIST,
            showCompleted=True,
            showHidden=True,
            maxResults=100,
        ).execute()

        completed_ids = [
            t["id"] for t in result.get("items", [])
            if t.get("status") == "completed"
        ]

        if completed_ids:
            placeholders = ",".join("?" * len(completed_ids))
            conn.execute(
                f"UPDATE tasks SET completed = 1 WHERE task_id IN ({placeholders})",
                completed_ids
            )
            conn.commit()
            log.info(f"Synkroniserede {len(completed_ids)} afsluttede tasks")

    except Exception as e:
        log.warning(f"Kunne ikke synkronisere tasks: {e}")


def sync_task_completion(service, conn, imap: IMAPClient):
    """
    Auto-markerer Google Tasks som completed baseret på IMAP mail-status.
    COMPLETE_ON_ANSWERED: task completed hvis mailen har \\Answered flag (besvaret).
    COMPLETE_ON_ARCHIVED: task completed hvis mail er i arkiv og ikke i INBOX.
    Kontrollerer kun tasks der ikke allerede er markeret completed i DB.
    """
    if not COMPLETE_ON_ANSWERED and not COMPLETE_ON_ARCHIVED:
        return

    rows = conn.execute(
        "SELECT task_id, message_id, subject FROM tasks WHERE completed = 0 AND message_id IS NOT NULL"
    ).fetchall()

    if not rows:
        return

    log.info(f"Tjekker auto-completion for {len(rows)} åbne tasks...")
    completed_count = 0

    for task_id, message_id, subject in rows:
        try:
            status = imap.check_mail_status(message_id)
        except Exception as e:
            log.warning(f"check_mail_status fejl for '{subject[:40]}': {e}")
            continue

        reason = None

        if COMPLETE_ON_ANSWERED and status["answered"]:
            reason = "mail besvaret (\\Answered)"
        elif COMPLETE_ON_ARCHIVED and status["in_archive"] and not status["in_inbox"]:
            reason = f"mail arkiveret i {IMAP_ARCHIVE_FOLDER}"

        if reason:
            try:
                service.tasks().patch(
                    tasklist=GOOGLE_TASKLIST,
                    task=task_id,
                    body={"status": "completed"}
                ).execute()
                conn.execute("UPDATE tasks SET completed = 1 WHERE task_id = ?", (task_id,))
                conn.commit()
                log.info(f"Task auto-completed: '{subject[:50]}' — {reason}")
                completed_count += 1
            except Exception as e:
                log.warning(f"Kunne ikke markere task completed for '{subject[:40]}': {e}")

    if completed_count:
        log.info(f"Auto-completed {completed_count} tasks")


# ── Tråd-gruppering ────────────────────────────────────────────────────────────
def _resolve_thread_id(mail: dict, conn, batch_thread_map: dict[str, str]) -> str:
    """
    Finder kanonisk thread_id for en mail via In-Reply-To og References headere.
    Slår op i batch-intern map og DB. Returnerer root Message-ID for tråden.
    """
    candidates: set[str] = set()

    for part in mail.get("in_reply_to", "").split():
        part = part.strip()
        if part:
            candidates.add(part)

    for part in mail.get("references", "").split():
        part = part.strip()
        if part:
            candidates.add(part)

    if not candidates:
        return mail["message_id"]

    candidate_list = list(candidates)

    # Tjek batch-intern map (mails allerede behandlet i denne kørsel)
    for cand in candidate_list:
        if cand in batch_thread_map:
            return batch_thread_map[cand]

    # Tjek DB for kendte mails med thread_id
    placeholders = ",".join("?" * len(candidate_list))
    row = conn.execute(
        f"SELECT thread_id FROM seen_mails WHERE message_id IN ({placeholders}) AND thread_id IS NOT NULL LIMIT 1",
        candidate_list,
    ).fetchone()
    if row and row[0]:
        return row[0]

    # Fallback: brug forælders message_id som thread_id (ældre rækker uden thread_id)
    row = conn.execute(
        f"SELECT message_id FROM seen_mails WHERE message_id IN ({placeholders}) LIMIT 1",
        candidate_list,
    ).fetchone()
    if row:
        return row[0]

    return mail["message_id"]


def group_mails_by_thread(mails: list[dict], conn) -> tuple[list[dict], list[dict]]:
    """
    Grupperer mails efter e-mail-tråd (pre-AI).
    Per tråd sendes kun den nyeste mail til AI; ældre i samme batch markeres som set.
    Returnerer (to_process, to_skip).
    """
    batch_thread_map: dict[str, str] = {}

    for mail in mails:
        tid = _resolve_thread_id(mail, conn, batch_thread_map)
        mail["thread_id"] = tid
        batch_thread_map[mail["message_id"]] = tid

    thread_groups: dict[str, list[dict]] = {}
    for mail in mails:
        thread_groups.setdefault(mail["thread_id"], []).append(mail)

    to_process: list[dict] = []
    to_skip: list[dict] = []

    for thread_id, group in thread_groups.items():
        if len(group) == 1:
            to_process.append(group[0])
        else:
            group.sort(key=lambda m: m["received_at"])
            to_process.append(group[-1])
            to_skip.extend(group[:-1])
            log.info(
                f"Tråd '{thread_id[:50]}': {len(group)} mails — "
                f"behandler nyeste, springer {len(group) - 1} over"
            )

    return to_process, to_skip


def find_open_thread_task(conn, thread_id: str) -> tuple[str, str] | None:
    """Returnerer (task_id, subject) for åben task i tråden, eller None."""
    row = conn.execute("""
        SELECT t.task_id, t.subject
        FROM tasks t
        JOIN seen_mails sm ON sm.message_id = t.message_id
        WHERE sm.thread_id = ? AND t.completed = 0
        LIMIT 1
    """, (thread_id,)).fetchone()
    return (row[0], row[1]) if row else None


def update_task(service, task_id: str, mail: dict, classification: dict, due_date: str) -> bool:
    """Opdaterer eksisterende Google Task med ny mail fra samme tråd."""
    try:
        existing = service.tasks().get(
            tasklist=GOOGLE_TASKLIST,
            task=task_id,
        ).execute()
    except Exception as e:
        log.warning(f"Kunne ikke hente task {task_id[:20]} til opdatering: {e}")
        return False

    new_section_lines = [
        "",
        "---",
        f"Fra: {mail['sender']}",
        f"Modtaget: {mail['received_at'][:10]}",
        f"Resumé: {classification.get('summary', '')}",
    ]
    if classification.get("action"):
        new_section_lines.append(f"Handling: {classification['action']}")

    current_notes = existing.get("notes", "")
    updated_notes = current_notes + "\n".join(new_section_lines)

    # Opgrader prioritet i titlen hvis ny mail har højere prioritet
    priority_order = {"high": 3, "medium": 2, "low": 1}
    new_priority   = classification.get("priority", "low")
    emoji_map      = {"high": "🔴", "medium": "🟡", "low": "🟢"}
    emoji_to_pri   = {"🔴": "high", "🟡": "medium", "🟢": "low"}

    current_title    = existing.get("title", "")
    current_emoji    = current_title[:2] if len(current_title) >= 2 else ""
    current_priority = emoji_to_pri.get(current_emoji, "low")
    updated_title    = current_title

    if priority_order.get(new_priority, 0) > priority_order.get(current_priority, 0):
        new_emoji = emoji_map.get(new_priority, "⚪")
        if current_emoji in emoji_to_pri:
            updated_title = new_emoji + current_title[2:]
        else:
            updated_title = f"{new_emoji} {current_title}"

    # Behold den tidligste frist
    current_due_str = (existing.get("due") or "")[:10]
    if current_due_str and current_due_str < due_date:
        updated_due = current_due_str
    else:
        updated_due = due_date

    patch_body: dict = {"notes": updated_notes}
    if updated_title != current_title:
        patch_body["title"] = updated_title
    if updated_due != current_due_str:
        patch_body["due"] = f"{updated_due}T00:00:00.000Z"

    try:
        service.tasks().patch(
            tasklist=GOOGLE_TASKLIST,
            task=task_id,
            body=patch_body,
        ).execute()
        log.info(f"Tråd-task opdateret: {updated_title[:60]}")
        return True
    except Exception as e:
        log.error(f"Fejl ved opdatering af task {task_id[:20]}: {e}")
        return False


def execute_pending_moves(conn, imap: IMAPClient):
    """
    Udfører udskudte mailflytninger når handlingen er fuldført:
    Google Task markeret completed ELLER mailen er besvaret (\\Answered).
    """
    rows = conn.execute("""
        SELECT sm.message_id, sm.pending_folder, t.completed
        FROM seen_mails sm
        LEFT JOIN tasks t ON t.task_id = sm.task_id
        WHERE sm.pending_folder IS NOT NULL AND sm.moved_to IS NULL
    """).fetchall()

    if not rows:
        return

    log.info(f"Tjekker {len(rows)} mails med afventende flytning...")
    moved_count = 0

    for message_id, pending_folder, completed in rows:
        should_move = (completed == 1)

        if not should_move:
            try:
                status = imap.check_mail_status(message_id)
                should_move = status["answered"]
            except Exception as e:
                log.warning(f"Statustjek fejl for afventende mail '{message_id[:40]}': {e}")
                continue

        if not should_move:
            continue

        uid = imap.find_uid_by_message_id(message_id)
        if uid is None:
            log.debug(f"Mail ikke fundet i INBOX (allerede flyttet?): {message_id[:40]}")
            continue

        success = imap.move_mail(uid, pending_folder)
        if success:
            conn.execute(
                "UPDATE seen_mails SET moved_to = ?, pending_folder = NULL WHERE message_id = ?",
                (pending_folder, message_id),
            )
            conn.commit()
            moved_count += 1

    if moved_count:
        log.info(f"Udskudte flytninger udført: {moved_count} mails")


# ── Hoved-pipeline ─────────────────────────────────────────────────────────────
def run():
    # Tjek om en instans allerede kører
    if is_already_running():
        log.warning("En instans kører allerede (PID-fil aktiv) — afslutter.")
        sys.exit(0)

    write_pid()
    log.info(f"=== mail-assistant kørsel starter (PID: {os.getpid()}, provider: {LLM_PROVIDER}) ===")

    try:
        _run()
    finally:
        # Slet PID-fil uanset om kørsel lykkedes eller fejlede
        remove_pid()


def _run():
    """Intern hoved-pipeline — kaldes af run() efter PID-tjek."""
    conn    = init_db()
    service = get_google_tasks_service()

    # Synkroniser afsluttede tasks fra Google Tasks → lokal DB (sæt completed=1)
    sync_completed_tasks(service, conn)

    # Find startdato for scanning
    since_date = get_scan_start_date(conn)
    log.info(f"Scanner mails siden: {since_date}")

    # IMAP — hent mappeliste ved opstart og byg system-prompt
    imap = IMAPClient()
    imap.connect()

    folders       = imap.fetch_folders()
    system_prompt = build_system_prompt(folders)

    try:
        # Auto-complete tasks baseret på IMAP-status (answered/arkiveret)
        sync_task_completion(service, conn, imap)

        # Udfør udskudte mailflytninger for færdiggjorte tasks/besvarede mails
        execute_pending_moves(conn, imap)

        mails = imap.fetch_new_mails(conn, since_date)

        # Grupper mails efter e-mail-tråd (pre-AI, baseret på In-Reply-To/References)
        mails_to_process, mails_to_skip = group_mails_by_thread(mails, conn)

        # Marker tråd-duplikater som set uden handling
        for mail in mails_to_skip:
            mark_seen(conn, mail["message_id"], mail["subject"],
                      mail["sender"], mail["received_at"],
                      None, False, None,
                      thread_id=mail.get("thread_id"))

        # Indlæs aktive filterregler én gang per kørsel
        filter_rules = load_filter_rules(conn)
        if filter_rules:
            log.info(f"Indlæste {len(filter_rules)} aktive filterregler")

        # Fordel mails på filterregler: no_llm_mails springes direkte over, llm_mails sendes til LLM
        rule_overrides: dict[str, dict] = {}
        llm_mails:    list[dict] = []
        no_llm_mails: list[dict] = []
        for mail in mails_to_process:
            override = apply_filter_rules(filter_rules, mail)
            rule_overrides[mail["message_id"]] = override
            (no_llm_mails if override["skip_llm"] else llm_mails).append(mail)

        created = updated = skipped = errors = moved = flagged = 0

        # Behandl mails der springer LLM over — syntetisk klassificering fra regelens handlinger
        for mail in no_llm_mails:
            override = rule_overrides[mail["message_id"]]
            thread_id = mail.get("thread_id", mail["message_id"])
            folder = (override["move_to"] or "").strip()
            moved_to = None
            if folder:
                folder_match = next((f for f in folders if f.lower() == folder.lower()), None)
                if folder_match:
                    if imap.move_mail(mail["uid"], folder_match):
                        moved_to = folder_match
                        moved += 1
                else:
                    log.warning(f"Filterregel mappe '{folder}' ikke fundet — mail beholder i INBOX")
            mark_seen(conn, mail["message_id"], mail["subject"],
                      mail["sender"], mail["received_at"],
                      None, False, moved_to, thread_id=thread_id)

        total_batches = (
            (len(llm_mails) + LLM_BATCH_SIZE - 1) // LLM_BATCH_SIZE
            if llm_mails else 0
        )

        for batch_start in range(0, len(llm_mails), LLM_BATCH_SIZE):
            batch = llm_mails[batch_start:batch_start + LLM_BATCH_SIZE]
            batch_num = batch_start // LLM_BATCH_SIZE + 1
            log.info(f"Klassificerer batch {batch_num}/{total_batches} ({len(batch)} mails)")

            classifications = classify_mail_batch(batch, system_prompt)

            for mail, classification in zip(batch, classifications):
                log.info(f"Behandler: {mail['subject'][:60]}")
                thread_id = mail.get("thread_id", mail["message_id"])
                override = rule_overrides[mail["message_id"]]

                if classification is None:
                    errors += 1
                    mark_seen(conn, mail["message_id"], mail["subject"],
                              mail["sender"], mail["received_at"],
                              None, False, None,
                              thread_id=thread_id)
                    continue

                # Anvend regelens mappeoverride på LLM-klassificeringen
                if override["move_to"]:
                    classification["folder"] = override["move_to"]
                elif override["skip_llm_move"]:
                    classification["folder"] = None

                task_id        = None
                moved_to       = None
                pending_folder = None
                needs_action   = classification.get("needs_action", False)

                # Regl kan forhindre task-oprettelse
                if override["skip_task"]:
                    needs_action = False

                # ── Flag mail hvis priority matcher FLAG_ON_PRIORITY ───────────
                if FLAG_ON_PRIORITY and classification.get("priority", "").lower() in FLAG_ON_PRIORITY:
                    if imap.set_flagged(mail["uid"]):
                        flagged += 1

                # ── Flyt mail — straks hvis ingen handling, ellers udskyd ──────
                folder = (classification.get("folder") or "").strip()
                if folder:
                    folder_match = next(
                        (f for f in folders if f.lower() == folder.lower()), None
                    )
                    if folder_match:
                        if needs_action:
                            pending_folder = folder_match
                            log.info(f"Flytning til '{folder_match}' afventer handling")
                        else:
                            success = imap.move_mail(mail["uid"], folder_match)
                            if success:
                                moved_to = folder_match
                                moved += 1
                    else:
                        log.warning(f"Mappe '{folder}' findes ikke på server — mail beholder i INBOX")

                # ── Opret eller opdater Google Task ────────────────────────────
                if needs_action:
                    due_date = resolve_due_date(classification, mail["received_at"])

                    existing = find_open_thread_task(conn, thread_id)
                    if existing:
                        task_id, _ = existing
                        update_task(service, task_id, mail, classification, due_date)
                        conn.execute(
                            "UPDATE tasks SET thread_id = ? WHERE task_id = ?",
                            (thread_id, task_id),
                        )
                        conn.commit()
                        updated += 1
                    else:
                        task_id = create_task(service, mail, classification, due_date)
                        if task_id:
                            conn.execute("""
                                INSERT OR REPLACE INTO tasks
                                    (task_id, message_id, subject, due_date, created_at, thread_id)
                                VALUES (?, ?, ?, ?, ?, ?)
                            """, (
                                task_id, mail["message_id"], mail["subject"],
                                due_date, datetime.now(timezone.utc).isoformat(), thread_id,
                            ))
                            conn.commit()
                            created += 1
                        else:
                            errors += 1
                else:
                    skipped += 1

                mark_seen(conn, mail["message_id"], mail["subject"],
                          mail["sender"], mail["received_at"],
                          task_id, needs_action, moved_to,
                          thread_id=thread_id, pending_folder=pending_folder)

    finally:
        imap.disconnect()

    log.info(
        f"=== Færdig: {created} tasks oprettet, {updated} opdateret, "
        f"{moved} mails flyttet, {flagged} flagget, "
        f"{skipped} ingen handling, {len(mails_to_skip)} tråd-duplikater, {errors} fejl ==="
    )
    conn.close()
    # PID-fil slettes af run() i finally-blokken


# ── --getfolders ───────────────────────────────────────────────────────────────
def run_get_folders():
    """
    Henter alle IMAP-mapper og opdaterer folders.json in-place.
    Nye mapper tilføjes med tom beskrivelse; eksisterende beskrivelser bevares.
    Kør: docker exec mail-assistant python /app/assistant.py --getfolders
    """
    log.info("Henter mappeliste fra IMAP...")

    imap = IMAPClient()
    imap.connect()
    imap_folders = imap.fetch_folders()
    imap.disconnect()

    existing = load_folder_descriptions()

    merged = dict(existing)
    added = 0
    for folder in imap_folders:
        if folder not in merged:
            merged[folder] = ""
            added += 1

    os.makedirs("/data", exist_ok=True)
    with open(FOLDERS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    log.info(
        f"folders.json opdateret: {added} nye mapper tilføjet, "
        f"{len(existing)} eksisterende beskrivelser bevaret"
    )

    print("\nFundne IMAP-mapper:")
    for folder in sorted(imap_folders):
        marker = " (ny)" if folder not in existing else ""
        print(f"  {folder}{marker}")
    print(f"\nGemt til: {FOLDERS_JSON_PATH}")


# ── Entrypoint ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--auth":
        run_google_auth()
    elif len(sys.argv) > 1 and sys.argv[1] == "--getfolders":
        run_get_folders()
    else:
        run()
