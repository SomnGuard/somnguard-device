"""WebRTC fase 2 (HU-DEVICE-005 AC-001/AC-002).

Pista de video H.264 vía aiortc alimentada del frame compartido
(``ctx.last_frame``). Importación perezosa: sin aiortc/av retorna
disponible=False y el publisher sigue en MJPEG.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


def webrtc_available() -> bool:
    try:
        import aiortc  # noqa: F401
        import av  # noqa: F401
        return True
    except Exception:
        return False


def _get_frame_provider(ctx: Any) -> Callable[[], Any]:
    def _get() -> Any:
        frame = getattr(ctx, "last_frame", None)
        fresh = time.monotonic() - float(getattr(ctx, "last_frame_time", 0.0) or 0.0) < 2.0
        return frame if (frame is not None and fresh) else None

    return _get


async def create_offer(ctx: Any, width: int = 640, height: int = 480) -> tuple[Any, str]:
    """Crea RTCPeerConnection sendonly H.264 y retorna (pc, sdp_offer)."""
    from aiortc import RTCPeerConnection, RTCRtpSender, RTCSessionDescription

    from app.streaming.webrtc_track import make_camera_track

    pc = RTCPeerConnection()
    stream = getattr(ctx, "stream", None)

    def _qos_fps() -> float:
        try:
            return float(getattr(stream, "qos_fps", 10.0) or 10.0)
        except Exception:
            return 10.0

    track = make_camera_track(_get_frame_provider(ctx), width=width, height=height, fps=_qos_fps)
    sender = pc.addTrack(track)
    try:
        caps = RTCRtpSender.getCapabilities("video").codecs
        h264 = [c for c in caps if c.mimeType == "video/H264"]
        if h264:
            for tr in pc.getTransceivers():
                if tr.sender == sender:
                    tr.setCodecPreferences(h264)
                    break
    except Exception as e:
        logger.debug("Sin preferencia H.264: %s", e)
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    # Gathering corto: host local sale en ms; el resto llega por trickle.
    for _ in range(20):
        if pc.iceGatheringState == "complete":
            break
        await asyncio.sleep(0.1)
    desc = pc.localDescription
    return pc, (desc.sdp if desc else offer.sdp)


async def handle_answer(pc: Any, sdp: str) -> None:
    from aiortc import RTCSessionDescription

    await pc.setRemoteDescription(RTCSessionDescription(sdp, "answer"))
    logger.info("WebRTC answer aplicada")


async def handle_remote_ice(pc: Any, candidate: Any) -> None:
    """ICE remoto best-effort (localhost conecta con candidatos host del SDP)."""
    try:
        from aiortc import RTCIceCandidate

        c = candidate if isinstance(candidate, dict) else {}
        sdp = c.get("candidate", "")
        if not sdp:
            return
        try:
            from aiortc.sdp import candidate_from_sdp

            cand = candidate_from_sdp(sdp)
            cand.sdpMid = c.get("sdpMid")
            cand.sdpMLineIndex = c.get("sdpMLineIndex")
            await pc.addIceCandidate(cand)
        except Exception:
            await pc.addIceCandidate(
                RTCIceCandidate(
                    component=1,
                    foundation="0",
                    ip="127.0.0.1",
                    port=9,
                    priority=0,
                    protocol="udp",
                    type="host",
                    sdpMid=c.get("sdpMid"),
                    sdpMLineIndex=c.get("sdpMLineIndex"),
                )
            )
    except Exception as e:
        logger.debug("ICE remoto ignorado: %s", e)


def is_connected(pc: Any) -> bool:
    try:
        return getattr(pc, "connectionState", "") == "connected"
    except Exception:
        return False


async def close_pc(pc: Any) -> None:
    try:
        await pc.close()
    except Exception:
        pass
