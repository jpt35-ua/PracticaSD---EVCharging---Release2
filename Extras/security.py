"""
Utilidades de seguridad para EVCharging Release 2.

Incluye generación/verificación de secretos, cifrado simétrico con Fernet y
helpers para firmar tokens temporales. Central, Registry y los CPs comparten
estas rutinas para garantizar una implementación homogénea.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any, Dict, Tuple

from cryptography.fernet import Fernet, InvalidToken


# -- Credenciales -----------------------------------------------------------

def generate_secret(length: int = 32) -> str:
    """
    Devuelve un secreto hexadecimal de longitud `length`.
    Se usa tanto para credenciales de CP como para tokens efímeros.
    """
    size = max(16, length)
    return secrets.token_hex(size // 2)


def hash_secret(secret: str, salt: str | None = None) -> Tuple[str, str]:
    """
    Aplica PBKDF2-HMAC-SHA256 al secreto y devuelve (hash, salt) en base64.
    Si no se aporta salt se genera automáticamente.
    """
    if salt is None:
        salt_bytes = secrets.token_bytes(16)
    else:
        salt_bytes = base64.b64decode(salt)
    dk = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt_bytes, 390000)
    hashed = base64.b64encode(dk).decode("ascii")
    if salt is None:
        salt = base64.b64encode(salt_bytes).decode("ascii")
    return hashed, salt


def verify_secret(secret: str, hashed: str, salt: str) -> bool:
    """Comprueba si el secreto coincide con el hash almacenado."""
    candidate, _ = hash_secret(secret, salt)
    return hmac.compare_digest(candidate, hashed)


# -- Tokens temporales ------------------------------------------------------

def generate_token(payload: Dict[str, Any], secret: str, ttl_seconds: int = 60) -> str:
    """
    Genera un token firmado HMAC-SHA256 auto contenible.
    payload -> diccionario con los datos a codificar (ej. cp_id, nonce)
    secret -> clave de firma (string)
    ttl_seconds -> segundos de validez
    """
    envelope = payload.copy()
    envelope["exp"] = int(time.time()) + ttl_seconds
    content = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), content, hashlib.sha256).digest()
    token = base64.urlsafe_b64encode(content).decode("ascii") + "." + base64.urlsafe_b64encode(signature).decode("ascii")
    return token


def verify_token(token: str, secret: str) -> Dict[str, Any] | None:
    """
    Verifica un token generado por generate_token. Devuelve el payload o None.
    """
    try:
        content_b64, signature_b64 = token.split(".")
        content = base64.urlsafe_b64decode(content_b64.encode("ascii"))
        expected = hmac.new(secret.encode("utf-8"), content, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, base64.urlsafe_b64decode(signature_b64.encode("ascii"))):
            return None
        payload = json.loads(content.decode("utf-8"))
        if payload.get("exp", 0) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


# -- Cifrado simétrico ------------------------------------------------------

def generate_symmetric_key() -> str:
    """Devuelve una clave Fernet codificada en base64."""
    return Fernet.generate_key().decode("ascii")


def encrypt_payload(key: str, data: Dict[str, Any]) -> str:
    """Cifra `data` (dict) y devuelve el ciphertext base64."""
    cipher = Fernet(key.encode("ascii"))
    payload = json.dumps(data).encode("utf-8")
    return cipher.encrypt(payload).decode("ascii")


def decrypt_payload(key: str, ciphertext: str) -> Dict[str, Any]:
    """Descifra un ciphertext generado por encrypt_payload."""
    cipher = Fernet(key.encode("ascii"))
    try:
        plain = cipher.decrypt(ciphertext.encode("ascii"))
        return json.loads(plain.decode("utf-8"))
    except InvalidToken as exc:
        raise ValueError("Token cifrado inválido") from exc


# -- Utilidades varias ------------------------------------------------------

def mask_secret(secret: str, keep: int = 4) -> str:
    """
    Devuelve el secreto ofuscado dejando `keep` caracteres visibles al final.
    Útil para logs.
    """
    if not secret:
        return ""
    visible = secret[-keep:]
    return "*" * max(2, len(secret) - keep) + visible


__all__ = [
    "generate_secret",
    "hash_secret",
    "verify_secret",
    "generate_token",
    "verify_token",
    "generate_symmetric_key",
    "encrypt_payload",
    "decrypt_payload",
    "mask_secret",
]
