#!/usr/bin/env python3
"""
Sample-synchronous multi-channel hydrophone recording pipeline
for Raspberry Pi 5.

Channel layout in SOFTWARE_EMBED mode:
    Ch. 1     Hydrophone input from audio interface
    Ch. 2     AFSK-encoded timestamp track
    Ch. 3     AFSK-encoded GPS WGS84 track
    Ch. 4..N  AFSK-encoded sensor / operational-state tracks

Important metrological note:
    The strongest measurement architecture is HARDWARE_EMBED mode,
    where timestamp/GPS/sensor metadata are electrically encoded and
    fed into real audio-interface inputs. In that case all channels are
    physically sampled by the same ADC clock.

    SOFTWARE_EMBED mode, implemented here, creates metadata channels in
    the same WAV sample grid during the audio callback. This is practical
    and useful for dataset generation, but it is not identical to a true
    hardware-loopback implementation.

Author: Attila Aradi / research prototype
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import queue
import signal
import socket
import struct
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

import numpy as np
import sounddevice as sd
import soundfile as sf


# ---------------------------------------------------------------------
# CRC-16/CCITT-FALSE
# ---------------------------------------------------------------------

def crc16_ccitt_false(data: bytes) -> int:
    """
    CRC-16/CCITT-FALSE
    Polynomial: 0x1021
    Initial:    0xFFFF
    XOR out:    0x0000
    """
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


# ---------------------------------------------------------------------
# Frame format
# ---------------------------------------------------------------------

class FrameType:
    TIMESTAMP = 0x01
    GPS = 0x02
    SENSOR = 0x03


def build_frame(frame_type: int, seq: int, payload: bytes) -> bytes:
    """
    Binary metadata frame.

    Frame:
        preamble: 8 x 0x55
        sync:     0x2D 0xD4
        type:     uint8
        seq:      uint16 little-endian
        length:   uint16 little-endian
        payload:  variable
        crc16:    uint16 little-endian, calculated over type+seq+length+payload

    This frame is later converted to bits and AFSK-modulated.
    """
    preamble = bytes([0x55] * 8)
    sync = bytes([0x2D, 0xD4])

    header = struct.pack("<BHH", frame_type, seq & 0xFFFF, len(payload))
    crc = crc16_ccitt_false(header + payload)
    return preamble + sync + header + payload + struct.pack("<H", crc)


def bytes_to_bits_lsb_first(data: bytes) -> List[int]:
    """
    Convert bytes to bits, least significant bit first.
    This must be matched by the decoder.
    """
    bits: List[int] = []
    for b in data:
        for i in range(8):
            bits.append((b >> i) & 1)
    return bits


# ---------------------------------------------------------------------
# AFSK modulator
# ---------------------------------------------------------------------

class AFSKModulator:
    """
    Continuous-phase AFSK modulator.

    Default tone pair is Bell-202-like:
        mark  = 1200 Hz
        space = 2200 Hz
        baud  = 1200 bit/s

    For robust decoding at 48 kHz, use integer-ish block sizes and avoid
    clipping. The output is float32 in [-amp, +amp].
    """

    def __init__(
        self,
        samplerate: int,
        baud: int = 1200,
        mark_hz: float = 1200.0,
        space_hz: float = 2200.0,
        amplitude: float = 0.35,
    ) -> None:
        self.fs = int(samplerate)
        self.baud = int(baud)
        self.mark_hz = float(mark_hz)
        self.space_hz = float(space_hz)
        self.amplitude = float(amplitude)

        self.samples_per_bit = self.fs / self.baud
        self.phase = 0.0
        self.bit_queue: queue.Queue[int] = queue.Queue()
        self.current_bit = 1  # idle mark
        self.samples_in_current_bit = 0.0
        self.lock = threading.Lock()

    def enqueue_bytes(self, data: bytes) -> None:
        bits = bytes_to_bits_lsb_first(data)
        with self.lock:
            for bit in bits:
                self.bit_queue.put(bit)

    def _next_bit_unlocked(self) -> int:
        try:
            return self.bit_queue.get_nowait()
        except queue.Empty:
            return 1  # idle mark tone

    def generate(self, n_samples: int) -> np.ndarray:
        """
        Generate n_samples of continuous-phase AFSK.
        """
        out = np.empty(n_samples, dtype=np.float32)
        idx = 0

        with self.lock:
            while idx < n_samples:
                if self.samples_in_current_bit <= 0:
                    self.current_bit = self._next_bit_unlocked()
                    self.samples_in_current_bit = self.samples_per_bit

                remaining = int(math.ceil(self.samples_in_current_bit))
                count = min(remaining, n_samples - idx)

                freq = self.mark_hz if self.current_bit else self.space_hz
                omega = 2.0 * math.pi * freq / self.fs

                t = np.arange(count, dtype=np.float64)
                segment = self.amplitude * np.sin(self.phase + omega * t)
                out[idx:idx + count] = segment.astype(np.float32)

                self.phase = (self.phase + omega * count) % (2.0 * math.pi)
                self.samples_in_current_bit -= count
                idx += count

                if self.samples_in_current_bit <= 0:
                    self.samples_in_current_bit = 0.0

        return out


# ---------------------------------------------------------------------
# GPS reader via gpsd
# ---------------------------------------------------------------------

@dataclasses.dataclass
class GPSState:
    valid: bool = False
    lat: float = 0.0
    lon: float = 0.0
    alt_m: float = 0.0
    speed_mps: float = 0.0
    track_deg: float = 0.0
    mode: int = 0
    hdop: float = 99.99
    timestamp_unix: float = 0.0


class GPSDReader(threading.Thread):
    """
    Minimal gpsd JSON reader.

    Requires:
        sudo systemctl enable gpsd
        sudo systemctl start gpsd

    Test:
        cgps
        gpsmon
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 2947) -> None:
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.state = GPSState()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()

    def get_state(self) -> GPSState:
        with self.lock:
            return dataclasses.replace(self.state)

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                with socket.create_connection((self.host, self.port), timeout=5.0) as sock:
                    sock.settimeout(2.0)
                    sock.sendall(b'?WATCH={"enable":true,"json":true};\n')
                    buffer = b""

                    while not self.stop_event.is_set():
                        try:
                            chunk = sock.recv(4096)
                            if not chunk:
                                break
                            buffer += chunk

                            while b"\n" in buffer:
                                line, buffer = buffer.split(b"\n", 1)
                                self._handle_line(line.decode("utf-8", errors="ignore").strip())

                        except socket.timeout:
                            continue

            except Exception:
                # GPS may be absent during lab testing.
                time.sleep(2.0)

    def _handle_line(self, line: str) -> None:
        if not line:
            return
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return

        cls = msg.get("class", "")
        if cls == "TPV":
            mode = int(msg.get("mode", 0) or 0)
            lat = float(msg.get("lat", 0.0) or 0.0)
            lon = float(msg.get("lon", 0.0) or 0.0)

            state = GPSState(
                valid=mode >= 2 and abs(lat) > 0.000001 and abs(lon) > 0.000001,
                lat=lat,
                lon=lon,
                alt_m=float(msg.get("altHAE", msg.get("alt", 0.0)) or 0.0),
                speed_mps=float(msg.get("speed", 0.0) or 0.0),
                track_deg=float(msg.get("track", 0.0) or 0.0),
                mode=mode,
                hdop=self.get_state().hdop,
                timestamp_unix=time.time(),
            )
            with self.lock:
                self.state = state

        elif cls == "SKY":
            hdop = float(msg.get("hdop", 99.99) or 99.99)
            with self.lock:
                self.state.hdop = hdop

    def stop(self) -> None:
        self.stop_event.set()


