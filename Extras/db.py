"""
Helpers de acceso a la base de datos SQLite compartida por Central y Registry.
"""

from __future__ import annotations

import os
import sqlite3

DEFAULT_DB_PATH = os.getenv("CENTRAL_DB", "Central/bdd/evcharging.db")


def ensure_dir(db_path: str):
    directory = os.path.dirname(db_path)
    if directory and not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DEFAULT_DB_PATH
    ensure_dir(path)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def ensure_schema(conn: sqlite3.Connection):
    """Crea las tablas necesarias si no existen y añade columnas nuevas."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS charging_points (
            cp_id TEXT PRIMARY KEY,
            state TEXT,
            last_update TIMESTAMP,
            total_charges INTEGER DEFAULT 0,
            location TEXT,
            registry_state TEXT,
            weather_status TEXT,
            last_weather_update TIMESTAMP,
            auth_state TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS charging_sessions (
            session_id INTEGER PRIMARY KEY AUTOINCREMENT,
            cp_id TEXT,
            driver_id TEXT,
            start_time TIMESTAMP,
            end_time TIMESTAMP,
            energy_kwh REAL,
            cost REAL,
            state TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TIMESTAMP,
            cp_id TEXT,
            event_type TEXT,
            description TEXT,
            actor TEXT,
            origin_ip TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cp_registry (
            cp_id TEXT PRIMARY KEY,
            location TEXT,
            registered INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1,
            client_secret_hash TEXT,
            client_secret_salt TEXT,
            credentials_issued_at TIMESTAMP,
            metadata TEXT,
            last_auth_ts TIMESTAMP,
            last_token TEXT,
            last_token_hash TEXT,
            last_token_salt TEXT,
            last_token_issued_at TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cp_security_keys (
            key_id INTEGER PRIMARY KEY AUTOINCREMENT,
            cp_id TEXT,
            symmetric_key TEXT,
            issued_at TIMESTAMP,
            active INTEGER DEFAULT 1,
            version INTEGER DEFAULT 1
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS weather_alerts (
            cp_id TEXT PRIMARY KEY,
            city TEXT,
            temperature REAL,
            active INTEGER,
            updated_at TIMESTAMP,
            extra TEXT
        )
        """
    )
    # Nuevas columnas para eventos y charging_points (por compatibilidad)
    if not _column_exists(conn, "events", "actor"):
        conn.execute("ALTER TABLE events ADD COLUMN actor TEXT")
    if not _column_exists(conn, "events", "origin_ip"):
        conn.execute("ALTER TABLE events ADD COLUMN origin_ip TEXT")
    for column in ("location", "registry_state", "weather_status", "last_weather_update", "auth_state"):
        if not _column_exists(conn, "charging_points", column):
            conn.execute(f"ALTER TABLE charging_points ADD COLUMN {column} TEXT")
    for column in ("last_token_hash", "last_token_salt", "last_token_issued_at"):
        if not _column_exists(conn, "cp_registry", column):
            conn.execute(f"ALTER TABLE cp_registry ADD COLUMN {column} TEXT")
    conn.commit()


__all__ = ["get_connection", "ensure_schema", "DEFAULT_DB_PATH"]
