import argparse
from pathlib import Path
import signal
import sys
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from storage import JsonMessageStore

BROKER_HOST = "broker.hivemq.com"
BROKER_PORT = 1883
MQTT_TOPIC = "3d/printer/49801a48-1dd7-4301-901b-e0f87f44521a"
DEFAULT_STORAGE_PATH = Path(__file__).resolve().parent / "data" / "messages.json"


def create_client(
    store: JsonMessageStore,
    host: str = BROKER_HOST,
    port: int = BROKER_PORT,
    topic: str = MQTT_TOPIC,
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
        payload_str = msg.payload.decode("utf-8", errors="replace")
        record = store.append_message(msg.topic, payload_str)
        print(
            f"[{record['timestamp']}] Topic: {msg.topic} | Payload: {payload_str}",
            flush=True,
        )

    def on_disconnect(client, userdata, flags, reason_code, properties):
        print(f"[MQTT] Disconnected (reason_code={reason_code})", flush=True)

    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message
    client.on_disconnect = on_disconnect
    return client


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
        "--output",
        type=Path,
        default=DEFAULT_STORAGE_PATH,
        help=f"Path to JSON output file (default: {DEFAULT_STORAGE_PATH})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    store = JsonMessageStore(args.output)
    client = create_client(store=store, host=args.host, port=args.port, topic=args.topic)

    def handle_sigint(signum, frame):
        print("\n[MQTT] Shutting down supervisory application...", flush=True)
        client.disconnect()

    signal.signal(signal.SIGINT, handle_sigint)
    signal.signal(signal.SIGTERM, handle_sigint)

    print(
        f"[MQTT] Connecting to {args.host}:{args.port} (saving messages to {args.output})...",
        flush=True,
    )
    try:
        client.connect(args.host, args.port, keepalive=60)
        client.loop_forever()
    except Exception as exc:
        print(f"[MQTT] Error: {exc}", file=sys.stderr, flush=True)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
