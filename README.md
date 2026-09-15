# SomnGuard Device

Software del dispositivo SomnGuard: nodo edge basado en **Raspberry Pi / Windows + cámara USB** que detecta fatiga, somnolencia y microsueños al volante, emite alertas sonoras locales y gestiona estados (Activo/Espera/Offline).

> **Estado actual**: HU-DEVICE-002 ✅ completa (inicialización, cámara, estados, heartbeat,
> self-register, device_config). Backend HU-API-006 ✅; pendiente HU-API-005 (`GET /config` con fallback local).

---

## Requisitos

- Python 3.11+
- Cámara USB (índice 0) o CSI (Raspberry Pi)
- Parlante (AS-01..AS-09)
- Windows 10/11, Linux o Raspberry Pi OS

---

## Inicio desde 0 — en 5 minutos (para devs)

### 0) Preparar entorno (una vez)

```powershell
cd somnguard-device
py -m venv .venv; .\.venv\Scripts\activate
py -m pip install -U pip
pip install opencv-python numpy mediapipe pytest pytest-asyncio
# httpx opcional: el device usa urllib (stdlib)
```

```bash
# Linux / RPi
python3 -m venv .venv; source .venv/bin/activate
pip install -U pip
pip install opencv-python numpy mediapipe pytest pytest-asyncio
```

Descargar modelo (una vez, ~15MB):
```powershell
mkdir models -Force
py -c "import urllib.request; urllib.request.urlretrieve('https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task','models/face_landmarker.task'); print('OK')"
```

Verificar sin hardware:
```powershell
py -m pytest tests/ -v  # 43 passed
```

### 1) Modo local — sin backend ni token (prueba cámara y estados)

```powershell
Copy-Item .env.example .env   # DEBE llamarse .env
py -m app.main
```

**Qué pasa:** `Cámara inicializada` + `AS-08` (ok) o `AS-09`+`ERROR` si falla. Crea `data/device_identity.json` **solo la primera vez** con `serial` (`dev-...` estable) y `firmware` `0.2.0`. Log: `Dispositivo en modo local (sin credenciales...)`, `inicializado en Xs (<60)` y queda `ACTIVO` (te ve) o `ESPERA` (sin rostro). Tapa cámara → `AS-09` a `5s` y `20s`, a `30s` pausa a `ESPERA` (una sola vez, no re-dispara hasta despejar). Sin rostro `30s` → `ESPERA`, vuelve rostro → `ACTIVO` inmediato. Sin backend sigue detectando (offline-first, no pausa).

Parar: `Ctrl+C`.

### 2) Con backend — auto-registro con token (una sola vez)

En `.env` pon:
```
SOMNGUARD_API_URL=http://localhost:8080   # sin /api/v1
SOMNGUARD_PROVISION_TOKEN=<token de POST /devices/provisioning-tokens>
```

```powershell
py -c "from app.common.config import load_env_config; print(bool(load_env_config()['provision_token']))"  # True
py -m app.main
```

**Qué pasa:** usa `serial`+`firmware` del archivo ya creado + `X-Provision-Token` → `POST /api/v1/devices/self-register` (`201` trae `device_id+api_key` **una sola vez**). Guarda `device_id+api_key` en `data/device_identity.json` (`chmod 600`) y **deja de usar el token**. Log: `Auto-registro completado: device_id=<uuid>`. Luego `heartbeat` cada `30s`.

Reinicia: `Dispositivo ya registrado: <uuid>` y **no** vuelve a hacer `self-register` (`200` no reexpone key, ADR-010). Si cambia el token en `.env` ya no lo usa (tiene credencial).

¿Token nuevo? Borra `data/device_identity.json` y repite paso 1.

### 3) Verificar

- **Cámara tapada al arrancar:** no suena inmediato, inicia contador `5/20/30` igual que en marcha → `5s AS-09`, `20s AS-09`, `30s` pausa a `ESPERA` + `AS-09` (solo una vez).
- **Offline:** corta backend → `Sin conectividad: modo OFFLINE (detección local continúa)` y sigue detectando; vuelve backend → próximo `heartbeat` → `Conectividad restaurada: OFFLINE -> ACTIVO/ESPERA`.
- **Opcional cámara:** `py preview_camera.py` (ESC sale).

---

## Qué hace (HU-DEVICE-002)

