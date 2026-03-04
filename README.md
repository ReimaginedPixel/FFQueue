# FFQueue

A production-ready Windows desktop background encoding manager powered by FFmpeg and NVIDIA NVENC.

Queue multiple video files, encode them one at a time to HEVC/H.265, and monitor progress locally via a Tkinter GUI or remotely over Tailscale via a secure FastAPI REST API.

---

## Features

- **NVENC hardware encoding** — `hevc_nvenc` with `-cq 24 -preset p4 -spatial-aq 1`
- **CPU fallback** — automatically falls back to `libx265 -crf 24 -preset medium` if NVENC is unavailable
- **Smart stream analysis** — runs `volumedetect` on each audio track; drops silent streams (≤ -90 dB), always keeps at least one
- **HEVC skip** — detects files already encoded as HEVC and skips re-encoding them
- **Safe in-place replacement** — encodes to a `_temp.mkv` alongside the original; renames over the original only on exit code 0; deletes the temp on failure, leaving the original untouched
- **Crash-safe queue** — queue persists to `queue.json`; interrupted encodes restart automatically on the next run
- **One file at a time** — HDD-safe; no parallel encodes
- **Live progress + ETA** — parses FFmpeg `-progress` output for percent and estimated time remaining
- **Secure REST API** — FastAPI on `0.0.0.0:8000`, all routes require `X-API-KEY` header
- **Tailscale ready** — bind to `0.0.0.0` makes the API reachable via Tailscale IP from any device
- **Per-file CSV log** — `logs/encode_log.csv` tracks size, reduction %, encode time, encoder used, audio streams kept/dropped
- **Optional auto-shutdown** — configure PC to power off when queue empties

---

## Requirements

- Windows 10/11
- Python 3.11+
- FFmpeg + FFprobe in `PATH` (or set paths in `config.json`)
- NVIDIA GPU with NVENC support (GTX 900 series or newer)

### Install Python dependencies

```bash
pip install -r requirements.txt
```

---

## Quick Start

```bash
python main.py
```

On first run `config.json` is created automatically with a random API key printed to the console:

```
[config] Created config.json
         API key : a3f9b2c1...
         API URL : http://0.0.0.0:8000
```

The GUI opens immediately. Add files and click **Start Encoding**.

---

## File Structure

```
FFQueue/
├── main.py            # Entry point
├── config.py          # Config loader / creator
├── queue_manager.py   # Thread-safe JSON queue
├── encoder.py         # FFmpeg worker thread + stream analysis
├── api.py             # FastAPI REST API
├── gui.py             # Tkinter desktop GUI
├── queue.json         # Persistent queue (auto-managed)
├── config.json        # Created on first run (gitignored)
├── requirements.txt
└── logs/
    ├── errors.log     # All warnings and errors (gitignored)
    └── encode_log.csv # Per-file encode stats (gitignored)
```

---

## Configuration (`config.json`)

| Key | Default | Description |
|---|---|---|
| `api_key` | auto-generated | Secret token for the REST API |
| `ffmpeg_path` | `ffmpeg` | Path to ffmpeg binary |
| `ffprobe_path` | `ffprobe` | Path to ffprobe binary |
| `auto_shutdown` | `false` | Shut down PC when queue finishes |
| `api_host` | `0.0.0.0` | API bind address |
| `api_port` | `8000` | API port |
| `silence_threshold_db` | `-90.0` | Audio streams at or below this level are considered silent |
| `silence_sample_seconds` | `60` | How many seconds of audio to sample for silence detection |

---

## REST API

All routes require the header:
```
X-API-KEY: <your_api_key>
```

| Method | Route | Description |
|---|---|---|
| `GET` | `/status` | Current encoder state |
| `GET` | `/queue` | All queue items |
| `POST` | `/add` | Add files `{"paths": [...]}` |
| `POST` | `/start` | Start encoder worker |
| `POST` | `/stop` | Stop after current file |
| `DELETE` | `/queue/{id}` | Remove a pending item |
| `GET` | `/logs?lines=100` | Last N lines of errors.log |

### Example: `/status` response

```json
{
  "status": "encoding",
  "current_file": "C:/Videos/movie.mkv",
  "phase": "encoding",
  "progress_percent": 42.3,
  "eta_minutes": 18.0,
  "queue_remaining": 5
}
```

### cURL examples

```bash
# Status
curl -H "X-API-KEY: your_key" http://<tailscale-ip>:8000/status

# Add files remotely
curl -X POST -H "X-API-KEY: your_key" -H "Content-Type: application/json" \
  -d '{"paths":["C:/Videos/movie.mkv"]}' \
  http://<tailscale-ip>:8000/add

# Stop after current
curl -X POST -H "X-API-KEY: your_key" http://<tailscale-ip>:8000/stop
```

Interactive API docs available at `http://localhost:8000/docs`.

---

## Encoding Pipeline

For each file the encoder:

1. **Probes video codec** — skips the file if already HEVC
2. **Probes audio streams** — lists all audio tracks with `ffprobe`
3. **Detects silence** — runs `volumedetect` on first 60 s of each track; drops any ≤ -90 dB
4. **Encodes** — maps video + non-silent audio into `_temp.mkv`
5. **On success** — atomically renames temp over original (`os.replace`)
6. **On failure** — deletes temp, logs error, marks item failed, moves to next file

### FFmpeg command (NVENC)

```
ffmpeg -hwaccel cuda
       -i input.mkv
       -map 0:v:0 -map 0:<audio_idx> [...]
       -c:v hevc_nvenc -preset p4 -cq 24 -spatial-aq 1 -aq-strength 8
       -c:a copy
       -progress pipe:1 -nostats
       input_temp.mkv
```

---

## Crash Recovery

If the app closes or crashes while encoding:
- `queue.json` will have the interrupted item with `status: "encoding"`
- On next startup it is automatically reset to `"pending"` and re-encoded from scratch
- The original file is safe because it was never touched (the temp file may exist and will be cleaned up before the retry)

---

## Tailscale Setup

1. Install [Tailscale](https://tailscale.com) on both the encoding PC and your remote device
2. The API already binds to `0.0.0.0:8000` — no extra configuration needed
3. Use the machine's Tailscale IP: `http://100.x.x.x:8000/status`

---

## Auto Shutdown

Set `"auto_shutdown": true` in `config.json`. When the queue empties and the worker exits normally, the PC is scheduled to shut down in 60 seconds (`shutdown /s /t 60`).
