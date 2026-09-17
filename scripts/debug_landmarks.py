"""Overlay de landmarks de boca/ojos — ver DÓNDE caen los puntos del MAR.

Uso:
    py scripts/debug_landmarks.py

Abre la ventana con tu cara: dibuja los 6 puntos de boca (índices + rol
p1..p6) y los 12 de ojos, más el ancho/alto en px que forman el MAR.
- Abre grande la boca: los puntos verdes deben pegarse a tus labios y el
  MAR (= alto/ancho) debe subir >0.8.
- Si los puntos quedan flotando fuera de los labios o el MAR no se mueve,
  saca captura y pásala: los índices están mal para tu modelo/cámara.

ESC sale.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analysis.ear_mar import eye_aspect_ratio, mouth_aspect_ratio
from app.analysis.landmarks import MOUTH_IDX, LandmarkDetector, get_key_points

ROLES = ["p1", "p2", "p3", "p4", "p5", "p6"]
EYE_IDX = [33, 160, 158, 133, 153, 144, 362, 385, 387, 263, 373, 380]


async def main() -> None:
    try:
        import cv2
    except ImportError:
        print("Falta opencv-python: pip install opencv-python")
        return
    try:
        lm_det = LandmarkDetector()
    except Exception as e:
        print(f"Sin FaceLandmarker: {e} (py scripts/download_models.py)")
        return
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("No se pudo abrir la cámara 0")
        return
    print("Mirá al frente, luego ABRÍ GRANDE 3s. Fijate si los puntos verdes siguen tus labios. ESC sale.")
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                await asyncio.sleep(0.1)
                continue
            h, w = frame.shape[:2]
            lm = await lm_det.process(frame)
            if lm is None:
                cv2.putText(frame, "SIN CARA", (10, 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 2)
            else:
                pts = lm.landmarks
                kp = get_key_points(pts)
                mar = mouth_aspect_ratio(kp["mouth"])
                ear = (eye_aspect_ratio(kp["left_eye"]) + eye_aspect_ratio(kp["right_eye"])) / 2.0
                for (idx, role) in zip(MOUTH_IDX, ROLES):
                    x = int(pts[idx, 0] * w)
                    y = int(pts[idx, 1] * h)
                    cv2.circle(frame, (x, y), 5, (0, 255, 0), -1)
                    cv2.putText(frame, f"{role}:{idx}", (x + 7, y - 7),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 0), 1)
                for idx in EYE_IDX:
                    x = int(pts[idx, 0] * w)
                    y = int(pts[idx, 1] * h)
                    cv2.circle(frame, (x, y), 3, (255, 0, 0), -1)
                m = kp["mouth"][:, :2] * [w, h]
                import numpy as np
                width = float(np.linalg.norm(m[0] - m[3]))
                h1 = float(np.linalg.norm(m[1] - m[5]))
                h2 = float(np.linalg.norm(m[2] - m[4]))
                cv2.putText(frame, f"MAR {mar:.2f} (ancho {width:.0f}px altos {h1:.0f}/{h2:.0f}px)",
                            (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
                cv2.putText(frame, f"EAR {ear:.2f}", (10, 60),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 0, 0), 2)
            cv2.imshow("debug_landmarks - ESC sale", frame)
            if cv2.waitKey(1) & 0xFF == 27:
                break
            await asyncio.sleep(0.01)
    finally:
        cap.release()
        try:
            cv2.destroyAllWindows()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
