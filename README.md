# EVCharging Release 2 (API + Seguridad)

Plataforma de gestión de puntos de recarga distribuida que incorpora los requisitos de la release 2 de la práctica: autenticación segura de CPs mediante un Registry HTTPS, cifrado punto a punto Central↔CP en Kafka, API REST completa y módulo meteorológico EV_W conectado a OpenWeather.

## 1. Servicios y módulos

| Módulo | Descripción | Cómo se ejecuta |
|--------|-------------|-----------------|
| **Kafka + Zookeeper** | Broker de mensajería compartido por todos los actores. | `docker compose up -d kafka zookeeper` (definir `HOST_IP`). |
| **Central** | Core del sistema (driver y CP coordinador) + API FastAPI (`/snapshot`, `/command`, `/auth/cp/*`, `/weather/alert`, `/events`). Persiste en SQLite y cifra comandos en Kafka con claves Fernet por CP. | `make run-central` o `python run.py central`. |
| **Registry (EV_Registry)** | API FastAPI expuesta por HTTPS (usa los certificados en `config/certs/`). Gestiona altas/bajas de CPs y emite credenciales + tokens HMAC. | `make run-registry` o `python run.py registry`. |
| **Weather Office (EV_W)** | Consulta OpenWeather cada 4 s y notifica alertas/desbloqueos a la Central vía `/weather/alert`. | `OPENWEATHER_API_KEY=xxx make run-weather` o `python run.py weather`. |
| **CP Monitor (EV_CP_M)** | CLI que interactúa con el Registry/Central para registrar el CP, solicitar tokens y descargar la clave simétrica. También monitoriza Kafka cifrado y envía faults. | `make run-cp-monitor CP_ID=CP01`. |
| **CP Engine (EV_CP_E)** | Simula el cargador. Lee la clave guardada por el monitor en `config/credentials/CPxx.json`, cifra telemetría y descifra comandos. | `make run-cp-engine CP_ID=CP01`. |
| **Driver** | Clientes Kafka (sin cambios funcionales). | `make run-driver DRIVER_ID=D01`. |
| **Frontend (web/)** | Dashboard Vue 3 + Vite que consume `/snapshot` y muestra CPs, clima, auditoría y drivers. | `cd web && npm install && npm run dev -- --host`. |

> Todos los módulos Python comparten dependencias en `requirements.txt` (incluye `cryptography`, `fastapi`, `requests`, etc.) y utilizan la misma BBDD `Central/bdd/evcharging.db` (asegúrate de compartir el volumen si usas contenedores).

## 2. Flujo seguro para un CP

1. **Levanta los servicios base:** Kafka, Registry (`https://<host>:8443` con los certificados de `config/certs/`) y Central (`http://<host>:8000` por defecto).
2. **Ejecuta el Monitor del CP:** `make run-cp-monitor CP_ID=CP01`. Desde su menú:
   - `register` → solicita el alta en el Registry aportando la localización. Se guarda `client_secret` en `config/credentials/CP01.json`.
   - `token` → autenticación TLS contra el Registry usando el secreto; devuelve un token efímero.
   - `auth` → llama a `POST /auth/cp/login` de la Central aportando el token; la Central valida contra el Registry y entrega la clave Fernet (versión y estado se guardan en el JSON).
3. **Arranca el Engine:** `make run-cp-engine CP_ID=CP01`. El proceso espera hasta que encuentre una clave válida en `config/credentials/CP01.json`, comienza a emitir `REGISTER`/heartbeats cifrados y queda listo para recibir comandos.
4. Si la Central revoca la clave (menú `revoke` o `POST /auth/cp/revoke`), el Engine/Monitor detectan el cambio de fichero y necesitarás repetir los pasos de token + auth.

## 3. Weather Control Office

El fichero `config/config.yaml` incluye una sección `weather` con:

```yaml
weather:
  polling_interval: 4
  threshold_celsius: 0
  openweather_api_key: ""   # opcional, puedes usar la env OPENWEATHER_API_KEY
  default_cities:
    CP01: "Valencia,ES"
```

