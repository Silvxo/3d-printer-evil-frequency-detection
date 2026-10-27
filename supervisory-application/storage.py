from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


class JsonMessageStore:
    """Stores incoming MQTT messages in a local JSON file."""

    def __init__(self, file_path: Path) -> None:
        self.file_path = file_path
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.file_path.exists():
            self._write_records([])

    def _read_records(self) -> list[dict[str, Any]]:
        if not self.file_path.exists():
            return []
        try:
            content = self.file_path.read_text(encoding="utf-8").strip()
            if not content:
                return []
            data = json.loads(content)
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def _write_records(self, records: list[dict[str, Any]]) -> None:
        temp_path = self.file_path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(records, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(self.file_path)

    def append_message(self, topic: str, payload: str) -> dict[str, Any]:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "topic": topic,
            "payload": payload,
        }
        records = self._read_records()
        records.append(record)
        self._write_records(records)
        return record
