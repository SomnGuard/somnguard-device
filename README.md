# SomnGuard Device

Software del dispositivo SomnGuard: nodo edge basado en **Raspberry Pi / Windows + cámara USB** que detecta fatiga, somnolencia y microsueños al volante, emite alertas sonoras locales y gestiona estados (Activo/Espera/Offline).

> **Estado actual**: HU-DEVICE-002 ✅ completa (inicialización, cámara, estados, heartbeat,
> self-register, device_config). Backend HU-API-006 ✅ (provisioning/self-register/claim);
> pendiente HU-API-005 (`GET /devices/{id}/config` — el device la consume cuando exista, con fallback local).

---

## Requisitos

- Python 3.11+
- Cámara USB (índice 0) o CSI (Raspberry Pi)
- Parlante/buzzer (para alertas AS-01..AS-09)
- Windows 10/11, Linux o Raspberry Pi OS

---

## Instalación y ejecución paso a paso

### Paso 1 — Probar en modo local (sin backend ni token)

```powershell
cd somnguard-device

py -m venv .venv; .\.venv\Scripts\activate
py -m pip install -U pip
pip install opencv-python numpy httpx mediapipe pytest pytest-asyncio
```

```powershell
# Linux / Raspberry Pi
python3 -m venv .venv; source .venv/bin/activate
pip install -U pip
pip install opencv-python numpy httpx mediapipe pytest pytest-asyncio
```

> **Nota**: `simpleaudio` (Linux), `aiosqlite`, `pydantic` son opcionales
> (`pip install -e ".[full]"`). En Windows se usa `winsound` nativo.
> El cliente backend del device usa solo stdlib (`urllib`), sin `httpx` obligatorio.

Descargar el modelo MediaPipe (solo primera vez):

```powershell
mkdir models -Force
py -c "import urllib.request; urllib.request.urlretrieve('https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task','models/face_landmarker.task'); print('OK')"
```

Tests (sin cámara ni red):

```powershell
py -m pytest tests/ -v   # Esperado: 43 passed
```

Primer arranque en modo local:

```powershell
Copy-Item .env.example .env   # el archivo DEBE llamarse .env (se autocarga)
py -m app.main
```

Esperado: `Cámara inicializada` + pitido suave (**AS-08**), `Dispositivo en modo
local (sin credenciales ni provision token)`, `Dispositivo inicializado en Xs`
(X < 60) y estado `ESPERA` (sin rostro) o `ACTIVO` (si te ve).

### Paso 2 — Poner el token en el `.env` y arrancar

```powershell
cd ..\somnguard-device
# En .env pon:
#   SOMNGUARD_API_URL=http://localhost:8080   # base backend (sin /api/v1)
#   SOMNGUARD_PROVISION_TOKEN=<token del paso anterior>   # solo primer arranque
# Nada más: tras el auto-registro el device opera con device_id + api_key
# guardados en data/device_identity.json (chmod 600) y deja de usar el token.

py -c "from app.common.config import load_env_config; print('token cargado:', bool(load_env_config()['provision_token']))"
# Esperado: token cargado: True

py -m app.main
```

Esperado: `Auto-registro completado: device_id=<uuid> estado=DEVICE_REGISTERED`
(`201`, trae `api_key + claim_code` una sola vez), estado `ASIGNADO` y heartbeat
cada 30s. Al reiniciar debe decir `Dispositivo ya registrado` sin más
`self-register` (reintentos `200` no reexponen la key, ADR-010).

### Ver cámara + landmarks (opcional)
```bash
py preview_camera.py   # Ventana OpenCV con landmarks; ESC para salir
```

---

## Qué hace (HU-DEVICE-002)

| AC | Comportamiento |
|----|----------------|
| **AC-001** | Arranque medido (<60s RNF-1.1, warning si excede) → cámara OK → **AS-08**; fallo → **AS-09** + estado `ERROR`. Modelo visión con fallback degradado (AS-09, reintento c/30s) |
| **AC-002** | Tapa cámara → **AS-09 a 5s** → **AS-09 a 20s** → 30s → **pausa detección** (`ESPERA` + AS-09). Chequeo FOV multi-frame en arranque |
| **AC-003** | Rostro detectado → `ACTIVO`; 30s sin rostro (reloj monotónico) → `ESPERA`; rostro vuelve → `ACTIVO` inmediato. Presencia nunca sale de `OFFLINE`/admin |
| **AC-004** | Heartbeat 30s `POST /api/v1/devices/{id}/heartbeat` (`X-Device-ID + X-API-Key`, body `firmware_version/pending_count/free_disk_pct/uptime_s`); sin red → `OFFLINE` (pausa detección); al volver → `ACTIVO`/`ESPERA`. `SUSPENDIDO`/`RETIRADO` del backend pausan hasta admin |
| **AC-005** | Config default + override local + `GET .../config` al arrancar y tras cada heartbeat OK (tolerante a 404 hasta HU-API-005); aplica umbrales/sound_patterns/volumen (`volume_scale`)/intervalos; cache en `data/device_config.cache.json` |
| **AC-006** | `serial_number`: override lab > CPU (`/proc/cpuinfo`/`product_uuid`) > estable persistido. `firmware_version`: env > `VERSION` > `pyproject` (se refresca cada boot). Token solo de `SOMNGUARD_PROVISION_TOKEN` en memoria |
| **AC-007** | Sin credenciales + token → `POST /devices/self-register` (`X-Provision-Token` + `Idempotency-Key` estable, `{serialNumber, firmwareVersion}`); `201` persiste `device_id + api_key` (chmod 600) y suelta el token; `200` no reexpone key (si se perdió → modo local + aviso de `rotate-key`); reintento en background c/60s |

**Estados**: `REGISTRADO` → `ASIGNADO` → `ACTIVO` ↔ `ESPERA` ↔ `OFFLINE` (red); `ESPERA` es sub-estado local (backend lo ve `ACTIVE`). Volátiles (`ACTIVO`/`ESPERA`/`OFFLINE`/`ERROR`) no se restauran tras reinicio.

**Alertas (AS-01..AS-09)**: Parametrizables en `config/device.default.json` (frecuencia, duración, repeticiones, loop, volumen).

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

1. Backend expone `GET /devices/{id}/config` → el device la consume **sin cambios
   de código** (`backend.py` hoy tolera el 404 y usa config local + override).
2. `HU-DEVICE-003` informará `pending_count` real en el heartbeat y hará pull de
   config tras cada sync de telemetría (el hook `_pull_remote_config` ya existe).
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