# ---------------------------------------------------------------------
# Sensor provider
# ---------------------------------------------------------------------

@dataclasses.dataclass
class SensorValue:
    name: str
    value: float
    unit: str = ""


class SensorProvider:
    """
    Replace read_all() with real sensors.

    Examples to integrate:
        - Modbus/TCP propulsion state
        - RS485 Modbus depth/pressure sensor
        - I2C temperature sensor
        - GPIO event marker
        - PLC digital state
    """

    def __init__(self, sensor_names: List[str]) -> None:
        self.sensor_names = sensor_names

    def read_all(self) -> Dict[str, SensorValue]:
        result: Dict[str, SensorValue] = {}

        for name in self.sensor_names:
            if name == "cpu_temp":
                result[name] = SensorValue(name, self._read_cpu_temp(), "degC")
            elif name == "event_marker":
                # Placeholder. Replace with GPIO read or operator trigger.
                result[name] = SensorValue(name, 0.0, "bool")
            elif name == "propulsion_state":
                # Placeholder. Replace with PLC/Modbus value.
                result[name] = SensorValue(name, 0.0, "state")
            else:
                # Unknown configured sensor: emit NaN.
                result[name] = SensorValue(name, float("nan"), "")

        return result

    @staticmethod
    def _read_cpu_temp() -> float:
        path = "/sys/class/thermal/thermal_zone0/temp"
        try:
            with open(path, "r", encoding="utf-8") as f:
                return float(f.read().strip()) / 1000.0
        except Exception:
            return float("nan")


