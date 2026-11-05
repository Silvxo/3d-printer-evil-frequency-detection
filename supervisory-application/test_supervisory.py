from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from main import (
    MQTT_TOPIC,
    RecordingSessionController,
    create_client,
    create_http_server,
    create_section_store,
)
from section_manager import SectionManager
from simulate_sensor import format_payload, generate_batch_payload, generate_sample
from storage import CsvMessageStore, parse_payload


class DummyMQTTMessage:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


class SupervisoryAppTests(unittest.TestCase):
    def test_parse_payload_valid(self) -> None:
        parsed_int_rate = parse_payload("0.016;-0.008;1.000;100")
        self.assertEqual(
            parsed_int_rate,
            {"x": 0.016, "y": -0.008, "z": 1.0, "samplingRate": 100},
        )
        self.assertIsInstance(parsed_int_rate["samplingRate"], int)

        parsed_float_rate = parse_payload("-1.25;0.5;9.81;100.5")
        self.assertEqual(
            parsed_float_rate,
            {"x": -1.25, "y": 0.5, "z": 9.81, "samplingRate": 100.5},
        )
        self.assertIsInstance(parsed_float_rate["samplingRate"], float)

    def test_parse_payload_invalid(self) -> None:
        for bad_payload in (
            "1.0;2.0;3.0",
            "1.0;2.0;3.0;100;5",
            "abc;2.0;3.0;100",
            "1.0;nan;3.0;100",
            "1.0;2.0;inf;100",
            "",
        ):
            with self.subTest(payload=bad_payload):
                with self.assertRaises(ValueError):
                    parse_payload(bad_payload)

    def test_csv_message_store_initializes_and_appends(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "data" / "messages.csv"
            store = CsvMessageStore(csv_path)

            self.assertTrue(csv_path.exists())
            lines = csv_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines, ["timestamp,topic,x,y,z,samplingRate,payload"])
            self.assertEqual(store.read_records(), [])

            first = store.append_message(MQTT_TOPIC, "0.016;-0.008;1.000;100")
            second = store.append_message(MQTT_TOPIC, "0.032;0.004;0.996;200.5")

            saved = store.read_records()
            self.assertEqual(len(saved), 2)
            self.assertEqual(saved[0]["topic"], MQTT_TOPIC)
            self.assertEqual(saved[0]["payload"], "0.016;-0.008;1.000;100")
            self.assertEqual(saved[0]["x"], 0.016)
            self.assertEqual(saved[0]["y"], -0.008)
            self.assertEqual(saved[0]["z"], 1.0)
            self.assertEqual(saved[0]["samplingRate"], 100)
            self.assertIsInstance(saved[0]["x"], float)
            self.assertIsInstance(saved[0]["y"], float)
            self.assertIsInstance(saved[0]["z"], float)
            self.assertIsInstance(saved[0]["samplingRate"], int)
            self.assertEqual(saved[0]["timestamp"], first["timestamp"])

            self.assertEqual(saved[1]["payload"], "0.032;0.004;0.996;200.5")
            self.assertEqual(saved[1]["x"], 0.032)
            self.assertEqual(saved[1]["y"], 0.004)
            self.assertEqual(saved[1]["z"], 0.996)
            self.assertEqual(saved[1]["samplingRate"], 200.5)
            self.assertIsInstance(saved[1]["samplingRate"], float)
            self.assertEqual(saved[1]["timestamp"], second["timestamp"])

    def test_on_message_callback_stores_valid_and_skips_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "messages.csv"
            store = CsvMessageStore(csv_path)
            client = create_client(store=store, topic=MQTT_TOPIC)

            valid_msg = DummyMQTTMessage(
                topic=MQTT_TOPIC,
                payload=b"0.055;-0.008;0.992;100\n",
            )
            invalid_msg = DummyMQTTMessage(
                topic=MQTT_TOPIC,
                payload=b"invalid_format_string",
            )

            stdout_buf = io.StringIO()
            with redirect_stdout(stdout_buf):
                client.on_message(client, None, valid_msg)
                client.on_message(client, None, invalid_msg)

            output = stdout_buf.getvalue()
            self.assertIn("x=0.055, y=-0.008, z=0.992, samplingRate=100", output)
            self.assertIn("Warning: ignoring malformed payload", output)

            saved = store.read_records()
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0]["topic"], MQTT_TOPIC)
            self.assertEqual(saved[0]["payload"], "0.055;-0.008;0.992;100")
            self.assertEqual(saved[0]["x"], 0.055)
            self.assertEqual(saved[0]["y"], -0.008)
            self.assertEqual(saved[0]["z"], 0.992)
            self.assertEqual(saved[0]["samplingRate"], 100)

    def test_section_manager_sequential_numbering(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            manager = SectionManager(data_dir)

            path1 = manager.resolve_section_path()
            self.assertEqual(path1, data_dir / "1.csv")
            CsvMessageStore(path1)

            path2 = manager.resolve_section_path()
            self.assertEqual(path2, data_dir / "2.csv")
            CsvMessageStore(path2)

            path3 = manager.resolve_section_path()
            self.assertEqual(path3, data_dir / "3.csv")
            CsvMessageStore(path3)

    def test_section_manager_custom_section_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            manager = SectionManager(data_dir)

            custom_path = manager.resolve_section_path("benchy_test")
            self.assertEqual(custom_path, data_dir / "benchy_test.csv")

            store = create_section_store(data_dir=data_dir, section_name="benchy_test")
            self.assertEqual(store.file_path, data_dir / "benchy_test.csv")
            self.assertTrue(store.file_path.exists())

            # Custom non-numeric section does not affect sequential numbering
            seq_path = manager.resolve_section_path()
            self.assertEqual(seq_path, data_dir / "1.csv")

    def test_section_manager_duplicate_section_naming(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            manager = SectionManager(data_dir)

            first_path = manager.resolve_section_path("benchy")
            self.assertEqual(first_path, data_dir / "benchy.csv")
            CsvMessageStore(first_path)

            second_path = manager.resolve_section_path("benchy")
            self.assertEqual(second_path, data_dir / "benchy_1.csv")
            CsvMessageStore(second_path)

            third_path = manager.resolve_section_path("benchy.csv")
            self.assertEqual(third_path, data_dir / "benchy_2.csv")
            CsvMessageStore(third_path)

    def test_recording_session_controller_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            controller = RecordingSessionController(data_dir=data_dir)
            client = create_client(controller=controller, topic=MQTT_TOPIC)

            # 1. Idle state ignores messages
            self.assertEqual(controller.state, "Idle")
            self.assertIsNone(controller.handle_message(MQTT_TOPIC, "0.1;0.2;0.9;100"))
            self.assertEqual(controller.list_sections(), [])

            # 2. Start recording "run" -> creates run.csv and records messages
            status = controller.start("run")
            self.assertEqual(status["state"], "Recording")
            self.assertEqual(status["activeSection"], "run.csv")
            self.assertEqual(status["sections"], ["run.csv"])

            msg1 = DummyMQTTMessage(MQTT_TOPIC, b"0.10;0.20;0.95;100\n")
            stdout_buf = io.StringIO()
            with redirect_stdout(stdout_buf):
                client.on_message(client, None, msg1)
            self.assertEqual(len(controller.get_section_records("run.csv")), 1)

            # 3. Pause recording -> ignores messages
            status = controller.pause()
            self.assertEqual(status["state"], "Paused")
            self.assertEqual(status["activeSection"], "run.csv")

            msg2 = DummyMQTTMessage(MQTT_TOPIC, b"0.50;0.60;0.99;100\n")
            with redirect_stdout(stdout_buf):
                client.on_message(client, None, msg2)
            self.assertEqual(len(controller.get_section_records("run.csv")), 1)

            # 4. Start while Paused -> resumes same run.csv and records messages
            status = controller.start("run")
            self.assertEqual(status["state"], "Recording")
            self.assertEqual(status["activeSection"], "run.csv")
            self.assertEqual(status["sections"], ["run.csv"])

            msg3 = DummyMQTTMessage(MQTT_TOPIC, b"0.30;0.40;0.97;100\n")
            with redirect_stdout(stdout_buf):
                client.on_message(client, None, msg3)
            records = controller.get_section_records("run.csv")
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]["x"], 0.10)
            self.assertEqual(records[1]["x"], 0.30)

            # 5. Stop recording -> ignores messages and increments completedSessions
            status = controller.stop()
            self.assertEqual(status["state"], "Stopped")
            self.assertEqual(status["completedSessions"], 1)
            self.assertIsNone(controller.handle_message(MQTT_TOPIC, "0.9;0.9;0.9;100"))
            self.assertEqual(len(controller.get_section_records("run.csv")), 2)

            # 6. Start again with same name "run" -> creates run_1.csv
            status = controller.start("run")
            self.assertEqual(status["state"], "Recording")
            self.assertEqual(status["activeSection"], "run_1.csv")
            self.assertEqual(status["sections"], ["run.csv", "run_1.csv"])
            controller.handle_message(MQTT_TOPIC, "0.77;0.88;1.01;200")
            self.assertEqual(len(controller.get_section_records("run.csv")), 2)
            self.assertEqual(len(controller.get_section_records("run_1.csv")), 1)

    def test_http_api_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            ui_dir = Path(tmpdir) / "ui"
            ui_dir.mkdir(parents=True, exist_ok=True)
            (ui_dir / "index.html").write_text("<html><body>UI</body></html>", encoding="utf-8")

            controller = RecordingSessionController(data_dir=data_dir)
            server = create_http_server(
                controller=controller,
                host="127.0.0.1",
                port=0,
                ui_dir=ui_dir,
            )
            port = server.server_address[1]
            base_url = f"http://127.0.0.1:{port}"
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def request_json(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
                data = json.dumps(body).encode("utf-8") if body is not None else None
                headers = {"Content-Type": "application/json"} if data is not None else {}
                req = Request(f"{base_url}{path}", data=data, headers=headers, method=method)
                try:
                    with urlopen(req, timeout=5) as resp:
                        return resp.status, json.loads(resp.read().decode("utf-8"))
                except HTTPError as err:
                    try:
                        return err.code, json.loads(err.read().decode("utf-8"))
                    finally:
                        err.close()

            try:
                # GET /api/status (initial)
                code, status = request_json("GET", "/api/status")
                self.assertEqual(code, 200)
                self.assertEqual(status["state"], "Idle")
                self.assertEqual(status["nextSequenceNumber"], 1)

                # POST /api/start with sectionName="benchy"
                code, status = request_json("POST", "/api/start", {"sectionName": "benchy"})
                self.assertEqual(code, 200)
                self.assertEqual(status["state"], "Recording")
                self.assertEqual(status["activeSection"], "benchy.csv")

                controller.handle_message(MQTT_TOPIC, "0.01;-0.02;0.98;100")

                # POST /api/pause
                code, status = request_json("POST", "/api/pause")
                self.assertEqual(code, 200)
                self.assertEqual(status["state"], "Paused")

                # POST /api/stop
                code, status = request_json("POST", "/api/stop")
                self.assertEqual(code, 200)
                self.assertEqual(status["state"], "Stopped")
                self.assertEqual(status["completedSessions"], 1)

                # POST /api/start again with "benchy" -> benchy_1.csv
                code, status = request_json("POST", "/api/start", {"sectionName": "benchy"})
                self.assertEqual(code, 200)
                self.assertEqual(status["activeSection"], "benchy_1.csv")

                # GET /api/sections
                code, sections_res = request_json("GET", "/api/sections")
                self.assertEqual(code, 200)
                self.assertEqual(sections_res["sections"], ["benchy.csv", "benchy_1.csv"])

                # GET /api/sections/benchy.csv
                code, sec_data = request_json("GET", "/api/sections/benchy.csv")
                self.assertEqual(code, 200)
                self.assertEqual(sec_data["section"], "benchy.csv")
                self.assertEqual(len(sec_data["records"]), 1)
                self.assertEqual(sec_data["records"][0]["x"], 0.01)

                # GET /api/sections/nonexistent.csv -> 404
                code, err_data = request_json("GET", "/api/sections/nonexistent.csv")
                self.assertEqual(code, 404)
                self.assertIn("error", err_data)
            finally:
                server.shutdown()
                server.server_close()

    def test_simulate_sensor_payload_generation(self) -> None:
        for idx in range(10):
            x, y, z = generate_sample(sample_index=idx, sampling_rate=200)
            payload = format_payload(x, y, z, 200)
            parsed = parse_payload(payload)
            self.assertAlmostEqual(parsed["x"], x, places=4)
            self.assertAlmostEqual(parsed["y"], y, places=4)
            self.assertAlmostEqual(parsed["z"], z, places=4)
            self.assertEqual(parsed["samplingRate"], 200)

    def test_multiline_batch_mqtt_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            controller = RecordingSessionController(data_dir=data_dir)
            controller.start("batch_run")
            client = create_client(controller=controller, topic=MQTT_TOPIC)

            batch_payload = generate_batch_payload(
                start_index=0,
                batch_size=25,
                sampling_rate=200,
            )
            self.assertEqual(len(batch_payload.splitlines()), 25)

            stdout_buf = io.StringIO()
            with redirect_stdout(stdout_buf):
                client.on_message(
                    client,
                    None,
                    DummyMQTTMessage(MQTT_TOPIC, batch_payload.encode("utf-8")),
                )

            records = controller.get_section_records("batch_run.csv")
            self.assertEqual(len(records), 25)

            # Also verify a multi-line message containing one malformed line saves valid lines
            mixed_payload = b"0.11;0.22;1.01;200\ncorrupted_line\n0.33;0.44;0.99;200\n"
            with redirect_stdout(stdout_buf):
                client.on_message(
                    client,
                    None,
                    DummyMQTTMessage(MQTT_TOPIC, mixed_payload),
                )

            records_after = controller.get_section_records("batch_run.csv")
            self.assertEqual(len(records_after), 27)
            self.assertEqual(records_after[-2]["x"], 0.11)
            self.assertEqual(records_after[-1]["x"], 0.33)
            self.assertIn("Warning: ignoring malformed payload line", stdout_buf.getvalue())


if __name__ == "__main__":
    unittest.main()
