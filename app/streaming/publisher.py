"""Publicador integrado HU-DEVICE-005 (un solo proceso, comparte cámara).

Usa el último frame del loop de captura (``ctx.last_frame``) en vez de abrir
la cámara dos veces. Solo publica si: streaming habilitado + credenciales +
cámara + estado ACTIVO + sesión activa en backend.

Sin ``websockets`` instalado se desactiva solo (warn una vez).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import urllib.parse
from typing import Any

from app.streaming.frames import encode_live_frame
from app.streaming.session import StreamManager

logger = logging.getLogger(__name__)

_logged_no_ws = False


def load_stream_settings() -> dict[str, Any]:
    enabled_raw = (os.getenv("SOMNGUARD_STREAM_ENABLED", "") or "").strip().lower()
    enabled = enabled_raw in ("1", "true", "yes", "on")
    poll_sec = float(os.getenv("SOMNGUARD_STREAM_POLL_SEC", "5") or 5)
    width, height, fps, quality, auto_stop = 640, 480, 8.0, 55, 30.0
    try:
        import json as _json
        from pathlib import Path

        cfg = Path(__file__).parent.parent.parent / "config" / "device.default.json"
        if cfg.is_file():
            data = _json.loads(cfg.read_text(encoding="utf-8"))
            if not enabled_raw:
                enabled = bool(data.get("streaming_enabled", False))
            width = int(data.get("stream_width", width) or width)
            height = int(data.get("stream_height", height) or height)
            fps = float(data.get("stream_fps", fps) or fps)
            poll_sec = float(os.getenv("SOMNGUARD_STREAM_POLL_SEC", "") or data.get("stream_poll_sec", poll_sec) or poll_sec)
            auto_stop = float(data.get("stream_auto_stop_sec", auto_stop) or auto_stop)
    except Exception:
        pass
    return {
        "enabled": enabled,
        "poll_sec": min(60.0, max(2.0, poll_sec)),
        "width": width,
        "height": height,
        "fps": max(0.5, min(10.0, fps)),
        "quality": quality,
        "auto_stop_sec": auto_stop,
    }


def ws_url_for(api_base: str) -> str:
    explicit = (os.getenv("SOMNGUARD_WS_URL", "") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    u = urllib.parse.urlparse((api_base or "http://localhost:8080").rstrip("/"))
    scheme = "wss:" if u.scheme == "https" else "ws:"
    return f"{scheme}//{u.netloc}/ws/stream"


async def run_stream_loop(ctx: Any) -> None:
    """Loop del publisher. Nunca lanza: ante error duerme y reintenta."""
    global _logged_no_ws
    try:
        import websockets  # noqa: F401
    except Exception:
        if not _logged_no_ws:
            logger.warning("Streaming desactivado: falta 'websockets' (py -m pip install websockets)")
            _logged_no_ws = True
        while getattr(ctx, "running", False):
            await asyncio.sleep(60.0)
        return

    import websockets as ws_lib

    settings = load_stream_settings()
    stream: StreamManager | None = getattr(ctx, "stream", None)
    if stream is None:
        stream = StreamManager(
            width=settings["width"], height=settings["height"], fps=int(settings["fps"]),
            auto_stop_sec=settings["auto_stop_sec"], enabled=settings["enabled"],
        )
        ctx.stream = stream
    else:
        stream.enabled = settings["enabled"]

    if not settings["enabled"]:
        logger.info("Streaming en vivo desactivado (SOMNGUARD_STREAM_ENABLED=true para activar)")
        while getattr(ctx, "running", False):
            await asyncio.sleep(30.0)
        return

    logger.info(
        "Streaming en vivo activo: poll %.0fs, %dx%d q%d %.1ffps (solo ACTIVO + sesión)",
        settings["poll_sec"], settings["width"], settings["height"],
        settings["quality"], settings["fps"],
    )
    interval = 1.0 / settings["fps"]
    while getattr(ctx, "running", False):
        try:
            await _tick_once(ctx, stream, settings, interval, ws_lib)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("Stream tick falló: %s", e)
        await asyncio.sleep(settings["poll_sec"])


async def _tick_once(ctx: Any, stream: StreamManager, settings: dict[str, Any],
                     interval: float, ws_lib: Any) -> None:
    from app.common.models import DeviceState

    backend = getattr(ctx, "backend", None)
    identity = getattr(ctx, "identity", None)
    device_id = getattr(identity, "device_id", None)
    api_key = getattr(identity, "api_key", None)
    if backend is None or not device_id or not api_key:
        return
    t0 = time.monotonic()
    try:
        body = await backend.get_stream_session(str(device_id), str(api_key))
    except Exception:
        if stream.is_live:
            stream.tick()
        return
    latency = time.monotonic() - t0
    if not body:
        # Sin sesión el Pi corta de inmediato: no gasta datos ni CPU.
        if stream.is_live:
            stream.stop()
            logger.info("Stream detenido (sin sesión en backend, 0 frames)")
        return
    session_id = str(body.get("session_id") or body.get("sessionId") or "")
    if not session_id:
        return
    stream.adapt_bitrate(latency)
    is_active = getattr(ctx, "current_state", None) is DeviceState.ACTIVO
    if not is_active:
        if stream.is_live:
            stream.stop()
        return
    stream.wants_view(session_id)
    stream.heartbeat_viewer()
    await _publish_session(ctx, stream, settings, interval, session_id, ws_lib)


async def _publish_session(ctx: Any, stream: StreamManager, settings: dict[str, Any],
                           interval: float, session_id: str, ws_lib: Any) -> None:
    backend = getattr(ctx, "backend", None)
    identity = getattr(ctx, "identity", None)
    api_base = str(getattr(ctx, "env_config", {}).get("api_url", "http://localhost:8080"))
    url = ws_url_for(api_base)
    try:
        async with ws_lib.connect(url, max_size=2 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"type": "hello", "session_id": session_id}))
            await ws.send(json.dumps({"type": "subscribe", "session_id": session_id}))
            logger.info("Publicando vivo sesión=%s", session_id[:8])
            stop_evt = asyncio.Event()
            state: dict[str, Any] = {"pc": None}
            state["pc"] = await _maybe_start_webrtc(ctx, ws, session_id, settings)
            recv_task = asyncio.create_task(_recv_loop(ctx, ws, state, session_id, settings, stop_evt))
            last_validate = time.monotonic()
            try:
                while getattr(ctx, "running", False) and stream.is_live and not stop_evt.is_set():
                    from app.streaming.webrtc import is_connected as _rtc_on

                    pc = state.get("pc")
                    if pc is not None and _rtc_on(pc):
                        # WebRTC conectado: sin MJPEG (ahorra datos).
                        stream.heartbeat_viewer()
                    else:
                        frame = getattr(ctx, "last_frame", None)
                        fresh = time.monotonic() - float(getattr(ctx, "last_frame_time", 0.0) or 0.0) < 2.0
                        if frame is not None and fresh:
                            jpg = encode_live_frame(frame, settings["width"], settings["height"], settings["quality"])
                            if jpg is not None:
                                try:
                                    b64 = base64.b64encode(jpg).decode("ascii")
                                    await ws.send(json.dumps(
                                        {"type": "frame", "session_id": session_id, "data": b64}))
                                except Exception as e:
                                    logger.info("WS vivo cortado: %s", e)
                                    break
                    if stream.tick():
                        logger.info("Stream auto-stop 30s sin viewer")
                        break
                    # Revalida sesión cada 10s (no cada frame: saturaba la API a 8 req/s).
                    now = time.monotonic()
                    if now - last_validate >= 10.0:
                        last_validate = now
                        try:
                            body = await backend.get_stream_session(
                                str(identity.device_id), str(identity.api_key))
                            sid = str((body or {}).get("session_id") or (body or {}).get("sessionId") or "")
                            if not body or (sid and sid != session_id):
                                stream.stop()
                                break
                            stream.heartbeat_viewer()
                        except Exception:
                            pass
                    await asyncio.sleep(interval)
            finally:
                recv_task.cancel()
                try:
                    from app.streaming.webrtc import close_pc as _close

                    if state.get("pc") is not None:
                        await _close(state["pc"])
                    state["pc"] = None
                except Exception:
                    pass
    except Exception as e:
        logger.info("Publicador vivo terminado: %s", e)
    finally:
        stream.stop()


async def _maybe_start_webrtc(ctx: Any, ws: Any, session_id: str, settings: dict[str, Any]) -> Any:
    """Oferta WebRTC H.264 best-effort; None = sigue MJPEG."""
    try:
        from app.streaming.webrtc import create_offer, webrtc_available

        if not webrtc_available():
            return None
        pc, sdp = await create_offer(ctx, width=int(settings["width"]), height=int(settings["height"]))
        await ws.send(json.dumps({"type": "offer", "session_id": session_id, "sdp": sdp}))
        logger.info("WebRTC offer enviada (H.264)")
        return pc
    except Exception as e:
        logger.info("WebRTC no disponible, sigue MJPEG: %s", e)
        return None


async def _recv_loop(ctx: Any, ws: Any, state: dict[str, Any], session_id: str,
                   settings: dict[str, Any], stop_evt: Any) -> None:
    """Answer/ICE/stop/request-offer del viewer."""
    try:
        from app.streaming.webrtc import (
            close_pc,
            create_offer,
            handle_answer,
            handle_remote_ice,
            is_connected,
            webrtc_available,
        )
    except Exception:
        return
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if not isinstance(msg, dict) or msg.get("session_id") != session_id:
                continue
            t = msg.get("type")
            try:
                pc = state.get("pc")
                if t == "answer" and pc is not None and msg.get("sdp"):
                    await handle_answer(pc, str(msg["sdp"]))
                elif t == "ice" and pc is not None and msg.get("candidate") is not None:
                    await handle_remote_ice(pc, msg["candidate"])
                elif t == "request-offer" and webrtc_available():
                    # El viewer llegó tarde o reintentó: re-oferta (relay sin memoria).
                    if pc is None or not is_connected(pc):
                        if pc is not None:
                            await close_pc(pc)
                        fresh, sdp = await create_offer(
                            ctx, width=int(settings["width"]), height=int(settings["height"]))
                        state["pc"] = fresh
                        await ws.send(json.dumps(
                            {"type": "offer", "session_id": session_id, "sdp": sdp}))
                        logger.info("WebRTC re-offer enviada (H.264)")
                elif t == "stop":
                    stop_evt.set()
                    break
            except Exception as e:
                logger.debug("Señal WS ignorada: %s", e)
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
