"""
EV_Central Release 2
--------------------

Novedades respecto a la release anterior:
- Integración con EV_Registry (SQLite compartido) para verificar altas/bajas
  y emitir claves simétricas por CP tras autenticación vía API REST.
- Cifrado de todo el tráfico Central<->CP sobre Kafka mediante claves Fernet
  únicas por CP y posibilidad de revocar/reemitir claves.
- Registro de auditoría enriquecido con actor y dirección IP.
- API REST ampliada (autenticación, comandos, alertas meteorológicas,
  consulta de eventos).
- Gestión de alertas meteo notificadas por EV_W que bloquean temporalmente
  un CP.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from kafka import KafkaConsumer, KafkaProducer

from Extras.db import ensure_schema, get_connection
from Extras.security import decrypt_payload, encrypt_payload, generate_symmetric_key, verify_secret
from Extras.utils import resolve_broker

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

with open("config/config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

BROKER = resolve_broker(config, "CENTRAL")
DB_PATH = os.getenv("CENTRAL_DB", "Central/bdd/evcharging.db")
TOPIC_CENTRAL = config["kafka"]["topic_central"]
TOPIC_DRIVER = config["kafka"]["topic_driver"]
TOPIC_CP_COMMANDS = config["kafka"]["topic_cp_commands"]
TOPIC_CP_DATA = config["kafka"]["topic_cp_data"]
TOPIC_CP_STATUS = config["kafka"]["topic_cp_status"]

REGISTRY_CFG = config.get("registry", {})
REGISTRY_TOKEN_TTL = REGISTRY_CFG.get("token_ttl", 180)
CENTRAL_API_CFG = config.get("central_api", {"host": "0.0.0.0", "port": 8000})

producer = KafkaProducer(
    bootstrap_servers=BROKER, value_serializer=lambda v: json.dumps(v).encode("utf-8")
)
consumer_driver = KafkaConsumer(
    TOPIC_CENTRAL,
    bootstrap_servers=BROKER,
    value_deserializer=lambda v: json.loads(v.decode("utf-8")),
    auto_offset_reset="earliest",
    group_id="central_drivers",
)
consumer_cp = KafkaConsumer(
    TOPIC_CP_DATA,
    TOPIC_CP_STATUS,
    bootstrap_servers=BROKER,
    value_deserializer=lambda v: json.loads(v.decode("utf-8")),
    auto_offset_reset="earliest",
    group_id="central_cps",
)

# ---------------------------------------------------------------------------
# Estado en memoria y BBDD
# ---------------------------------------------------------------------------

db_lock = threading.Lock()
conn = get_connection(DB_PATH)
ensure_schema(conn)

# cp_id -> info persistida + runtime
CP_STATE: Dict[str, Dict[str, Any]] = {}
# cp_id -> clave simétrica activa
CP_KEYS: Dict[str, str] = {}
# cp_id -> alert info
WEATHER_ALERTS: Dict[str, Dict[str, Any]] = {}

ACTIVE_SESSIONS: Dict[tuple, int] = {}
drivers_state: Dict[str, Dict[str, Any]] = {}
STALE_THRESHOLD = 15
DRIVER_STALE = 300


def now_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_initial_state():
    with db_lock:
        for row in conn.execute(
            "SELECT cp_id, state, last_update, location, registry_state, weather_status, "
            "last_weather_update, auth_state FROM charging_points"
        ):
            CP_STATE[row["cp_id"]] = {
                "status": row["state"] or "unknown",
                "last_seen": datetime.fromisoformat(row["last_update"]).timestamp()
                if row["last_update"]
                else None,
                "location": row["location"],
                "registry_state": row["registry_state"] or "PENDIENTE",
                "auth_state": row["auth_state"],
            }
        for row in conn.execute(
            "SELECT cp_id, symmetric_key FROM cp_security_keys WHERE active=1"
        ):
            CP_KEYS[row["cp_id"]] = row["symmetric_key"]
        for row in conn.execute(
            "SELECT cp_id, city, temperature, updated_at FROM weather_alerts WHERE active=1"
        ):
            WEATHER_ALERTS[row["cp_id"]] = {
                "city": row["city"],
                "temperature": row["temperature"],
                "updated_at": row["updated_at"],
            }
    print(f"[CENTRAL] Estado inicial: {len(CP_STATE)} CPs, {len(CP_KEYS)} claves activas.")


def log_event(
    cp_id: Optional[str],
    event_type: str,
    description: str,
    actor: str = "central",
    origin: Optional[str] = None,
):
    with db_lock:
        conn.execute(
            "INSERT INTO events(timestamp, cp_id, event_type, description, actor, origin_ip) "
            "VALUES(?,?,?,?,?,?)",
            (now_ts(), cp_id, event_type, description, actor, origin),
        )
        conn.commit()


def ensure_cp_entry(cp_id: str) -> Dict[str, Any]:
    entry = CP_STATE.setdefault(
        cp_id,
        {
            "status": "unknown",
            "last_seen": None,
            "location": None,
            "registry_state": "PENDIENTE",
            "auth_state": "NO_AUTENTICADO",
        },
    )
    return entry


def update_charging_point(cp_id: str, **kwargs):
    entry = ensure_cp_entry(cp_id)
    entry.update(kwargs)
    with db_lock:
        conn.execute(
            """
            INSERT INTO charging_points(cp_id, state, last_update, total_charges, location, registry_state, auth_state)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(cp_id) DO UPDATE SET
                state=excluded.state,
                last_update=excluded.last_update,
                location=excluded.location,
                registry_state=excluded.registry_state,
                auth_state=COALESCE(excluded.auth_state, charging_points.auth_state)
            """,
            (
                cp_id,
                entry.get("status", "unknown"),
                now_ts(),
                0,
                entry.get("location"),
                entry.get("registry_state"),
                entry.get("auth_state"),
            ),
        )
        conn.commit()


def refresh_registry_snapshot():
    with db_lock:
        for row in conn.execute(
            "SELECT cp_id, location, registered, active FROM cp_registry"
        ):
            entry = ensure_cp_entry(row["cp_id"])
            entry["registry_state"] = "REGISTRADO" if row["registered"] else "BAJA"
            entry["location"] = row["location"]


def mark_cp_seen(cp_id: str, status: str):
    entry = ensure_cp_entry(cp_id)
    entry["last_seen"] = time.time()
    entry["status"] = status
    update_charging_point(cp_id, status=status)


def weather_blocked(cp_id: str) -> bool:
    return cp_id in WEATHER_ALERTS


def cp_has_key(cp_id: str) -> bool:
    return cp_id in CP_KEYS


def cp_is_registered(cp_id: str) -> bool:
    entry = ensure_cp_entry(cp_id)
    return entry.get("registry_state") == "REGISTRADO"


def cp_is_connected(cp_id: str) -> bool:
    entry = ensure_cp_entry(cp_id)
    last_seen = entry.get("last_seen")
    return bool(last_seen and (time.time() - last_seen) <= STALE_THRESHOLD)


def cp_available(cp_id: str) -> (bool, str):
    if not cp_is_registered(cp_id):
        return False, "CP_no_registrado"
    if not cp_has_key(cp_id):
        return False, "CP_no_autenticado"
    if weather_blocked(cp_id):
        return False, "CP_bloqueado_meteo"
    if not cp_is_connected(cp_id):
        return False, "CP_desconectado"
    entry = ensure_cp_entry(cp_id)
    if entry.get("status") == "faulty":
        return False, "CP_averiado"
    return True, "OK"


def load_active_keys():
    with db_lock:
        rows = conn.execute(
            "SELECT cp_id, symmetric_key FROM cp_security_keys WHERE active=1"
        ).fetchall()
    for row in rows:
        CP_KEYS[row["cp_id"]] = row["symmetric_key"]


def ensure_driver_state(driver_id: str, state: str, cp_id: Optional[str]):
    drivers_state[driver_id] = {"state": state, "cp_id": cp_id, "last_update": time.time()}


def decrypt_cp_message(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if "ciphertext" not in raw:
        return raw
    cp_id = raw.get("cp_id")
    if not cp_id:
        return None
    key = CP_KEYS.get(cp_id)
    if not key:
        log_event(cp_id, "DECRYPT_FAIL", "Mensaje cifrado sin clave activa", "cp")
        return None
    try:
        payload = decrypt_payload(key, raw["ciphertext"])
        payload.setdefault("cp_id", cp_id)
        return payload
    except Exception as exc:
        log_event(cp_id, "DECRYPT_FAIL", f"Error descifrando: {exc}", "cp")
        return None


def send_command(cp_id: str, payload: Dict[str, Any]) -> bool:
    key = CP_KEYS.get(cp_id)
    if not key:
        log_event(cp_id, "COMMAND_FAIL", "Sin clave activa para enviar comando", "central")
        return False
    message = {"cp_id": cp_id, "ciphertext": encrypt_payload(key, payload)}
    producer.send(TOPIC_CP_COMMANDS, message)
    producer.flush()
    return True


def broadcast_command(payload: Dict[str, Any]):
    for cp_id in list(CP_KEYS.keys()):
        send_command(cp_id, payload)


def start_session(cp_id: str, driver_id: str) -> int:
    with db_lock:
        cur = conn.execute(
            "INSERT INTO charging_sessions(cp_id, driver_id, start_time, state) VALUES(?,?,?,?)",
            (cp_id, driver_id, now_ts(), "running"),
        )
        conn.commit()
        return cur.lastrowid


def update_session(session_id: int, energy=None, cost=None, end=False, cancelled=False, fault=False):
    with db_lock:
        fields = []
        params = []
        if energy is not None:
            fields.append("energy_kwh=?")
            params.append(energy)
        if cost is not None:
            fields.append("cost=?")
            params.append(cost)
        if end:
            fields.append("end_time=?")
            params.append(now_ts())
            if cancelled:
                fields.append("state=?")
                params.append("cancelled")
            elif fault:
                fields.append("state=?")
                params.append("fault")
            else:
                fields.append("state=?")
                params.append("finished")
        if not fields:
            return
        params.append(session_id)
        conn.execute(f"UPDATE charging_sessions SET {', '.join(fields)} WHERE session_id=?", params)
        conn.commit()


# ---------------------------------------------------------------------------
# Kafka handlers
# ---------------------------------------------------------------------------


def handle_driver_messages():
    print("[CENTRAL] Driver consumer funcionando.")
    for msg in consumer_driver:
        data = msg.value
        mtype = data.get("type")
        if mtype == "REQUEST_CHARGE":
            driver_id = data.get("driver_id")
            cp_id = data.get("cp_id")
            ensure_driver_state(driver_id, "requesting", cp_id)
            available, reason = cp_available(cp_id)
            if not available:
                resp = {"type": "AUTH_DENIED", "driver_id": driver_id, "reason": reason}
                producer.send(TOPIC_DRIVER, resp)
                producer.flush()
                ensure_driver_state(driver_id, "idle", None)
                log_event(cp_id, "AUTH_DENIED", f"{driver_id} -> {reason}", driver_id)
                continue
            ensure_driver_state(driver_id, "authorized", cp_id)
            session_id = ACTIVE_SESSIONS.get((cp_id, driver_id))
            if not session_id:
                session_id = start_session(cp_id, driver_id)
                ACTIVE_SESSIONS[(cp_id, driver_id)] = session_id
            cmd = {"type": "START_CHARGE", "cp_id": cp_id, "driver_id": driver_id, "timestamp": time.time()}
            if send_command(cp_id, cmd):
                resp = {"type": "AUTH_OK", "driver_id": driver_id, "cp_id": cp_id}
                producer.send(TOPIC_DRIVER, resp)
                producer.flush()
                log_event(cp_id, "AUTH_OK", f"Driver {driver_id} autorizado", driver_id)
            else:
                resp = {"type": "AUTH_DENIED", "driver_id": driver_id, "reason": "CP_sin_clave"}
                producer.send(TOPIC_DRIVER, resp)
                producer.flush()
            ensure_driver_state(driver_id, "idle", None)
        elif mtype == "CANCEL_CHARGE":
            driver_id = data.get("driver_id")
            cp_id = data.get("cp_id")
            cmd = {"type": "CANCEL_CHARGE", "driver_id": driver_id, "cp_id": cp_id, "timestamp": time.time()}
            if cp_id:
                send_command(cp_id, cmd)
            log_event(cp_id or "ALL", "CANCEL", f"Cancel solicitado por {driver_id}", driver_id)
            ensure_driver_state(driver_id, "idle", None)
        elif mtype == "DRIVER_HELLO":
            driver_id = data.get("driver_id")
            cp_id = data.get("cp_id")
            state = data.get("state", "idle")
            ensure_driver_state(driver_id, state, cp_id)


def process_cp_payload(data: Dict[str, Any]):
    cp_id = data.get("cp_id")
    mtype = data.get("type")
    if not cp_id or not mtype:
        return
    if mtype == "REGISTER":
        mark_cp_seen(cp_id, "ok")
        log_event(cp_id, "CP_REGISTER", "Registro recibido", cp_id)
    elif mtype in ("HEARTBEAT", "STATUS"):
        status = data.get("status", "ok")
        mark_cp_seen(cp_id, status)
    elif mtype == "CHARGE_DATA":
        driver_id = data.get("driver_id")
        session_id = ACTIVE_SESSIONS.get((cp_id, driver_id))
        if not session_id:
            session_id = start_session(cp_id, driver_id)
            ACTIVE_SESSIONS[(cp_id, driver_id)] = session_id
        update_session(session_id, energy=data.get("energy"), cost=data.get("cost"))
        if driver_id:
            ensure_driver_state(driver_id, "charging", cp_id)
            update = {
                "type": "CHARGING_UPDATE",
                "driver_id": driver_id,
                "info": {"kwh": data.get("energy"), "cost": data.get("cost")},
            }
            producer.send(TOPIC_DRIVER, update)
            producer.flush()
    elif mtype == "CHARGE_COMPLETE":
        driver_id = data.get("driver_id")
        session_id = ACTIVE_SESSIONS.pop((cp_id, driver_id), None)
        update_session(
            session_id,
            energy=data.get("energy"),
            cost=data.get("cost"),
            end=True,
            cancelled=data.get("cancelled", False),
        ) if session_id else None
        ticket = {
            "cp_id": cp_id,
            "energy": data.get("energy"),
            "cost": data.get("cost"),
            "timestamp": time.time(),
        }
        if driver_id:
            finish = {"type": "FINISHED", "driver_id": driver_id, "ticket": ticket}
            producer.send(TOPIC_DRIVER, finish)
            producer.flush()
            ensure_driver_state(driver_id, "idle", None)
        log_event(cp_id, "CHARGE_COMPLETE", f"Driver {driver_id} energía {data.get('energy')}")
    elif mtype == "FAULT":
        mark_cp_seen(cp_id, "faulty")
        log_event(cp_id, "FAULT", "Reporte de avería", cp_id)
        broadcast = {"type": "BROADCAST", "message": f"CP {cp_id} reported FAULT"}
        producer.send(TOPIC_DRIVER, broadcast)
        producer.flush()
        for (cp, drv), sid in list(ACTIVE_SESSIONS.items()):
            if cp == cp_id:
                update_session(sid, end=True, fault=True)
                ACTIVE_SESSIONS.pop((cp, drv), None)
                err = {"type": "CP_ERROR", "driver_id": drv, "error": "CP_fault"}
                producer.send(TOPIC_DRIVER, err)
                producer.flush()
                drivers_state[drv] = {"state": "error", "cp_id": None}
    else:
        log_event(cp_id, "UNKNOWN", f"Tipo desconocido {mtype}", cp_id)


def handle_cp_messages():
    print("[CENTRAL] CP consumer started.")
    for msg in consumer_cp:
        data = decrypt_cp_message(msg.value)
        if not data:
            continue
        process_cp_payload(data)


# ---------------------------------------------------------------------------
# API REST
# ---------------------------------------------------------------------------

app = FastAPI(title="EVCharging Central API", version="2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def snapshot_data():
    cps = []
    now = time.time()
    active_by_cp = {cp_id: drv_id for (cp_id, drv_id) in ACTIVE_SESSIONS.keys()}
    with db_lock:
        sessions_rows = conn.execute(
            "SELECT session_id, cp_id, driver_id, energy_kwh, cost, state FROM charging_sessions ORDER BY session_id DESC LIMIT 50"
        ).fetchall()
    for cp_id, info in CP_STATE.items():
        status = info.get("status", "unknown")
        last_seen = info.get("last_seen")
        stale = last_seen and now - last_seen > STALE_THRESHOLD
        readable_status = "DESCONECTADO"
        if status == "faulty":
            readable_status = "AVERIA"
        elif not stale:
            readable_status = "CONECTADO"
            if cp_id in active_by_cp:
                readable_status = "CARGANDO"
        weather = WEATHER_ALERTS.get(cp_id)
        cps.append(
            {
                "cp_id": cp_id,
                "status": readable_status,
                "last_seen": last_seen,
                "last_seen_delta": (now - last_seen) if last_seen else None,
                "driver_id": active_by_cp.get(cp_id),
                "location": info.get("location"),
                "registry_state": info.get("registry_state"),
                "auth_state": info.get("auth_state"),
                "encryption": "OK" if cp_has_key(cp_id) else "PENDIENTE",
                "weather": weather,
            }
        )
    drivers = []
    stale_cutoff = now - DRIVER_STALE
    for drv, info in list(drivers_state.items()):
        if info.get("last_update", 0) < stale_cutoff:
            drivers_state.pop(drv, None)
            continue
        d_state = info.get("state", "idle")
        state_label = "DESCONECTADO"
        if d_state == "charging":
            state_label = "CARGANDO"
        elif d_state in ("authorized", "requesting", "idle"):
            state_label = "CONECTADO"
        elif d_state == "error":
            state_label = "ERROR"
        drivers.append({"driver_id": drv, "state": state_label, "cp_id": info.get("cp_id")})
    active_sessions = []
    for (cp_id, drv_id), sid in ACTIVE_SESSIONS.items():
        with db_lock:
            row = conn.execute(
                "SELECT session_id, cp_id, driver_id, energy_kwh, cost, state FROM charging_sessions WHERE session_id=?",
                (sid,),
            ).fetchone()
        if row:
            active_sessions.append(dict(row))
    sessions = [dict(row) for row in sessions_rows]
    with db_lock:
        events_rows = conn.execute(
            "SELECT event_id, timestamp, cp_id, event_type, description, actor, origin_ip FROM events ORDER BY event_id DESC LIMIT 40"
        ).fetchall()
    events = [dict(row) for row in events_rows]
    return {
        "cps": cps,
        "drivers": drivers,
        "sessions": sessions,
        "active_sessions": active_sessions,
        "events": events,
    }


@app.get("/snapshot")
def snapshot():
    return snapshot_data()


@app.post("/command")
def command(payload: dict):
    action = payload.get("action")
    cp_id = payload.get("cp_id")
    if action not in ("pause", "resume", "stop", "cancel", "start"):
        return {"status": "error", "message": "acción no soportada"}
    if action in ("pause", "resume", "stop"):
        target = cp_id
        cmd_type = {"pause": "PAUSE_CHARGE", "resume": "RESUME_CHARGE", "stop": "STOP_CHARGE"}[action]
        if payload.get("scope") == "all":
            broadcast_command({"type": cmd_type, "timestamp": time.time()})
        else:
            if not cp_id:
                return {"status": "error", "message": "cp_id requerido"}
            send_command(cp_id, {"type": cmd_type, "cp_id": cp_id, "timestamp": time.time()})
        log_event(cp_id or "ALL", cmd_type, "Enviado via API")
        return {"status": "ok"}
    if action == "cancel":
        driver_id = payload.get("driver_id")
        cmd = {"type": "CANCEL_CHARGE", "driver_id": driver_id, "cp_id": cp_id, "timestamp": time.time()}
        if cp_id:
            send_command(cp_id, cmd)
        log_event(cp_id or "ALL", "CANCEL", "Enviado via API")
        if driver_id:
            ensure_driver_state(driver_id, "idle", None)
        return {"status": "ok"}
    if action == "start":
        driver_id = payload.get("driver_id")
        if not driver_id or not cp_id:
            return {"status": "error", "message": "driver_id y cp_id requeridos"}
        available, reason = cp_available(cp_id)
        if not available:
            return {"status": "error", "message": reason}
        session_id = ACTIVE_SESSIONS.get((cp_id, driver_id))
        if not session_id:
            session_id = start_session(cp_id, driver_id)
            ACTIVE_SESSIONS[(cp_id, driver_id)] = session_id
        ensure_driver_state(driver_id, "authorized", cp_id)
        send_command(cp_id, {"type": "START_CHARGE", "cp_id": cp_id, "driver_id": driver_id, "timestamp": time.time()})
        log_event(cp_id, "AUTH_OK", f"Driver {driver_id} autorizado vía API")
        return {"status": "ok"}
    return {"status": "error", "message": "acción no soportada"}


@app.get("/events")
def list_events():
    with db_lock:
        rows = conn.execute(
            "SELECT event_id, timestamp, cp_id, event_type, description, actor, origin_ip FROM events ORDER BY event_id DESC LIMIT 100"
        ).fetchall()
    return {"events": [dict(row) for row in rows]}


@app.post("/auth/cp/login")
def cp_login(payload: dict, request: Request):
    cp_id = payload.get("cp_id", "").strip().upper()
    token = payload.get("token")
    if not cp_id or not token:
        raise HTTPException(status_code=400, detail="cp_id y token requeridos")
    with db_lock:
        row = conn.execute("SELECT * FROM cp_registry WHERE cp_id=?", (cp_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="CP no registrado")
    if not row["active"] or not row["registered"]:
        raise HTTPException(status_code=403, detail="CP no activo")
    token_hash = row["last_token_hash"]
    token_salt = row["last_token_salt"]
    token_issued_at = row["last_token_issued_at"]
    if not token_hash or not token_salt or not token_issued_at:
        raise HTTPException(status_code=400, detail="No hay token pendiente en registry")
    issued_dt = datetime.fromisoformat(token_issued_at)
    if issued_dt < datetime.now(timezone.utc) - timedelta(seconds=REGISTRY_TOKEN_TTL):
        raise HTTPException(status_code=401, detail="Token caducado")
    if not verify_secret(token, token_hash, token_salt):
        log_event(cp_id, "CP_AUTH_FAIL", "Token inválido", cp_id, request.client.host if request.client else None)
        raise HTTPException(status_code=401, detail="Token inválido")
    entry = ensure_cp_entry(cp_id)
    entry["location"] = row["location"]
    entry["registry_state"] = "REGISTRADO"
    key, version = issue_cp_key(cp_id)
    log_event(cp_id, "CP_AUTH_OK", f"Clave v{version} emitida", cp_id, request.client.host if request.client else None)
    return {"cp_id": cp_id, "symmetric_key": key, "version": version}


def issue_cp_key(cp_id: str):
    key = generate_symmetric_key()
    issued_at = now_ts()
    with db_lock:
        conn.execute("UPDATE cp_security_keys SET active=0 WHERE cp_id=?", (cp_id,))
        cur = conn.execute("SELECT COALESCE(MAX(version),0) FROM cp_security_keys WHERE cp_id=?", (cp_id,))
        next_version = (cur.fetchone()[0] or 0) + 1
        conn.execute(
            "INSERT INTO cp_security_keys(cp_id, symmetric_key, issued_at, active, version) VALUES(?,?,?,?,?)",
            (cp_id, key, issued_at, 1, next_version),
        )
        conn.execute(
            "UPDATE cp_registry SET last_token=NULL, last_token_hash=NULL, last_token_salt=NULL, last_token_issued_at=NULL, last_auth_ts=? WHERE cp_id=?",
            (issued_at, cp_id),
        )
        conn.execute("UPDATE charging_points SET auth_state='AUTHENTICADO' WHERE cp_id=?", (cp_id,))
        conn.commit()
    CP_KEYS[cp_id] = key
    entry = ensure_cp_entry(cp_id)
    entry["auth_state"] = "AUTHENTICADO"
    entry["registry_state"] = "REGISTRADO"
    update_charging_point(cp_id, status=entry.get("status", "ok"))
    return key, next_version


@app.post("/auth/cp/revoke")
def cp_revoke(payload: dict):
    cp_id = payload.get("cp_id", "").strip().upper()
    if not cp_id:
        raise HTTPException(status_code=400, detail="cp_id requerido")
    revoked = revoke_cp_key(cp_id)
    if not revoked:
        raise HTTPException(status_code=404, detail="No existe clave activa")
    log_event(cp_id, "CP_KEY_REVOKE", "Clave revocada vía API")
    return {"status": "ok"}


def revoke_cp_key(cp_id: str) -> bool:
    if cp_id not in CP_KEYS:
        return False
    CP_KEYS.pop(cp_id, None)
    with db_lock:
        conn.execute("UPDATE cp_security_keys SET active=0 WHERE cp_id=?", (cp_id,))
        conn.execute("UPDATE charging_points SET auth_state='NO_AUTENTICADO' WHERE cp_id=?", (cp_id,))
        conn.commit()
    ensure_cp_entry(cp_id)["auth_state"] = "NO_AUTENTICADO"
    return True


@app.post("/weather/alert")
def weather_alert(payload: dict, request: Request):
    cp_id = payload.get("cp_id", "").strip().upper()
    if not cp_id:
        raise HTTPException(status_code=400, detail="cp_id requerido")
    status = str(payload.get("status", "alert")).lower()
    city = payload.get("city")
    temperature = payload.get("temperature")
    updated_at = now_ts()
    if status == "alert":
        WEATHER_ALERTS[cp_id] = {"city": city, "temperature": temperature, "updated_at": updated_at}
        with db_lock:
            conn.execute(
                "INSERT INTO weather_alerts(cp_id, city, temperature, active, updated_at, extra) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(cp_id) DO UPDATE SET "
                "city=excluded.city, temperature=excluded.temperature, active=1, updated_at=excluded.updated_at",
                (cp_id, city, temperature, 1, updated_at, json.dumps(payload)),
            )
            conn.execute(
                "UPDATE charging_points SET weather_status='ALERTA', last_weather_update=? WHERE cp_id=?",
                (updated_at, cp_id),
            )
            conn.commit()
        log_event(cp_id, "WEATHER_ALERT", f"{city} temp={temperature}", payload.get("source", "ev_w"), request.client.host if request.client else None)
        send_command(cp_id, {"type": "STOP_CHARGE", "cp_id": cp_id, "reason": "WEATHER_ALERT", "timestamp": time.time()})
    else:
        WEATHER_ALERTS.pop(cp_id, None)
        with db_lock:
            conn.execute(
                "UPDATE weather_alerts SET active=0, updated_at=?, extra=? WHERE cp_id=?",
                (updated_at, json.dumps(payload), cp_id),
            )
            conn.execute(
                "UPDATE charging_points SET weather_status='OK', last_weather_update=? WHERE cp_id=?",
                (updated_at, cp_id),
            )
            conn.commit()
        log_event(cp_id, "WEATHER_CLEAR", f"Fin alerta ({city})", payload.get("source", "ev_w"), request.client.host if request.client else None)
    return {"status": "ok"}


def start_api():
    def run():
        host = CENTRAL_API_CFG.get("host", "0.0.0.0")
        port = int(CENTRAL_API_CFG.get("port", 8000))
        uvicorn.run(app, host=host, port=port, log_level="warning")

    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------------------
# Consola administrativa
# ---------------------------------------------------------------------------


def admin_console():
    def print_menu():
        print("\n" + "=" * 60)
        print("Central - Menú admin")
        print("=" * 60)
        print("Comandos:")
        print("  list                     - Listar CPs conocidos")
        print("  list-drivers             - Listar drivers activos")
        print("  revoke <cp>              - Revocar clave simétrica del CP")
        print("  pause <cp|all>           - Pausar carga(s)")
        print("  resume <cp|all>          - Reanudar carga(s)")
        print("  stop <cp|all>            - Detener carga(s)")
        print("  q                        - Salir")
        print("=" * 60)

    print_menu()
    while True:
        try:
            cmd = input("[CENTRAL] > ").strip().lower()
        except EOFError:
            break
        if not cmd:
            continue
        parts = cmd.split()
        action = parts[0]
        if action == "list":
            for cp_id, info in CP_STATE.items():
                last_seen = info.get("last_seen")
                delta_txt = f"{time.time()-last_seen:.1f}s ago" if last_seen else "never"
                print(
                    f"{cp_id}: status={info.get('status')} registry={info.get('registry_state')} "
                    f"auth={'yes' if cp_has_key(cp_id) else 'no'} last_seen={delta_txt}"
                )
        elif action == "list-drivers":
            now = time.time()
            if not drivers_state:
                print("Sin drivers activos")
            for drv, info in drivers_state.items():
                lu = info.get("last_update")
                delta = f"{now-lu:.1f}s ago" if lu else "n/a"
                print(f"{drv}: state={info.get('state')} cp={info.get('cp_id')} last_upd={delta}")
        elif action in ("pause", "resume", "stop"):
            target = None if len(parts) == 1 or parts[1] == "all" else parts[1].upper()
            mtype = {"pause": "PAUSE_CHARGE", "resume": "RESUME_CHARGE", "stop": "STOP_CHARGE"}[action]
            if target:
                send_command(target, {"type": mtype, "cp_id": target, "timestamp": time.time()})
            else:
                broadcast_command({"type": mtype, "timestamp": time.time()})
            log_event(target or "ALL", mtype, "Enviado desde consola")
            print(f"Enviado {mtype} a {target or 'ALL'}")
        elif action == "revoke":
            if len(parts) < 2:
                print("Uso: revoke <CP_ID>")
                continue
            cp = parts[1].upper()
            if revoke_cp_key(cp):
                log_event(cp, "CP_KEY_REVOKE", "Revocada desde consola")
                print(f"Clave de {cp} revocada.")
            else:
                print(f"No existe clave activa para {cp}.")
        elif action in ("q", "quit", "exit"):
            break
        else:
            print("Comando no reconocido.")
        print_menu()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    load_initial_state()
    refresh_registry_snapshot()
    load_active_keys()
    t1 = threading.Thread(target=handle_driver_messages, daemon=True)
    t2 = threading.Thread(target=handle_cp_messages, daemon=True)
    t3 = threading.Thread(target=admin_console, daemon=True)
    t1.start()
    t2.start()
    t3.start()
    start_api()
    print("[CENTRAL] EV_Central running. Ctrl+C para salir.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("[CENTRAL] Shutting down.")


if __name__ == "__main__":
    main()
