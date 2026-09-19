# SomnGuard Device

Software del dispositivo SomnGuard: nodo edge basado en **Raspberry Pi / Windows + cámara USB** que detecta fatiga, somnolencia y microsueños al volante, emite alertas sonoras locales y gestiona estados (Activo/Espera/Offline).

> **Estado actual**: HU-DEVICE-002 ✅ completa (inicialización, cámara, estados, heartbeat,
> self-register, device_config). HU-DEVICE-001 ✅ funcional (somnolencia + distracción con
> pipeline <2s/<1s; teléfono v1 por ObjectDetector; cinturón ⛔ desactivado por flag hasta
> validación HW). Backend HU-API-006 ✅; pendiente HU-API-005 (`GET /config` con fallback local).

---

## Requisitos

- Python 3.11+
- Cámara USB (índice 0) o CSI (Raspberry Pi)
- Parlante (AS-01..AS-09)
- Windows 10/11, Linux o Raspberry Pi OS

---

## Inicio desde 0 — en 5 minutos (para devs)

```powershell
git clone <url> SomnGuard; cd SomnGuard\00-repos\somnguard-device
```

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

Descargar modelos (una vez):
```powershell
py scripts/download_models.py   # face (~15MB) + phone EfficientDet (~12MB)
```

Verificar sin hardware:
```powershell
py -m pytest tests/ -v  # 87 passed
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

## Estructura actual (HU-DEVICE-002 + HU-DEVICE-001)

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
│   │   ├── landmarks.py   # MediaPipe FaceLandmarker (468 pts, model_path inyectable)
│   │   ├── detector.py    # FOV + obstruction + escalamiento 5s/20s/pausa 30s
│   │   ├── ear_mar.py     # EAR/MAR/PERCLOS + head-pose solvePnP + gate plausibilidad
│   │   ├── severity.py    # Tabla canónica EV→(categoría, severidad, AS) Apéndice 2
│   │   ├── somnolence.py  # EV-SOM-01..05 (blink/cierre/bostezo/cabeceo/microsueño)
│   │   ├── distraction.py # EV-DIS-01..05 (teléfono/gaze/movimiento) + GazeEstimator
│   │   ├── seatbelt.py    # EV-CIN-01/02 (⛔ desactivado por belt_enabled=false)
│   │   └── pipeline.py    # Orquestador <2s/<1s + fallback multi-modelo
│   ├── monitoring/
│   │   └── presence.py    # Timeout 30s monotónico Activo↔Espera
│   └── alerting/
│       └── sound_player.py # winsound (Win) / simpleaudio (Linux) + volume_scale
├── config/
│   └── device.default.json # 9 sound_patterns + umbrales
├── tests/unit/           # 87 tests sin hardware ni red (pytest)
├── models/
│   ├── face_landmarker.task  # (no versionado, descargar)
│   └── phone_detector.tflite # (no versionado, descargar)
├── scripts/
│   └── download_models.py  # Descarga face + phone (solo stdlib)
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
py -m pytest tests/ -v   # 87 tests: presencia, escalamiento AS-09, config,
                         # .env, identidad/credenciales, contratos HTTP, estados,
                         # somnolencia (EAR/MAR/PERCLOS/pico), distracción (gaze
                         # suavizado/calibrado/tope, teléfono, movimiento),
                         # cinturón (desactivado), pipeline/latencias
```

Sin cámara ni red: dependencias pesadas (`cv2`/`mediapipe`) con stubs en
`tests/unit/conftest.py`.

---

## Qué hace (HU-DEVICE-001 — visión)

