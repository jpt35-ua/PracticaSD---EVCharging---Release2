"""
Weather Control Office (EV_W)
- Consulta OpenWeather periódicamente para cada CP configurado.
- Envía alertas/recuperaciones a la Central mediante el endpoint /weather/alert.
- Permite gestionar las localizaciones manualmente a través de un menú sencillo.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime

import requests
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, "config", "config.yaml"), "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

LOCATIONS_FILE = os.path.join(ROOT, "config", "weather_locations.json")


class WeatherOffice:
    def __init__(self):
        weather_cfg = config.get("weather", {})
        self.threshold = weather_cfg.get("threshold_celsius", 0)
        self.poll_interval = weather_cfg.get("polling_interval", 4)
        self.api_key = os.getenv("OPENWEATHER_API_KEY") or weather_cfg.get("openweather_api_key")
        self.central_base = config.get("central_api", {}).get("base_url", "http://localhost:8000").rstrip("/")
        self.locations = self.load_locations(weather_cfg.get("default_cities", {}))
        self.alerts_active = {}
        self.last_temperatures = {}
        self._stop = threading.Event()
        self.session = requests.Session()

    def load_locations(self, defaults):
        if os.path.exists(LOCATIONS_FILE):
            with open(LOCATIONS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        return defaults or {}

    def save_locations(self):
        os.makedirs(os.path.dirname(LOCATIONS_FILE), exist_ok=True)
        with open(LOCATIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(self.locations, f, indent=2, ensure_ascii=False)

    def start(self):
        threading.Thread(target=self.poll_loop, daemon=True).start()
        self.menu_loop()

    def poll_loop(self):
        while not self._stop.is_set():
            if not self.locations or not self.api_key:
                time.sleep(self.poll_interval)
                continue
            for cp_id, city in list(self.locations.items()):
                temp = self.fetch_temperature(city)
                if temp is None:
                    continue
                self.last_temperatures[cp_id] = {"city": city, "temperature": temp, "timestamp": datetime.utcnow().isoformat()}
                self.evaluate_temperature(cp_id, city, temp)
            time.sleep(self.poll_interval)

    def fetch_temperature(self, city: str):
        try:
            params = {"q": city, "appid": self.api_key, "units": "metric"}
            resp = self.session.get("https://api.openweathermap.org/data/2.5/weather", params=params, timeout=8)
            resp.raise_for_status()
            data = resp.json()
            return data.get("main", {}).get("temp")
        except requests.RequestException as exc:
            print(f"[EV_W] Error consultando clima para {city}: {exc}")
            return None

    def evaluate_temperature(self, cp_id: str, city: str, temperature: float):
        below = temperature <= self.threshold
        active = self.alerts_active.get(cp_id, False)
        if below and not active:
            self.send_alert(cp_id, city, temperature, "alert")
            self.alerts_active[cp_id] = True
        elif not below and active:
            self.send_alert(cp_id, city, temperature, "clear")
            self.alerts_active.pop(cp_id, None)

    def send_alert(self, cp_id: str, city: str, temperature: float, status: str):
        payload = {
            "cp_id": cp_id,
            "city": city,
            "temperature": temperature,
            "status": status,
            "source": "ev_w",
            "provider": "openweather",
        }
        try:
            resp = self.session.post(f"{self.central_base}/weather/alert", json=payload, timeout=8)
            resp.raise_for_status()
            action = "ALERTA" if status == "alert" else "RECUPERACIÓN"
            print(f"[EV_W] {action} enviada para {cp_id} ({city}) temp={temperature}°C")
        except requests.RequestException as exc:
            print(f"[EV_W] Error notificando a la central: {exc}")

    def add_location(self):
        cp_id = input("CP_ID: ").strip().upper()
        if not cp_id:
            print("CP_ID requerido.")
            return
        city = input("Ciudad (formato Ciudad,Pais): ").strip()
        if not city:
            print("Ciudad requerida.")
            return
        self.locations[cp_id] = city
        self.save_locations()
        print(f"[EV_W] Localización guardada para {cp_id} -> {city}")

    def remove_location(self):
        cp_id = input("CP_ID a eliminar: ").strip().upper()
        if cp_id in self.locations:
            self.locations.pop(cp_id)
            self.save_locations()
            print(f"[EV_W] Localización eliminada para {cp_id}")
        else:
            print("CP no encontrado.")

    def list_locations(self):
        if not self.locations:
            print("No hay CP configurados.")
            return
        for cp_id, city in self.locations.items():
            info = self.last_temperatures.get(cp_id)
            temp_txt = f"{info['temperature']}°C" if info else "sin datos"
            print(f"{cp_id} -> {city} | última temp: {temp_txt}")

    def check_now(self):
        if not self.locations:
            print("Sin localizaciones.")
            return
        for cp_id, city in self.locations.items():
            temp = self.fetch_temperature(city)
            if temp is None:
                continue
            self.last_temperatures[cp_id] = {"city": city, "temperature": temp, "timestamp": datetime.utcnow().isoformat()}
            self.evaluate_temperature(cp_id, city, temp)

    def set_threshold(self):
        try:
            value = float(input(f"Nueva temperatura umbral (actual {self.threshold}°C): "))
            self.threshold = value
            print(f"Umbral actualizado a {value}°C.")
        except ValueError:
            print("Valor no válido.")

    def menu_loop(self):
        print("EV_W Weather Control listo. Ctrl+C para salir.")
        while True:
            print("\n=== Menú EV_W ===")
            print("1 - Listar CPs configurados")
            print("2 - Añadir/actualizar CP")
            print("3 - Eliminar CP")
            print("4 - Consultar clima ahora")
            print("5 - Cambiar umbral de temperatura")
            print("q - Salir")
            try:
                cmd = input("Opción: ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                cmd = "q"
            if cmd == "1":
                self.list_locations()
            elif cmd == "2":
                self.add_location()
            elif cmd == "3":
                self.remove_location()
            elif cmd == "4":
                self.check_now()
            elif cmd == "5":
                self.set_threshold()
            elif cmd == "q":
                self._stop.set()
                break
            else:
                print("Opción no válida.")


def main():
    office = WeatherOffice()
    if not office.api_key:
        print("ADVERTENCIA: No hay API Key de OpenWeather. Configura weather.openweather_api_key o la env OPENWEATHER_API_KEY.")
    office.start()


if __name__ == "__main__":
    main()
