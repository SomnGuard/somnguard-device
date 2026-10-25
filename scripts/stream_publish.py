"""Publicador MJPEG fase 1 (HU-DEVICE-005).

Uso:
    py scripts/stream_publish.py --device-id <uuid> --session-id <uuid>
    # o con .env: SOMNGUARD_API_URL + SOMNGUARD_DEVICE_ID + SOMNGUARD_API_KEY
    # SESSION_ID se detecta por poll si no se pasa.

Flujo: poll GET /stream/session -> abre cámara -> envía JPEG 640x480 q60
a 5fps por WS /ws/stream como {type:frame, session_id, data:base64}.
Requiere: pip install websockets opencv-python numpy
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.device.backend import BackendClient  # noqa: E402


def load_dotenv_simple() -> None:
    for name in (".env", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")):
        if os.path.isfile(name):
            for line in open(name, encoding="utf-8"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip().strip("\"'"))
            break


def api_base() -> str:
    return (os.environ.get("SOMNGUARD_API_URL") or "http://localhost:8080").rstrip("/")


def ws_url() -> str:
    explicit = os.environ.get("SOMNGUARD_WS_URL")
    if explicit:
        return explicit.rstrip("/")
    base = api_base()
    u = urllib.parse.urlparse(base)
    scheme = "wss:" if u.scheme == "https" else "ws:"
    return f"{scheme}//{u.netloc}/ws/stream"


def encode_jpg(frame, width=640, height=480, quality=60) -> bytes | None:
    import cv2

    small = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    return bytes(buf.tobytes()) if ok else None


async def main_async(device_id: str, api_key: str, session_id: str | None, fps: float,
                     width: int, height: int, quality: int) -> int:
    try:
        import cv2  # noqa
    except Exception:
        print("ERROR: falta opencv-python: pip install opencv-python numpy")
        return 2
    try:
        import websockets  # noqa
    except Exception:
        print("ERROR: falta websockets: pip install websockets")
        return 2

    import websockets as ws_lib

    backend = BackendClient(api_base())
    if not session_id:
        print("Poll GET /stream/session para detectar sesión...")
        for _ in range(30):
            try:
                body = backend.get_stream_session_sync(device_id, api_key)
            except Exception as e:
                print(f"poll falló: {e}")
                body = None
            if body:
                session_id = str(body.get("session_id") or body.get("sessionId") or "")
                if session_id:
                    print(f"Sesión detectada: {session_id}")
                    break
            await asyncio.sleep(2)
        if not session_id:
            print("Sin sesión activa. Abre 'Ver en vivo' en el portal y reintenta.")
            return 1

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("ERROR: no se pudo abrir la cámara 0")
        return 3

    url = ws_url()
    print(f"WS {url} session={session_id} {fps}fps (Ctrl+C para parar)")
    print("NOTA: la cámara solo la abre un proceso. Pausa app.main si publica a la vez.")
    interval = 1.0 / max(fps, 0.5)
    failures = 0
    try:
        async with ws_lib.connect(url, max_size=2 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"type": "hello", "session_id": session_id}))
            await ws.send(json.dumps({"type": "subscribe", "session_id": session_id}))
            while True:
                ok, frame = cap.read()
                if not ok:
                    failures += 1
                    if failures >= 15:
                        print("Cámara ocupada o sin frames (¿app.main corriendo?).")
                        print("Para app.main un momento y reintenta solo el publisher.")
                        break
                    await asyncio.sleep(0.2)
                    continue
                failures = 0
                jpg = await asyncio.to_thread(encode_jpg, frame, width, height, quality)
                if jpg is None:
                    continue
                b64 = base64.b64encode(jpg).decode("ascii")
                try:
                    await ws.send(json.dumps({"type": "frame", "session_id": session_id, "data": b64}))
                except Exception as e:
                    print(f"WS send falló: {e}")
                    break
                await asyncio.sleep(interval)
    except KeyboardInterrupt:
        print("Parado por usuario")
    finally:
        cap.release()
    return 0


def resolve_credentials(cli_device: str, cli_key: str) -> tuple[str, str]:
    """Env/flags primero; si faltan, lee data/device_identity.json (post self-register)."""
    device_id = (cli_device or "").strip()
    api_key = (cli_key or "").strip()
    if device_id and api_key:
        return device_id, api_key
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        data_dir = (os.environ.get("SOMNGUARD_DATA_DIR") or "data").strip() or "data"
        base = data_dir if os.path.isabs(data_dir) else os.path.join(root, data_dir)
        with open(os.path.join(base, "device_identity.json"), encoding="utf-8") as f:
            data = json.load(f)
        device_id = device_id or str(data.get("device_id") or "")
        api_key = api_key or str(data.get("api_key") or "")
    except Exception:
        pass
    return device_id, api_key


def main() -> int:
    load_dotenv_simple()
    p = argparse.ArgumentParser()
    p.add_argument("--device-id", default=os.environ.get("SOMNGUARD_DEVICE_ID", ""))
    p.add_argument("--api-key", default=os.environ.get("SOMNGUARD_API_KEY", ""))
    p.add_argument("--session-id", default=os.environ.get("STREAM_SESSION_ID", ""))
    p.add_argument("--fps", type=float, default=8.0)
    p.add_argument("--width", type=int, default=640)
    p.add_argument("--height", type=int, default=480)
    p.add_argument("--quality", type=int, default=55)
    a = p.parse_args()
    device_id, api_key = resolve_credentials(a.device_id, a.api_key)
    if not device_id or not api_key:
        print("Falta device_id/api_key: ni .env/flags ni data/device_identity.json los tienen.")
        print("Corre self-register primero (py -m app.main) o pasa --device-id/--api-key.")
        return 2
    return asyncio.run(main_async(device_id, api_key, a.session_id or None, a.fps,
                                 a.width, a.height, a.quality))


if __name__ == "__main__":
    raise SystemExit(main())
