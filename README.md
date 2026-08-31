# SomnGuard Device

Software del dispositivo SomnGuard: nodo edge basado en **Raspberry Pi / Windows + cámara USB** que detecta fatiga, somnolencia y microsueños al volante, emite alertas sonoras locales y gestiona estados (Activo/Espera/Offline).

> **Estado actual**: HU-DEVICE-002 ✅ completa (inicialización, cámara, estados, alertas). HU-API-006 en desarrollo (backend).

---

## Requisitos

- Python 3.11+
- Cámara USB (índice 0) o CSI (Raspberry Pi)
- Parlante/buzzer (para alertas AS-01..AS-09)
- Windows 10/11, Linux o Raspberry Pi OS

---

## Instalación y ejecución

### 1. Clonar y entrar
```bash
cd somnguard-device
```

### 2. Crear entorno virtual e instalar dependencias
```bash
# Windows (PowerShell)
py -m venv .venv
.venv\Scripts\activate
py -m pip install -U pip
pip install opencv-python numpy httpx mediapipe

# Linux / Raspberry Pi
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install opencv-python numpy httpx mediapipe
```

> **Nota**: `simpleaudio`, `aiosqlite`, `pydantic` son opcionales (se usan cuando HU-API-006 esté lista). En Windows se usa `winsound` nativo.

### 3. Descargar modelo MediaPipe (solo primera vez)
```bash
# Desde la raíz del proyecto
mkdir -p models
py -c "
import urllib.request, os
url = 'https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task'
urllib.request.urlretrieve(url, 'models/face_landmarker.task')
print('Modelo descargado en models/face_landmarker.task')
"
```

### 4. Configurar variables de entorno (opcional)
```bash
# Copiar plantilla y editar si necesitas backend real
cp .env.example .env
# Editar .env con tus valores:
# SOMNGUARD_API_URL=http://localhost:8000
# SOMNGUARD_API_KEY=tu-api-key
# SOMNGUARD_LOG_LEVEL=INFO
```
> Sin `.env` usa defaults: `localhost:8000`, modo local (sin backend).

### 5. Ejecutar
```bash
# Opción A: Makefile (si tienes make)
make run

# Opción B: Directo (Windows/Linux)
py -m app.main        # Windows
python3 -m app.main   # Linux
```

### 6. Ver cámara + landmarks (opcional)
```bash
py preview_camera.py   # Ventana OpenCV con landmarks; ESC para salir
```

---

## Qué hace (HU-DEVICE-002)

| AC | Comportamiento |
|----|----------------|
| **AC-001** | Arranque <60s → cámara OK → suena **AS-08** (pitido suave); fallo → **AS-09** |
| **AC-002** | Tapa cámara → **AS-09 a 5s** → **AS-09 a 20s** → 30s → pausa detección (`ESPERA`) |
| **AC-003** | Rostro detectado → `ACTIVO`; 30s sin rostro → `ESPERA`; rostro vuelve → `ACTIVO` |
| **AC-004** | Heartbeat 30s; sin red → `OFFLINE`; red vuelve → `ACTIVO`/`ESPERA` |
| **AC-005** | Config default cargada; merge remoto listo para cuando HU-API-006 exista |

**Estados**: `REGISTRADO` → `ASIGNADO` → `ACTIVO` ↔ `ESPERA` ↔ `OFFLINE`

**Alertas (AS-01..AS-09)**: Parametrizables en `config/device.default.json` (frecuencia, duración, repeticiones, loop, volumen).

---

## Estructura actual (solo HU-DEVICE-002)

```
somnguard-device/
├── app/
│   ├── common/
│   │   ├── config.py      # Config + merge remoto
│   │   ├── models.py      # Tipos: DeviceState, AlertCode, SoundPattern, DeviceConfig
│   │   ├── logging.py
│   │   └── clock.py
│   ├── device/
│   │   ├── identity.py    # Serial único (CPU) + API key hash
│   │   └── manager.py     # State machine + orquestación
│   ├── capture/
│   │   └── camera.py      # OpenCV + verificación frames
│   ├── analysis/
│   │   ├── landmarks.py   # MediaPipe FaceLandmarker (468 pts)
│   │   └── detector.py    # FOV + obstruction + escalamiento 5s/20s
│   ├── monitoring/
│   │   └── presence.py    # Timeout 30s Activo↔Espera
│   └── alerting/
│       └── sound_player.py # winsound (Win) / simpleaudio (Linux)
├── config/
│   └── device.default.json # 9 sound_patterns + umbrales
├── models/
│   └── face_landmarker.task  # (no versionado, descargar)
├── preview_camera.py       # Test visual cámara + landmarks
├── pyproject.toml
├── Makefile
├── .gitignore
├── .env.example
├── README.md
└── LICENSE
```

---

## Próximos pasos (cuando HU-API-006 esté lista)

1. `pip install -e ".[full]"` (instala `aiosqlite`, `pydantic`, `simpleaudio`)
2. Backend real responde:
   - `POST /devices/register` → `device_id` real
   - `GET /devices/{id}/config` → `device_config` remoto (umbrales, sonidos, intervalos)
   - `POST /devices/{id}/heartbeat` → heartbeat real
   - `POST /telemetry/events` + `GET /devices/{id}/config` → sync automático
3. **Cero cambios de código** — `manager.py` ya tiene stubs listos.

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