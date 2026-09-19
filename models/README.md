# models/ — Modelos de visión edge (HU-DEVICE-001 / HU-DEVICE-002)

No versionados (ver `.gitignore`). Descargar una vez:

```powershell
mkdir models -Force
py -c "import urllib.request; urllib.request.urlretrieve('https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task','models/face_landmarker.task'); print('OK')"
```

| Modelo | Estado | Uso |
|--------|--------|-----|
| `face_landmarker.task` (~15MB) | ✅ activo | Landmarks 468pts -> EAR/MAR/PERCLOS, gaze, cabeceo (AC-001/003) |
| `phone_detector.tflite` (~12MB, EfficientDet-Lite0) | ✅ v1 funcional | `PhoneDetector` (MediaPipe ObjectDetector, clase `cell phone`, sin nuevas deps pip). Descargar con `py scripts/download_models.py --only phone`. Sin archivo: degradado seguro |
| cinturón | ⛔ desactivado | `belt_enabled=false` en `device.default.json` hasta validación HW. Sin modelo ni intentos de carga |

Limitación HW documentada (AC-004): cámara frontal a rostro con FOV estrecho
no ve torso/cinturón. `assess_fov_for_belt()` marca no-evaluable si el rostro
ocupa >40% del alto. Para AC-004 real se requiere FOV amplio o 2ª cámara;
mientras tanto el detector queda en degradado silencioso.
