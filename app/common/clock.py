from __future__ import annotations
import time
from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def monotonic() -> float:
    return time.monotonic()


def elapsed_since(start: float) -> float:
    return time.monotonic() - start