#!/usr/bin/env python3
"""Simulate an ESP32 + ADXL345 sensor publishing 3D printer vibration data over MQTT.

Publishes semicolon-delimited payloads in the format:
    x;y;z;samplingRate
to the configured MQTT broker and topic.
"""

import argparse
import math
import random
import signal
import sys
import time

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

BROKER_HOST = "broker.hivemq.com"
BROKER_PORT = 1883
MQTT_TOPIC = "3d/printer/49801a48-1dd7-4301-901b-e0f87f44521a"

DEFAULT_SAMPLING_RATE = 200
DEFAULT_PUBLISH_INTERVAL = 0.05  # 20 msg/s over network by default
DEFAULT_BATCH_SIZE = 10  # 10 lines per MQTT message by default
DEFAULT_NOISE_STD = 0.025


def generate_sample(
    sample_index: int,
    sampling_rate: float = DEFAULT_SAMPLING_RATE,
    freq_x: float = 42.0,
    freq_y: float = 36.0,
    freq_z: float = 58.0,
    noise_std: float = DEFAULT_NOISE_STD,
    rng: random.Random | None = None,
) -> tuple[float, float, float]:
    """Generate a synthetic 3-axis accelerometer reading (in g) for sample index n.

    Uses discrete sample time t = sample_index / sampling_rate so that FFT/DFT
    analysis over recorded CSV rows accurately resolves the simulated resonance peaks.
    """
    rand = rng if rng is not None else random
    t = sample_index / sampling_rate
    two_pi = 2.0 * math.pi

    # X-axis: primary resonance + 2nd harmonic + noise
    x = (
        0.45 * math.sin(two_pi * freq_x * t)
        + 0.22 * math.sin(two_pi * (freq_x * 2.0) * t + 0.4)
        + rand.gauss(0.0, noise_std)
    )

    # Y-axis: primary resonance + 2nd harmonic + noise
    y = (
        0.40 * math.sin(two_pi * freq_y * t + 0.8)
        + 0.18 * math.sin(two_pi * (freq_y * 2.0) * t + 1.1)
        + rand.gauss(0.0, noise_std)
    )

    # Z-axis: 1.0g gravity offset + structural resonance + noise
    z = (
        1.0
        + 0.25 * math.sin(two_pi * freq_z * t + 0.3)
        + 0.10 * math.sin(two_pi * (freq_z * 1.5) * t + 0.9)
        + rand.gauss(0.0, noise_std)
    )

    return round(x, 4), round(y, 4), round(z, 4)


def format_payload(x: float, y: float, z: float, sampling_rate: int | float) -> str:
    """Format sensor reading into 'x;y;z;samplingRate' string."""
    sr_str = (
        str(int(sampling_rate))
        if float(sampling_rate).is_integer()
        else f"{sampling_rate:g}"
    )
    return f"{x:.4f};{y:.4f};{z:.4f};{sr_str}"


