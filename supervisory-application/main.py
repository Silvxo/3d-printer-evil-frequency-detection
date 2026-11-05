import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import signal
import sys
import threading
from typing import Any
from urllib.parse import unquote, urlparse

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from section_manager import SectionManager
from storage import CsvMessageStore, parse_payload

BROKER_HOST = "broker.hivemq.com"
BROKER_PORT = 1883
MQTT_TOPIC = "3d/printer/49801a48-1dd7-4301-901b-e0f87f44521a"
DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_UI_DIR = Path(__file__).resolve().parent / "ui"
DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8000


class RecordingSessionController:
    """Thread-safe controller synchronizing UI recording state and CSV storage."""

    def __init__(self, data_dir: Path = DEFAULT_DATA_DIR) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state: str = "Idle"
        self.active_store: CsvMessageStore | None = None
        self.active_section_name: str | None = None
        self.completed_sessions: int = 0
        self._lock = threading.Lock()

    def _list_sections_unlocked(self) -> list[str]:
        if not self.data_dir.exists():
            return []
        return sorted([p.name for p in self.data_dir.glob("*.csv") if p.is_file()])

    def _get_status_unlocked(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "activeSection": self.active_section_name,
            "completedSessions": self.completed_sessions,
            "nextSequenceNumber": SectionManager(self.data_dir).get_next_sequence_number(),
            "sections": self._list_sections_unlocked(),
        }

    def start(
        self,
        section_name: str | None = None,
        output_path: Path | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if self.state == "Paused" and self.active_store is not None:
                self.state = "Recording"
                return self._get_status_unlocked()

            if self.state == "Recording" and self.active_store is not None:
                return self._get_status_unlocked()

            if output_path is not None:
                target_path = Path(output_path)
            else:
                target_path = SectionManager(self.data_dir).resolve_section_path(section_name)

            self.active_store = CsvMessageStore(target_path)
            self.active_section_name = target_path.name
            self.state = "Recording"
            return self._get_status_unlocked()

    def pause(self) -> dict[str, Any]:
        with self._lock:
            if self.state == "Recording":
                self.state = "Paused"
            return self._get_status_unlocked()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self.state in ("Recording", "Paused"):
                self.state = "Stopped"
                self.completed_sessions += 1
                self.active_store = None
            return self._get_status_unlocked()

    def handle_messages(self, topic: str, payload: str) -> list[dict[str, Any]] | None:
        with self._lock:
            if self.state != "Recording" or self.active_store is None:
                return None
            return self.active_store.append_messages(topic, payload)

    def handle_message(self, topic: str, payload: str) -> dict[str, Any] | None:
        records = self.handle_messages(topic, payload)
        if not records:
            return None
        return records[-1]

    def list_sections(self) -> list[str]:
        with self._lock:
            return self._list_sections_unlocked()

    def get_section_records(self, filename: str) -> list[dict[str, Any]]:
        safe_name = Path(filename).name
        if not safe_name:
            raise FileNotFoundError(f"Section {filename!r} not found")
        if not safe_name.lower().endswith(".csv"):
            safe_name = f"{safe_name}.csv"
        target = self.data_dir / safe_name
        if not target.is_file():
            raise FileNotFoundError(f"Section {safe_name!r} not found")
        return CsvMessageStore(target).read_records()

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return self._get_status_unlocked()


def create_section_store(
    data_dir: Path = DEFAULT_DATA_DIR,
    section_name: str | None = None,
    output_path: Path | None = None,
) -> CsvMessageStore:
    if output_path is not None:
        return CsvMessageStore(output_path)
    section_path = SectionManager(data_dir).resolve_section_path(section_name)
    return CsvMessageStore(section_path)


def create_client(
    store: CsvMessageStore | None = None,
    host: str = BROKER_HOST,
    port: int = BROKER_PORT,
    topic: str = MQTT_TOPIC,
    controller: RecordingSessionController | None = None,
) -> mqtt.Client:
    client = mqtt.Client(callback_api_version=CallbackAPIVersion.VERSION2)

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            print(f"[MQTT] Connected to {host}:{port}", flush=True)
            client.subscribe(topic)
        else:
            print(f"[MQTT] Connection failed with reason code: {reason_code}", flush=True)

    def on_subscribe(client, userdata, mid, reason_code_list, properties):
        print(f"[MQTT] Subscribed to topic: {topic}", flush=True)

    def on_message(client, userdata, msg):
        payload_str = msg.payload.decode("utf-8", errors="replace").strip()
        try:
            if controller is not None:
                records = controller.handle_messages(msg.topic, payload_str)
                if records is None:
                    return
            elif store is not None:
                records = store.append_messages(msg.topic, payload_str)
            else:
                return
        except ValueError as exc:
            lines = [line.strip() for line in payload_str.splitlines() if line.strip()]
            if len(lines) <= 1:
                print(
                    f"[MQTT] Warning: ignoring malformed payload on {msg.topic} "
                    f"({payload_str!r}): {exc}",
                    flush=True,
                )
                return

            valid_lines: list[str] = []
            for line in lines:
                try:
                    parse_payload(line)
                    valid_lines.append(line)
                except ValueError as line_exc:
                    print(
                        f"[MQTT] Warning: ignoring malformed payload line on {msg.topic} "
                        f"({line!r}): {line_exc}",
                        flush=True,
                    )

            if not valid_lines:
                return

            valid_batch = "\n".join(valid_lines)
            if controller is not None:
                records = controller.handle_messages(msg.topic, valid_batch)
                if records is None:
                    return
            elif store is not None:
                records = store.append_messages(msg.topic, valid_batch)
            else:
                return

        for record in records:
            print(
                f"[{record['timestamp']}] Topic: {msg.topic} | "
                f"x={record['x']}, y={record['y']}, z={record['z']}, "
                f"samplingRate={record['samplingRate']} | Raw: {record['payload']}",
                flush=True,
            )

    def on_disconnect(client, userdata, flags, reason_code, properties):
        print(f"[MQTT] Disconnected (reason_code={reason_code})", flush=True)

    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message
    client.on_disconnect = on_disconnect
    return client


def create_http_server(
    controller: RecordingSessionController,
    host: str = DEFAULT_WEB_HOST,
    port: int = DEFAULT_WEB_PORT,
    ui_dir: Path = DEFAULT_UI_DIR,
) -> ThreadingHTTPServer:
    resolved_ui_dir = Path(ui_dir).resolve()

    class SupervisoryRequestHandler(BaseHTTPRequestHandler):
        def _send_json(self, status_code: int, data: dict[str, Any]) -> None:
            body = json.dumps(data).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json_body(self) -> dict[str, Any]:
            content_length = int(self.headers.get("Content-Length", "0") or "0")
            if content_length <= 0:
                return {}
            raw = self.rfile.read(content_length).decode("utf-8", errors="replace")
            if not raw.strip():
                return {}
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}

        def do_GET(self) -> None:
            parsed_url = urlparse(self.path)
            path = parsed_url.path

            if path == "/api/status":
                self._send_json(200, controller.get_status())
                return

            if path == "/api/sections":
                self._send_json(200, {"sections": controller.list_sections()})
                return

            if path.startswith("/api/sections/"):
                raw_name = path[len("/api/sections/") :]
                filename = unquote(raw_name)
                try:
                    records = controller.get_section_records(filename)
                except FileNotFoundError:
                    self._send_json(404, {"error": "Section not found"})
                    return
                self._send_json(200, {"section": filename, "records": records})
                return

            # Serve static UI files
            rel_path = "index.html" if path in ("", "/") else path.lstrip("/")
            candidate = (resolved_ui_dir / rel_path).resolve()
            try:
                candidate.relative_to(resolved_ui_dir)
            except ValueError:
                self._send_json(403, {"error": "Forbidden"})
                return

            if not candidate.is_file():
                self._send_json(404, {"error": "Not found"})
                return

            content_type, _ = mimetypes.guess_type(str(candidate))
            if not content_type:
                content_type = "application/octet-stream"
            data = candidate.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            parsed_url = urlparse(self.path)
            path = parsed_url.path

            if path == "/api/start":
                try:
                    payload = self._read_json_body()
                except json.JSONDecodeError:
                    self._send_json(400, {"error": "Invalid JSON body"})
                    return
                section_name = payload.get("sectionName")
                if section_name is not None and not isinstance(section_name, str):
                    section_name = str(section_name)
                try:
                    status = controller.start(section_name=section_name)
                except ValueError as exc:
                    self._send_json(400, {"error": str(exc)})
                    return
                self._send_json(200, status)
                return

            if path == "/api/pause":
                self._send_json(200, controller.pause())
                return

            if path == "/api/stop":
                self._send_json(200, controller.stop())
                return

            self._send_json(404, {"error": "Endpoint not found"})

        def log_message(self, format: str, *args: Any) -> None:
            return

    return ThreadingHTTPServer((host, port), SupervisoryRequestHandler)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Supervisory MQTT subscriber for 3D printer accelerometer data."
    )
    parser.add_argument(
        "--host",
        default=BROKER_HOST,
        help=f"MQTT broker host (default: {BROKER_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=BROKER_PORT,
        help=f"MQTT broker TCP port (default: {BROKER_PORT})",
    )
    parser.add_argument(
        "--topic",
        default=MQTT_TOPIC,
        help=f"MQTT topic to subscribe to (default: {MQTT_TOPIC})",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="Directory to store section CSV files",
    )
    parser.add_argument(
        "--section",
        type=str,
        default=None,
        help="Section name for the recording (offered for UI/CLI; defaults to next sequential number)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional explicit path to CSV output file (overrides --data-dir and --section)",
    )
    parser.add_argument(
        "--web-host",
        default=DEFAULT_WEB_HOST,
        help=f"HTTP server host for Supervisory UI (default: {DEFAULT_WEB_HOST})",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=DEFAULT_WEB_PORT,
        help=f"HTTP server port for Supervisory UI (default: {DEFAULT_WEB_PORT})",
    )
    parser.add_argument(
        "--no-web",
        action="store_true",
        help="Disable the built-in HTTP server for the Supervisory UI",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    controller = RecordingSessionController(data_dir=args.data_dir)

    if args.output is not None or args.section is not None:
        controller.start(section_name=args.section, output_path=args.output)

    http_server: ThreadingHTTPServer | None = None
    if not args.no_web:
        http_server = create_http_server(
            controller=controller,
            host=args.web_host,
            port=args.web_port,
        )
        web_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
        web_thread.start()
        print(
            f"[WEB] Supervisory UI available at http://{args.web_host}:{args.web_port}",
            flush=True,
        )

    client = create_client(
        controller=controller,
        host=args.host,
        port=args.port,
        topic=args.topic,
    )

    stop_event = threading.Event()

    def handle_sigint(signum, frame):
        print("\n[MQTT] Shutting down supervisory application...", flush=True)
        stop_event.set()
        if http_server is not None:
            threading.Thread(target=http_server.shutdown, daemon=True).start()
        try:
            client.disconnect()
        except Exception:
            pass

    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)

    if controller.active_store is not None:
        print(
            f"[MQTT] Connecting to {args.host}:{args.port} "
            f"(saving messages to {controller.active_store.file_path})...",
            flush=True,
        )
    else:
        print(
            f"[MQTT] Connecting to {args.host}:{args.port} "
            f"(standing by in Idle state until recording is started in UI)...",
            flush=True,
        )

    try:
        client.connect(args.host, args.port, keepalive=60)
        client.loop_forever()
    except Exception as exc:
        print(f"[MQTT] Error: {exc}", file=sys.stderr, flush=True)
        if http_server is not None and not stop_event.is_set():
            print(
                "[WEB] Keeping Supervisory UI server running (press Ctrl+C to exit)...",
                flush=True,
            )
            stop_event.wait()
        else:
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