# ---------------------------------------------------------------------
# Metadata scheduler
# ---------------------------------------------------------------------

class MetadataScheduler(threading.Thread):
    """
    Periodically creates timestamp, GPS and sensor frames and feeds
    them into AFSK modulators.
    """

    def __init__(
        self,
        samplerate: int,
        timestamp_mod: AFSKModulator,
        gps_mod: AFSKModulator,
        sensor_mods: Dict[str, AFSKModulator],
        gps_reader: GPSDReader,
        sensor_provider: SensorProvider,
        sample_counter_func,
        timestamp_rate_hz: float = 5.0,
        gps_rate_hz: float = 1.0,
        sensor_rate_hz: float = 5.0,
    ) -> None:
        super().__init__(daemon=True)
        self.fs = samplerate
        self.timestamp_mod = timestamp_mod
        self.gps_mod = gps_mod
        self.sensor_mods = sensor_mods
        self.gps_reader = gps_reader
        self.sensor_provider = sensor_provider
        self.sample_counter_func = sample_counter_func

        self.timestamp_period = 1.0 / timestamp_rate_hz
        self.gps_period = 1.0 / gps_rate_hz
        self.sensor_period = 1.0 / sensor_rate_hz

        self.stop_event = threading.Event()
        self.seq_timestamp = 0
        self.seq_gps = 0
        self.seq_sensor: Dict[str, int] = {name: 0 for name in sensor_mods.keys()}

    def run(self) -> None:
        next_ts = time.monotonic()
        next_gps = time.monotonic()
        next_sensor = time.monotonic()

        while not self.stop_event.is_set():
            now = time.monotonic()

            if now >= next_ts:
                self._emit_timestamp()
                next_ts += self.timestamp_period

            if now >= next_gps:
                self._emit_gps()
                next_gps += self.gps_period

            if now >= next_sensor:
                self._emit_sensors()
                next_sensor += self.sensor_period

            time.sleep(0.002)

    def _emit_timestamp(self) -> None:
        t = time.time()
        sec = int(t)
        nsec = int((t - sec) * 1_000_000_000)
        sample_index = int(self.sample_counter_func())

        # Payload:
        #   uint64 unix seconds
        #   uint32 nanoseconds
        #   uint64 approximate current audio sample index
        payload = struct.pack("<QIQ", sec, nsec, sample_index)
        frame = build_frame(FrameType.TIMESTAMP, self.seq_timestamp, payload)
        self.timestamp_mod.enqueue_bytes(frame)
        self.seq_timestamp = (self.seq_timestamp + 1) & 0xFFFF

    def _emit_gps(self) -> None:
        gps = self.gps_reader.get_state()

        lat_e7 = int(round(gps.lat * 1e7))
        lon_e7 = int(round(gps.lon * 1e7))
        alt_cm = int(round(gps.alt_m * 100.0))
        speed_cms = int(round(gps.speed_mps * 100.0))
        track_cdeg = int(round(gps.track_deg * 100.0))
        hdop_cm = int(round(gps.hdop * 100.0))
        fix_quality = int(gps.mode if gps.valid else 0)

        # Payload:
        #   int32 lat_e7
        #   int32 lon_e7
        #   int32 alt_cm
        #   uint16 speed_cms
        #   uint16 track_cdeg
        #   uint8 fix_quality
        #   uint16 hdop_cm
        #   uint64 approximate audio sample index
        sample_index = int(self.sample_counter_func())

        payload = struct.pack(
            "<iiiHHBHQ",
            lat_e7,
            lon_e7,
            alt_cm,
            speed_cms & 0xFFFF,
            track_cdeg & 0xFFFF,
            fix_quality & 0xFF,
            hdop_cm & 0xFFFF,
            sample_index,
        )
        frame = build_frame(FrameType.GPS, self.seq_gps, payload)
        self.gps_mod.enqueue_bytes(frame)
        self.seq_gps = (self.seq_gps + 1) & 0xFFFF

    def _emit_sensors(self) -> None:
        values = self.sensor_provider.read_all()
        sample_index = int(self.sample_counter_func())

        for name, mod in self.sensor_mods.items():
            sensor = values.get(name, SensorValue(name, float("nan"), ""))

            # Payload:
            #   uint8 name length
            #   bytes sensor name
            #   float32 value
            #   uint64 approximate audio sample index
            name_bytes = name.encode("utf-8")[:64]
            payload = struct.pack("<B", len(name_bytes))
            payload += name_bytes
            payload += struct.pack("<fQ", float(sensor.value), sample_index)

            seq = self.seq_sensor[name]
            frame = build_frame(FrameType.SENSOR, seq, payload)
            mod.enqueue_bytes(frame)
            self.seq_sensor[name] = (seq + 1) & 0xFFFF

    def stop(self) -> None:
        self.stop_event.set()