| AC | Comportamiento |
|----|----------------|
| **AC-001** | `SomnolenceDetector`: blink anómalo 15s o cierre lento 0.5-2s → `EV-SOM-01`, ojos>2s → `EV-SOM-02`, bostezo prolongado ≥3s → `EV-SOM-03` (cada uno alerta; conteo en ventana como evidencia), tilt relativo ≥15°/3s (neutro auto-cero) → `EV-SOM-04`, ojos>3s+tilt → `EV-SOM-05` |
| **AC-002** | `severity.py` canónico: `AS-01 LEVE / AS-02 MODERADA / AS-03 SEVERA / AS-04 CRITICA` |
| **AC-003** | `DistractionDetector`: teléfono v1 por ObjectDetector (`cell phone`>2s `EV-DIS-01`, >5s `EV-DIS-02` c/3s; sin `.tflite` degradado seguro), mirada con `GazeEstimator` suavizado EMA + histéresis (entra 30°/sale 22°, spike aislado no latcha) + auto-cero + deltas con wrap (sesgo pitch +170° absorbido) + force-off tras 1s en zona ciega >2s `EV-DIS-03` (>5s `EV-DIS-04`, máx 5 repeticiones por episodio, gracia 0.4s), movimiento>3s `EV-DIS-05`. Gaze se evalúa con landmarks aunque el FOV falle |
| **AC-004** | ⛔ DESACTIVADO (`belt_enabled=false`): `SeatbeltDetector` retorna siempre [] hasta validación HW. Lógica 10s + intermitente c/5s lista y testeada con `belt_enabled=true`. Requiere FOV amplio o 2ª cámara para producción |
| **AC-005** | `VisionPipeline`: downscale 640px, métricas `capture_to_validated/process_sec`, skip-frame adaptativo, presupuesto `frame_budget_sec: 2.0`. Manager: log inmediato + sonido en background (<1s), fix doble `read_frame` en ESPERA |
| **AC-006** | `load_models()` por modelo con `models_status`; fallo → `EV-SYS-02/AS-09` cooldown 30s + reintento 30s (patrón HU-DEVICE-002). `LandmarkDetector(model_path=...)` inyectable |

Calibración: umbrales en `config/device.default.json` alineados a Apéndice 2
(`blink_rate_max/min`, `eye_closed_min_sec`, `yawn_count_min`, `head_tilt_deg_min`,
`phone/gaze/movement_*`, `belt_*`). `_sanitize_config` valida rangos + aliases legacy.

### Ajuste anti-spam en campo (sin tocar código)

Crea `data/device_config.override.json` (se fusiona al arrancar, manda sobre
el default) con solo lo que quieras cambiar:

```json
{
  "detection_thresholds": {
    "yawn_mar_threshold": 0.8,
    "yawn_peak_mar_min": 1.0,
    "yawn_min_duration_sec": 3.0,
    "yawn_cooldown_sec": 60.0,
    "yawn_count_min": 2
  }
}
```

| Mando | Default | Subir ⇧ | Bajar ⇩ |
|-------|---------|---------|---------|
| `yawn_mar_threshold` (boca abierta) | 0.85 | menos alertas, solo bocas grandes (0.9) | más sensible (0.7) |
| `yawn_peak_mar_min` (pico del episodio) | 1.0 | mata FP de habla fuerte: solo estirón máximo (1.1) | bostezos chicos (0.85) |
| `yawn_min_duration_sec` (PLOS: fatiga ≥3s) | 3.0 | más estricto (3.5) | más sensible (2.5) |
| `yawn_cooldown_sec` (separación mínima) | 30 | menos conteos seguidos (60) | cuenta bostezos encadenados (15) |
| `yawn_count_min` (alertas por ventana) | 1 | 2 = estilo norma 2/5min | 1 = cada bostezo alerta |
| `head_tilt_deg_min` (MDPI: fatiga 12-20°) | 15 | menos sensible (20) | más sensible (12) |
| `gaze_duration_sec` (NHTSA: riesgo >2s) | 2.0 | menos sensible (3) | 1.5 = muy sensible |
| `blink_slow_sec` (cierre lento aislado) | 0.5 | solo muy lentos (0.8) | más sensible (0.4) |
| `prolonged_max_repeats` (mirada/teléfono) | 5 | más insistencia | 2–3 = se calla antes |

Regla práctica: habla normal pica MAR ~0.5–0.65, habla fuerte sostiene
~0.75–0.85, bostezo real pica ~0.9–1.2. Si hay FP hablando → sube
`yawn_peak_mar_min` a 1.0. Si bostezo real no dispara → bájalo a 0.8.

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