"""Persist detector events with their connectivity state at capture time."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.common.models import build_telemetry_event, new_event_id

logger = logging.getLogger(__name__)


async def enqueue_events(
    buffer: Any,
    identity: Any,
    events: list,
    frame: Any = None,
    *,
    data_directory: Any = None,
    is_offline_sync: bool,
) -> int:
    """Store events and evidence, preserving whether capture happened offline."""
    if buffer is None:
        return 0

    try:
        from app.capture.evidence import save_event_frame
    except Exception as error:
        logger.warning("Evidence capture is unavailable; events will be queued without frames: %s", error)
        save_event_frame = None

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    device_id = getattr(identity, "device_id", "") or ""
    enqueued = 0
    for event in events:
        if not isinstance(event, dict):
            continue
        try:
            event_id = str(event.get("event_id") or new_event_id())
            event.setdefault("event_id", event_id)
            severity = str(event.get("severity", "INFO"))
            evidence_path = None
            if save_event_frame is not None and data_directory is not None:
                try:
                    if loop is None:
                        evidence_path = save_event_frame(frame, event_id, severity, data_directory)
                    else:
                        evidence_path = await loop.run_in_executor(
                            None,
                            lambda e=event_id, s=severity, f=frame: save_event_frame(
                                f, e, s, data_directory
                            ),
                        )
                except Exception as error:
                    logger.exception("Could not save evidence for event %s: %s", event_id, error)

            telemetry = build_telemetry_event(
                event,
                device_id=device_id,
                event_id=event_id,
                has_evidence=bool(evidence_path),
                is_offline_sync=is_offline_sync,
            )
            if evidence_path:
                telemetry["has_evidence"] = True
            if buffer.enqueue(event_id, telemetry, evidence_path):
                enqueued += 1
        except Exception as error:
            logger.exception("Could not queue detected event %r: %s", event.get("event_id"), error)

    if enqueued:
        logger.debug("Buffer: %d event(s) queued (offline_sync=%s)", enqueued, is_offline_sync)
    return enqueued