| AC | Comportamiento |
|----|----------------|
| **AC-001** | Boot `<60s` → cámara OK `AS-08`, fallo `AS-09`+`ERROR`. Modelo con fallback `AS-09` reintento `30s` |
| **AC-002** | Obstrucción/FOV malo → `AS-09 5s` → `AS-09 20s` → `30s` pausa `ESPERA`+`AS-09` (una vez). Arranque con cámara tapada inicia mismo contador, no inmediato |
| **AC-003** | `ACTIVO` (rostro) ↔ `ESPERA` (30s sin rostro, reloj monotónico) ↔ `ACTIVO` inmediato. Nunca sale de `OFFLINE`/`SUSPENDIDO` por presencia |
| **AC-004** | `heartbeat 30s POST /devices/{id}/heartbeat` (`X-Device-ID/X-API-Key` `firmware/pending/free_disk/uptime`); sin red → `OFFLINE` (sigue detectando, offline-first); al volver → `ACTIVO/ESPERA`. `SUSPENDIDO/RETIRADO` pausan hasta admin |
| **AC-005** | `device_config` = default `config/device.default.json` → caché `data/device_config.cache.json` (último pull) → override `data/device_config.override.json` + `GET /config` **solo manual** cuando `heartbeat` responde `config_pending=true` (usuario pulsa Actualizar en portal/app). Al arrancar no hace pull: restaura la caché para no revertir a default mientras la API reporta `applied==global`. Aplica umbrales/volumen/intervalos |
| **AC-006** | `serial` estable `dev-...` (solo primera vez), `firmware` `VERSION`/`pyproject` (se refresca si cambia). Token solo en memoria `SOMNGUARD_PROVISION_TOKEN` |
| **AC-007** | Sin `device_id/api_key` + token → `POST /self-register` (`X-Provision-Token`+`Idempotency-Key` estable `{serial,fw}`); `201` persiste `device_id/api_key` `600`; `200` no reexpone key; retry boot `2x` + background `60s` |

**Estados (device no sabe si está asignado):** `REGISTRADO` → `ACTIVO` ↔ `ESPERA` ↔ `OFFLINE` (red); backend ve `ESPERA` como `ACTIVE`. Solo `SUSPENDIDO`/`RETIRADO` vienen de backend. Volátiles no se restauran.

**Alertas AS-01..AS-09:** en `config/device.default.json`.

---

## Estructura actual (solo HU-DEVICE-002)

```
somnguard-device/
├── app/
│   ├── common/
│   │   ├── config.py      # Config + merge/apply remoto + overrides env
│   │   ├── models.py      # Tipos + mapping estados edge<->backend (HU-API-006)
│   │   ├── logging.py
│   │   └── clock.py
│   ├── device/
│   │   ├── identity.py    # Serial/fw estables + device_id/api_key (chmod 600)
│   │   ├── backend.py     # Cliente stdlib: self-register/heartbeat/config
│   │   └── manager.py     # State machine + orquestación + boot <60s
│   ├── capture/
│   │   └── camera.py      # OpenCV + verificación frames + fallback backend
│   ├── analysis/
│   │   ├── landmarks.py   # MediaPipe FaceLandmarker (468 pts)
│   │   └── detector.py    # FOV + obstruction + escalamiento 5s/20s/pausa 30s
│   ├── monitoring/
│   │   └── presence.py    # Timeout 30s monotónico Activo↔Espera
│   └── alerting/
│       └── sound_player.py # winsound (Win) / simpleaudio (Linux) + volume_scale
├── config/
│   └── device.default.json # 9 sound_patterns + umbrales
├── tests/unit/           # 43 tests sin hardware ni red (pytest)
├── models/
│   └── face_landmarker.task  # (no versionado, descargar)
├── VERSION               # firmware_version (fuente tras env)
├── preview_camera.py       # Test visual cámara + landmarks
├── pyproject.toml
├── Makefile
├── .gitignore (.env y data/ excluidos: secretos fuera del repo)
├── .env.example
├── README.md
└── LICENSE
```

---

## Tests

```bash
py -m pytest tests/ -v   # 43 tests: presencia, escalamiento AS-09, config,
                         # .env, identidad/credenciales, contratos HTTP, estados
```

Sin cámara ni red: dependencias pesadas (`cv2`/`mediapipe`) con stubs en
`tests/unit/conftest.py`.

---

## Próximos pasos (HU-API-005 pendiente en backend)

1. Backend HU-API-005 ya expone `GET /devices/{id}/config` y `POST /devices/{id}/config/refresh` + `config_pending` en `heartbeat`. El device solo pulla cuando el heartbeat avisa `pending=true` (flujo manual).
2. `HU-DEVICE-003` informará `pending_count` real en el heartbeat.
3. `pip install -e ".[full]"` para `simpleaudio` en Linux (volumen real;
   `winsound` en Windows no soporta volumen).

---

## Comandos útiles

```bash
# Ver logs en tiempo real
py -m app.main 2>&1 | tee logs/app.log

# Test rápido de cámara
py -c "import cv2; cap=cv2.VideoCapture(0); print('OK' if cap.isOpened() else 'FAIL')"

# Ver modelo descargado
ls -la models/

# Limpiar caches
Remove-Item -Recurse -Force app/**/__pycache__, .venv  # PowerShell
rm -rf app/**/__pycache__ .venv  # Linux
```