import csv
from datetime import datetime, timezone
import math
from pathlib import Path
from typing import Any

CSV_FIELDNAMES = ["timestamp", "topic", "x", "y", "z", "samplingRate", "payload"]


def parse_payload(payload: str) -> dict[str, float | int]:
    """Parse 'x;y;z;samplingRate' string into numeric fields.

    Raises ValueError if the format or numeric conversion is invalid.
    """
    parts = payload.strip().split(";")
    if len(parts) != 4:
        raise ValueError(
            f"Expected 4 semicolon-separated values (x;y;z;samplingRate), got {len(parts)}"
        )

    x = float(parts[0])
    y = float(parts[1])
    z = float(parts[2])
    sampling_rate_float = float(parts[3])

    if not all(math.isfinite(v) for v in (x, y, z, sampling_rate_float)):
        raise ValueError("Payload contains non-finite numeric values")

    sampling_rate: int | float = (
        int(sampling_rate_float)
        if sampling_rate_float.is_integer()
        else sampling_rate_float
    )

    return {
        "x": x,
        "y": y,
        "z": z,
        "samplingRate": sampling_rate,
    }


class CsvMessageStore:
    """Stores incoming MQTT messages in a local CSV file."""

    def __init__(self, file_path: Path) -> None:
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.file_path.exists() or self.file_path.stat().st_size == 0:
            with self.file_path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
                writer.writeheader()
                f.flush()

    def append_messages(self, topic: str, payload: str) -> list[dict[str, Any]]:
        """Parse and store one or more newline-separated payload lines in a single batch write."""
        lines = [line.strip() for line in payload.splitlines() if line.strip()]
        if not lines:
            parse_payload("")

        timestamp = datetime.now(timezone.utc).isoformat()
        records: list[dict[str, Any]] = []
        for clean_line in lines:
            parsed = parse_payload(clean_line)
            records.append(
                {
                    "timestamp": timestamp,
                    "topic": topic,
                    "payload": clean_line,
                    **parsed,
                }
            )

        with self.file_path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
            writer.writerows(records)
            f.flush()
        return records

    def append_message(self, topic: str, payload: str) -> dict[str, Any]:
        records = self.append_messages(topic, payload)
        return records[-1]

    def read_records(self) -> list[dict[str, Any]]:
        if not self.file_path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self.file_path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                sr_float = float(row["samplingRate"])
                sampling_rate: int | float = (
                    int(sr_float) if sr_float.is_integer() else sr_float
                )
                records.append(
                    {
                        "timestamp": row["timestamp"],
                        "topic": row["topic"],
                        "x": float(row["x"]),
                        "y": float(row["y"]),
                        "z": float(row["z"]),
                        "samplingRate": sampling_rate,
                        "payload": row["payload"],
                    }
                )
        return records
