"""UUIDv7 generation (RFC 9562) using only the Python standard library.

Layout::

    48 bits  unix timestamp in milliseconds (big endian)
     4 bits  version (0b0111)
    12 bits  rand_a
     2 bits  variant (0b10)
    62 bits  rand_b

Generation is strictly monotonic for values produced through the implicit clock
(``timestamp_ms=None``): a same-millisecond call increments ``rand_a`` and a
backwards clock reuses the last timestamp, so byte order still matches creation
order. An explicit ``timestamp_ms`` is used exactly as supplied so tests and
migrations can reproduce a deterministic timestamp component.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

_LOCK = threading.Lock()
_LAST_MS = -1
_LAST_RAND_A = 0

MAX_TIMESTAMP_MS = (1 << 48) - 1


def _random_bits(count: int) -> int:
    """Return ``count`` uniformly random bits from ``os.urandom``."""
    byte_count = (count + 7) // 8
    return int.from_bytes(os.urandom(byte_count), "big") >> (byte_count * 8 - count)


def uuid7(timestamp_ms: Optional[int] = None) -> uuid.UUID:
    """Return a new UUIDv7.

    ``timestamp_ms`` may be supplied by tests or by an injected clock; when
    omitted the current wall clock is used.
    """
    global _LAST_MS, _LAST_RAND_A
    with _LOCK:
        if timestamp_ms is None:
            timestamp_ms = time.time_ns() // 1_000_000
            if not 0 <= timestamp_ms <= MAX_TIMESTAMP_MS:
                raise ValueError("timestamp_ms out of range for UUIDv7")
            if timestamp_ms < _LAST_MS:
                # A backwards clock must not break ordering.
                timestamp_ms = _LAST_MS
        else:
            # Explicit timestamps are used exactly as supplied so tests and
            # migrations can reproduce a deterministic timestamp component.
            timestamp_ms = int(timestamp_ms)
            if not 0 <= timestamp_ms <= MAX_TIMESTAMP_MS:
                raise ValueError("timestamp_ms out of range for UUIDv7")

        if timestamp_ms == _LAST_MS:
            # Same millisecond: increment rand_a so byte order still matches
            # creation order, rolling over into the next millisecond.
            rand_a = _LAST_RAND_A + 1
            if rand_a >= (1 << 12):
                timestamp_ms += 1
                if timestamp_ms > MAX_TIMESTAMP_MS:
                    raise ValueError("UUIDv7 timestamp overflow")
                rand_a = _random_bits(12)
        else:
            rand_a = _random_bits(12)
        _LAST_MS = timestamp_ms
        _LAST_RAND_A = rand_a

        rand_b = _random_bits(62)
        value = (
            (timestamp_ms << 80)
            | (0x7 << 76)
            | (rand_a << 64)
            | (0b10 << 62)
            | rand_b
        )
        return uuid.UUID(int=value)


def uuid7_timestamp_ms(value: uuid.UUID) -> int:
    """Extract the 48-bit millisecond timestamp from a UUIDv7."""
    return (value.int >> 80) & MAX_TIMESTAMP_MS


def uuid7_datetime(value: uuid.UUID) -> datetime:
    """Return the embedded timestamp as an aware UTC datetime."""
    return datetime.fromtimestamp(uuid7_timestamp_ms(value) / 1000.0, tz=timezone.utc)


def uuid7_from_datetime(moment: datetime) -> uuid.UUID:
    """Create a UUIDv7 whose timestamp comes from ``moment``."""
    if moment.tzinfo is None:
        raise ValueError("uuid7_from_datetime requires a timezone-aware datetime")
    return uuid7(int(moment.timestamp() * 1000))
