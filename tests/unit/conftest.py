"""Stubs de dependencias pesadas para tests sin hardware (cv2/mediapipe).

numpy es real (instalada). cv2/mediapipe/simpleaudio se sustituyen por
MagicMock si no están importables, para poder probar la lógica edge
(estados, config, identidad, backend, escalamiento) sin cámara ni red.
"""
import sys
from unittest.mock import MagicMock

for _name in ("cv2", "mediapipe", "simpleaudio"):
    if _name not in sys.modules:
        try:
            __import__(_name)
        except Exception:
            sys.modules[_name] = MagicMock(name=f"stub-{_name}")
