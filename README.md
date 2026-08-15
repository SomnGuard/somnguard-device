# SomnGuard Device

Software del dispositivo SomnGuard: nodo edge basado en **Raspberry Pi + cámara** que detecta fatiga, somnolencia y microsueños al volante, emite alertas sonoras locales, almacena eventos offline y sincroniza con el backend.

---

## Estructura del repositorio

```text
somnguard-device/
├── README.md              # Este archivo: qué es el repo, estructura y cómo arrancar
├── LICENSE                # Licencia del proyecto
├── .gitignore             # Archivos que NO se versionan (secrets, datos, logs)
├── .env.example           # Plantilla de variables de entorno
├── pyproject.toml         # Dependencias y empaquetado (Python)
├── Makefile               # Comandos estándar: setup, run, test, provision
│
├── app/                   # Código fuente de la aplicación edge
│   ├── main.py            # Punto de entrada y orquestador de servicios
│   ├── device/            # Ciclo de vida y gestión del dispositivo
│   ├── capture/           # Captura desde la cámara
│   ├── analysis/          # Procesamiento local de visión
│   ├── alerting/          # Alertas sonoras locales
│   ├── monitoring/        # Estado del conductor (presencia)
│   ├── events/            # Generación de eventos y evidencia
│   ├── storage/           # Buffer local offline (SQLite + multimedia)
│   ├── sync/              # Sincronización con el backend
│   └── common/            # Utilidades transversales
│
├── config/                # Configuración por defecto del dispositivo
├── data/                  # Datos de ejecución (NO versionado)
├── models/                # Pesos del modelo de visión (solo metadatos)
├── scripts/               # Aprovisionamiento y automatización
├── deploy/                # Despliegue (unit de systemd)
├── tests/                 # Pruebas automatizadas

```

---

## Explicación de archivos y carpetas

### Raíz

#### `README.md`
Puerta de entrada del repositorio. Debe contener: qué es el dispositivo, requisitos de hardware, estructura, cómo instalar, cómo correr y cómo probar.

```markdown
# SomnGuard Device
Dispositivo edge para detección de somnolencia al volante.

## Requisitos
- Raspberry Pi 4 (o superior) + cámara CSI/USB + parlante/buzzer

## Instalación
make setup

## Uso
make run
```

#### `LICENSE`
Licencia legal del proyecto (MIT, Apache-2.0, etc.). Define cómo otros pueden usar el código.

#### `.gitignore`
Lista de archivos/carpetas que Git ignora. Mínimo obligatorio: `.env`, `data/`, logs, modelos pesados y cachés de Python.

```text
.env
data/
*.log
__pycache__/
*.pyc
models/*.onnx
```

#### `.env.example`
Plantilla de variables de entorno con valores de ejemplo (nunca secretos reales). El `.env` real se copia de aquí y no se versiona.

```env
SOMNGUARD_API_URL=https://api.somnguard.example.com
SOMNGUARD_API_KEY=replace-me
SOMNGUARD_SENSITIVITY=medium
SOMNGUARD_BUFFER_LIMIT_MB=2048
```

#### `pyproject.toml`
Metadatos del paquete Python, dependencias y configuración de herramientas (ruff, pytest).

```toml
[project]
name = "somnguard-device"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "opencv-python",
    "picamera2",
    "httpx",
    "numpy",
]
```

#### `Makefile`
Comandos estándar del repositorio para no memorizar instrucciones.

~~~makefile
setup:
	python -m venv .venv && . .venv/bin/activate && python -m pip install -U pip && python -m pip install -e .

run:
	python -m app.main

test:
	pytest tests/unit

provision:
	bash scripts/provision.sh
~~~

---

### `app/` — aplicación edge

Núcleo del dispositivo. Cada subcarpeta es una responsabilidad y debe contener módulos pequeños y enfocados.

