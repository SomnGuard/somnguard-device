"""Monitor en vivo de tilt/cabeceo (EV-SOM-04) — ver lo que ve el modelo.

Uso:
    py scripts/debug_tilt.py

Muestra cada ~0.3s: cara SI/NO, FOV, yaw/pitch, tilt, ojos, y el progreso
hacia EV-SOM-04 (>20° por >3s). Sirve para dos diagnósticos:
1. Si al inclinar de lado el yaw no sube de ~10°: problema de geometría
   (cámara lejos, de frente total) — el modelo no puede medir el ángulo.
2. Si yaw sube a 30-40° pero no hay evento: problema de lógica.
3. Si CARA dice NO mirando a la cámara: el modelo no te ve (luz/distancia/
   ángulo) — eso también explica quedarse en ESPERA con "cara".
ESC sale.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.common.config import load_default_config


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=180.0)
    args = ap.parse_args()

    try:
        import cv2
    except ImportError:
        print("Falta opencv-python: pip install opencv-python")
        return

    cfg = load_default_config()
    try:
        from app.common.config import load_cached_remote, load_local_override
        from app.device.identity import data_dir
        cfg = load_local_override(load_cached_remote(cfg, data_dir()), data_dir())
    except Exception:
        pass
    th = dict(cfg.detection_thresholds or {})

    from app.analysis.distraction import DistractionDetector  # noqa (calibración gaze)
    from app.analysis.ear_mar import estimate_head_pose, eye_aspect_ratio
    from app.analysis.landmarks import LandmarkDetector, get_key_points
    from app.analysis.somnolence import SomnolenceDetector

    som = SomnolenceDetector(th)
    print(f"EV-SOM-04: tilt>{som.tilt_deg:.0f}° por >{som.tilt_dur:.0f}s "
          f"(tilt relativo al neutro frontal; mirá al frente los primeros "
          f"{som.tilt_est.calib_samples} frames para calibrar)")

    try:
        lm_det = LandmarkDetector()
    except Exception as e:
        print(f"Sin FaceLandmarker: {e} (py scripts/download_models.py)")
        return

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("No se pudo abrir la cámara 0")
        return

    t0 = time.monotonic()
    last = 0.0
    print("Mirá al frente 3s, luego inclina de LADO (oreja al hombro) y mantené 5s. ESC sale.")
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                await asyncio.sleep(0.1)
                continue
            now = time.monotonic()
            if now - t0 > args.seconds:
                break
            lm = await lm_det.process(frame)
            if lm is None:
                print(f"t={now-t0:5.1f}s CARA=NO  (si la ves en el video, el modelo no te detecta)")
            else:
                evs = som.process_landmarks(lm, now)
                for e in evs:
                    print(f"*** {e['event_type_id']} {e.get('message','')} [{e.get('alert_code','')}]")
                if now - last >= 0.3:
                    last = now
                    h, w = lm.image_shape
                    _, _, bw, bh = lm.bbox
                    ratio = (bw * bh) / (w * h)
                    pitch, yaw, _ = estimate_head_pose(lm.landmarks, lm.image_shape)
                    ryaw, rpitch = som.tilt_est.relative()
                    base = som.tilt_est.neutral()
                    tilt = som.tilt_est._tilt
                    print(f"t={now-t0:5.1f}s CARA=SI fov={ratio:.3f} "
                          f"yaw_raw={yaw:+6.1f} pitch_raw={pitch:+6.1f} "
                          f"neutro=({base[0]:+5.1f},{base[1]:+5.1f}) "
                          f"rel=({ryaw:+5.1f},{rpitch:+5.1f}) tilt={tilt:5.1f} "
                          f"cal={'SI' if som.tilt_est.calibrated else 'NO'} "
                          f"tilted={som._tilted}")
            await asyncio.sleep(0.01)
    finally:
        cap.release()


if __name__ == "__main__":
    asyncio.run(main())
