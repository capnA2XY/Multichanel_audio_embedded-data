# Sample-Synchronous Hydrophone Recording Pipeline for Raspberry Pi 5

This repository contains a Python-based recording pipeline for Raspberry Pi 5 designed for **sample-synchronous hydrophone measurement** with embedded metadata tracks.

The system records a multi-channel WAV file in which the hydrophone signal, timestamp information, GPS position, and sensor or operational-state data are stored in separate audio channels sharing the same sample index.

## Concept

The proposed channel layout is:

| Channel | Content | Description |
|---|---|---|
| 1 | Hydrophone | Underwater acoustic recording |
| 2 | Timestamp | Encoded absolute Unix timestamp |
| 3 | GPS | Encoded WGS84 latitude/longitude |
| 4...N | Sensors / states | Encoded sensor values or operational states |

The main idea is to treat metadata as **measurement channels**, not only as external log-file annotations. This reduces post-hoc synchronization uncertainty when building hydrophone datasets for marine monitoring, bioacoustics, condition monitoring, or machine-learning workflows.

## Important Measurement Note

There are two operating modes:

### 1. Software metadata embedding

The hydrophone is recorded from a physical audio input, while timestamp, GPS, and sensor metadata tracks are generated in software and written into the same WAV file.

This mode is useful for:

- development,
- dataset generation,
- decoder testing,
- field prototyping.

However, the metadata tracks are not physically sampled by the audio ADC.

### 2. Hardware metadata embedding

Timestamp, GPS, and sensor metadata are generated externally and electrically injected into real audio-interface inputs.

This is the stronger metrological configuration because all channels are physically sampled by the same audio ADC clock.

Use this mode for strict measurement-traceability claims.

## Repository Contents

```text
.
├── recorder_pipeline.py     # Main Raspberry Pi 5 recording script
├── README.md                # Project documentation
└── recordings/              # Suggested output folder
```

## Requirements

Tested target platform:

```text
Raspberry Pi 5
Raspberry Pi OS 64-bit
Python 3.11+
USB multi-channel audio interface
Optional GPS receiver via gpsd
```

Python packages:

```text
numpy
sounddevice
soundfile
```

System packages:

```bash
sudo apt update
sudo apt install -y python3-pip python3-numpy python3-sounddevice libportaudio2 libsndfile1 gpsd gpsd-clients
```

Install Python dependencies:

```bash
pip3 install soundfile
```

## Audio Device Setup

List available audio devices:

```bash
python3 -m sounddevice
```

Use the correct input device index with the `--device` option.

## GPS Setup

If a GPS receiver is used through `gpsd`, enable and start the service:

```bash
sudo systemctl enable gpsd
sudo systemctl start gpsd
```

Check GPS output:

```bash
cgps
```

or:

```bash
gpsmon
```

The recorder reads GPS data from:

```text
127.0.0.1:2947
```

## Basic Usage

### Software metadata mode

Use this mode when only the hydrophone is connected to the audio interface and metadata tracks are generated in software.

```bash
python3 recorder_pipeline.py \
  --mode software \
  --device 0 \
  --input-channels 1 \
  --samplerate 48000 \
  --duration 300 \
  --outfile recordings/survey_001.wav
```

This produces a WAV file with:

```text
Channel 1     hydrophone input
Channel 2     software-generated timestamp AFSK
Channel 3     software-generated GPS AFSK
Channel 4...N software-generated sensor AFSK
```

### Hardware metadata mode

Use this mode when timestamp, GPS, and sensor metadata are already injected into physical audio-interface inputs.

```bash
python3 recorder_pipeline.py \
  --mode hardware \
  --device 0 \
  --input-channels 8 \
  --samplerate 48000 \
  --duration 300 \
  --outfile recordings/survey_001_hardware.wav
```

Expected hardware channel layout:

```text
Channel 1     hydrophone
Channel 2     timestamp encoder output
Channel 3     GPS encoder output
Channel 4...N sensor/state encoder outputs
```

This is the recommended mode for strict sample-synchronous measurement experiments.

## Command-Line Options

| Option | Description |
|---|---|
| `--mode` | `software` or `hardware` |
| `--device` | Audio input device index |
| `--samplerate` | Audio sample rate, e.g. `48000` or `96000` |
| `--blocksize` | Audio callback block size |
| `--input-channels` | Number of physical input channels |
| `--outfile` | Output WAV filename |
| `--duration` | Recording duration in seconds; omit for continuous recording |
| `--sensors` | Comma-separated sensor names for software mode |

Example with custom sensors:

```bash
python3 recorder_pipeline.py \
  --mode software \
  --device 0 \
  --input-channels 1 \
  --samplerate 48000 \
  --duration 120 \
  --sensors cpu_temp,event_marker,propulsion_state \
  --outfile recordings/test.wav
```