`EV_W.py` mantiene un archivo `config/weather_locations.json` editable desde el menú (añadir/quitar CP). Cada iteración consulta OpenWeather (`/data/2.5/weather?q=Ciudad`) y, si la temperatura baja del umbral, llama a `POST /weather/alert` en la Central. Cuando se recupera, envía `status=clear`, lo que desbloquea el CP automáticamente. Las alertas aparecen en el dashboard y bloquean nuevas sesiones.

## 4. API REST expuesta por la Central


| Endpoint | Método | Descripción |
|----------|--------|-------------|
| `/snapshot` | GET | Snapshot completo `cps`, `drivers`, `sessions`, `active_sessions`, `events`. |
| `/command` | POST | Enviar comandos (`pause/resume/stop/start/cancel`) con `action` y parámetros (`driver_id`, `cp_id`, `scope`). |
| `/auth/cp/login` | POST | Cuerpo `{ cp_id, token }`. Valida el token emitido por el Registry, revoca claves anteriores y devuelve `{ symmetric_key, version }`. |
| `/auth/cp/revoke` | POST | Revoca la clave activa de un CP. |
| `/weather/alert` | POST | Consumido por EV_W. Cuerpo `{ cp_id, city, temperature, status }` donde `status` ∈ `alert|clear`. |
| `/events` | GET | Últimos 100 eventos de auditoría (ya incluidos también en `/snapshot`). |

Todos los eventos se registran en `events` con `timestamp`, `cp_id`, `event_type`, `description`, `actor` y `origin_ip`.

## 5. Seguridad implementada

- **Canal seguro CP ↔ Registry:** el servicio se publica por HTTPS con los certificados autofirmados en `config/certs/`. Las credenciales (`client_secret`) se almacenan con PBKDF2 + salt en la BBDD.
- **Autenticación CP ↔ Central:** el Monitor solicita tokens efímeros al Registry y los usa en `/auth/cp/login`. La Central verifica el hash del token y emite una clave Fernet única por CP que se almacena en `cp_security_keys`.
- **Cifrado Kafka Central↔CP:** todos los mensajes (`REGISTER`, heartbeats, telemetría, comandos) se empaquetan como `{ cp_id, ciphertext }` usando la clave Fernet correspondiente. Central sólo procesa mensajes descifrables y permite revocar claves desde CLI o API.
- **Auditoría completa:** cada evento (autenticaciones, comandos, incidencias, alertas meteo…) queda registrado con actor y dirección de origen. El frontend muestra la traza en tiempo real.

## 6. Despliegue con Docker

`docker-compose.yml` incluye servicios para Kafka, Registry, Weather Office, Central y el frontend. Ejemplo mínimo:

```bash
export HOST_IP=10.0.0.5
export OPENWEATHER_API_KEY="tu_api_key"
docker compose up -d zookeeper kafka
docker compose up -d registry weather central
docker compose up web
```

> Tanto Central como Registry montan `./Central/bdd` y `./config` para compartir la BBDD y el certificado TLS. Si prefieres otro certificado, sustituye los ficheros `config/certs/registry_cert.pem` y `registry_key.pem` o ajusta `config/config.yaml`.

## 7. Directorios clave

- `config/config.yaml`: broker Kafka, rutas TLS, umbral meteo, etc.
- `config/credentials/`: la CLI del Monitor guarda aquí `CPxx.json` con secretos y claves.
- `Central/bdd/evcharging.db`: SQLite con sesiones, eventos, registro de CPs y claves.
- `Weather/EV_W.py`, `Registry/EV_Registry.py`, `Central/EV_Central.py`: módulos principales nuevos.

## 8. Próximos pasos sugeridos

- Añadir balanceo real de CPs en varios hosts reales (la Central ya usa `resolve_broker` para autodetectar).
- Activar TLS también en la API de la Central (añadiendo `ssl_certfile` en `start_api`).
- Completar automatizaciones en EV_W para sincronizar localizaciones con el Registry.
