# Copyright (C) 2026 Mathew Levett
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import asyncio
import base64
import binascii
import csv
import hashlib
import html
import io
import json
import os
import secrets
import shutil
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("RAYNET_DATA_DIR", BASE_DIR / "data"))
BACKUP_DIR = Path(os.getenv("RAYNET_BACKUP_DIR", BASE_DIR / "backups"))
DB_PATH = Path(os.getenv("RAYNET_DB_PATH", DATA_DIR / "raynet_logger.db"))
ORG_NAME = os.getenv("RAYNET_ORG_NAME", "Message Logger")
DEFAULT_TAGLINE = "South East Hampshire Raynet"
DEFAULT_LOGO_URL = "/static/default-brand-logo.png"
LOCAL_TIMEZONE = os.getenv("RAYNET_TIMEZONE", "Europe/London")
SESSION_HOURS = int(os.getenv("RAYNET_SESSION_HOURS", "12"))
BACKUP_INTERVAL_SECONDS = int(os.getenv("RAYNET_BACKUP_INTERVAL_SECONDS", "300"))
BACKUP_RETENTION = int(os.getenv("RAYNET_BACKUP_RETENTION", "48"))
COOKIE_NAME = "raynet_session"

DATA_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utcnow().isoformat(timespec="seconds")


def normalize_callsign(value: str) -> str:
    return " ".join(value.strip().upper().split())