# ---------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------

@dataclasses.dataclass
class RecorderConfig:
    mode: str
    device: Optional[int]
    samplerate: int
    blocksize: int
    input_channels: int
    outfile: str
    duration: Optional[float]
    sensor_names: List[str]
    subtype: str = "PCM_24"


class MultiChannelRecorder:
    def __init__(self, cfg: RecorderConfig) -> None:
        self.cfg = cfg
        self.stop_event = threading.Event()
        self.audio_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=64)
        self.sample_counter = 0
        self.sample_counter_lock = threading.Lock()

        self.gps_reader = GPSDReader()
        self.sensor_provider = SensorProvider(cfg.sensor_names)

        self.timestamp_mod = AFSKModulator(cfg.samplerate)
        self.gps_mod = AFSKModulator(cfg.samplerate)
        self.sensor_mods = {
            name: AFSKModulator(cfg.samplerate)
            for name in cfg.sensor_names
        }

        self.scheduler = MetadataScheduler(
            samplerate=cfg.samplerate,
            timestamp_mod=self.timestamp_mod,
            gps_mod=self.gps_mod,
            sensor_mods=self.sensor_mods,
            gps_reader=self.gps_reader,
            sensor_provider=self.sensor_provider,
            sample_counter_func=self.get_sample_counter,
        )

        if cfg.mode == "software":
            self.output_channels = 3 + len(cfg.sensor_names)
        else:
            self.output_channels = cfg.input_channels

    def get_sample_counter(self) -> int:
        with self.sample_counter_lock:
            return self.sample_counter

    def _increment_sample_counter(self, frames: int) -> None:
        with self.sample_counter_lock:
            self.sample_counter += frames

    def _callback_software(self, indata, frames, time_info, status) -> None:
        if status:
            print(f"Audio callback status: {status}", file=sys.stderr)

        # Ch.1 hydrophone from first input channel
        hydro = indata[:, 0].astype(np.float32)

        timestamp_track = self.timestamp_mod.generate(frames)
        gps_track = self.gps_mod.generate(frames)

        tracks = [hydro, timestamp_track, gps_track]

        for name in self.cfg.sensor_names:
            tracks.append(self.sensor_mods[name].generate(frames))

        block = np.column_stack(tracks).astype(np.float32)

        try:
            self.audio_queue.put_nowait(block)
        except queue.Full:
            print("ERROR: audio queue overflow. Disk too slow or blocksize too small.", file=sys.stderr)
            self.stop_event.set()

        self._increment_sample_counter(frames)

    def _callback_hardware(self, indata, frames, time_info, status) -> None:
        """
        Hardware mode:
            All channels are already present at the audio-interface inputs.
            Example:
                Ch.1 hydrophone
                Ch.2 timestamp AFSK from external encoder
                Ch.3 GPS AFSK from external encoder
                Ch.4..N sensors from external encoders
        """
        if status:
            print(f"Audio callback status: {status}", file=sys.stderr)

        block = indata.copy().astype(np.float32)

        try:
            self.audio_queue.put_nowait(block)
        except queue.Full:
            print("ERROR: audio queue overflow. Disk too slow or blocksize too small.", file=sys.stderr)
            self.stop_event.set()

        self._increment_sample_counter(frames)

    def _writer_thread(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.cfg.outfile)) or ".", exist_ok=True)

        with sf.SoundFile(
            self.cfg.outfile,
            mode="w",
            samplerate=self.cfg.samplerate,
            channels=self.output_channels,
            subtype=self.cfg.subtype,
            format="WAV",
        ) as wav:
            while not self.stop_event.is_set() or not self.audio_queue.empty():
                try:
                    block = self.audio_queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                wav.write(block)

    def start(self) -> None:
        print("Starting recording pipeline")
        print(f"Mode:          {self.cfg.mode}")
        print(f"Sample rate:   {self.cfg.samplerate}")
        print(f"Block size:    {self.cfg.blocksize}")
        print(f"Input channels:{self.cfg.input_channels}")
        print(f"Output file:   {self.cfg.outfile}")
        print(f"Output chans:  {self.output_channels}")
        print(f"Sensors:       {self.cfg.sensor_names}")
        print("Press Ctrl+C to stop.\n")

        writer = threading.Thread(target=self._writer_thread, daemon=True)
        writer.start()

        if self.cfg.mode == "software":
            self.gps_reader.start()
            self.scheduler.start()
            callback = self._callback_software
        else:
            callback = self._callback_hardware

        started = time.monotonic()

        try:
            with sd.InputStream(
                samplerate=self.cfg.samplerate,
                blocksize=self.cfg.blocksize,
                device=self.cfg.device,
                channels=self.cfg.input_channels,
                dtype="float32",
                callback=callback,
            ):
                while not self.stop_event.is_set():
                    if self.cfg.duration is not None:
                        if time.monotonic() - started >= self.cfg.duration:
                            break
                    time.sleep(0.1)

        except KeyboardInterrupt:
            print("\nStopping by user request.")
        finally:
            self.stop()

        writer.join(timeout=10.0)
        print(f"Recording finished: {self.cfg.outfile}")
        print(f"Samples recorded: {self.get_sample_counter()}")

    def stop(self) -> None:
        self.stop_event.set()
        self.scheduler.stop()
        self.gps_reader.stop()


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Multi-channel hydrophone recorder with embedded timestamp, GPS and sensor tracks."
    )

    parser.add_argument(
        "--mode",
        choices=["software", "hardware"],
        default="software",
        help=(
            "software: record hydrophone input and synthesize metadata channels into WAV; "
            "hardware: record all channels already present on the audio interface inputs."
        ),
    )

    parser.add_argument(
        "--device",
        type=int,
        default=None,
        help="sounddevice input device index. Use 'python3 -m sounddevice' to list devices.",
    )

    parser.add_argument(
        "--samplerate",
        type=int,
        default=48000,
        help="audio sample rate, e.g. 48000 or 96000",
    )

    parser.add_argument(
        "--blocksize",
        type=int,
        default=1024,
        help="audio callback block size",
    )

    parser.add_argument(
        "--input-channels",
        type=int,
        default=1,
        help=(
            "software mode: number of physical input channels, first channel is hydrophone; "
            "hardware mode: total number of physical input channels to record."
        ),
    )

    parser.add_argument(
        "--outfile",
        type=str,
        default="hydrophone_multichannel.wav",
        help="output multi-channel WAV file",
    )

    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="recording duration in seconds. Omit for continuous recording.",
    )

    parser.add_argument(
        "--sensors",
        type=str,
        default="cpu_temp,event_marker,propulsion_state",
        help="comma-separated sensor names for software metadata channels",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    sensor_names = [s.strip() for s in args.sensors.split(",") if s.strip()]

    if args.mode == "hardware" and args.input_channels < 4:
        print(
            "WARNING: hardware mode usually requires at least 4 input channels: "
            "hydrophone, timestamp, GPS, and at least one sensor channel.",
            file=sys.stderr,
        )

    if args.mode == "software" and args.input_channels < 1:
        raise ValueError("software mode requires at least one physical input channel for the hydrophone")

    cfg = RecorderConfig(
        mode=args.mode,
        device=args.device,
        samplerate=args.samplerate,
        blocksize=args.blocksize,
        input_channels=args.input_channels,
        outfile=args.outfile,
        duration=args.duration,
        sensor_names=sensor_names,
    )

    recorder = MultiChannelRecorder(cfg)

    def _handle_signal(signum, frame):
        recorder.stop()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    recorder.start()


if __name__ == "__main__":
    main()