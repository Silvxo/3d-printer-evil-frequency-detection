from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from main import MQTT_TOPIC, create_client
from storage import JsonMessageStore


class DummyMQTTMessage:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


class SupervisoryAppTests(unittest.TestCase):
    def test_json_message_store_initializes_and_appends(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            json_path = Path(tmpdir) / "data" / "messages.json"
            store = JsonMessageStore(json_path)

            self.assertTrue(json_path.exists())
            self.assertEqual(json.loads(json_path.read_text(encoding="utf-8")), [])

            first = store.append_message(MQTT_TOPIC, "Raw [X: 10 Y: -5 Z: 250]")
            second = store.append_message(MQTT_TOPIC, "Raw [X: 12 Y: -3 Z: 255]")

            saved = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(len(saved), 2)
            self.assertEqual(saved[0]["topic"], MQTT_TOPIC)
            self.assertEqual(saved[0]["payload"], "Raw [X: 10 Y: -5 Z: 250]")
            self.assertEqual(saved[0]["timestamp"], first["timestamp"])
            self.assertEqual(saved[1]["payload"], "Raw [X: 12 Y: -3 Z: 255]")
            self.assertEqual(saved[1]["timestamp"], second["timestamp"])

    def test_on_message_callback_stores_and_prints_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            json_path = Path(tmpdir) / "messages.json"
            store = JsonMessageStore(json_path)
            client = create_client(store=store, topic=MQTT_TOPIC)

            sample_payload = (
                "Raw [X:    14 Y:    -2 Z:   254] | "
                "Accel [X:+0.055 g, Y:-0.008 g, Z:+0.992 g]"
            )
            msg = DummyMQTTMessage(
                topic=MQTT_TOPIC,
                payload=sample_payload.encode("utf-8"),
            )

            stdout_buf = io.StringIO()
            with redirect_stdout(stdout_buf):
                client.on_message(client, None, msg)

            output = stdout_buf.getvalue()
            self.assertIn(MQTT_TOPIC, output)
            self.assertIn(sample_payload, output)

            saved = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0]["topic"], MQTT_TOPIC)
            self.assertEqual(saved[0]["payload"], sample_payload)


if __name__ == "__main__":
    unittest.main()