def get_branding(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT key,value FROM settings WHERE key IN ('org_name','tagline','logo_data_url')").fetchall()
    values = {row["key"]: row["value"] for row in rows}
    return {
        "org_name": values.get("org_name", ORG_NAME),
        "tagline": values.get("tagline", DEFAULT_TAGLINE),
        "logo_data_url": values.get("logo_data_url") or DEFAULT_LOGO_URL,
    }


@contextmanager
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    schema = """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        display_name TEXT NOT NULL,
        callsign TEXT NOT NULL DEFAULT '',
        password_salt BLOB NOT NULL,
        password_hash BLOB NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('admin','controller','operator','viewer')),
        active INTEGER NOT NULL DEFAULT 1,
        in_control INTEGER NOT NULL DEFAULT 0,
        control_call TEXT NOT NULL DEFAULT 'CONTROL',
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        token_hash TEXT NOT NULL UNIQUE,
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        location TEXT NOT NULL DEFAULT '',
        control_callsign TEXT NOT NULL DEFAULT '',
        event_notes TEXT NOT NULL DEFAULT '',
        location_details TEXT NOT NULL DEFAULT '',
        phone_numbers TEXT NOT NULL DEFAULT '',
        event_contacts TEXT NOT NULL DEFAULT '',
        radio_frequency TEXT NOT NULL DEFAULT '',
        ctcss_tones TEXT NOT NULL DEFAULT '',
        radio_mode TEXT NOT NULL DEFAULT '',
        talk_groups TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','closed')),
        created_at TEXT NOT NULL,
        started_at TEXT NOT NULL,
        closed_at TEXT,
        created_by INTEGER NOT NULL REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS stations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        name TEXT NOT NULL DEFAULT '',
        callsign TEXT NOT NULL,
        tactical_call TEXT NOT NULL DEFAULT '',
        check_interval_minutes INTEGER NOT NULL DEFAULT 30 CHECK(check_interval_minutes BETWEEN 1 AND 1440),
        on_duty INTEGER NOT NULL DEFAULT 1,
        duty_status TEXT NOT NULL DEFAULT 'on_duty' CHECK(duty_status IN ('on_duty','off_duty','stood_down')),
        in_control INTEGER NOT NULL DEFAULT 0,
        last_heard_at TEXT,
        notes TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(event_id, callsign COLLATE NOCASE)
    );

    CREATE TABLE IF NOT EXISTS event_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        tactical_call TEXT NOT NULL DEFAULT '',
        in_control INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(event_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS control_callsigns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        callsign TEXT NOT NULL UNIQUE COLLATE NOCASE,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        sequence INTEGER NOT NULL,
        dtg TEXT NOT NULL,
        callsign TEXT NOT NULL,
        station_id INTEGER REFERENCES stations(id) ON DELETE SET NULL,
        station_callsign TEXT NOT NULL DEFAULT '',
        tactical_call TEXT NOT NULL DEFAULT '',
        address_mode TEXT NOT NULL DEFAULT 'callsign' CHECK(address_mode IN ('callsign','tactical','both')),
        message TEXT NOT NULL,
        direction TEXT NOT NULL CHECK(direction IN ('received','sent','info')),
        priority TEXT NOT NULL DEFAULT 'routine' CHECK(priority IN ('routine','priority','immediate')),
        operator_id INTEGER NOT NULL REFERENCES users(id),
        operator_control_call TEXT NOT NULL DEFAULT '',
        version INTEGER NOT NULL DEFAULT 1,
        voided INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(event_id, sequence)
    );

    CREATE TABLE IF NOT EXISTS message_revisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
        version INTEGER NOT NULL,
        previous_json TEXT NOT NULL,
        reason TEXT NOT NULL,
        changed_by INTEGER NOT NULL REFERENCES users(id),
        changed_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id INTEGER REFERENCES events(id) ON DELETE CASCADE,
        user_id INTEGER REFERENCES users(id),
        action TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        entity_id INTEGER,
        before_json TEXT,
        after_json TEXT,
        created_at TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_messages_event_sequence ON messages(event_id, sequence DESC);
    CREATE INDEX IF NOT EXISTS idx_stations_event ON stations(event_id);
    CREATE INDEX IF NOT EXISTS idx_event_users_event ON event_users(event_id);
    CREATE INDEX IF NOT EXISTS idx_audit_event_created ON audit_log(event_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_sessions_hash ON sessions(token_hash);
    """
    with db() as conn:
        conn.executescript(schema)
        conn.execute("INSERT OR IGNORE INTO control_callsigns(callsign,created_at) VALUES('CONTROL',?)", (iso_now(),))
        conn.execute("INSERT OR IGNORE INTO control_callsigns(callsign,created_at) VALUES('RAYNET CONTROL',?)", (iso_now(),))
        user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "in_control" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN in_control INTEGER NOT NULL DEFAULT 0")
        if "control_call" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN control_call TEXT NOT NULL DEFAULT 'CONTROL'")
        event_columns = {row["name"] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
        for column in ("event_notes", "location_details", "phone_numbers", "event_contacts", "radio_frequency", "ctcss_tones", "radio_mode", "talk_groups"):
            if column not in event_columns:
                conn.execute(f"ALTER TABLE events ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        station_columns = {row["name"] for row in conn.execute("PRAGMA table_info(stations)").fetchall()}
        if "name" not in station_columns:
            conn.execute("ALTER TABLE stations ADD COLUMN name TEXT NOT NULL DEFAULT ''")
        if "in_control" not in station_columns:
            conn.execute("ALTER TABLE stations ADD COLUMN in_control INTEGER NOT NULL DEFAULT 0")
        if "duty_status" not in station_columns:
            conn.execute("ALTER TABLE stations ADD COLUMN duty_status TEXT NOT NULL DEFAULT 'on_duty'")
            conn.execute("UPDATE stations SET duty_status=CASE WHEN on_duty=1 THEN 'on_duty' ELSE 'off_duty' END")
        message_columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
        migrations = {
            "station_id": "ALTER TABLE messages ADD COLUMN station_id INTEGER REFERENCES stations(id) ON DELETE SET NULL",
            "station_callsign": "ALTER TABLE messages ADD COLUMN station_callsign TEXT NOT NULL DEFAULT ''",
            "tactical_call": "ALTER TABLE messages ADD COLUMN tactical_call TEXT NOT NULL DEFAULT ''",
            "address_mode": "ALTER TABLE messages ADD COLUMN address_mode TEXT NOT NULL DEFAULT 'callsign'",
            "operator_control_call": "ALTER TABLE messages ADD COLUMN operator_control_call TEXT NOT NULL DEFAULT ''",
        }
        for column, statement in migrations.items():
            if column not in message_columns:
                conn.execute(statement)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_station ON messages(station_id)")
        # Preserve old logs while enriching them with the best matching operator identity.
        conn.execute(
            """UPDATE messages
               SET station_id=(SELECT s.id FROM stations s WHERE s.event_id=messages.event_id AND
                                  (UPPER(s.callsign)=UPPER(messages.callsign) OR
                                   (s.tactical_call<>'' AND UPPER(s.tactical_call)=UPPER(messages.callsign))) LIMIT 1)
               WHERE station_id IS NULL"""
        )
        conn.execute(
            """UPDATE messages SET
                 station_callsign=COALESCE((SELECT s.callsign FROM stations s WHERE s.id=messages.station_id), callsign),
                 tactical_call=COALESCE((SELECT s.tactical_call FROM stations s WHERE s.id=messages.station_id), '')
               WHERE station_callsign=''"""
        )
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (iso_now(),))


def hash_password(password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return salt, digest


def verify_password(password: str, salt: bytes, expected: bytes) -> bool:
    _, actual = hash_password(password, salt)
    return secrets.compare_digest(actual, expected)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def rowdict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def public_user(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    return {key: data[key] for key in ("id", "username", "display_name", "callsign", "role", "active", "created_at") if key in data}


def audit(
    conn: sqlite3.Connection,
    *,
    event_id: int | None,
    user_id: int | None,
    action: str,
    entity_type: str,
    entity_id: int | None,
    before: Any = None,
    after: Any = None,
) -> None:
    conn.execute(
        """INSERT INTO audit_log(event_id,user_id,action,entity_type,entity_id,before_json,after_json,created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (
            event_id,
            user_id,
            action,
            entity_type,
            entity_id,
            json.dumps(before, separators=(",", ":"), default=str) if before is not None else None,
            json.dumps(after, separators=(",", ":"), default=str) if after is not None else None,
            iso_now(),
        ),
    )


class SetupIn(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    display_name: str = Field(min_length=1, max_length=100)
    callsign: str = Field(default="", max_length=30)
    password: str = Field(min_length=10, max_length=200)


class LoginIn(BaseModel):
    username: str
    password: str


class BrandingIn(BaseModel):
    org_name: str = Field(min_length=1, max_length=100)
    tagline: str = Field(default="", max_length=120)
    logo_data_url: str = Field(default="", max_length=1_500_000)


class UserIn(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    display_name: str = Field(min_length=1, max_length=100)
    callsign: str = Field(default="", max_length=30)
    password: str = Field(min_length=10, max_length=200)
    role: Literal["admin", "controller", "operator", "viewer"] = "operator"


class UserPatch(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    callsign: str | None = Field(default=None, max_length=30)
    password: str | None = Field(default=None, min_length=10, max_length=200)
    role: Literal["admin", "controller", "operator", "viewer"] | None = None
    active: bool | None = None


class EventIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    location: str = Field(default="", max_length=200)
    control_callsign: str = Field(default="", max_length=30)
    event_notes: str = Field(default="", max_length=4000)
    location_details: str = Field(default="", max_length=1000)
    phone_numbers: str = Field(default="", max_length=1000)
    event_contacts: str = Field(default="", max_length=2000)
    radio_frequency: str = Field(default="", max_length=200)
    ctcss_tones: str = Field(default="", max_length=200)
    radio_mode: str = Field(default="", max_length=100)
    talk_groups: str = Field(default="", max_length=1000)


class EventPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    location: str | None = Field(default=None, max_length=200)
    control_callsign: str | None = Field(default=None, max_length=30)
    event_notes: str | None = Field(default=None, max_length=4000)
    location_details: str | None = Field(default=None, max_length=1000)
    phone_numbers: str | None = Field(default=None, max_length=1000)
    event_contacts: str | None = Field(default=None, max_length=2000)
    radio_frequency: str | None = Field(default=None, max_length=200)
    ctcss_tones: str | None = Field(default=None, max_length=200)
    radio_mode: str | None = Field(default=None, max_length=100)
    talk_groups: str | None = Field(default=None, max_length=1000)
    status: Literal["active", "closed"] | None = None


class StationIn(BaseModel):
    name: str = Field(default="", max_length=100)
    callsign: str = Field(min_length=1, max_length=30)
    tactical_call: str = Field(default="", max_length=50)
    check_interval_minutes: int = Field(default=30, ge=1, le=1440)
    on_duty: bool = True
    duty_status: Literal["on_duty", "off_duty", "stood_down"] = "on_duty"
    in_control: bool = False
    notes: str = Field(default="", max_length=500)

    @field_validator("callsign", "tactical_call")
    @classmethod
    def upper_calls(cls, value: str) -> str:
        return normalize_callsign(value)


class StationPatch(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    callsign: str | None = Field(default=None, min_length=1, max_length=30)
    tactical_call: str | None = Field(default=None, max_length=50)
    check_interval_minutes: int | None = Field(default=None, ge=1, le=1440)
    on_duty: bool | None = None
    duty_status: Literal["on_duty", "off_duty", "stood_down"] | None = None
    in_control: bool | None = None
    last_heard_at: str | None = None
    notes: str | None = Field(default=None, max_length=500)

    @field_validator("callsign", "tactical_call")
    @classmethod
    def upper_calls(cls, value: str | None) -> str | None:
        return normalize_callsign(value) if value is not None else None


class EventUserIn(BaseModel):
    user_id: int = Field(ge=1)
    tactical_call: str = Field(default="", max_length=50)
    in_control: bool = False
    check_interval_minutes: int = Field(default=30, ge=1, le=1440)

    @field_validator("tactical_call")
    @classmethod
    def upper_tactical(cls, value: str) -> str:
        return normalize_callsign(value)


class EventUserPatch(BaseModel):
    tactical_call: str | None = Field(default=None, max_length=50)
    in_control: bool | None = None

    @field_validator("tactical_call")
    @classmethod
    def upper_tactical(cls, value: str | None) -> str | None:
        return normalize_callsign(value) if value is not None else None


class ControlCallsignIn(BaseModel):
    callsign: str = Field(min_length=1, max_length=50)

    @field_validator("callsign")
    @classmethod
    def upper_callsign(cls, value: str) -> str:
        return normalize_callsign(value)


class MessageIn(BaseModel):
    callsign: str = Field(min_length=1, max_length=100)
    station_id: int | None = Field(default=None, ge=1)
    identity_mode: Literal["callsign", "tactical", "both"] = "both"
    message: str = Field(min_length=1, max_length=4000)
    direction: Literal["received", "sent", "info"]
    priority: Literal["routine", "priority", "immediate"] = "routine"
    force_unknown: bool = False
    update_last_heard: bool | None = None

    @field_validator("callsign")
    @classmethod
    def upper_call(cls, value: str) -> str:
        return normalize_callsign(value)


class MessageCorrection(BaseModel):
    callsign: str = Field(min_length=1, max_length=100)
    station_id: int | None = Field(default=None, ge=1)
    identity_mode: Literal["callsign", "tactical", "both"] = "both"
    message: str = Field(min_length=1, max_length=4000)
    direction: Literal["received", "sent", "info"]
    priority: Literal["routine", "priority", "immediate"] = "routine"
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("callsign")
    @classmethod
    def upper_call(cls, value: str) -> str:
        return normalize_callsign(value)


class VoidIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class ConnectionManager:
    def __init__(self) -> None:
        self.connections: dict[int, dict[WebSocket, dict[str, Any]]] = {}
        self.lock = asyncio.Lock()

    async def connect(self, event_id: int, websocket: WebSocket, user: dict[str, Any]) -> None:
        await websocket.accept()
        async with self.lock:
            self.connections.setdefault(event_id, {})[websocket] = user
        await self.broadcast_presence(event_id)

    async def disconnect(self, event_id: int, websocket: WebSocket) -> None:
        async with self.lock:
            if event_id in self.connections:
                self.connections[event_id].pop(websocket, None)
                if not self.connections[event_id]:
                    self.connections.pop(event_id, None)
        await self.broadcast_presence(event_id)

    async def broadcast(self, event_id: int, event_type: str, payload: Any = None) -> None:
        packet = {"type": event_type, "payload": payload, "at": iso_now()}
        async with self.lock:
            sockets = list(self.connections.get(event_id, {}).keys())
        dead: list[WebSocket] = []
        for socket in sockets:
            try:
                await socket.send_json(packet)
            except Exception:
                dead.append(socket)
        for socket in dead:
            await self.disconnect(event_id, socket)

    async def broadcast_presence(self, event_id: int) -> None:
        async with self.lock:
            users = list(self.connections.get(event_id, {}).values())
        unique = {user["id"]: {"id": user["id"], "display_name": user["display_name"], "callsign": user["callsign"]} for user in users}
        await self.broadcast(event_id, "presence", {"count": len(users), "users": list(unique.values())})


manager = ConnectionManager()


@asynccontextmanager
async def lifespan(application: FastAPI):
    init_db()
    application.state.backup_task = asyncio.create_task(backup_loop())
    try:
        yield
    finally:
        task = getattr(application.state, "backup_task", None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="Message Logger", version="1.0", lifespan=lifespan)


def get_session_user(session_token: str | None) -> dict[str, Any] | None:
    if not session_token:
        return None
    with db() as conn:
        row = conn.execute(
            """SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id
               WHERE s.token_hash=? AND s.expires_at>? AND u.active=1""",
            (token_digest(session_token), iso_now()),
        ).fetchone()
    return public_user(row) if row else None


def require_user(raynet_session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
    user = get_session_user(raynet_session)
    if not user:
        raise HTTPException(401, "Authentication required")
    return user


def require_roles(*roles: str):
    def dependency(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
        if user["role"] not in roles:
            raise HTTPException(403, "Insufficient permission")
        return user
    return dependency


def ensure_event(conn: sqlite3.Connection, event_id: int) -> sqlite3.Row:
    event = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if not event:
        raise HTTPException(404, "Event not found")
    return event


def get_station_match(conn: sqlite3.Connection, event_id: int, callsign: str, mode: str = "both") -> sqlite3.Row | None:
    normalized = normalize_callsign(callsign)
    candidates = [normalized]
    if "/" in normalized:
        candidates.extend(normalize_callsign(part) for part in normalized.split("/") if part.strip())
    if mode == "callsign":
        condition = "UPPER(callsign)=UPPER(?)"
    elif mode == "tactical":
        condition = "tactical_call<>'' AND UPPER(tactical_call)=UPPER(?)"
    else:
        condition = "UPPER(callsign)=UPPER(?) OR (tactical_call<>'' AND UPPER(tactical_call)=UPPER(?))"
    for candidate in dict.fromkeys(candidates):
        params = (event_id, candidate) if mode in {"callsign", "tactical"} else (event_id, candidate, candidate)
        row = conn.execute(f"SELECT * FROM stations WHERE event_id=? AND ({condition}) LIMIT 1", params).fetchone()
        if row:
            return row
    return None


def resolve_station(conn: sqlite3.Connection, event_id: int, station_id: int | None, identifier: str, identity_mode: str) -> sqlite3.Row | None:
    if station_id is not None:
        row = conn.execute("SELECT * FROM stations WHERE id=? AND event_id=?", (station_id, event_id)).fetchone()
        if not row:
            raise HTTPException(409, "The selected operator is no longer on this event")
        return row
    return get_station_match(conn, event_id, identifier, identity_mode)


def station_identity(station: sqlite3.Row | dict[str, Any], mode: str) -> str:
    callsign = station["callsign"]
    tactical = station["tactical_call"] or ""
    if mode == "tactical" and tactical:
        return tactical
    if mode == "both" and tactical:
        return f"{callsign} / {tactical}"
    return callsign


def insert_log_message(
    conn: sqlite3.Connection,
    *,
    event_id: int,
    operator_id: int,
    addressed_as: str,
    text: str,
    direction: str,
    priority: str,
    station: sqlite3.Row | dict[str, Any] | None = None,
    address_mode: str = "callsign",
) -> sqlite3.Row:
    now = iso_now()
    sequence = conn.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM messages WHERE event_id=?", (event_id,)).fetchone()[0]
    station_id = station["id"] if station else None
    station_callsign = station["callsign"] if station else addressed_as
    tactical_call = (station["tactical_call"] or "") if station else ""
    assignment = conn.execute(
        "SELECT tactical_call,in_control FROM event_users WHERE event_id=? AND user_id=?",
        (event_id, operator_id),
    ).fetchone()
    operator_control_call = assignment["tactical_call"] if assignment and assignment["in_control"] else ""
    cur = conn.execute(
        """INSERT INTO messages(event_id,sequence,dtg,callsign,station_id,station_callsign,tactical_call,address_mode,message,direction,priority,operator_id,operator_control_call,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (event_id, sequence, now, addressed_as, station_id, station_callsign, tactical_call, address_mode, text, direction, priority, operator_id, operator_control_call, now, now),
    )
    return conn.execute(
        """SELECT m.*,u.display_name AS operator_name,u.callsign AS operator_callsign
           FROM messages m JOIN users u ON u.id=m.operator_id WHERE m.id=?""",
        (cur.lastrowid,),
    ).fetchone()


def get_message_rows(conn: sqlite3.Connection, event_id: int, limit: int = 5000) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT m.*,u.display_name AS operator_name,u.callsign AS operator_callsign
           FROM messages m JOIN users u ON u.id=m.operator_id
           WHERE m.event_id=? ORDER BY m.sequence DESC LIMIT ?""",
        (event_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


async def backup_loop() -> None:
    if BACKUP_INTERVAL_SECONDS <= 0:
        return
    while True:
        await asyncio.sleep(BACKUP_INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(create_backup)
        except Exception as exc:
            print(f"Backup failed: {exc}")


def create_backup() -> Path:
    if not DB_PATH.exists():
        raise FileNotFoundError(DB_PATH)
    destination = BACKUP_DIR / f"raynet-{utcnow().strftime('%Y%m%d-%H%M%S')}.db"
    source_conn = sqlite3.connect(DB_PATH)
    destination_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(destination_conn)
    finally:
        destination_conn.close()
        source_conn.close()
    backups = sorted(BACKUP_DIR.glob("raynet-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[BACKUP_RETENTION:]:
        old.unlink(missing_ok=True)
    return destination


@app.get("/api/bootstrap-status")
def bootstrap_status() -> dict[str, Any]:
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        branding = get_branding(conn)
    return {"needs_setup": count == 0, "org_name": branding["org_name"], "branding": branding, "timezone": LOCAL_TIMEZONE, "version": app.version}


@app.post("/api/setup")
def setup(payload: SetupIn, response: Response) -> dict[str, Any]:
    with db() as conn:
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] != 0:
            raise HTTPException(409, "Initial setup has already been completed")
        salt, password_hash = hash_password(payload.password)
        cur = conn.execute(
            """INSERT INTO users(username,display_name,callsign,password_salt,password_hash,role,active,created_at)
               VALUES(?,?,?,?,?,'admin',1,?)""",
            (payload.username.strip(), payload.display_name.strip(), normalize_callsign(payload.callsign), salt, password_hash, iso_now()),
        )
        user_id = cur.lastrowid
        audit(conn, event_id=None, user_id=user_id, action="setup", entity_type="user", entity_id=user_id, after={"username": payload.username, "role": "admin"})
    token = create_session(user_id)
    set_session_cookie(response, token)
    return {"ok": True}


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = (utcnow() + timedelta(hours=SESSION_HOURS)).isoformat(timespec="seconds")
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (iso_now(),))
        conn.execute(
            "INSERT INTO sessions(user_id,token_hash,expires_at,created_at) VALUES(?,?,?,?)",
            (user_id, token_digest(token), expires, iso_now()),
        )
    return token


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_HOURS * 3600,
        httponly=True,
        samesite="lax",
        secure=os.getenv("RAYNET_SECURE_COOKIE", "0") == "1",
        path="/",
    )


@app.post("/api/login")
def login(payload: LoginIn, response: Response) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE AND active=1", (payload.username.strip(),)).fetchone()
    if not row or not verify_password(payload.password, row["password_salt"], row["password_hash"]):
        raise HTTPException(401, "Invalid username or password")
    token = create_session(row["id"])
    set_session_cookie(response, token)
    return {"user": public_user(row)}


@app.post("/api/logout")
def logout(response: Response, raynet_session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, bool]:
    if raynet_session:
        with db() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_digest(raynet_session),))
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    with db() as conn:
        branding = get_branding(conn)
    return {"user": user, "org_name": branding["org_name"], "branding": branding, "timezone": LOCAL_TIMEZONE}


@app.get("/api/branding")
def branding(user: dict[str, Any] = Depends(require_user)) -> dict[str, str]:
    del user
    with db() as conn:
        return get_branding(conn)


@app.put("/api/branding")
def update_branding(payload: BrandingIn, user: dict[str, Any] = Depends(require_roles("admin"))) -> dict[str, str]:
    logo = payload.logo_data_url.strip()
    if logo == DEFAULT_LOGO_URL:
        logo = ""
    if logo:
        try:
            header, encoded = logo.split(",", 1)
            mime = header.removeprefix("data:").removesuffix(";base64")
            if not header.endswith(";base64") or mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
                raise ValueError
            image_bytes = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise HTTPException(422, "Logo must be a PNG, JPEG, WebP or GIF image")
        if len(image_bytes) > 1_000_000:
            raise HTTPException(413, "Logo must be no larger than 1 MB")
    with db() as conn:
        before = get_branding(conn)
        values = {"org_name": payload.org_name.strip(), "tagline": payload.tagline.strip(), "logo_data_url": logo}
        for key, value in values.items():
            conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        after = get_branding(conn)
        audit(conn, event_id=None, user_id=user["id"], action="update", entity_type="branding", entity_id=None,
              before={"org_name": before["org_name"], "tagline": before["tagline"], "custom_logo": before["logo_data_url"] != DEFAULT_LOGO_URL},
              after={"org_name": after["org_name"], "tagline": after["tagline"], "custom_logo": after["logo_data_url"] != DEFAULT_LOGO_URL})
    return after


@app.get("/api/users")
def users(user: dict[str, Any] = Depends(require_roles("admin"))) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY display_name COLLATE NOCASE").fetchall()
    return [public_user(row) for row in rows]


@app.get("/api/user-directory")
def user_directory(user: dict[str, Any] = Depends(require_roles("admin", "controller"))) -> list[dict[str, Any]]:
    del user
    with db() as conn:
        rows = conn.execute("SELECT id,display_name,callsign FROM users WHERE active=1 ORDER BY display_name COLLATE NOCASE").fetchall()
    return [dict(row) for row in rows]


@app.get("/api/control-callsigns")
def control_callsigns(user: dict[str, Any] = Depends(require_user)) -> list[dict[str, Any]]:
    del user
    with db() as conn:
        rows = conn.execute("SELECT * FROM control_callsigns ORDER BY callsign COLLATE NOCASE").fetchall()
    return [dict(row) for row in rows]


@app.post("/api/control-callsigns")
def create_control_callsign(payload: ControlCallsignIn, user: dict[str, Any] = Depends(require_roles("admin"))) -> dict[str, Any]:
    with db() as conn:
        try:
            cur = conn.execute("INSERT INTO control_callsigns(callsign,created_at) VALUES(?,?)", (payload.callsign, iso_now()))
        except sqlite3.IntegrityError:
            return dict(conn.execute("SELECT * FROM control_callsigns WHERE callsign=? COLLATE NOCASE", (payload.callsign,)).fetchone())
        row = conn.execute("SELECT * FROM control_callsigns WHERE id=?", (cur.lastrowid,)).fetchone()
        audit(conn, event_id=None, user_id=user["id"], action="create", entity_type="control_callsign", entity_id=cur.lastrowid, after=dict(row))
    return dict(row)


@app.delete("/api/control-callsigns/{callsign_id}")
def delete_control_callsign(callsign_id: int, user: dict[str, Any] = Depends(require_roles("admin"))) -> dict[str, bool]:
    with db() as conn:
        before = conn.execute("SELECT * FROM control_callsigns WHERE id=?", (callsign_id,)).fetchone()
        if not before:
            raise HTTPException(404, "Control callsign not found")
        conn.execute("DELETE FROM control_callsigns WHERE id=?", (callsign_id,))
        audit(conn, event_id=None, user_id=user["id"], action="delete", entity_type="control_callsign", entity_id=callsign_id, before=dict(before))
    return {"ok": True}


@app.patch("/api/control-callsigns/{callsign_id}")
def patch_control_callsign(callsign_id: int, payload: ControlCallsignIn, user: dict[str, Any] = Depends(require_roles("admin"))) -> dict[str, Any]:
    with db() as conn:
        before = conn.execute("SELECT * FROM control_callsigns WHERE id=?", (callsign_id,)).fetchone()
        if not before:
            raise HTTPException(404, "Control callsign not found")
        try:
            conn.execute("UPDATE control_callsigns SET callsign=? WHERE id=?", (payload.callsign, callsign_id))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "That Control callsign already exists")
        after = conn.execute("SELECT * FROM control_callsigns WHERE id=?", (callsign_id,)).fetchone()
        audit(conn, event_id=None, user_id=user["id"], action="update", entity_type="control_callsign", entity_id=callsign_id, before=dict(before), after=dict(after))
    return dict(after)


@app.post("/api/users")
def create_user(payload: UserIn, user: dict[str, Any] = Depends(require_roles("admin"))) -> dict[str, Any]:
    salt, password_hash = hash_password(payload.password)
    with db() as conn:
        try:
            cur = conn.execute(
                """INSERT INTO users(username,display_name,callsign,password_salt,password_hash,role,active,created_at)
                   VALUES(?,?,?,?,?,?,1,?)""",
                (payload.username.strip(), payload.display_name.strip(), normalize_callsign(payload.callsign), salt, password_hash, payload.role, iso_now()),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Username already exists")
        created = conn.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
        audit(conn, event_id=None, user_id=user["id"], action="create", entity_type="user", entity_id=cur.lastrowid, after=public_user(created))
    return public_user(created)


@app.patch("/api/users/{user_id}")
def patch_user(user_id: int, payload: UserPatch, user: dict[str, Any] = Depends(require_roles("admin"))) -> dict[str, Any]:
    with db() as conn:
        before = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not before:
            raise HTTPException(404, "User not found")
        values = payload.model_dump(exclude_unset=True)
        if user_id == user["id"] and values.get("active") is False:
            raise HTTPException(400, "You cannot deactivate your own account")
        columns: list[str] = []
        params: list[Any] = []
        for field in ("display_name", "callsign", "role", "active"):
            if field in values:
                columns.append(f"{field}=?")
                value = values[field]
                if field == "callsign": value = normalize_callsign(value)
                if field == "active": value = int(value)
                params.append(value)
        if "password" in values:
            salt, password_hash = hash_password(values["password"])
            columns.extend(["password_salt=?", "password_hash=?"])
            params.extend([salt, password_hash])
        if columns:
            params.append(user_id)
            conn.execute(f"UPDATE users SET {', '.join(columns)} WHERE id=?", params)
        after = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        audit(conn, event_id=None, user_id=user["id"], action="update", entity_type="user", entity_id=user_id, before=public_user(before), after=public_user(after))
    return public_user(after)


@app.get("/api/events")
def list_events(user: dict[str, Any] = Depends(require_user)) -> list[dict[str, Any]]:
    del user
    with db() as conn:
        rows = conn.execute(
            """SELECT e.*,u.display_name AS created_by_name,
               (SELECT COUNT(*) FROM messages m WHERE m.event_id=e.id) AS message_count,
               (SELECT COUNT(*) FROM stations s WHERE s.event_id=e.id AND s.on_duty=1) AS on_duty_count
               FROM events e JOIN users u ON u.id=e.created_by
               ORDER BY CASE e.status WHEN 'active' THEN 0 ELSE 1 END, e.started_at DESC"""
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/events")
async def create_event(payload: EventIn, user: dict[str, Any] = Depends(require_roles("admin", "controller"))) -> dict[str, Any]:
    now = iso_now()
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO events(name,location,control_callsign,event_notes,location_details,phone_numbers,event_contacts,radio_frequency,ctcss_tones,radio_mode,talk_groups,status,created_at,started_at,created_by)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?)""",
            (payload.name.strip(), "", normalize_callsign(payload.control_callsign), payload.event_notes.strip(), payload.location_details.strip(), payload.phone_numbers.strip(), payload.event_contacts.strip(), payload.radio_frequency.strip(), payload.ctcss_tones.strip(), payload.radio_mode.strip(), payload.talk_groups.strip(), now, now, user["id"]),
        )
        event = conn.execute("SELECT * FROM events WHERE id=?", (cur.lastrowid,)).fetchone()
        audit(conn, event_id=cur.lastrowid, user_id=user["id"], action="create", entity_type="event", entity_id=cur.lastrowid, after=dict(event))
    return dict(event)


@app.patch("/api/events/{event_id}")
async def patch_event(event_id: int, payload: EventPatch, user: dict[str, Any] = Depends(require_roles("admin", "controller"))) -> dict[str, Any]:
    with db() as conn:
        before = ensure_event(conn, event_id)
        values = payload.model_dump(exclude_unset=True)
        columns: list[str] = []
        params: list[Any] = []
        for key, value in values.items():
            if key == "control_callsign": value = normalize_callsign(value)
            columns.append(f"{key}=?")
            params.append(value)
        if values.get("status") == "closed":
            columns.append("closed_at=?")
            params.append(iso_now())
        elif values.get("status") == "active":
            columns.append("closed_at=NULL")
        if columns:
            params.append(event_id)
            conn.execute(f"UPDATE events SET {', '.join(columns)} WHERE id=?", params)
        after = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        audit(conn, event_id=event_id, user_id=user["id"], action="update", entity_type="event", entity_id=event_id, before=dict(before), after=dict(after))
    await manager.broadcast(event_id, "event_updated", dict(after))
    return dict(after)


@app.delete("/api/events/{event_id}")
async def delete_event(event_id: int, user: dict[str, Any] = Depends(require_roles("admin"))) -> dict[str, Any]:
    with db() as conn:
        before = ensure_event(conn, event_id)
        counts = {
            "messages": conn.execute("SELECT COUNT(*) FROM messages WHERE event_id=?", (event_id,)).fetchone()[0],
            "stations": conn.execute("SELECT COUNT(*) FROM stations WHERE event_id=?", (event_id,)).fetchone()[0],
            "event_users": conn.execute("SELECT COUNT(*) FROM event_users WHERE event_id=?", (event_id,)).fetchone()[0],
            "audit_entries": conn.execute("SELECT COUNT(*) FROM audit_log WHERE event_id=?", (event_id,)).fetchone()[0],
        }
        conn.execute("DELETE FROM events WHERE id=?", (event_id,))
        audit(conn, event_id=None, user_id=user["id"], action="delete", entity_type="event", entity_id=event_id,
              before={**dict(before), "deleted_records": counts})
    await manager.broadcast(event_id, "event_deleted", {"id": event_id})
    return {"deleted": True, "id": event_id, "name": before["name"], "records": counts}


@app.get("/api/events/{event_id}/snapshot")
def snapshot(event_id: int, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    del user
    with db() as conn:
        event = ensure_event(conn, event_id)
        stations = [dict(row) for row in conn.execute("SELECT * FROM stations WHERE event_id=? ORDER BY on_duty DESC, callsign COLLATE NOCASE", (event_id,)).fetchall()]
        event_users = [dict(row) for row in conn.execute(
            """SELECT eu.*,u.display_name,u.callsign FROM event_users eu JOIN users u ON u.id=eu.user_id
               WHERE eu.event_id=? ORDER BY u.display_name COLLATE NOCASE""", (event_id,)
        ).fetchall()]
        messages = get_message_rows(conn, event_id)
    return {"event": dict(event), "stations": stations, "event_users": event_users, "messages": messages}


@app.post("/api/events/{event_id}/join")
async def join_event(event_id: int, user: dict[str, Any] = Depends(require_roles("admin", "controller", "operator"))) -> dict[str, Any]:
    created_assignment = False
    created_station = False
    with db() as conn:
        ensure_event(conn, event_id)
        assignment = conn.execute("SELECT * FROM event_users WHERE event_id=? AND user_id=?", (event_id, user["id"])).fetchone()
        now = iso_now()
        if not assignment:
            cur = conn.execute(
                "INSERT INTO event_users(event_id,user_id,tactical_call,in_control,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (event_id, user["id"], "", 0, now, now),
            )
            assignment = conn.execute("SELECT * FROM event_users WHERE id=?", (cur.lastrowid,)).fetchone()
            created_assignment = True
            audit(conn, event_id=event_id, user_id=user["id"], action="auto_assign", entity_type="event_user", entity_id=cur.lastrowid, after=dict(assignment))

        station = conn.execute("SELECT * FROM stations WHERE event_id=? AND callsign=? COLLATE NOCASE", (event_id, user["callsign"])).fetchone() if user["callsign"] else None
        if user["callsign"] and not station:
            cur = conn.execute(
                """INSERT INTO stations(event_id,name,callsign,tactical_call,check_interval_minutes,on_duty,in_control,notes,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (event_id, user["display_name"], user["callsign"], assignment["tactical_call"], 30, 1, int(assignment["in_control"]), "Permanent user", now, now),
            )
            station = conn.execute("SELECT * FROM stations WHERE id=?", (cur.lastrowid,)).fetchone()
            created_station = True
            audit(conn, event_id=event_id, user_id=user["id"], action="auto_assign", entity_type="station", entity_id=cur.lastrowid, after=dict(station))
    if created_assignment:
        await manager.broadcast(event_id, "event_user_created", {"user_id": user["id"]})
    if created_station:
        await manager.broadcast(event_id, "station_created", dict(station))
    return {"assignment_created": created_assignment, "station_created": created_station}


@app.post("/api/events/{event_id}/operators")
async def create_event_user(event_id: int, payload: EventUserIn, user: dict[str, Any] = Depends(require_roles("admin", "controller"))) -> dict[str, Any]:
    with db() as conn:
        ensure_event(conn, event_id)
        assigned_user = conn.execute("SELECT id FROM users WHERE id=? AND active=1", (payload.user_id,)).fetchone()
        if not assigned_user:
            raise HTTPException(404, "Active user not found")
        now = iso_now()
        try:
            cur = conn.execute(
                "INSERT INTO event_users(event_id,user_id,tactical_call,in_control,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (event_id, payload.user_id, payload.tactical_call, int(payload.in_control), now, now),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "That user is already assigned to this event")
        row = conn.execute("""SELECT eu.*,u.display_name,u.callsign FROM event_users eu JOIN users u ON u.id=eu.user_id WHERE eu.id=?""", (cur.lastrowid,)).fetchone()
        audit(conn, event_id=event_id, user_id=user["id"], action="create", entity_type="event_user", entity_id=cur.lastrowid, after=dict(row))
        station = None
        if row["callsign"]:
            station = conn.execute("SELECT * FROM stations WHERE event_id=? AND callsign=? COLLATE NOCASE", (event_id, row["callsign"])).fetchone()
            if not station:
                station_cur = conn.execute(
                    """INSERT INTO stations(event_id,name,callsign,tactical_call,check_interval_minutes,on_duty,in_control,notes,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (event_id, row["display_name"], row["callsign"], payload.tactical_call, payload.check_interval_minutes, 1, int(payload.in_control), "Permanent user", now, now),
                )
                station = conn.execute("SELECT * FROM stations WHERE id=?", (station_cur.lastrowid,)).fetchone()
                audit(conn, event_id=event_id, user_id=user["id"], action="create", entity_type="station", entity_id=station_cur.lastrowid, after=dict(station))
    await manager.broadcast(event_id, "event_user_created", dict(row))
    if station:
        await manager.broadcast(event_id, "station_created", dict(station))
    return dict(row)


@app.patch("/api/event-operators/{assignment_id}")
async def patch_event_user(assignment_id: int, payload: EventUserPatch, user: dict[str, Any] = Depends(require_roles("admin", "controller"))) -> dict[str, Any]:
    with db() as conn:
        before = conn.execute("SELECT * FROM event_users WHERE id=?", (assignment_id,)).fetchone()
        if not before:
            raise HTTPException(404, "Event operator not found")
        values = payload.model_dump(exclude_unset=True)
        columns, params = [], []
        for key, value in values.items():
            columns.append(f"{key}=?")
            params.append(int(value) if key == "in_control" else value)
        if columns:
            columns.append("updated_at=?")
            params.extend([iso_now(), assignment_id])
            conn.execute(f"UPDATE event_users SET {', '.join(columns)} WHERE id=?", params)
        after = conn.execute("""SELECT eu.*,u.display_name,u.callsign FROM event_users eu JOIN users u ON u.id=eu.user_id WHERE eu.id=?""", (assignment_id,)).fetchone()
        audit(conn, event_id=before["event_id"], user_id=user["id"], action="update", entity_type="event_user", entity_id=assignment_id, before=dict(before), after=dict(after))
    await manager.broadcast(before["event_id"], "event_user_updated", dict(after))
    return dict(after)


@app.delete("/api/event-operators/{assignment_id}")
async def delete_event_user(assignment_id: int, user: dict[str, Any] = Depends(require_roles("admin", "controller"))) -> dict[str, Any]:
    station_before: sqlite3.Row | None = None
    with db() as conn:
        before = conn.execute(
            """SELECT eu.*,u.display_name,u.callsign
               FROM event_users eu JOIN users u ON u.id=eu.user_id
               WHERE eu.id=?""",
            (assignment_id,),
        ).fetchone()
        if not before:
            raise HTTPException(404, "Event operator not found")
        if before["callsign"]:
            station_before = conn.execute(
                "SELECT * FROM stations WHERE event_id=? AND callsign=? COLLATE NOCASE",
                (before["event_id"], before["callsign"]),
            ).fetchone()
            if station_before:
                conn.execute("DELETE FROM stations WHERE id=?", (station_before["id"],))
                audit(conn, event_id=before["event_id"], user_id=user["id"], action="delete", entity_type="station", entity_id=station_before["id"], before=dict(station_before))
        conn.execute("DELETE FROM event_users WHERE id=?", (assignment_id,))
        audit(conn, event_id=before["event_id"], user_id=user["id"], action="delete", entity_type="event_user", entity_id=assignment_id, before=dict(before))
    if station_before:
        await manager.broadcast(before["event_id"], "station_deleted", {"id": station_before["id"]})
    await manager.broadcast(before["event_id"], "event_user_deleted", {"id": assignment_id})
    return {"ok": True, "station_id": station_before["id"] if station_before else None}


@app.post("/api/events/{event_id}/stations")
async def create_station(event_id: int, payload: StationIn, user: dict[str, Any] = Depends(require_roles("admin", "controller"))) -> dict[str, Any]:
    with db() as conn:
        ensure_event(conn, event_id)
        now = iso_now()
        try:
            cur = conn.execute(
                """INSERT INTO stations(event_id,name,callsign,tactical_call,check_interval_minutes,on_duty,duty_status,in_control,notes,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (event_id, payload.name.strip(), payload.callsign, payload.tactical_call, payload.check_interval_minutes, int(payload.duty_status == "on_duty" and payload.on_duty), payload.duty_status if payload.on_duty else "off_duty", int(payload.in_control), payload.notes.strip(), now, now),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "That callsign is already on this event")
        station = conn.execute("SELECT * FROM stations WHERE id=?", (cur.lastrowid,)).fetchone()
        audit(conn, event_id=event_id, user_id=user["id"], action="create", entity_type="station", entity_id=cur.lastrowid, after=dict(station))
    await manager.broadcast(event_id, "station_created", dict(station))
    return dict(station)


@app.patch("/api/stations/{station_id}")
async def patch_station(station_id: int, payload: StationPatch, user: dict[str, Any] = Depends(require_roles("admin", "controller"))) -> dict[str, Any]:
    identity_message: dict[str, Any] | None = None
    with db() as conn:
        before = conn.execute("SELECT * FROM stations WHERE id=?", (station_id,)).fetchone()
        if not before:
            raise HTTPException(404, "Station not found")
        values = payload.model_dump(exclude_unset=True)
        if "duty_status" in values:
            values["on_duty"] = values["duty_status"] == "on_duty"
        elif "on_duty" in values:
            values["duty_status"] = "on_duty" if values["on_duty"] else "off_duty"
        columns: list[str] = []
        params: list[Any] = []
        for key, value in values.items():
            if key in {"on_duty", "in_control"}:
                value = int(value)
            columns.append(f"{key}=?")
            params.append(value)
        columns.append("updated_at=?")
        params.append(iso_now())
        params.append(station_id)
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(f"UPDATE stations SET {', '.join(columns)} WHERE id=?", params)
        except sqlite3.IntegrityError:
            raise HTTPException(409, "That callsign is already on this event")
        after = conn.execute("SELECT * FROM stations WHERE id=?", (station_id,)).fetchone()
        identity_changed = before["callsign"] != after["callsign"] or before["tactical_call"] != after["tactical_call"]
        audit(
            conn,
            event_id=before["event_id"],
            user_id=user["id"],
            action="identity_change" if identity_changed else "update",
            entity_type="station",
            entity_id=station_id,
            before=dict(before),
            after=dict(after),
        )
        if identity_changed:
            old_identity = station_identity(before, "both")
            new_identity = station_identity(after, "both")
            logged = insert_log_message(
                conn,
                event_id=before["event_id"],
                operator_id=user["id"],
                addressed_as=station_identity(after, "both"),
                text=f"Operator identity changed: {old_identity} → {new_identity}.",
                direction="info",
                priority="routine",
                station=after,
                address_mode="both" if after["tactical_call"] else "callsign",
            )
            identity_message = dict(logged)
            audit(conn, event_id=before["event_id"], user_id=user["id"], action="create", entity_type="message", entity_id=logged["id"], after=identity_message)
    await manager.broadcast(before["event_id"], "station_updated", dict(after))
    if identity_message:
        await manager.broadcast(before["event_id"], "message_created", identity_message)
    return {**dict(after), "identity_log_message": identity_message}


@app.post("/api/stations/{station_id}/heard")
async def station_heard(station_id: int, user: dict[str, Any] = Depends(require_roles("admin", "controller", "operator"))) -> dict[str, Any]:
    with db() as conn:
        before = conn.execute("SELECT * FROM stations WHERE id=?", (station_id,)).fetchone()
        if not before:
            raise HTTPException(404, "Station not found")
        now = iso_now()
        conn.execute("UPDATE stations SET last_heard_at=?,updated_at=? WHERE id=?", (now, now, station_id))
        after = conn.execute("SELECT * FROM stations WHERE id=?", (station_id,)).fetchone()
        audit(conn, event_id=before["event_id"], user_id=user["id"], action="heard", entity_type="station", entity_id=station_id, before={"last_heard_at": before["last_heard_at"]}, after={"last_heard_at": now})
    await manager.broadcast(before["event_id"], "station_updated", dict(after))
    return dict(after)


@app.delete("/api/stations/{station_id}")
async def delete_station(station_id: int, user: dict[str, Any] = Depends(require_roles("admin", "controller"))) -> dict[str, bool]:
    with db() as conn:
        before = conn.execute("SELECT * FROM stations WHERE id=?", (station_id,)).fetchone()
        if not before:
            raise HTTPException(404, "Station not found")
        conn.execute("DELETE FROM stations WHERE id=?", (station_id,))
        audit(conn, event_id=before["event_id"], user_id=user["id"], action="delete", entity_type="station", entity_id=station_id, before=dict(before))
    await manager.broadcast(before["event_id"], "station_deleted", {"id": station_id})
    return {"ok": True}


@app.post("/api/events/{event_id}/messages")
async def create_message(event_id: int, payload: MessageIn, user: dict[str, Any] = Depends(require_roles("admin", "controller", "operator"))) -> dict[str, Any]:
    with db() as conn:
        event = ensure_event(conn, event_id)
        if event["status"] != "active":
            raise HTTPException(409, "This event is closed")
        station = resolve_station(conn, event_id, payload.station_id, payload.callsign, payload.identity_mode)
        if not station and payload.callsign not in {"INFO", "ALL STATIONS"} and not payload.force_unknown:
            raise HTTPException(status_code=409, detail={"code": "unknown_callsign", "callsign": payload.callsign})
        conn.execute("BEGIN IMMEDIATE")
        addressed_as = station_identity(station, payload.identity_mode) if station else payload.callsign
        message = insert_log_message(
            conn,
            event_id=event_id,
            operator_id=user["id"],
            addressed_as=addressed_as,
            text=payload.message.strip(),
            direction=payload.direction,
            priority=payload.priority,
            station=station,
            address_mode=payload.identity_mode if station else "callsign",
        )
        update_last_heard = payload.direction in ("received", "sent")
        if payload.callsign == "ALL STATIONS":
            update_last_heard = False
        station_after = None
        if station and update_last_heard:
            now = iso_now()
            conn.execute("UPDATE stations SET last_heard_at=?,updated_at=? WHERE id=?", (now, now, station["id"]))
            station_after = conn.execute("SELECT * FROM stations WHERE id=?", (station["id"],)).fetchone()
        audit(conn, event_id=event_id, user_id=user["id"], action="create", entity_type="message", entity_id=message["id"], after=dict(message))
    await manager.broadcast(event_id, "message_created", dict(message))
    if station_after:
        await manager.broadcast(event_id, "station_updated", dict(station_after))
    return {"message": dict(message), "known_station": bool(station)}


@app.post("/api/messages/{message_id}/correct")
async def correct_message(message_id: int, payload: MessageCorrection, user: dict[str, Any] = Depends(require_roles("admin", "controller"))) -> dict[str, Any]:
    with db() as conn:
        before = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        if not before:
            raise HTTPException(404, "Message not found")
        reason = payload.reason.strip()
        conn.execute(
            """INSERT INTO message_revisions(message_id,version,previous_json,reason,changed_by,changed_at)
               VALUES(?,?,?,?,?,?)""",
            (message_id, before["version"], json.dumps(dict(before), default=str), reason, user["id"], iso_now()),
        )
        station = resolve_station(conn, before["event_id"], payload.station_id, payload.callsign, payload.identity_mode)
        addressed_as = station_identity(station, payload.identity_mode) if station else payload.callsign
        conn.execute(
            """UPDATE messages SET callsign=?,station_id=?,station_callsign=?,tactical_call=?,address_mode=?,message=?,direction=?,priority=?,version=version+1,updated_at=? WHERE id=?""",
            (
                addressed_as,
                station["id"] if station else None,
                station["callsign"] if station else payload.callsign,
                (station["tactical_call"] or "") if station else "",
                payload.identity_mode if station else "callsign",
                payload.message.strip(), payload.direction, payload.priority, iso_now(), message_id,
            ),
        )
        after = conn.execute(
            """SELECT m.*,u.display_name AS operator_name,u.callsign AS operator_callsign
               FROM messages m JOIN users u ON u.id=m.operator_id WHERE m.id=?""",
            (message_id,),
        ).fetchone()
        audit(conn, event_id=before["event_id"], user_id=user["id"], action="correct", entity_type="message", entity_id=message_id, before=dict(before), after={**dict(after), "reason": reason})
    await manager.broadcast(before["event_id"], "message_updated", dict(after))
    return dict(after)


@app.post("/api/messages/{message_id}/void")
async def void_message(message_id: int, payload: VoidIn, user: dict[str, Any] = Depends(require_roles("admin", "controller"))) -> dict[str, Any]:
    with db() as conn:
        before = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        if not before:
            raise HTTPException(404, "Message not found")
        conn.execute("UPDATE messages SET voided=1,version=version+1,updated_at=? WHERE id=?", (iso_now(), message_id))
        after = conn.execute(
            """SELECT m.*,u.display_name AS operator_name,u.callsign AS operator_callsign
               FROM messages m JOIN users u ON u.id=m.operator_id WHERE m.id=?""",
            (message_id,),
        ).fetchone()
        audit(conn, event_id=before["event_id"], user_id=user["id"], action="void", entity_type="message", entity_id=message_id, before=dict(before), after={**dict(after), "reason": payload.reason.strip()})
    await manager.broadcast(before["event_id"], "message_updated", dict(after))
    return dict(after)


@app.get("/api/events/{event_id}/audit")
def event_audit(event_id: int, user: dict[str, Any] = Depends(require_roles("admin", "controller"))) -> list[dict[str, Any]]:
    with db() as conn:
        ensure_event(conn, event_id)
        rows = conn.execute(
            """SELECT a.*,u.display_name AS user_name,u.callsign AS user_callsign
               FROM audit_log a LEFT JOIN users u ON u.id=a.user_id
               WHERE a.event_id=? ORDER BY a.id DESC LIMIT 1000""",
            (event_id,),
        ).fetchall()
    return [dict(row) for row in rows]


EXPORT_SECTIONS = {"summary", "messages", "operators", "audit"}


def parse_export_sections(value: str) -> set[str]:
    requested = {part.strip().lower() for part in value.split(",") if part.strip()}
    invalid = requested - EXPORT_SECTIONS
    if invalid or not requested:
        raise HTTPException(400, f"Invalid export section: {', '.join(sorted(invalid))}" if invalid else "Select at least one export section")
    return requested


def audit_description(row: dict[str, Any]) -> str:
    before = json.loads(row["before_json"]) if row.get("before_json") else {}
    after = json.loads(row["after_json"]) if row.get("after_json") else {}
    changed = []
    for key in sorted(set(before) | set(after)):
        if key in {"updated_at", "created_at", "password_hash", "password_salt"} or before.get(key) == after.get(key):
            continue
        changed.append(f"{key.replace('_', ' ').title()}: {before.get(key, '—')} -> {after.get(key, '—')}")
    return "; ".join(changed) or f"{row['action'].replace('_', ' ').title()} recorded"


def build_event_pdf(event: dict[str, Any], messages: list[dict[str, Any]], stations: list[dict[str, Any]], audits: list[dict[str, Any]], sections: set[str], branding: dict[str, str]) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    output = io.BytesIO()
    doc = SimpleDocTemplate(output, pagesize=landscape(A4), rightMargin=12 * mm, leftMargin=12 * mm, topMargin=18 * mm, bottomMargin=16 * mm, title=f"{event['name']} export", author=branding["org_name"])
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="SectionTitle", parent=styles["Heading2"], textColor=colors.HexColor("#123b5d"), spaceBefore=8, spaceAfter=8))
    styles.add(ParagraphStyle(name="Cell", parent=styles["BodyText"], fontSize=7.2, leading=9))
    styles.add(ParagraphStyle(name="Small", parent=styles["BodyText"], fontSize=8, leading=10, textColor=colors.HexColor("#526570")))
    cell = lambda value: Paragraph(html.escape(str(value if value not in (None, "") else "—")), styles["Cell"])

    def table(rows: list[list[Any]], widths: list[float] | None = None) -> Table:
        result = Table(rows, colWidths=widths, repeatRows=1, hAlign="LEFT")
        result.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#17384f")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, 0), 7.5),
            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("GRID", (0, 0), (-1, -1), .3, colors.HexColor("#c7d3db")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f6f8")]),
            ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        return result

    story: list[Any] = []
    logo = branding.get("logo_data_url", "")
    logo_source: Any = None
    try:
        if logo.startswith("data:image"):
            logo_source = io.BytesIO(base64.b64decode(logo.split(",", 1)[1]))
        elif logo.startswith("/static/"):
            logo_source = BASE_DIR / logo.lstrip("/")
        brand_image = Image(logo_source, width=24 * mm, height=18 * mm, kind="proportional") if logo_source else ""
    except Exception:
        brand_image = ""
    brand = Table([[brand_image, [Paragraph(html.escape(branding["tagline"]), styles["Small"]), Paragraph(html.escape(branding["org_name"]), styles["Title"])]]], colWidths=[30 * mm, 220 * mm])
    brand.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story += [brand, Spacer(1, 5 * mm), Paragraph(html.escape(event["name"]), styles["Heading1"]), Paragraph("Event export", styles["Small"]), Spacer(1, 5 * mm)]
    if "summary" in sections:
        story += [Paragraph("Event summary", styles["SectionTitle"]), table([
            ["Field", "Value"], ["Event", cell(event["name"])], ["Status", cell(event["status"].title())],
            ["Started", cell(event["started_at"])], ["Closed", cell(event["closed_at"])],
            ["Notes", cell(event.get("event_notes"))], ["Location information", cell(event.get("location_details"))],
            ["Event contacts", cell("\n".join(value for value in (event.get("event_contacts"), event.get("phone_numbers")) if value))],
            ["Radio frequency", cell(event.get("radio_frequency"))], ["CTCSS tones", cell(event.get("ctcss_tones"))],
            ["Radio mode", cell(event.get("radio_mode"))], ["DMR talk groups", cell(event.get("talk_groups"))],
        ], [45 * mm, 210 * mm]), Spacer(1, 5 * mm)]
    if "messages" in sections:
        if story: story.append(PageBreak())
        rows = [["#", "Date and time", "Priority", "Callsign", "Tactical", "Direction", "Message", "Operator"]]
        rows += [[cell(m["sequence"]), cell(m["dtg"]), cell(m["priority"].title()), cell(m["station_callsign"] or m["callsign"]), cell(m["tactical_call"]), cell(m["direction"].title()), cell(f"{m['message']}{' [VOID]' if m['voided'] else ''}"), cell(f"{m['operator_name']} / {m['operator_callsign']}{' / ' + m['operator_control_call'] if m['operator_control_call'] else ''}")] for m in messages]
        story += [Paragraph("Message log", styles["SectionTitle"]), table(rows, [10*mm, 31*mm, 20*mm, 23*mm, 22*mm, 20*mm, 85*mm, 52*mm])]
    if "operators" in sections:
        story.append(PageBreak())
        rows = [["Name", "Callsign", "Tactical call", "Duty / position", "Check interval", "Last heard", "Notes"]]
        rows += [[cell(s["name"]), cell(s["callsign"]), cell(s["tactical_call"]), cell("Control" if s["in_control"] and s["on_duty"] else ("On duty" if s["on_duty"] else ("Stood down" if s["duty_status"] == "stood_down" else "Off duty"))), cell("Exempt" if s["in_control"] and s["on_duty"] else f"{s['check_interval_minutes']} minutes"), cell(s["last_heard_at"]), cell(s["notes"])] for s in stations]
        story += [Paragraph("Operators and stations", styles["SectionTitle"]), table(rows, [35*mm, 27*mm, 30*mm, 32*mm, 30*mm, 42*mm, 67*mm])]
    if "audit" in sections:
        story.append(PageBreak())
        rows = [["Date and time", "User", "Action", "Record", "Change details"]]
        rows += [[cell(a["created_at"]), cell(f"{a.get('user_name') or 'System'} / {a.get('user_callsign') or '—'}"), cell(a["action"].replace("_", " ").title()), cell(f"{a['entity_type'].replace('_', ' ').title()} #{a['entity_id'] or '—'}"), cell(audit_description(a))] for a in audits]
        story += [Paragraph("Audit trail", styles["SectionTitle"]), table(rows, [38*mm, 44*mm, 30*mm, 38*mm, 113*mm])]

    generated = datetime.now().astimezone().strftime("%d %b %Y, %H:%M")
    def footer(canvas: Any, document: Any) -> None:
        canvas.saveState(); canvas.setFont("Helvetica", 7); canvas.setFillColor(colors.HexColor("#526570"))
        canvas.drawString(12 * mm, 8 * mm, f"{branding['org_name']} - {event['name']} - generated {generated}")
        canvas.drawRightString(285 * mm, 8 * mm, f"Page {document.page}"); canvas.restoreState()
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()


@app.get("/api/events/{event_id}/export/{format_name}")
def export_event(event_id: int, format_name: Literal["csv", "txt", "json", "pdf"], sections: str = "summary,messages,operators,audit", user: dict[str, Any] = Depends(require_user)) -> StreamingResponse:
    selected = parse_export_sections(sections)
    if "audit" in selected and user["role"] not in {"admin", "controller"}:
        raise HTTPException(403, "Audit export requires Controller or Admin access")
    with db() as conn:
        event = ensure_event(conn, event_id)
        messages = list(reversed(get_message_rows(conn, event_id)))
        stations = [dict(row) for row in conn.execute("SELECT * FROM stations WHERE event_id=? ORDER BY callsign", (event_id,)).fetchall()]
        audits = [dict(row) for row in conn.execute("""SELECT a.*,u.display_name AS user_name,u.callsign AS user_callsign FROM audit_log a LEFT JOIN users u ON u.id=a.user_id WHERE a.event_id=? ORDER BY a.id""", (event_id,)).fetchall()]
        branding = get_branding(conn)
    event_data = dict(event)
    safe_name = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in event["name"]).strip("_") or f"event-{event_id}"
    if format_name == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        if "summary" in selected:
            writer.writerows([["EVENT SUMMARY"], ["Event", event["name"]], ["Status", event["status"]], ["Notes", event["event_notes"]], ["Location information", event["location_details"]], ["Event contacts", "\n".join(value for value in (event["event_contacts"], event["phone_numbers"]) if value)], ["Radio frequency", event["radio_frequency"]], ["CTCSS tones", event["ctcss_tones"]], ["Radio mode", event["radio_mode"]], ["DMR talk groups", event["talk_groups"]], []])
        if "messages" in selected:
            writer.writerow(["MESSAGE LOG"]); writer.writerow(["Sequence", "Date and time", "Priority", "Callsign", "Tactical call", "Message", "Direction", "Operator", "Operator callsign", "Control call", "Version", "Voided"])
            for item in messages: writer.writerow([item["sequence"], item["dtg"], item["priority"], item["station_callsign"] or item["callsign"], item["tactical_call"], item["message"], item["direction"], item["operator_name"], item["operator_callsign"], item["operator_control_call"], item["version"], "YES" if item["voided"] else "NO"])
            writer.writerow([])
        if "operators" in selected:
            writer.writerow(["OPERATORS AND STATIONS"]); writer.writerow(["Name", "Callsign", "Tactical call", "Duty status", "Control", "Check interval (minutes)", "Last heard", "Notes"])
            for item in stations: writer.writerow([item["name"], item["callsign"], item["tactical_call"], item["duty_status"].replace("_", " ").title(), item["in_control"], item["check_interval_minutes"], item["last_heard_at"], item["notes"]])
            writer.writerow([])
        if "audit" in selected:
            writer.writerow(["AUDIT TRAIL"]); writer.writerow(["Date and time", "User", "Callsign", "Action", "Record", "Change details"])
            for item in audits: writer.writerow([item["created_at"], item["user_name"], item["user_callsign"], item["action"], f"{item['entity_type']} #{item['entity_id'] or '—'}", audit_description(item)])
        data = output.getvalue().encode("utf-8-sig")
        media = "text/csv"
        ext = "csv"
    elif format_name == "txt":
        lines = [branding["org_name"], event["name"], "=" * 72]
        if "summary" in selected: lines += [f"Status: {event['status']}", f"Started: {event['started_at']}", f"Notes: {event['event_notes'] or '—'}", f"Location information: {event['location_details'] or '—'}", f"Event contacts: {'; '.join(value for value in (event['event_contacts'], event['phone_numbers']) if value) or '—'}", f"Radio frequency: {event['radio_frequency'] or '—'}", f"CTCSS tones: {event['ctcss_tones'] or '—'}", f"Radio mode: {event['radio_mode'] or '—'}", f"DMR talk groups: {event['talk_groups'] or '—'}", ""]
        if "messages" in selected:
            lines += ["MESSAGE LOG", "-"]
            for item in messages: lines.append(f"#{item['sequence']} {item['dtg']} [{item['priority'].upper()}] {item['station_callsign'] or item['callsign']} / {item['tactical_call']} {item['direction'].upper()}: {item['message']}{' [VOID]' if item['voided'] else ''} - {item['operator_name']}")
            lines.append("")
        if "operators" in selected:
            lines += ["OPERATORS AND STATIONS", "-"] + [f"{s['name']} - {s['callsign']} / {s['tactical_call']} - {('CONTROL' if s['in_control'] else str(s['check_interval_minutes']) + ' min') if s['on_duty'] else s['duty_status'].replace('_', ' ').upper()} - last heard {s['last_heard_at'] or 'never'}" for s in stations] + [""]
        if "audit" in selected:
            lines += ["AUDIT TRAIL", "-"] + [f"{a['created_at']} - {a.get('user_name') or 'System'} - {a['action']}: {a['entity_type']} #{a['entity_id'] or '—'} - {audit_description(a)}" for a in audits]
        data = ("\n".join(lines) + "\n").encode("utf-8")
        media = "text/plain"
        ext = "txt"
    elif format_name == "json":
        payload: dict[str, Any] = {"exported_at": iso_now(), "branding": branding}
        if "summary" in selected: payload["event"] = event_data
        if "messages" in selected: payload["messages"] = messages
        if "operators" in selected: payload["stations"] = stations
        if "audit" in selected: payload["audit"] = [{**a, "description": audit_description(a)} for a in audits]
        data = json.dumps(payload, indent=2).encode("utf-8")
        media = "application/json"
        ext = "json"
    else:
        data = build_event_pdf(event_data, messages, stations, audits, selected, branding)
        media = "application/pdf"
        ext = "pdf"
    headers = {"Content-Disposition": f'attachment; filename="{safe_name}.{ext}"'}
    return StreamingResponse(io.BytesIO(data), media_type=media, headers=headers)


@app.post("/api/admin/backup")
def manual_backup(user: dict[str, Any] = Depends(require_roles("admin"))) -> dict[str, Any]:
    path = create_backup()
    with db() as conn:
        audit(conn, event_id=None, user_id=user["id"], action="backup", entity_type="database", entity_id=None, after={"filename": path.name})
    return {"ok": True, "filename": path.name}


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "time": iso_now(), "database": str(DB_PATH), "version": app.version}


@app.websocket("/ws/events/{event_id}")
async def event_socket(websocket: WebSocket, event_id: int) -> None:
    token = websocket.cookies.get(COOKIE_NAME)
    user = get_session_user(token)
    if not user:
        await websocket.close(code=4401)
        return
    with db() as conn:
        exists = conn.execute("SELECT 1 FROM events WHERE id=?", (event_id,)).fetchone()
    if not exists:
        await websocket.close(code=4404)
        return
    await manager.connect(event_id, websocket, user)
    try:
        while True:
            message = await websocket.receive_text()
            if message == "ping":
                await websocket.send_json({"type": "pong", "at": iso_now()})
    except WebSocketDisconnect:
        await manager.disconnect(event_id, websocket)
    except Exception:
        await manager.disconnect(event_id, websocket)


STATIC_DIR = BASE_DIR / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/service-worker.js", include_in_schema=False)
def service_worker() -> FileResponse:
    return FileResponse(STATIC_DIR / "service-worker.js", media_type="application/javascript", headers={"Service-Worker-Allowed": "/"})


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest() -> FileResponse:
    return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.exception_handler(sqlite3.OperationalError)
def sqlite_error(_: Request, exc: sqlite3.OperationalError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": f"Database temporarily unavailable: {exc}"})
