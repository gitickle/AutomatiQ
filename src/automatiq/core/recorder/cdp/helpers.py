"""Shared CDP helpers — timestamp calibration and header reconciliation."""

import time
from datetime import UTC, datetime


class TimestampConverter:
    """Converts CDP MonotonicTime to human-readable ISO 8601 timestamps."""

    def __init__(self):
        self.monotonic_to_wall_offset: float | None = None
        self.offsets_collected = []

    def calibrate(self, monotonic_time: float, wall_time: float) -> None:
        if len(self.offsets_collected) >= 5:
            return
        offset = wall_time - monotonic_time
        self.offsets_collected.append(offset)
        self.monotonic_to_wall_offset = sum(self.offsets_collected) / len(self.offsets_collected)

    def to_unix_timestamp(self, monotonic_time: float) -> float:
        if self.monotonic_to_wall_offset is None:
            self.monotonic_to_wall_offset = time.time() - monotonic_time
        return monotonic_time + self.monotonic_to_wall_offset

    def to_iso8601(self, monotonic_time: float) -> str:
        unix_timestamp = self.to_unix_timestamp(monotonic_time)
        dt = datetime.fromtimestamp(unix_timestamp, tz=UTC)
        return dt.isoformat(timespec="milliseconds")

    def current_iso8601(self) -> str:
        return datetime.now(UTC).isoformat(timespec="milliseconds")


def merge_headers(req, extra_headers):
    if not extra_headers:
        return
    if not req["response_data"]:
        req["response_data"] = {
            "status": 0,
            "headers": {},
            "body": None,
            "base64_encoded": False,
            "charset": "utf-8",
        }
    current = req["response_data"]["headers"]
    for k, v in extra_headers.items():
        current[k] = v