def generate_batch_payload(
    start_index: int,
    batch_size: int = DEFAULT_BATCH_SIZE,
    sampling_rate: float = DEFAULT_SAMPLING_RATE,
    freq_x: float = 42.0,
    freq_y: float = 36.0,
    freq_z: float = 58.0,
    noise_std: float = DEFAULT_NOISE_STD,
    rng: random.Random | None = None,
) -> str:
    """Generate a newline-separated batch of 'x;y;z;samplingRate' lines for a single MQTT message."""
    count = max(1, int(batch_size))
    lines: list[str] = []
    for offset in range(count):
        x, y, z = generate_sample(
            sample_index=start_index + offset,
            sampling_rate=sampling_rate,
            freq_x=freq_x,
            freq_y=freq_y,
            freq_z=freq_z,
            noise_std=noise_std,
            rng=rng,
        )
        lines.append(format_payload(x, y, z, sampling_rate))
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Simulate ADXL345 sensor data publishing to the MQTT broker."
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
        help=f"MQTT topic to publish to (default: {MQTT_TOPIC})",
    )
    parser.add_argument(
        "--sampling-rate",
        type=float,
        default=DEFAULT_SAMPLING_RATE,
        help=f"Simulated sensor sampling rate in Hz (default: {DEFAULT_SAMPLING_RATE})",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_PUBLISH_INTERVAL,
        help=f"Delay in seconds between published MQTT messages (default: {DEFAULT_PUBLISH_INTERVAL})",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Number of sensor reading lines per MQTT message (default: {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=0,
        help="Number of MQTT messages to send before exiting (default: 0 = infinite)",
    )
    parser.add_argument(
        "--freq-x",
        type=float,
        default=42.0,
        help="Simulated X-axis primary ghosting frequency in Hz (default: 42.0)",
    )
    parser.add_argument(
        "--freq-y",
        type=float,
        default=36.0,
        help="Simulated Y-axis primary ghosting frequency in Hz (default: 36.0)",
    )
    parser.add_argument(
        "--freq-z",
        type=float,
        default=58.0,
        help="Simulated Z-axis primary resonance frequency in Hz (default: 58.0)",
    )
    parser.add_argument(
        "--noise",
        type=float,
        default=DEFAULT_NOISE_STD,
        help=f"Gaussian noise standard deviation in g (default: {DEFAULT_NOISE_STD})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    connected_event = False
    stop_requested = False

    def handle_signal(signum, frame):
        nonlocal stop_requested
        print("\n[SIMULATOR] Stopping sensor simulation...", flush=True)
        stop_requested = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    client = mqtt.Client(callback_api_version=CallbackAPIVersion.VERSION2)

    def on_connect(client, userdata, flags, reason_code, properties):
        nonlocal connected_event
        if reason_code == 0:
            connected_event = True
            print(f"[SIMULATOR] Connected to {args.host}:{args.port}", flush=True)
        else:
            print(
                f"[SIMULATOR] Failed to connect (reason_code={reason_code})",
                file=sys.stderr,
                flush=True,
            )

    client.on_connect = on_connect

    batch_size = max(1, args.batch_size)
    print(
        f"[SIMULATOR] Connecting to {args.host}:{args.port} "
        f"(topic: {args.topic}, samplingRate: {args.sampling_rate:g} Hz, batchSize: {batch_size})...",
        flush=True,
    )

    try:
        client.connect(args.host, args.port, keepalive=60)
        client.loop_start()
    except Exception as exc:
        print(f"[SIMULATOR] Connection error: {exc}", file=sys.stderr, flush=True)
        return 1

    # Wait briefly for connection callback
    wait_start = time.monotonic()
    while not connected_event and not stop_requested and (time.monotonic() - wait_start) < 10.0:
        time.sleep(0.05)

    msg_idx = 0
    sample_idx = 0
    try:
        while not stop_requested:
            if args.count > 0 and msg_idx >= args.count:
                break

            payload = generate_batch_payload(
                start_index=sample_idx,
                batch_size=batch_size,
                sampling_rate=args.sampling_rate,
                freq_x=args.freq_x,
                freq_y=args.freq_y,
                freq_z=args.freq_z,
                noise_std=args.noise,
            )
            info = client.publish(args.topic, payload=payload, qos=0)
            info.wait_for_publish(timeout=2.0)

            msg_idx += 1
            sample_idx += batch_size
            first_line = payload.splitlines()[0]
            if batch_size == 1:
                print(
                    f"[SIMULATOR] #{msg_idx:04d} Published -> {first_line}",
                    flush=True,
                )
            else:
                print(
                    f"[SIMULATOR] #{msg_idx:04d} Published {batch_size} lines "
                    f"(samples {sample_idx - batch_size + 1}..{sample_idx}, first: {first_line})",
                    flush=True,
                )

            if args.count > 0 and msg_idx >= args.count:
                break

            if args.interval > 0:
                time.sleep(args.interval)
    finally:
        client.loop_stop()
        try:
            client.disconnect()
        except Exception:
            pass
        print(
            f"[SIMULATOR] Disconnected after publishing {msg_idx} messages ({sample_idx} samples).",
            flush=True,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
