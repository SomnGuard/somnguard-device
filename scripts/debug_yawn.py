"""Debug en vivo del MAR (bostezo) — para calibrar sin adivinar.

Uso:
    py scripts/debug_yawn.py
    py scripts/debug_yawn.py --csv data/mar_log.csv --seconds 120

Qué hace:
- Abre la cámara (índice 0), corre FaceLandmarker + SomnolenceDetector.
- Imprime por consola cada ~0.5s: MAR, EAR, estado bostezo, nº bostezos contados.
- Si hay display (cv2.imshow), muestra el frame con el MAR superpuesto. ESC sale.
- Guarda CSV con (t, mar, ear, yawning, yawns) para que me lo pases y ajustemos.

Con esto verás qué MAR das tú:
- boca cerrada / quieto  -> suele ser 0.15-0.35
- hablando normal       -> picos 0.5-0.75, oscilante
- habla fuerte / risa   -> sostiene 0.75-0.95
- bostezo real          -> sostiene 0.9-1.3 durante 2.5-5s

Si hablando superas `yawn_mar_threshold`, hay que subirlo. Si en quieto
ya marcas >0.6 de base, la cámara está lejos / hay ruido de landmarks.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analysis.ear_mar import eye_aspect_ratio, mouth_aspect_ratio
from app.analysis.landmarks import get_key_points
from app.analysis.somnolence import SomnolenceDetector
from app.common.config import load_default_config


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data/mar_log.csv")
    ap.add_argument("--seconds", type=float, default=120.0)
    ap.add_argument("--no-show", action="store_true", help="no abrir ventana, solo consola+CSV")
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
        print("Config efectiva: default + caché + override local")
    except Exception as e:
        print(f"Sin override/caché ({e}); usando defaults")
    th = dict(cfg.detection_thresholds or {})
    det = SomnolenceDetector(th)
    print(f"Umbrales: open={det.yawn_open:.2f} close={det.yawn_close:.2f} "
          f"pico_min={det.yawn_peak_min:.2f} min_dur={det.yawn_min_dur:.1f}s "
          f"max_dur={det.yawn_max_dur:.1f}s ratio_min={det.yawn_min_open_ratio:.2f}")

    from app.analysis.landmarks import LandmarkDetector
    try:
        lm_det = LandmarkDetector()
    except Exception as e:
        print(f"No se pudo cargar FaceLandmarker: {e}")
        print("Descarga modelos con: py scripts/download_models.py")
        return

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("No se pudo abrir la cámara 0")
        return

    csv_path = Path(args.csv)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(csv_path, "w", newline="", encoding="utf-8")
    wr = csv.writer(f)
    wr.writerow(["t_sec", "mar", "ear", "yawning", "yawns_counted", "events"])

    t0 = time.monotonic()
    last_print = 0.0
    print("Hablá 20s, quedate quieto 20s, bostezá (o abrí grande 3s) si podés. ESC sale.")
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                print("Frame vacío")
                await asyncio.sleep(0.1)
                continue
            now = time.monotonic()
            if now - t0 > args.seconds:
                break
            lm = await lm_det.process(frame)
            if lm is None:
                mar, ear = 0.0, 0.0
                evs: list = []
            else:
                kp = get_key_points(lm.landmarks)
                ear = (eye_aspect_ratio(kp["left_eye"]) + eye_aspect_ratio(kp["right_eye"])) / 2.0
                mar = mouth_aspect_ratio(kp["mouth"])
                evs = det.update(ear, mar, 0.0, now)
            for e in evs:
                print(f"*** EVENTO {e['event_type_id']} {e.get('message','')}")
            wr.writerow([f"{now-t0:.2f}", f"{mar:.3f}", f"{ear:.3f}",
                         int(det._yawning), len(det._yawns),
                         ";".join(e["event_type_id"] for e in evs)])
            if now - last_print >= 0.5:
                last_print = now
                flag = "BOSTEZANDO?" if det._yawning else "         "
                print(f"t={now-t0:5.1f}s MAR={mar:.2f} EAR={ear:.2f} {flag} "
                      f"bostezos={len(det._yawns)} pico={det._yawn_peak:.2f}")
            if not args.no_show:
                try:
                    cv2.putText(frame, f"MAR {mar:.2f} (open {det.yawn_open:.2f})",
                                (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
                    cv2.putText(frame, f"yawning={det._yawning} yawns={len(det._yawns)}",
                                (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                    cv2.imshow("debug_yawn - ESC sale", frame)
                    if cv2.waitKey(1) & 0xFF == 27:
                        break
                except Exception:
                    pass
            await asyncio.sleep(0.01)
    finally:
        f.close()
        cap.release()
        try:
            import cv2 as _cv
            _cv.destroyAllWindows()
        except Exception:
            pass
        print(f"CSV guardado en {csv_path} — pasámelo y ajustamos fino.")


if __name__ == "__main__":
    asyncio.run(main())
