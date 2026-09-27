"""Streaming en vivo post-MVP (HU-DEVICE-005, ADR-013 Propuesta).

Iteración 1: polling + estado local + frames JPEG 640x480.
Sin aiortc/websocket aún: el Pi consulta GET /stream/session y expone
frames JPEG para el futuro publisher WebRTC. No toca la detección.
"""
from app.streaming.session import StreamManager, StreamState
from app.streaming.frames import encode_live_frame
from app.streaming.publisher import run_stream_loop

__all__ = ["StreamManager", "StreamState", "encode_live_frame", "run_stream_loop"]
