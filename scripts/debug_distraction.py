"""Debug en vivo de distracción (EV-DIS-01..05) — ver por qué "no funciona".

Uso:
    py scripts/debug_distraction.py
    py scripts/debug_distraction.py --no-show

Muestra por consola cada ~0.5s:
- yaw/pitch crudos y relativos (rel = lo que compara con 30°)
- gaze_off, calibrado sí/no, progreso calibración
- phone_present + estado modelo (ok / stub-sin-modelo)
- moving (diff actual vs umbral), temporizadores hacia EV-DIS-01/03/05
- eventos que dispararían (sin sonar)

Protocolo de prueba (importante por la calibración):
1. Arranca MIRANDO AL FRENTE 6s sin moverte (calibración, no alerta).
2. Luego mirá fijo a la izquierda/derecha >3s -> debe salir EV-DIS-03.
3. Sostené >5s -> EV-DIS-04.
4. Levantá el teléfono a la cámara >2s -> EV-DIS-01 (solo si el modelo existe).
5. Movete brusco >3s -> EV-DIS-05.

Si gaze nunca calibra: la causa es FOV inestable (cara muy chica/lejos o
luz mala). Si phone siempre dice stub-sin-modelo: falta descargar el modelo.
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
    ap.add_argument("--no-show", action="store_true")
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

    from app.analysis.distraction import DistractionDetector
    from app.analysis.landmarks import LandmarkDetector

    det = DistractionDetector(th)
    print(f"gaze: entra {det.gaze.deviation_deg:.0f}° sale {det.gaze.exit_deg:.0f}° "
          f"calib {det.gaze.calib_samples} muestras | phone>{det.phone_dur:.0f}s "
          f"gaze>{det.gaze_dur:.0f}s move>{det.move_dur:.0f}s "
          f"(move_diff>{det.movement_est.diff_threshold:.0f})")

    try:
        lm_det = LandmarkDetector()
    except Exception as e:
        print(f"Sin FaceLandmarker: {e} (py scripts/download_models.py)")
        return

    phone_ok = det.phone.load()
    print(f"Modelo teléfono: {'OK' if phone_ok else 'STUB-SIN-MODELO -> EV-DIS-01/02 NUNCA disparan'}"
          + ("" if phone_ok else " (descargalo: py scripts/download_models.py --only phone)"))
    if not phone_ok:
        print("Tip: para probar gaze/movimiento sin teléfono, igual sirve este script.")

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("No se pudo abrir la cámara 0")
        return

    from app.analysis.pipeline import VisionPipeline
    pipe = VisionPipeline(th)
    pipe.distraction = det  # reutiliza el nuestro (con debug_state)
    # Reutiliza landmarker ya abierto para no duplicar modelo
    pipe.obstruction._landmark_detector = lm_det  # type: ignore[attr-defined]

    t0 = time.monotonic()
    last = 0.0
    print("Mirá AL FRENTE 6s (calibra), luego probá mirar al costado >3s. ESC sale.")
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                await asyncio.sleep(0.1)
                continue
            now = time.monotonic()
            if now - t0 > args.seconds:
                break
            res = await pipe.process(frame, capture_time=now)
            for e in res.events:
                print(f"*** {e['event_type_id']} {e.get('message','')} [{e.get('alert_code','')}]")
            st = det.debug_state(now)
            if now - last >= 0.5:
                last = now
                print(f"t={now-t0:5.1f}s face={res.face_present} fov={res.fov_ok} "
                      f"cal={'SI' if st['gaze_calibrated'] else 'NO'} "
                      f"rel=({st['gaze_rel_yaw']:+.0f},{st['gaze_rel_pitch']:+.0f}) "
                      f"off={int(st['gaze_off'])} phone={int(st['phone_active'])}:{st['phone_dur_sec']:.1f}s "
                      f"gaze_t={st['gaze_dur_sec']:.1f}s move={int(st['move_active'])}:{st['move_dur_sec']:.1f}s")
            if not args.no_show:
                try:
                    cv2.putText(frame, f"cal={st['gaze_calibrated']} off={st['gaze_off']} "
                                f"rel=({st['gaze_rel_yaw']:+.0f},{st['gaze_rel_pitch']:+.0f})",
                                (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                    cv2.imshow("debug_distraction - ESC sale", frame)
                    if cv2.waitKey(1) & 0xFF == 27:
                        break
                except Exception:
                    pass
            await asyncio.sleep(0.01)
    finally:
        cap.release()
        try:
            import cv2 as _cv
            _cv.destroyAllWindows()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
