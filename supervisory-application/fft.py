#!/usr/bin/env python3
"""Simple script to compute and plot the FFT of recorded 3D printer accelerometer CSV data."""

import argparse
import csv
from pathlib import Path
import sys

import matplotlib.pyplot as plt
import numpy as np

DEFAULT_SAMPLING_RATE = 1000.0
DEFAULT_MIN_FREQ = 1.0
DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "data"


def load_csv_axes(
    csv_path: Path, default_fs: float = DEFAULT_SAMPLING_RATE
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """Load x, y, z accelerometer series and sampling rate from a recorded CSV file."""
    x_vals: list[float] = []
    y_vals: list[float] = []
    z_vals: list[float] = []
    fs = default_fs

    with Path(csv_path).open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                x_vals.append(float(row["x"]))
                y_vals.append(float(row["y"]))
                z_vals.append(float(row["z"]))
                if row.get("samplingRate"):
                    sr = float(row["samplingRate"])
                    if sr > 0:
                        fs = sr
            except (KeyError, ValueError):
                continue

    return (
        np.asarray(x_vals, dtype=np.float64),
        np.asarray(y_vals, dtype=np.float64),
        np.asarray(z_vals, dtype=np.float64),
        fs,
    )


def compute_axis_fft(
    signal: np.ndarray,
    fs: float = DEFAULT_SAMPLING_RATE,
    min_freq: float = DEFAULT_MIN_FREQ,
    min_peak_distance_hz: float = 1.0,
) -> tuple[np.ndarray, np.ndarray, list[tuple[float, float]]]:
    """Compute one-sided FFT magnitude spectrum (with DC/drift removed) and top peak frequencies."""
    signal_arr = np.asarray(signal, dtype=np.float64)
    signal_arr = signal_arr[np.isfinite(signal_arr)]
    n = len(signal_arr)
    if n == 0:
        return np.array([]), np.array([]), []

    # Remove DC offset (static gravity component)
    centered = signal_arr - np.mean(signal_arr)

    # Apply Hann window and compensate for exact window coherent gain
    window = np.hanning(n) if n > 2 else np.ones(n, dtype=np.float64)
    window_sum = float(np.sum(window))
    if window_sum <= 0.0:
        window = np.ones(n, dtype=np.float64)
        window_sum = float(n)

    fft_vals = np.fft.rfft(centered * window)
    freqs = np.fft.rfftfreq(n, d=1.0 / fs)
    magnitudes = np.abs(fft_vals) * (2.0 / window_sum)
    if len(magnitudes) > 0:
        magnitudes[0] *= 0.5
        if n % 2 == 0 and len(magnitudes) > 1:
            magnitudes[-1] *= 0.5

    # Suppress DC and sub-Hz sensor drift below min_freq
    if len(magnitudes) > 0:
        magnitudes[freqs < min_freq] = 0.0

    # Detect local maxima separated by at least min_peak_distance_hz
    peaks: list[tuple[float, float]] = []
    if len(magnitudes) >= 3:
        local_max_indices = (
            np.where((magnitudes[1:-1] > magnitudes[:-2]) & (magnitudes[1:-1] >= magnitudes[2:]))[0]
            + 1
        )
        if len(local_max_indices) > 0:
            sorted_indices = local_max_indices[np.argsort(magnitudes[local_max_indices])[::-1]]
            for idx in sorted_indices:
                freq = float(freqs[idx])
                mag = float(magnitudes[idx])
                if mag <= 0:
                    break
                if all(abs(freq - existing_f) >= min_peak_distance_hz for existing_f, _ in peaks):
                    peaks.append((freq, mag))
                    if len(peaks) >= 3:
                        break

    return freqs, magnitudes, peaks


def _downsample_curve(
    freqs: np.ndarray,
    values: np.ndarray,
    max_points: int,
    value_key: str,
) -> list[dict[str, float]]:
    """Envelope-preserve (max-pool) a frequency spectrum curve to at most max_points bins."""
    m = len(freqs)
    if m == 0:
        return []
    if max_points <= 0 or m <= max_points:
        return [
            {"frequency": round(float(f), 2), value_key: round(float(v), 5)}
            for f, v in zip(freqs, values)
        ]

    edges = np.linspace(0, m, max_points + 1, dtype=int)
    points: list[dict[str, float]] = []
    for i in range(max_points):
        start, end = int(edges[i]), int(edges[i + 1])
        if end <= start:
            continue
        segment = values[start:end]
        best_idx = start + int(np.argmax(segment))
        points.append(
            {
                "frequency": round(float(freqs[best_idx]), 2),
                value_key: round(float(values[best_idx]), 5),
            }
        )
    return points


def analyze_axis_signal(
    signal: np.ndarray,
    fs: float = DEFAULT_SAMPLING_RATE,
    min_freq: float = DEFAULT_MIN_FREQ,
    max_points: int = 1000,
    filename: str | None = None,
) -> dict[str, object]:
    """Compute broadband spectrum and ghost-frequency FFT payload for one accelerometer axis."""
    signal_arr = np.asarray(signal, dtype=np.float64)
    signal_arr = signal_arr[np.isfinite(signal_arr)]
    n = len(signal_arr)
    effective_fs = float(fs) if fs > 0 else DEFAULT_SAMPLING_RATE
    nyquist = effective_fs / 2.0

    if n == 0:
        return {
            "empty": True,
            "filename": filename,
            "maxFrequency": nyquist,
            "spectrumPoints": [],
            "fftPoints": [],
            "maxAmplitude": 1.0,
            "maxMagnitude": 1.0,
            "threshold": 0.35,
            "ghostPeaks": [],
        }

    if n == 1:
        amp = float(abs(signal_arr[0]))
        max_amp = max(0.1, round(amp * 1.25, 4))
        return {
            "empty": False,
            "filename": filename,
            "maxFrequency": nyquist,
            "spectrumPoints": [
                {"frequency": 0.0, "amplitude": round(amp, 5)},
                {"frequency": round(nyquist, 2), "amplitude": 0.0},
            ],
            "fftPoints": [
                {"frequency": 0.0, "magnitude": 0.0},
                {"frequency": round(nyquist, 2), "magnitude": 0.0},
            ],
            "maxAmplitude": max_amp,
            "maxMagnitude": 1.0,
            "threshold": 0.35,
            "ghostPeaks": [],
        }

    # 1. Broadband spectrum (zero-mean rectangular window FFT)
    centered = signal_arr - np.mean(signal_arr)
    raw_fft = np.fft.rfft(centered)
    freqs = np.fft.rfftfreq(n, d=1.0 / effective_fs)
    broadband_amps = np.abs(raw_fft) * (2.0 / n)
    broadband_amps[0] *= 0.5
    if n % 2 == 0 and len(broadband_amps) > 1:
        broadband_amps[-1] *= 0.5

    # 2. Windowed FFT for ghost frequencies with DC/sub-Hz drift suppression
    _, magnitudes, peaks = compute_axis_fft(
        signal_arr,
        fs=effective_fs,
        min_freq=min_freq,
    )

    max_spec_amp = float(np.max(broadband_amps)) if len(broadband_amps) > 0 else 0.0
    max_mag = float(np.max(magnitudes)) if len(magnitudes) > 0 else 0.0
    active_mags = magnitudes[freqs >= min_freq]
    avg_mag = float(np.mean(active_mags)) if len(active_mags) > 0 else 0.0

    clean_max_amp = round(max_spec_amp * 1.15, 4) if max_spec_amp > 0 else 1.0
    clean_max_mag = round(max_mag * 1.15, 4) if max_mag > 0 else 1.0
    threshold = (
        round(max(avg_mag * 1.6, max_mag * 0.45), 4)
        if max_mag > 0
        else 0.35
    )

    ghost_peaks = [
        {
            "frequency": round(freq, 2),
            "magnitude": round(mag, 5),
            "label": f"{freq:.1f} Hz",
        }
        for freq, mag in peaks
        if mag >= threshold
    ]
    if not ghost_peaks and peaks:
        ghost_peaks = [
            {
                "frequency": round(freq, 2),
                "magnitude": round(mag, 5),
                "label": f"{freq:.1f} Hz",
            }
            for freq, mag in peaks
        ]

    return {
        "empty": False,
        "filename": filename,
        "maxFrequency": nyquist,
        "spectrumPoints": _downsample_curve(freqs, broadband_amps, max_points, "amplitude"),
        "fftPoints": _downsample_curve(freqs, magnitudes, max_points, "magnitude"),
        "maxAmplitude": clean_max_amp,
        "maxMagnitude": clean_max_mag,
        "threshold": threshold,
        "ghostPeaks": ghost_peaks,
    }


def analyze_csv_section(
    csv_path: Path,
    default_fs: float = DEFAULT_SAMPLING_RATE,
    min_freq: float = DEFAULT_MIN_FREQ,
    max_points: int = 1000,
) -> dict[str, object]:
    """Load CSV section and compute per-axis FFT analysis in Python."""
    path_obj = Path(csv_path)
    x, y, z, fs = load_csv_axes(path_obj, default_fs=default_fs)
    section_name = path_obj.name
    return {
        "section": section_name,
        "sampleCount": int(len(x)),
        "samplingRate": float(fs),
        "axes": {
            "x": analyze_axis_signal(x, fs=fs, min_freq=min_freq, max_points=max_points, filename=section_name),
            "y": analyze_axis_signal(y, fs=fs, min_freq=min_freq, max_points=max_points, filename=section_name),
            "z": analyze_axis_signal(z, fs=fs, min_freq=min_freq, max_points=max_points, filename=section_name),
        },
    }


def find_default_csv(data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    """Return the most recently modified non-empty CSV file in data_dir."""
    candidates = [
        p for p in Path(data_dir).glob("*.csv") if p.is_file() and p.stat().st_size > 100
    ]
    if not candidates:
        candidates = [p for p in Path(data_dir).glob("*.csv") if p.is_file()]
    if not candidates:
        raise FileNotFoundError(f"No CSV files found in {data_dir}")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def plot_fft(
    csv_path: Path,
    sampling_rate: float = DEFAULT_SAMPLING_RATE,
    min_freq: float = DEFAULT_MIN_FREQ,
    max_freq: float | None = None,
    output_path: Path | None = None,
    show: bool = True,
) -> Path:
    """Compute FFT for X, Y, Z axes from csv_path, print peaks, and save/show plot."""
    x, y, z, fs = load_csv_axes(csv_path, default_fs=sampling_rate)
    if len(x) == 0:
        raise ValueError(f"No valid accelerometer samples found in {csv_path}")

    print(f"Loaded {len(x)} samples from {csv_path.name} (Sampling Rate: {fs:g} Hz)")

    axes_data = [
        ("X Axis", x, "#38bdf8"),
        ("Y Axis", y, "#4ade80"),
        ("Z Axis", z, "#f59e0b"),
    ]

    fig, subplots = plt.subplots(3, 1, figsize=(10, 8), sharex=True)
    fig.suptitle(f"FFT Frequency Spectrum — {csv_path.name} (fs = {fs:g} Hz)", fontsize=13)

    plot_max_freq = max_freq if max_freq is not None else (fs / 2.0)

    for ax_plot, (label, samples, color) in zip(subplots, axes_data):
        freqs, magnitudes, peaks = compute_axis_fft(samples, fs=fs, min_freq=min_freq)

        mask = (freqs >= min_freq) & (freqs <= plot_max_freq)
        ax_plot.plot(freqs[mask], magnitudes[mask], color=color, linewidth=1.0, label=label)
        ax_plot.set_ylabel(f"{label}\nMagnitude (g)")
        ax_plot.grid(True, alpha=0.3)

        if peaks:
            peaks_str = ", ".join(f"{freq:.2f} Hz ({mag:.4f} g)" for freq, mag in peaks)
            print(f"  {label} dominant peaks: {peaks_str}")
            for freq, mag in peaks:
                if min_freq <= freq <= plot_max_freq:
                    ax_plot.plot(freq, mag, "ro", markersize=5)
                    ax_plot.annotate(
                        f"{freq:.1f} Hz",
                        xy=(freq, mag),
                        xytext=(6, 4),
                        textcoords="offset points",
                        fontsize=8,
                    )
        else:
            print(f"  {label}: no dominant peaks detected")

        ax_plot.legend(loc="upper right")

    subplots[-1].set_xlabel("Frequency (Hz)")
    subplots[-1].set_xlim(0, plot_max_freq)
    fig.tight_layout()

    save_path = output_path if output_path is not None else csv_path.with_name(f"{csv_path.stem}_fft.png")
    fig.savefig(save_path, dpi=150)
    print(f"Saved FFT plot to: {save_path}")

    if show:
        plt.show()
    plt.close(fig)
    return save_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compute and plot FFT frequency spectrum from a recorded CSV file."
    )
    parser.add_argument(
        "csv_file",
        nargs="?",
        type=Path,
        default=None,
        help="Path to the CSV file (defaults to latest non-empty CSV in data/)",
    )
    parser.add_argument(
        "--fs",
        type=float,
        default=DEFAULT_SAMPLING_RATE,
        help=f"Sampling rate in Hz (default: {DEFAULT_SAMPLING_RATE:g})",
    )
    parser.add_argument(
        "--min-freq",
        type=float,
        default=DEFAULT_MIN_FREQ,
        help=f"Minimum frequency in Hz to exclude low-frequency drift (default: {DEFAULT_MIN_FREQ:g})",
    )
    parser.add_argument(
        "--max-freq",
        type=float,
        default=None,
        help="Maximum frequency in Hz to display on X-axis (default: Nyquist = fs/2)",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Output PNG path (default: <csv_stem>_fft.png)",
    )
    parser.add_argument(
        "--no-show",
        action="store_true",
        help="Save the plot image without opening an interactive window",
    )
    args = parser.parse_args(argv)

    try:
        target_csv = args.csv_file if args.csv_file is not None else find_default_csv()
        if not target_csv.exists() and (DEFAULT_DATA_DIR / target_csv).exists():
            target_csv = DEFAULT_DATA_DIR / target_csv

        plot_fft(
            csv_path=target_csv,
            sampling_rate=args.fs,
            min_freq=args.min_freq,
            max_freq=args.max_freq,
            output_path=args.output,
            show=not args.no_show,
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