| Carpeta/Archivo | Para qué sirve | Ejemplo |
|-----------------|----------------|---------|
| `main.py` | Punto de entrada: inicializa config, servicios y el bucle principal (captura → análisis → alerta/registro → sync) | `python -m app.main` |
| `device/` | Ciclo de vida del dispositivo: identidad (`serial_number`, `api_key_hash`, `firmware_version`), registro, heartbeat y aplicación de configuración remota | `manager.py` registra el device contra el backend al arrancar |
| `capture/` | Driver de la cámara y lectura de frames (resolución, FPS) | `camera.py` abre la cámara CSI y entrega frames al analizador |
| `analysis/` | Visión local: detección de fatiga, somnolencia y microsueños (parpadeo, bostezos, pose de cabeza) | `detector.py` recibe un frame y devuelve un resultado de detección |
| `alerting/` | Reproduce el `sound_pattern` asociado a la severidad del evento detectado | `sound_player.py` suena 3 veces si la severidad es crítica |
| `monitoring/` | Detecta ausencia del conductor y pausa/reanuda el monitoreo | `presence.py` pausa la captura si no hay rostro durante N segundos |
| `events/` | Construye y persiste `event` (con `event_type_id`, `occurred_at`, `is_offline_sync`) y `evidence` (media_type + archivo) | `event_builder.py` crea el evento a partir del resultado de detección |
| `storage/` | Buffer local: SQLite con cola de sincronización, evidencia multimedia en disco y política de retención | `local_db.py` guarda eventos pendientes; `retention.py` borra los más viejos al llegar al límite |
| `sync/` | Cliente HTTP del backend, envío de JSON + multimedia, reintentos con backoff e idempotencia | `uploader.py` envía en lote y marca eventos como sincronizados |
| `common/` | Transversales: lectura de configuración (env + JSONB), logs, fuente de tiempo y dataclasses de dominio | `models.py` define `Event`, `Evidence`, `Alert` |

Ejemplo del flujo que une `app/`:

```text
capture/frame_reader.py ──> analysis/detector.py ──> alerting/sound_player.py
                              │
                              └──> events/recorder.py ──> storage/local_db.py ──> sync/uploader.py
```

#### `app/common/config.py` (ejemplo)
```python
import os

API_URL = os.getenv("SOMNGUARD_API_URL")
API_KEY = os.getenv("SOMNGUARD_API_KEY")
SENSITIVITY = os.getenv("SOMNGUARD_SENSITIVITY", "medium")
BUFFER_LIMIT_MB = int(os.getenv("SOMNGUARD_BUFFER_LIMIT_MB", "2048"))
```

---

### `config/`
Configuración base del dispositivo antes de recibir la configuración remota del backend (`device_config` JSONB). Debe contener valores por defecto de sensibilidad, catálogos y patrones de sonido.

```json
{
  "sensitivity": "medium",
  "camera": { "resolution": [1280, 720], "fps": 30 },
  "sounds": { "critical": "alert_critical.wav" },
  "buffer_limit_mb": 2048
}
```

### `data/`
Datos de ejecución del dispositivo. **Nunca se versiona** (está en `.gitignore`). Contiene la base SQLite (`db/`), evidencia multimedia pendiente de sincronizar (`media/`) y logs (`logs/`).

### `models/`
Pesos del modelo de visión (`.onnx`, `.tflite`, etc.). Los archivos pesados no se versionan; solo metadatos en `README.md` (origen, licencia, versión, cómo descargarlos).

```markdown
# Modelos

| Archivo | Versión | Origen | Uso |
|---------|---------|--------|-----|
| face_detector.onnx | v1.0 | (URL) | Detección de rostro previa al análisis |
```

### `scripts/`
Automatización para operar el dispositivo en campo.

| Archivo | Para qué sirve |
|---------|----------------|
| `provision.sh` | Instala dependencias del SO en la Raspberry Pi (cámara, audio, Python) |
| `install_service.sh` | Copia el unit de systemd y habilita el servicio |
| `update.sh` | Actualiza el software del dispositivo (pull + reinicio del servicio) |
| `factory_reset.sh` | Restablece configuración de fábrica (borra `data/` y configuración local) |

### `deploy/`
Unit de systemd para que el dispositivo arranque solo al encender la Pi.

```ini
[Unit]
Description=SomnGuard Device
After=network.target

[Service]
ExecStart=/home/pi/somnguard-device/.venv/bin/python -m app.main
WorkingDirectory=/home/pi/somnguard-device
Restart=always

[Install]
WantedBy=multi-user.target
```

### `tests/`
Pruebas automatizadas:

| Carpeta | Para qué sirve |
|---------|----------------|
| `tests/unit/` | Pruebas unitarias (análisis, buffer, retry, deduplicación) |
| `tests/integration/` | Pruebas de integración (sincronización contra backend simulado) |
| `tests/hardware/` | Pruebas con hardware real (cámara, audio) — se ejecutan en la Pi |


---

## Cómo arrancar (orden de trabajo sugerido)

1. Completar la **raíz** (README, `.gitignore`, `.env.example`, dependencias, Makefile, CI).
2. Implementar `app/device/` + `app/common/` (identidad, registro y configuración).
3. Implementar `app/capture/` y `app/analysis/` (cámara y detección).
4. Implementar `app/events/`, `app/storage/` y `app/alerting/`.
5. Implementar `app/sync/`.
6. Completar `scripts/`, `deploy/` y `tests/hardware/` para operar en la Pi.