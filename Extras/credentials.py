"""
Gestión de credenciales locales de los CPs (almacenadas en config/credentials/CPxx.json)
"""

from __future__ import annotations

import json
import os
from typing import Dict, Tuple

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CredentialStore:
    def __init__(self, config: Dict):
        base = config.get("security", {}).get("credentials_dir", "config/credentials")
        self.base_dir = base if os.path.isabs(base) else os.path.join(ROOT_DIR, base)
        os.makedirs(self.base_dir, exist_ok=True)

    def path_for(self, cp_id: str) -> str:
        return os.path.join(self.base_dir, f"{cp_id}.json")

    def load(self, cp_id: str) -> Dict:
        try:
            with open(self.path_for(cp_id), "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def save(self, cp_id: str, data: Dict):
        os.makedirs(self.base_dir, exist_ok=True)
        with open(self.path_for(cp_id), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def update(self, cp_id: str, **fields) -> Dict:
        data = self.load(cp_id)
        for key, value in fields.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        self.save(cp_id, data)
        return data

    def mtime(self, cp_id: str) -> float:
        try:
            return os.path.getmtime(self.path_for(cp_id))
        except OSError:
            return 0.0

    def load_if_changed(self, cp_id: str, last_mtime: float) -> Tuple[Dict, float]:
        current = self.mtime(cp_id)
        if current <= last_mtime:
            return {}, last_mtime
        return self.load(cp_id), current


__all__ = ["CredentialStore"]
