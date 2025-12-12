"""
EV_CP_M.py - Monitor del CP (Release 2)
- Gestiona el ciclo de registro/autenticación contra EV_Registry y EV_Central.
- Guarda las credenciales locales en config/credentials/<CP>.json.
- Envía eventos/averías cifrados a través de Kafka y escucha el estado del Engine.
"""

import json
import os
import threading
import time
from datetime import datetime

import requests
import yaml
from kafka import KafkaConsumer, KafkaProducer
from kafka.errors import KafkaError, NoBrokersAvailable

from Extras.credentials import CredentialStore
from Extras.security import decrypt_payload, encrypt_payload
from Extras.utils import resolve_broker

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
with open(os.path.join(ROOT, "config", "config.yaml"), "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

BROKER = resolve_broker(config, "CP_MONITOR")
TOPIC_CP_STATUS = config["kafka"]["topic_cp_status"]
TOPIC_CP_DATA = config["kafka"]["topic_cp_data"]
REGISTRY_BASE = config.get("registry", {}).get("base_url", "https://localhost:8443").rstrip("/")
CENTRAL_BASE = config.get("central_api", {}).get("base_url", "http://localhost:8000").rstrip("/")


def resolve_path(path: str) -> str:
    if not path:
        return ""
    return path if os.path.isabs(path) else os.path.join(ROOT, path)


REGISTRY_VERIFY = (
    False
    if os.getenv("CP_INSECURE_REGISTRY", "0") == "1"
    else resolve_path(config.get("registry", {}).get("cert_file", ""))
)
if REGISTRY_VERIFY and not os.path.exists(REGISTRY_VERIFY):
    REGISTRY_VERIFY = True


class Monitor:
    def __init__(self, cp_id: str):
        self.cp_id = cp_id.upper()
        self.cred_store = CredentialStore(config)
        self.credentials = self.cred_store.load(self.cp_id)
        self._cred_mtime = self.cred_store.mtime(self.cp_id)
        self.symmetric_key = self.credentials.get("symmetric_key")
        self.producer = None
        self.consumer = None
        self._kafka_lock = threading.Lock()
        self.last_heartbeat = None
        self.engine_state = "unknown"
        self._start_ts = time.time()
        self._fault_sent = False
        self._stop = threading.Event()
        self._connect_kafka()
        self._start_credential_watcher()
        # Registro inicial (se enviará cifrado si hay key)
        self._publish_status({"type": "REGISTER", "cp_id": self.cp_id, "info": {"monitor": "cp_monitor"}})

    def _connect_kafka(self):
        with self._kafka_lock:
            while True:
                try:
                    print(f"[Monitor {self.cp_id}] Conectando con Kafka en {BROKER}...")
                    self.producer = KafkaProducer(
                        bootstrap_servers=BROKER,
                        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
                    )
                    self.consumer = KafkaConsumer(
                        TOPIC_CP_STATUS,
                        TOPIC_CP_DATA,
                        bootstrap_servers=BROKER,
                        value_deserializer=lambda v: json.loads(v.decode("utf-8")),
                        group_id=f"monitor_{self.cp_id}",
                        auto_offset_reset="earliest",
                        consumer_timeout_ms=1000,
                    )
                    print(f"[Monitor {self.cp_id}] Kafka conectado.")
                    break
                except (KafkaError, NoBrokersAvailable) as e:
                    print(f"[Monitor {self.cp_id}] Error conectando a Kafka: {e}. Reintentando en 2s...")
                    time.sleep(2)

    def _start_credential_watcher(self):
        threading.Thread(target=self._credentials_watchdog, daemon=True).start()

    def _credentials_watchdog(self):
        while not self._stop.is_set():
            data, mtime = self.cred_store.load_if_changed(self.cp_id, self._cred_mtime)
            if data:
                self._cred_mtime = mtime
                self.credentials = data
                new_key = data.get("symmetric_key")
                if new_key and new_key != self.symmetric_key:
                    self.symmetric_key = new_key
                    print(f"[Monitor {self.cp_id}] Nueva clave simétrica cargada.")
            time.sleep(4)

    def start(self):
        threading.Thread(target=self.listen_status, daemon=True).start()
        threading.Thread(target=self.watchdog_loop, daemon=True).start()
        try:
            self.print_menu()
            while True:
                cmd = input(f"\n[Monitor {self.cp_id}] > ").strip().lower()
                if cmd in ("status", "s"):
                    self.print_status()
                elif cmd in ("register", "1"):
                    self.register_cp()
                elif cmd in ("token", "2"):
                    self.request_token()
                elif cmd in ("auth", "3"):
                    self.authenticate_central()
                elif cmd in ("credentials", "4"):
                    self.show_credentials()
                elif cmd in ("deregister", "5"):
                    self.deregister_cp()
                elif cmd == "fault":
                    self.send_fault()
                elif cmd == "repair":
                    self.send_repair()
                elif cmd in ("q", "quit", "exit"):
                    break
                else:
                    print("Comandos: status | register | token | auth | credentials | deregister | fault | repair | q")
                self.print_menu()
        except KeyboardInterrupt:
            pass
        finally:
            self._stop.set()
        print("Monitor exiting.")

    def listen_status(self):
        while not self._stop.is_set():
            try:
                for msg in self.consumer:
                    data = self._decode_message(msg.value)
                    if not data or data.get("cp_id") != self.cp_id:
                        continue
                    mtype = data.get("type")
                    if mtype in ("HEARTBEAT", "STATUS"):
                        self.last_heartbeat = time.time()
                        if data.get("status") == "faulty":
                            self.engine_state = "faulty"
                            self._fault_sent = True
                        else:
                            self.engine_state = "ok"
                            self._fault_sent = False
                    if mtype in ("FAULT", "CHARGE_COMPLETE"):
                        print(f"[Monitor {self.cp_id}] Status message: {data}")
            except (KafkaError, NoBrokersAvailable) as e:
                print(f"[Monitor {self.cp_id}] Error en consumer Kafka: {e}. Reintentando conexión...")
                time.sleep(1)
                self._connect_kafka()
            except Exception as e:
                print(f"[Monitor {self.cp_id}] Error inesperado en listen_status: {e}")
                time.sleep(1)

    def _decode_message(self, data):
        if not isinstance(data, dict):
            return None
        if "ciphertext" in data:
            if not self.symmetric_key:
                return None
            try:
                payload = decrypt_payload(self.symmetric_key, data["ciphertext"])
                payload.setdefault("cp_id", data.get("cp_id"))
                return payload
            except Exception:
                return None
        return data

    def _publish_status(self, payload: dict):
        payload.setdefault("cp_id", self.cp_id)
        message = payload
        if self.symmetric_key:
            try:
                message = {"cp_id": self.cp_id, "ciphertext": encrypt_payload(self.symmetric_key, payload)}
            except Exception as exc:
                print(f"[Monitor {self.cp_id}] Error cifrando mensaje: {exc}")
                return
        self.producer.send(TOPIC_CP_STATUS, message)
        self.producer.flush()

    def send_fault(self):
        fault = {"type": "FAULT", "cp_id": self.cp_id, "timestamp": datetime.utcnow().isoformat()}
        self._publish_status(fault)

    def send_repair(self):
        repair = {"type": "STATUS", "cp_id": self.cp_id, "status": "ok", "timestamp": datetime.utcnow().isoformat()}
        self._publish_status(repair)

    def register_cp(self):
        location = input("Localización (Ciudad,Pais): ").strip()
        if not location:
            print("Localización requerida.")
            return
        try:
            resp = requests.post(
                f"{REGISTRY_BASE}/api/v1/registry/register",
                json={"cp_id": self.cp_id, "location": location},
                verify=REGISTRY_VERIFY,
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            self.credentials = self.cred_store.update(
                self.cp_id,
                cp_id=self.cp_id,
                location=data.get("location"),
                client_secret=data.get("client_secret"),
                metadata=data.get("metadata"),
                registry_registered_at=data.get("issued_at"),
            )
            self.symmetric_key = self.credentials.get("symmetric_key")
            print(f"[Monitor {self.cp_id}] Registrado en Registry. Secreto guardado.")
        except requests.RequestException as exc:
            print(f"[Monitor {self.cp_id}] Error registrando: {exc}")

    def request_token(self):
        secret = self.credentials.get("client_secret")
        if not secret:
            print("No hay client_secret almacenado. Regístrate primero.")
            return
        try:
            resp = requests.post(
                f"{REGISTRY_BASE}/api/v1/registry/token",
                json={"cp_id": self.cp_id, "client_secret": secret},
                verify=REGISTRY_VERIFY,
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            self.credentials = self.cred_store.update(
                self.cp_id, registry_token=data.get("token"), token_issued_at=datetime.utcnow().isoformat()
            )
            print(f"[Monitor {self.cp_id}] Token recibido. Validez {data.get('ttl')}s.")
        except requests.RequestException as exc:
            print(f"[Monitor {self.cp_id}] Error solicitando token: {exc}")

    def authenticate_central(self):
        token = self.credentials.get("registry_token")
        if not token:
            print("No hay token disponible. Solicítalo primero.")
            return
        try:
            resp = requests.post(
                f"{CENTRAL_BASE}/auth/cp/login",
                json={"cp_id": self.cp_id, "token": token},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            self.credentials = self.cred_store.update(
                self.cp_id,
                symmetric_key=data.get("symmetric_key"),
                symmetric_key_version=data.get("version"),
                registry_token=None,
                last_authenticated=datetime.utcnow().isoformat(),
            )
            self.symmetric_key = data.get("symmetric_key")
            print(f"[Monitor {self.cp_id}] Clave recibida (v{data.get('version')}).")
        except requests.RequestException as exc:
            print(f"[Monitor {self.cp_id}] Error autenticando contra Central: {exc}")

    def deregister_cp(self):
        try:
            resp = requests.post(
                f"{REGISTRY_BASE}/api/v1/registry/deregister",
                json={"cp_id": self.cp_id},
                verify=REGISTRY_VERIFY,
                timeout=10,
            )
            resp.raise_for_status()
            self.credentials = self.cred_store.update(
                self.cp_id,
                client_secret=None,
                registry_token=None,
                symmetric_key=None,
            )
            self.symmetric_key = None
            print(f"[Monitor {self.cp_id}] CP dado de baja.")
        except requests.RequestException as exc:
            print(f"[Monitor {self.cp_id}] Error dando de baja: {exc}")

    def show_credentials(self):
        safe = {k: ("***" if "secret" in k or "key" in k else v) for k, v in self.credentials.items()}
        print(f"[Monitor {self.cp_id}] Credenciales: {json.dumps(safe, indent=2, ensure_ascii=False)}")

    def watchdog_loop(self):
        while not self._stop.is_set():
            time.sleep(5)
            now = time.time()
            if self.last_heartbeat is None:
                if not self._fault_sent and (now - self._start_ts) > 15:
                    print(f"[Monitor {self.cp_id}] Sin heartbeats iniciales: enviando FAULT")
                    self.send_fault()
                    self.engine_state = "faulty"
                    self._fault_sent = True
                continue
            if now - self.last_heartbeat > 15:
                print(f"[Monitor {self.cp_id}] Heartbeat perdido: enviando FAULT")
                self.send_fault()
                self.last_heartbeat = now
                self.engine_state = "disconnected"
                self._fault_sent = True

    def compute_status(self):
        if self.engine_state == "ok":
            return "Conectado"
        if self.engine_state == "faulty":
            return "Avería"
        if self.engine_state == "disconnected":
            return "Desconectado"
        return "Desconocido"

    def print_status(self):
        status = self.compute_status()
        hb_txt = "n/a"
        if self.last_heartbeat:
            hb_txt = f"hace {time.time()-self.last_heartbeat:.1f}s"
        print(f"[Monitor {self.cp_id}] Estado: {status} | Último heartbeat: {hb_txt}")

    def print_menu(self):
        status = self.compute_status()
        print("\n" + "=" * 70)
        print(f"Monitor {self.cp_id} - Menú | Estado CP: {status}")
        print("=" * 70)
        print("Comandos:")
        print("  status        - Mostrar estado actual")
        print("  register (1)  - Registrar CP en EV_Registry")
        print("  token (2)     - Solicitar token seguro al Registry")
        print("  auth (3)      - Autenticarse contra EV_Central y recibir key")
        print("  credentials   - Mostrar credenciales almacenadas")
        print("  deregister    - Dar de baja el CP")
        print("  fault/repair  - Notificar avería o reparación a Central")
        print("  q             - Salir")
        print("=" * 70)


def main(cp_id):
    monitor = Monitor(cp_id)
    monitor.start()


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Uso: python EV_CP_M.py <CP_ID>")
        sys.exit(1)
    main(sys.argv[1])
