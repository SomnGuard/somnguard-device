"""Publicador LiveKit SFU fase 2 (video fuera de red local).

Usa el frame compartido (``ctx.last_frame``) sin abrir la cámara dos veces.
Importación perezosa: sin ``livekit`` retorna disponible=False y se sigue
el relay P2P/MJPEG existente.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


def _silence_sdk_shutdown_noise() -> None:
    """El SDK livekit suelta asserts de FFI al apagar el intérprete (ruido
    inofensivo pero alarmante). Se filtran solo esos, nada más."""
    import sys

    prev = getattr(sys, "unraisablehook", None)

    def _hook(args: Any) -> None:
        try:
            err = args.exc_value
            where = str(getattr(args, "object", ""))
            if (
                isinstance(err, AssertionError)
                and ("_ffi_client" in where or "FfiHandle" in type(args.object).__name__)
            ):
                logger.debug("Ruido de cierre livekit FFI ignorado")
                return
        except Exception:
            pass
        if callable(prev):
            try:
                prev(args)
                return
            except Exception:
                pass
        try:
            sys.__unraisablehook__(args)
        except Exception:
            pass

    try:
        sys.unraisablehook = _hook  # type: ignore[assignment]
    except Exception:
        pass


def livekit_available() -> bool:
    try:
        import livekit.rtc  # noqa: F401
        return True
    except Exception:
        return False


async def publish_livekit(
    ctx: Any,
    session_id: str,
    room_url: str,
    token: str,
    width: int = 640,
    height: int = 480,
    fps: float = 8.0,
) -> None:
    """Publica a la room hasta que se acabe la sesión o el contexto."""
    from livekit import rtc

    _silence_sdk_shutdown_noise()
    # localhost en Windows resuelve a ::1 y Docker no escucha ahí: IPv4 directo.
    room_url = room_url.replace("://localhost", "://127.0.0.1").replace("://[::1]", "://127.0.0.1")
    backend = getattr(ctx, "backend", None)
    identity = getattr(ctx, "identity", None)
    stream = getattr(ctx, "stream", None)
    interval = 1.0 / max(0.5, min(15.0, fps))
    room = rtc.Room()
    source = None
    track = None
    try:
        await room.connect(room_url, token)
        logger.info("LiveKit conectado sesión=%s", session_id[:8])
        source = rtc.VideoSource(width, height)
        track = rtc.LocalVideoTrack.create_video_track("cam", source)
        await room.local_participant.publish_track(track)
        last_validate = time.monotonic()
        while getattr(ctx, "running", False):
            if stream is not None and not getattr(stream, "is_live", True):
                break
            try:
                interval = 1.0 / max(0.5, min(15.0, float(getattr(stream, "qos_fps", fps))))
            except Exception:
                pass
            frame = getattr(ctx, "last_frame", None)
            fresh = time.monotonic() - float(getattr(ctx, "last_frame_time", 0.0) or 0.0) < 2.0
            if frame is not None and fresh:
                buf = _to_rgba(frame, width, height)
                if buf is not None:
                    try:
                        source.capture_frame(rtc.VideoFrame(width, height, rtc.VideoBufferType.RGBA, buf))
                    except Exception as e:
                        logger.debug("LiveKit frame descartado: %s", e)
            if stream is not None:
                try:
                    stream.heartbeat_viewer()
                except Exception:
                    pass
            now = time.monotonic()
            if now - last_validate >= 10.0 and backend is not None and identity is not None:
                last_validate = now
                try:
                    from app.streaming.publisher import _poll_detection_paused

                    await _poll_detection_paused(ctx)
                except Exception:
                    pass
                try:
                    body = await backend.get_stream_session(
                        str(identity.device_id), str(identity.api_key))
                    sid = str((body or {}).get("session_id") or (body or {}).get("sessionId") or "")
                    if not body or (sid and sid != session_id):
                        break
                except Exception:
                    pass
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.info("LiveKit terminado: %s", e)
    finally:
        try:
            if track is not None:
                await room.local_participant.unpublish_track(track.sid)
        except Exception:
            pass
        try:
            await room.disconnect()
        except Exception:
            pass
        try:
            del track
            del source
            del room
        except Exception:
            pass
        # Sin gc.collect() a propósito: forzarlo aquí finaliza landmarkers
        # huérfanos en este hilo y su __del__ cuelga el loop (visto en campo).
        logger.info("LiveKit desconectado sesión=%s", session_id[:8])


def _to_rgba(frame: Any, width: int, height: int) -> bytes | None:
    try:
        import cv2

        small = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
        if len(small.shape) == 2:
            small = cv2.cvtColor(small, cv2.COLOR_GRAY2RGBA)
        else:
            small = cv2.cvtColor(small, cv2.COLOR_BGR2RGBA)
        return small.tobytes()
    except Exception:
        return None
