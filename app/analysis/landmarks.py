from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import cv2
import mediapipe as mp
import numpy as np


logger = logging.getLogger(__name__)


@dataclass
class FaceLandmarks:
    landmarks: np.ndarray          # (468, 3) normalized x,y,z
    bbox: tuple[int, int, int, int]  # x, y, w, h en pixels
    score: float                   # confidence score
    image_shape: tuple[int, int]   # h, w


class LandmarkDetector:
    def __init__(
        self,
        min_detection_confidence: float = 0.5,
        min_tracking_confidence: float = 0.5,
        max_num_faces: int = 1,
        model_path: str = "models/face_landmarker.task",
    ):
        self.min_detection_confidence = min_detection_confidence
        self.min_tracking_confidence = min_tracking_confidence
        self.max_num_faces = max_num_faces
        self.model_path = model_path
        try:
            self._landmarker = mp.tasks.vision.FaceLandmarker.create_from_options(
                mp.tasks.vision.FaceLandmarkerOptions(
                    base_options=mp.tasks.BaseOptions(
                        model_asset_path=model_path,
                    ),
                    running_mode=mp.tasks.vision.RunningMode.LIVE_STREAM,
                    num_faces=max_num_faces,
                    min_face_detection_confidence=min_detection_confidence,
                    min_face_presence_confidence=min_tracking_confidence,
                    min_tracking_confidence=min_tracking_confidence,
                    output_face_blendshapes=False,
                    output_facial_transformation_matrixes=False,
                    result_callback=self._result_callback,
                )
            )
        except Exception as e:
            raise RuntimeError(f"No se pudo cargar {model_path}: {e}") from e
        self._latest_result: Optional[mp.tasks.vision.FaceLandmarkerResult] = None
        self._initialized = True
        logger.debug("FaceLandmarker inicializado (MediaPipe Tasks API)")

    def _result_callback(self, result: mp.tasks.vision.FaceLandmarkerResult, output_image: mp.Image, timestamp_ms: int):
        self._latest_result = result

    async def process(self, frame: np.ndarray) -> Optional[FaceLandmarks]:
        if frame is None or frame.size == 0:
            return None

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        timestamp_ms = int(asyncio.get_running_loop().time() * 1000)

        # detect_async puede colgar su dispatcher interno (visto en campo:
        # loop parado con wait eterno). Va a executor con timeout: si falla,
        # se salta el frame y tras racha se recrea el landmarker.
        try:
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, self._landmarker.detect_async, mp_image, timestamp_ms),
                timeout=2.0,
            )
            self._detect_timeouts = 0
        except asyncio.TimeoutError:
            self._detect_timeouts = getattr(self, "_detect_timeouts", 0) + 1
            logger.warning(
                "MediaPipe sin respuesta %.1fs (racha %d): salto de frame",
                2.0, self._detect_timeouts,
            )
            if self._detect_timeouts >= 5:
                self._detect_timeouts = 0
                await self._recreate_landmarker()
            return None
        except Exception as e:
            logger.debug("detect_async falló: %s", e)
            return None

        # Esperar resultado (polling simple)
        for _ in range(10):
            if self._latest_result is not None:
                break
            await asyncio.sleep(0.01)

        if not self._latest_result or not self._latest_result.face_landmarks:
            return None

        face_landmarks = self._latest_result.face_landmarks[0]
        h, w = frame.shape[:2]

        landmarks = np.array(
            [[lm.x, lm.y, lm.z] for lm in face_landmarks],
            dtype=np.float32,
        )

        xs = (landmarks[:, 0] * w).astype(int)
        ys = (landmarks[:, 1] * h).astype(int)
        x_min, x_max = xs.min(), xs.max()
        y_min, y_max = ys.min(), ys.max()
        bbox = (x_min, y_min, x_max - x_min, y_max - y_min)

        return FaceLandmarks(
            landmarks=landmarks,
            bbox=bbox,
            score=1.0,
            image_shape=(h, w),
        )

    def close(self) -> None:
        if self._initialized:
            try:
                self._landmarker.close()
            except Exception:
                pass
            self._initialized = False

    async def _recreate_landmarker(self) -> None:
        """Recupera un dispatcher atascado (cierra con timeout y recrea)."""
        logger.warning("Recreando FaceLandmarker tras racha de timeouts")
        old = getattr(self, "_landmarker", None)
        try:
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, self._safe_close, old), timeout=5.0)
        except Exception as e:
            logger.debug("Cierre viejo landmarker falló: %s", e)
        try:
            import mediapipe as mp

            loop = asyncio.get_running_loop()
            self._landmarker = await loop.run_in_executor(None, self._create_landmarker)
            self._latest_result = None
            self._initialized = True
            logger.info("FaceLandmarker recreado")
        except Exception as e:
            logger.error("No se pudo recrear landmarker: %s", e)

    @staticmethod
    def _safe_close(landmarker: object) -> None:
        try:
            if landmarker is not None and hasattr(landmarker, "close"):
                landmarker.close()
        except Exception:
            pass

    def _create_landmarker(self) -> object:
        import mediapipe as mp

        return mp.tasks.vision.FaceLandmarker.create_from_options(
            mp.tasks.vision.FaceLandmarkerOptions(
                base_options=mp.tasks.BaseOptions(model_asset_path=self.model_path),
                running_mode=mp.tasks.vision.RunningMode.LIVE_STREAM,
                num_faces=self.max_num_faces,
                min_face_detection_confidence=self.min_detection_confidence,
                min_face_presence_confidence=self.min_tracking_confidence,
                min_tracking_confidence=self.min_tracking_confidence,
                output_face_blendshapes=False,
                output_facial_transformation_matrixes=False,
                result_callback=self._result_callback,
            )
        )


def get_key_points(landmarks: np.ndarray) -> dict[str, np.ndarray]:
    return {
        "left_eye": landmarks[[33, 160, 158, 133, 153, 144]],      # 6 pts
        "right_eye": landmarks[[362, 385, 387, 263, 373, 380]],   # 6 pts
        # Boca con apertura INTERNA (p1..p6 para la fórmula MAR):
        # p1=61/p4=291 comisuras externas (ancho estable),
        # p2=13/p6=14 labio interno centro (alto principal),
        # p3=82/p5=87 labio interno izq (alto secundario).
        # El set anterior (84/17/314/405/320, borde externo) saturaba en
        # ~0.6 con la boca muy abierta de frente y solo subía al girar
        # (acortamiento del ancho por perspectiva): imposible de umbralizar.
        "mouth": landmarks[[61, 13, 82, 291, 87, 14]],             # 6 pts
        "nose_tip": landmarks[1:2],                                 # 1 pt
        "chin": landmarks[152:153],                                 # 1 pt
        "left_ear": landmarks[234:235],                             # 1 pt
        "right_ear": landmarks[454:455],                            # 1 pt
        "forehead": landmarks[10:11],                               # 1 pt
    }


# Índices de boca en orden p1..p6 (fuente única para overlays/debug).
MOUTH_IDX = [61, 13, 82, 291, 87, 14]