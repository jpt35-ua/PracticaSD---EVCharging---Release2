"""
EV_CP_E.py - Engine del CP (Release 2)
- Consume comandos cifrados desde CENTRAL.
- Envía heartbeats y telemetría cifrada usando la clave simétrica obtenida tras la autenticación.
- Comparte credenciales con el Monitor mediante archivos locales en config/credentials/.
"""

import json
import os
import threading
import time
from datetime import datetime

import yaml
from kafka import KafkaConsumer, KafkaProducer
from kafka.errors import KafkaError, NoBrokersAvailable

from Extras.credentials import CredentialStore
from Extras.security import decrypt_payload, encrypt_payload
from Extras.utils import resolve_broker

try:
    with open("config/config.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
except FileNotFoundError:
    with open("../config/config.yaml", "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

BROKER = resolve_broker(config, "CP_ENGINE")
TOPIC_CP_COMMANDS = config["kafka"]["topic_cp_commands"]
TOPIC_CP_DATA = config["kafka"]["topic_cp_data"]
TOPIC_CP_STATUS = config["kafka"]["topic_cp_status"]


class Engine:
    def __init__(self, cp_id):
        self.cp_id = cp_id.upper()
        self.cfg = config["cps"].get(self.cp_id, {})
        self.price = self.cfg.get("price_per_kwh", 0.25)
        self.heartbeat_interval = self.cfg.get("heartbeat_interval", 5)
        self.battery_capacity = config["simulation"].get("battery_capacity", 50.0)
        self.is_faulty = False
        self.is_charging = False
        self.is_paused = False
        self.current_driver = None
        self.total_energy = 0.0
        self.total_cost = 0.0
        self.session_start = None
        self.producer = None
        self.consumer = None
        self._kafka_lock = threading.Lock()
        self._stop = threading.Event()
        self.cred_store = CredentialStore(config)
        self.credentials = self.cred_store.load(self.cp_id)
        self._cred_mtime = self.cred_store.mtime(self.cp_id)
        self.symmetric_key = self.credentials.get("symmetric_key")
        self._warn_no_key = False
        self._connect_kafka()
        self._start_credential_watcher()

    def _connect_kafka(self):
        with self._kafka_lock:
            while True:
                try:
                    self.producer = KafkaProducer(
                        bootstrap_servers=BROKER,
                        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
                    )
                    self.consumer = KafkaConsumer(
                        TOPIC_CP_COMMANDS,
                        bootstrap_servers=BROKER,
                        value_deserializer=lambda v: json.loads(v.decode("utf-8")),
                        group_id=f"cp_{self.cp_id}",
                        auto_offset_reset="earliest",
                        consumer_timeout_ms=1000,
                    )
                    break
                except (KafkaError, NoBrokersAvailable) as e:
                    print(f"[{self.cp_id}] Error conectando a Kafka: {e}. Reintentando en 2s...")
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
                    self._warn_no_key = False
                    print(f"[{self.cp_id}] Nueva clave simétrica cargada.")
            time.sleep(4)

    def wait_for_key(self):
        while not self._stop.is_set() and not self.symmetric_key:
            print(f"[{self.cp_id}] Esperando clave simétrica. Autentica el CP desde el Monitor...")
            time.sleep(5)
            data, mtime = self.cred_store.load_if_changed(self.cp_id, self._cred_mtime)
            if data:
                self._cred_mtime = mtime
                self.credentials = data
                self.symmetric_key = data.get("symmetric_key")

    def start(self):
        self.wait_for_key()
        self._publish_status({"type": "REGISTER", "cp_id": self.cp_id, "info": {"price": self.price}})
        threading.Thread(target=self.listen_commands, daemon=True).start()
        threading.Thread(target=self.heartbeat_loop, daemon=True).start()
        threading.Thread(target=self.console_loop, daemon=True).start()
        print(f"[{self.cp_id}] Engine listo. Escuchando comandos y heartbeats.")
        try:
            while not self._stop.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            print(f"[{self.cp_id}] Stopping engine.")
        self._stop.set()

    def listen_commands(self):
        while not self._stop.is_set():
            try:
                for msg in self.consumer:
                    payload = self._decode_command(msg.value)
                    if not payload:
                        continue
                    target = payload.get("cp_id")
                    if target and target != self.cp_id:
                        continue
                    mtype = payload.get("type")
                    if mtype == "START_CHARGE":
                        self.start_charging(payload.get("driver_id"))
                    elif mtype == "CANCEL_CHARGE":
                        self.cancel_charging()
                    elif mtype == "STOP_CHARGE":
                        self.stop_charging()
                    elif mtype == "PAUSE_CHARGE":
                        self.pause_charging()
                    elif mtype == "RESUME_CHARGE":
                        self.resume_charging()
            except (KafkaError, NoBrokersAvailable) as e:
                print(f"[{self.cp_id}] Error en consumer Kafka: {e}. Reintentando conexión...")
                time.sleep(1)
                self._connect_kafka()
            except Exception as e:
                print(f"[{self.cp_id}] Error inesperado en listen_commands: {e}")
                time.sleep(1)

    def _decode_command(self, data):
        if not isinstance(data, dict):
            return None
        if "ciphertext" in data:
            if not self.symmetric_key:
                return None
            try:
                payload = decrypt_payload(self.symmetric_key, data["ciphertext"])
                payload.setdefault("cp_id", data.get("cp_id"))
                return payload
            except Exception as exc:
                print(f"[{self.cp_id}] Error descifrando comando: {exc}")
                return None
        return data

    def heartbeat_loop(self):
        while not self._stop.is_set():
            try:
                status = {
                    "type": "HEARTBEAT",
                    "cp_id": self.cp_id,
                    "status": ("faulty" if self.is_faulty else "ok"),
                    "timestamp": datetime.utcnow().isoformat(),
                }
                self._publish_status(status)
                time.sleep(self.heartbeat_interval)
            except (KafkaError, NoBrokersAvailable) as e:
                print(f"[{self.cp_id}] Heartbeat no enviado: {e}. Reintentando conexión...")
                time.sleep(1)
                self._connect_kafka()
            except Exception as e:
                print(f"[{self.cp_id}] Heartbeat error: {e}")
                time.sleep(2)

    def _publish_status(self, payload):
        payload.setdefault("cp_id", self.cp_id)
        self._publish(payload, TOPIC_CP_STATUS)

    def _publish_data(self, payload):
        payload.setdefault("cp_id", self.cp_id)
        self._publish(payload, TOPIC_CP_DATA)

    def _publish(self, payload, topic):
        if not self.symmetric_key:
            if not self._warn_no_key:
                print(f"[{self.cp_id}] Sin clave activa. Mensaje descartado.")
                self._warn_no_key = True
            return
        try:
            message = {"cp_id": self.cp_id, "ciphertext": encrypt_payload(self.symmetric_key, payload)}
            self.producer.send(topic, message)
            self.producer.flush()
        except Exception as exc:
            print(f"[{self.cp_id}] Error enviando mensaje cifrado: {exc}")

    def start_charging(self, driver_id):
        if self.is_faulty:
            print(f"[{self.cp_id}] Cannot start charge: faulty")
            return
        if self.is_charging:
            print(f"[{self.cp_id}] Already charging")
            return
        self.current_driver = driver_id
        self.is_charging = True
        self.total_energy = 0.0
        self.total_cost = 0.0
        self.session_start = datetime.utcnow()
        threading.Thread(target=self.charging_loop, daemon=True).start()

    def charging_loop(self):
        charge_duration = config["simulation"].get("charge_duration", 120)
        interval = config["simulation"].get("charge_interval", 1)
        energy_per_sec = self.battery_capacity / charge_duration
        while self.is_charging and not self._stop.is_set():
            if self.is_faulty:
                self.stop_charging()
                break
            if self.is_paused:
                time.sleep(1)
                continue
            energy_inc = energy_per_sec * interval
            cost_inc = energy_inc * self.price
            self.total_energy += energy_inc
            self.total_cost += cost_inc
            duration = int((datetime.utcnow() - self.session_start).total_seconds())
            data = {
                "type": "CHARGE_DATA",
                "cp_id": self.cp_id,
                "driver_id": self.current_driver,
                "energy": round(self.total_energy, 3),
                "cost": round(self.total_cost, 2),
                "duration": duration,
                "timestamp": datetime.utcnow().isoformat(),
            }
            self._publish_data(data)
            if self.total_energy >= self.battery_capacity:
                complete = {
                    "type": "CHARGE_COMPLETE",
                    "cp_id": self.cp_id,
                    "driver_id": self.current_driver,
                    "energy": round(self.total_energy, 3),
                    "cost": round(self.total_cost, 2),
                }
                self._publish_data(complete)
                self.stop_charging(send_complete=False)
                break
            time.sleep(interval)

    def stop_charging(self, send_complete=True):
        if not self.is_charging:
            return
        self.is_paused = False
        self.is_charging = False
        if send_complete and self.current_driver:
            stop_msg = {
                "type": "CHARGE_COMPLETE",
                "cp_id": self.cp_id,
                "driver_id": self.current_driver,
                "energy": round(self.total_energy, 3),
                "cost": round(self.total_cost, 2),
                "cancelled": True,
                "reason": "stopped",
            }
            self._publish_data(stop_msg)
        self.current_driver = None

    def cancel_charging(self):
        if self.is_charging:
            self.is_charging = False
            print(f"[{self.cp_id}] Charging cancelled by CENTRAL/Driver")
            cancel = {
                "type": "CHARGE_COMPLETE",
                "cp_id": self.cp_id,
                "driver_id": self.current_driver,
                "energy": round(self.total_energy, 3),
                "cost": round(self.total_cost, 2),
                "cancelled": True,
            }
            self._publish_data(cancel)
            self.current_driver = None

    def pause_charging(self):
        if self.is_charging and not self.is_paused:
            self.is_paused = True

    def resume_charging(self):
        if self.is_charging and self.is_paused:
            self.is_paused = False

    def console_loop(self):
        def print_menu():
            print("\n" + "=" * 60)
            status = "FAULTY" if self.is_faulty else ("PAUSED" if self.is_paused else ("CHARGING" if self.is_charging else "IDLE"))
            driver_txt = f" (Driver {self.current_driver})" if self.is_charging and self.current_driver else ""
            print(f"CP Engine {self.cp_id} - Menú | Estado: {status}{driver_txt}")
            print("=" * 60)
            print("Comandos:")
            print("  1 - Iniciar carga (start <driver_id>)")
            print("  2 - Pausar carga")
            print("  3 - Reanudar carga")
            print("  4 - Detener carga")
            print("  5 - Marcar fallo")
            print("  6 - Reparar (estado OK)")
            print("  7 - Mostrar estado")
            print("  q - Salir")
            print("=" * 60)

        print_menu()
        while not self._stop.is_set():
            try:
                cmd = input(f"[{self.cp_id}] > ").strip().lower()
            except EOFError:
                break
            if not cmd:
                continue
            parts = cmd.split()
            action = parts[0]
            if action == "1" or (action == "start" and len(parts) > 1):
                driver = parts[1] if len(parts) > 1 else input("Driver ID: ").strip().upper()
                self.start_charging(driver)
            elif action in ("2", "pause"):
                self.pause_charging()
            elif action in ("3", "resume"):
                self.resume_charging()
            elif action in ("4", "stop"):
                self.stop_charging()
            elif action in ("5", "fault"):
                self.is_faulty = True
                print(f"[{self.cp_id}] Marcado como faulty")
            elif action in ("6", "repair"):
                self.is_faulty = False
                print(f"[{self.cp_id}] Marcado como ok")
            elif action in ("7", "status"):
                print(
                    f"[{self.cp_id}] estado -> charging={self.is_charging} paused={self.is_paused} "
                    f"faulty={self.is_faulty} driver={self.current_driver}"
                )
            elif action in ("q", "quit", "exit"):
                self._stop.set()
                break
            else:
                print("Comando no reconocido.")
            print_menu()


def main(cp_id):
    engine = Engine(cp_id)
    engine.start()


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Uso: python EV_CP_E.py <CP_ID>")
        sys.exit(1)
    main(sys.argv[1])
