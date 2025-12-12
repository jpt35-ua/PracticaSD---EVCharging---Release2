"""
EV_Registry - API Rest responsable de gestionar el alta/baja de CPs y emitir
credenciales seguras para la autenticación con EV_Central.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from Extras.db import ensure_schema, get_connection
from Extras.security import (
    generate_secret,
    hash_secret,
    mask_secret,
    verify_secret,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)

with open(os.path.join(ROOT_DIR, "config", "config.yaml"), "r", encoding="utf-8") as f:
    CONFIG = yaml.safe_load(f)

REGISTRY_CFG = CONFIG.get("registry", {})
TOKEN_TTL = REGISTRY_CFG.get("token_ttl", 180)

conn = get_connection()
ensure_schema(conn)
db_lock = threading.Lock()

app = FastAPI(title="EV Registry API", version="2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def now_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_cp_id(cp_id: str) -> str:
    return cp_id.strip().upper()


def log_event(cp_id: Optional[str], event_type: str, description: str, actor: str, origin: Optional[str]):
    with db_lock:
        conn.execute(
            "INSERT INTO events(timestamp, cp_id, event_type, description, actor, origin_ip) VALUES(?,?,?,?,?,?)",
            (now_ts(), cp_id, event_type, description, actor, origin),
        )
        conn.commit()


def to_json(data: Optional[Dict[str, Any]]) -> str:
    return json.dumps(data or {}, ensure_ascii=False)


def fetch_cp(cp_id: str):
    cur = conn.execute("SELECT * FROM cp_registry WHERE cp_id=?", (cp_id,))
    return cur.fetchone()


class RegisterRequest(BaseModel):
    cp_id: str
    location: str
    metadata: Optional[Dict[str, Any]] = None


class DeregisterRequest(BaseModel):
    cp_id: str


class TokenRequest(BaseModel):
    cp_id: str
    client_secret: str


@app.post("/api/v1/registry/register")
def register_cp(payload: RegisterRequest, request: Request):
    cp_id = normalize_cp_id(payload.cp_id)
    if not cp_id:
        raise HTTPException(status_code=400, detail="cp_id requerido")
    location = payload.location.strip()
    if not location:
        raise HTTPException(status_code=400, detail="location requerido")

    client_secret = generate_secret(32)
    hashed, salt = hash_secret(client_secret)
    metadata_json = to_json(payload.metadata)
    issued_at = now_ts()
    with db_lock:
        conn.execute(
            """
            INSERT INTO cp_registry(cp_id, location, registered, active, client_secret_hash,
                                    client_secret_salt, credentials_issued_at, metadata, last_auth_ts,
                                    last_token, last_token_hash, last_token_salt, last_token_issued_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(cp_id) DO UPDATE SET
                location=excluded.location,
                registered=1,
                active=1,
                client_secret_hash=excluded.client_secret_hash,
                client_secret_salt=excluded.client_secret_salt,
                credentials_issued_at=excluded.credentials_issued_at,
                metadata=excluded.metadata
            """,
            (
                cp_id,
                location,
                1,
                1,
                hashed,
                salt,
                issued_at,
                metadata_json,
                None,
                None,
                None,
                None,
                None,
            ),
        )
        conn.execute(
            """
            INSERT INTO charging_points(cp_id, state, last_update, total_charges, location, registry_state)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(cp_id) DO UPDATE SET
                location=excluded.location,
                registry_state='REGISTRADO'
            """,
            (cp_id, "registrado", issued_at, 0, location, "REGISTRADO"),
        )
        conn.commit()
    log_event(cp_id, "REGISTRY_REGISTER", f"CP {cp_id} registrado en {location}", "registry", request.client.host if request.client else None)
    return {
        "cp_id": cp_id,
        "location": location,
        "client_secret": client_secret,
        "issued_at": issued_at,
        "metadata": payload.metadata or {},
    }


@app.post("/api/v1/registry/deregister")
def deregister_cp(payload: DeregisterRequest, request: Request):
    cp_id = normalize_cp_id(payload.cp_id)
    row = fetch_cp(cp_id)
    if not row:
        raise HTTPException(status_code=404, detail="CP no registrado")
    with db_lock:
        conn.execute(
            "UPDATE cp_registry SET registered=0, active=0 WHERE cp_id=?",
            (cp_id,),
        )
        conn.execute(
            "UPDATE charging_points SET registry_state='BAJA', state='fuera_servicio' WHERE cp_id=?",
            (cp_id,),
        )
        conn.commit()
    log_event(cp_id, "REGISTRY_DEREGISTER", f"CP {cp_id} dado de baja", "registry", request.client.host if request.client else None)
    return {"status": "ok", "cp_id": cp_id}


@app.post("/api/v1/registry/token")
def issue_token(payload: TokenRequest, request: Request):
    cp_id = normalize_cp_id(payload.cp_id)
    row = fetch_cp(cp_id)
    if not row:
        raise HTTPException(status_code=404, detail="CP no registrado")
    if not row["active"]:
        raise HTTPException(status_code=403, detail="CP desactivado")
    if not verify_secret(payload.client_secret, row["client_secret_hash"], row["client_secret_salt"]):
        log_event(cp_id, "REGISTRY_AUTH_FAIL", "Credenciales invalidas", "registry", request.client.host if request.client else None)
        raise HTTPException(status_code=401, detail="Credenciales inválidas")
    token = generate_secret(48)
    token_hash, token_salt = hash_secret(token)
    with db_lock:
        ts = now_ts()
        conn.execute(
            "UPDATE cp_registry SET last_token=?, last_token_hash=?, last_token_salt=?, last_token_issued_at=? WHERE cp_id=?",
            (mask_secret(token), token_hash, token_salt, ts, cp_id),
        )
        conn.commit()
    log_event(cp_id, "REGISTRY_TOKEN", "Token emitido", "registry", request.client.host if request.client else None)
    return {"cp_id": cp_id, "token": token, "ttl": TOKEN_TTL}


@app.get("/api/v1/registry/cp/{cp_id}")
def cp_status(cp_id: str):
    row = fetch_cp(normalize_cp_id(cp_id))
    if not row:
        raise HTTPException(status_code=404, detail="CP no encontrado")
    metadata = json.loads(row["metadata"]) if row["metadata"] else {}
    return {
        "cp_id": row["cp_id"],
        "location": row["location"],
        "registered": bool(row["registered"]),
        "active": bool(row["active"]),
        "metadata": metadata,
        "last_auth_ts": row["last_auth_ts"],
        "last_token": row["last_token"],
        "last_token_issued_at": row["last_token_issued_at"],
    }


@app.get("/api/v1/registry/cps")
def list_cps():
    rows = conn.execute("SELECT cp_id, location, registered, active, metadata, last_auth_ts, last_token_issued_at FROM cp_registry ORDER BY cp_id").fetchall()
    data = []
    for row in rows:
        data.append(
            {
                "cp_id": row["cp_id"],
                "location": row["location"],
                "registered": bool(row["registered"]),
                "active": bool(row["active"]),
                "metadata": json.loads(row["metadata"]) if row["metadata"] else {},
                "last_auth_ts": row["last_auth_ts"],
                "last_token_issued_at": row["last_token_issued_at"],
            }
        )
    return {"items": data}


def main():
    host = REGISTRY_CFG.get("host", "0.0.0.0")
    port = int(REGISTRY_CFG.get("port", 8443))
    cert = REGISTRY_CFG.get("cert_file")
    key = REGISTRY_CFG.get("key_file")
    disable_tls = os.getenv("REGISTRY_DISABLE_TLS", "0") == "1"
    uvicorn_kwargs = {"host": host, "port": port}
    if not disable_tls and cert and key:
        cert_path = os.path.join(ROOT_DIR, cert) if not os.path.isabs(cert) else cert
        key_path = os.path.join(ROOT_DIR, key) if not os.path.isabs(key) else key
        uvicorn_kwargs["ssl_certfile"] = cert_path
        uvicorn_kwargs["ssl_keyfile"] = key_path
    else:
        print("[REGISTRY] Ejecutando SIN TLS (solo para pruebas).")
    uvicorn.run(app, **uvicorn_kwargs)


if __name__ == "__main__":
    main()
