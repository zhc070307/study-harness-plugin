"""UUIDv7 properties: version, variant, uniqueness, ordering and timestamp."""

from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timezone

from tests._support import _SRC  # noqa: F401  (ensures src/ on sys.path)
from study_agent.uuid7 import uuid7, uuid7_datetime, uuid7_from_datetime, uuid7_timestamp_ms


class Uuid7Tests(unittest.TestCase):
    def test_version_and_variant(self) -> None:
        value = uuid7()
        self.assertEqual(value.version, 7)
        self.assertEqual(value.variant, uuid.RFC_4122)

    def test_uniqueness(self) -> None:
        values = {str(uuid7()) for _ in range(5000)}
        self.assertEqual(len(values), 5000)

    def test_cross_millisecond_ordering(self) -> None:
        base = 1_760_000_000_000
        first = uuid7(base)
        second = uuid7(base + 1)
        third = uuid7(base + 1000)
        self.assertLess(first.bytes, second.bytes)
        self.assertLess(second.bytes, third.bytes)

    def test_same_millisecond_values_are_unique(self) -> None:
        base = 1_760_000_000_000
        values = [uuid7(base) for _ in range(300)]
        self.assertEqual(len(set(values)), 300)

    def test_implicit_stream_is_monotonic(self) -> None:
        # The implicit clock stream is strictly monotonic: same-millisecond
        # calls increment rand_a, and a backwards clock clamps forward.
        values = [uuid7() for _ in range(300)]
        for left, right in zip(values, values[1:]):
            self.assertLess(left.bytes, right.bytes)

    def test_timestamp_round_trip(self) -> None:
        moment = datetime(2026, 9, 9, 4, 0, 0, tzinfo=timezone.utc)
        value = uuid7_from_datetime(moment)
        self.assertEqual(uuid7_timestamp_ms(value), int(moment.timestamp() * 1000))
        self.assertEqual(uuid7_datetime(value), moment)

    def test_out_of_range_timestamp_rejected(self) -> None:
        with self.assertRaises(ValueError):
            uuid7(-1)
        with self.assertRaises(ValueError):
            uuid7(1 << 48)

    def test_naive_datetime_rejected(self) -> None:
        with self.assertRaises(ValueError):
            uuid7_from_datetime(datetime(2026, 9, 9, 12, 0, 0))


if __name__ == "__main__":
    unittest.main()
