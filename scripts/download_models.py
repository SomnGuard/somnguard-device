"""Descarga modelos de visión edge (HU-DEVICE-001).

Uso:
    py scripts/download_models.py [--only face|phone]

- face:  MediaPipe FaceLandmarker (~15MB, obligatorio).
- phone: MediaPipe EfficientDet-Lite0 (~12MB, detector COCO con clase
  `cell phone`; se guarda como models/phone_detector.tflite).

Sin dependencias nuevas (solo stdlib). El cinturón sigue desactivado por
flag (`belt_enabled=false`) hasta validación HW — no hay modelo que bajar.
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.request

URLS = {
    "face": (
        "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task",
        "models/face_landmarker.task",
    ),
    "phone": (
        "https://storage.googleapis.com/mediapipe-models/object_detector/efficientdet_lite0/float16/1/efficientdet_lite0.tflite",
        "models/phone_detector.tflite",
    ),
}


def _download(url: str, dest: str) -> None:
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        print(f"OK (ya existe): {dest}")
        return
    print(f"Descargando {url} -> {dest} ...")
    urllib.request.urlretrieve(url, dest)
    print(f"OK: {dest} ({os.path.getsize(dest)} bytes)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["face", "phone"], default=None)
    args = ap.parse_args()
    targets = [args.only] if args.only else ["face", "phone"]
    try:
        for name in targets:
            url, dest = URLS[name]
            _download(url, dest)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print("Cinturón: sin modelo (belt_enabled=false hasta HW).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