## Metadata Encoding

The software implementation uses AFSK-style audio-band metadata encoding.

Default parameters:

```text
Mark frequency:  1200 Hz
Space frequency: 2200 Hz
Baud rate:       1200 bit/s
Amplitude:       0.35
```

Each metadata frame contains:

```text
Preamble
Sync word
Frame type
Sequence number
Payload length
Payload
CRC-16/CCITT-FALSE
```

Frame types:

| Type | Meaning |
|---|---|
| `0x01` | Timestamp frame |
| `0x02` | GPS frame |
| `0x03` | Sensor frame |

## Timestamp Payload

The timestamp payload contains:

```text
Unix seconds
Nanoseconds
Approximate audio sample index
```

The sample index helps relate decoded metadata frames to the WAV sample grid.

## GPS Payload

The GPS payload contains:

```text
Latitude as signed integer, degrees × 1e7
Longitude as signed integer, degrees × 1e7
Altitude in centimetres
Speed in cm/s
Track in centi-degrees
Fix quality
HDOP × 100
Approximate audio sample index
```

Decoded coordinates:

```text
latitude  = lat_e7 / 1e7
longitude = lon_e7 / 1e7
```

Coordinate system:

```text
WGS84
```

## Sensor Payload

Each sensor frame contains:

```text
Sensor name length
Sensor name
Float32 sensor value
Approximate audio sample index
```

The default software sensors are placeholders:

```text
cpu_temp
event_marker
propulsion_state
```

To connect real sensors, modify the `SensorProvider.read_all()` method in `recorder_pipeline.py`.

Possible integrations:

- Modbus/TCP PLC state,
- RS485 Modbus pressure or depth sensor,
- GPIO event marker,
- I2C temperature sensor,
- propulsion-state signal,
- manual annotation button.

## Output File

The recorder writes a multi-channel WAV file:

```text
PCM_24 WAV
48 kHz default
N channels depending on mode and sensor count
```

The WAV file is intended to be self-contained: acoustic data and metadata tracks remain together during copying, segmentation, and offline analysis.

## Recommended Hardware

For serious experiments:

- Raspberry Pi 5,
- stable USB multi-channel audio interface,
- hydrophone preamplifier,
- GPS receiver,
- metadata encoder or microcontroller for hardware mode,
- shielded cables,
- clean power supply,
- proper grounding and isolation.

For hardware metadata embedding, a microcontroller can generate AFSK signals for timestamp, GPS, and sensor tracks and feed them into separate audio-interface inputs.

## Measurement Limitations

This project does not automatically guarantee sub-sample synchronization.

A conservative claim is:

```text
sample-synchronous acquisition with a common discrete-time sample index
```

Sub-sample timing accuracy should only be claimed after explicit validation.

Important limitations:

- metadata channels can clip if amplitude is too high,
- crosstalk may contaminate hydrophone channels,
- lossy compression must not be used,
- GPS receiver latency is preserved unless corrected,
- PLC or microcontroller acquisition delay is preserved unless characterized,
- software mode is not equivalent to hardware ADC sampling of metadata tracks.

## Validation Strategy

A recommended validation procedure is a loopback test.

Generate the same event in two ways:

1. as an electrical pulse recorded on an audio input,
2. as a timestamped metadata event.

Then compare sample positions:

```text
e_t = (n_metadata - n_pulse) / sample_rate
```

Recommended metrics:

```text
mean timing error
standard deviation
maximum absolute error
frame error rate
bit error rate
GPS valid-fix ratio
long-term drift
```

## Scientific Context

This software supports the architecture described in the related method paper:

```text
A Sample-Synchronous Multi-Channel Measurement Architecture for
Hydrophone-Based Marine Monitoring with Embedded Timestamp, GPS,
and Sensor Tracks
```

The architecture is intended for:

- hydrophone-based marine monitoring,
- passive acoustic monitoring,
- georeferenced underwater-noise mapping,
- ship-component acoustic measurement,
- machine-learning dataset generation,
- acoustic condition monitoring.

## Suggested Citation

```bibtex
@inproceedings{aradi_sample_synchronous_hydrophone,
  author    = {Aradi, Attila},
  title     = {A Sample-Synchronous Multi-Channel Measurement Architecture for Hydrophone-Based Marine Monitoring with Embedded Timestamp, GPS, and Sensor Tracks},
  booktitle = {IEEE MetroSea},
  year      = {2026},
  note      = {Method/architecture paper}
}
```

## License

Choose a license before publishing. For academic and open research use, MIT is a practical default.

Example:

```text
MIT License
```

## Disclaimer

This repository is a research prototype. It is intended for scientific and engineering experiments, not for certified navigation, safety-critical control, or legally binding metrology without additional validation and calibration.
