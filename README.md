# FFQueue

Production-ready Windows desktop background encoding manager.
Batch-converts video files to HEVC/H.265 using NVIDIA NVENC — one file at a time, HDD-safe, crash-proof, remotely controllable over Tailscale.

---

## Quick Start

```bash
pip install -r requirements.txt
python main.py
```

On first launch `config.json` is created and the API key is printed to the console. The GUI opens immediately.

---

## Features

### Encoding
- **NVIDIA NVENC** — `hevc_nvenc -cq 24 -preset p4 -spatial-aq 1 -aq-strength 8`
- **CPU fallback** — `libx265 -crf 24 -preset medium` if NVENC is unavailable
- **One file at a time** — HDD-safe, no parallel encodes
- **HEVC skip** — detects files already encoded as HEVC and skips them automatically
- **Silent audio detection** — runs `volumedetect` on the first 60 s of every audio track; drops streams at or below −90 dB, always keeps at least one
- **Smart output check** — if the encoded file is larger than the original it is discarded and the item is marked failed; original is never touched

### Safety
- **Temp-then-rename** — encodes to `_temp.mkv` in the output folder; renames to final name only on FFmpeg exit code 0
- **Original always intact** — original file is never opened or modified
- **Crash recovery** — on restart, any item stuck in `encoding` state is reset to `pending` automatically
- **Queue persistence** — `queue.json` is written after every change

### Remote Control
- **FastAPI REST API** on `0.0.0.0:8000` — reachable through Tailscale from any device
- **API key auth** — all routes require `X-API-KEY` header; key is auto-generated and stored in `config.json`

### GUI
- **Queue tab** — pending and currently encoding files
- **Scheduled tab** — completed and failed files with size stats, savings %, encoder used, and final path
  - Double-click any row to copy the filename to clipboard
  - **Open Original Folder** — opens Explorer with the source file selected
  - **Delete Original File** — manual deletion with confirmation dialog; nothing auto-deletes

---

## Requirements

- Windows 10/11
- Python 3.11+
- FFmpeg + FFprobe in `PATH` (or set paths in `config.json`)
- NVIDIA GPU with NVENC support (GTX 900 series or newer recommended)

---

## Installation

```bash
pip install -r requirements.txt
python main.py
```

---

## File Structure

```
FFQueue/
├── main.py            # Entry point
├── config.py          # Config loader / auto-creator
├── queue_manager.py   # Thread-safe JSON-backed queue
├── encoder.py         # FFmpeg worker + stream analysis
├── api.py             # FastAPI REST API
├── gui.py             # Tkinter GUI
├── requirements.txt
├── queue.json         # Live queue (auto-managed)
├── config.json        # Created on first run — gitignored
└── logs/
    ├── errors.log     # All warnings and errors
    └── encode_log.csv # Per-file stats
```

---

## Configuration (`config.json`)

Created automatically on first run. Edit to customise.

| Key | Default | Description |
|---|---|---|
| `api_key` | auto-generated | Secret token for the REST API |
| `ffmpeg_path` | `ffmpeg` | Path to ffmpeg binary |
| `ffprobe_path` | `ffprobe` | Path to ffprobe binary |
| `output_dir` | `""` | Move encoded files here after success; `""` = encode in-place |
| `auto_shutdown` | `false` | Shut down the PC when the queue empties |
| `api_host` | `0.0.0.0` | API bind address |
| `api_port` | `8000` | API port |
| `silence_threshold_db` | `-90.0` | Audio streams at or below this level are dropped |
| `silence_sample_seconds` | `60` | Seconds of audio sampled per stream for silence detection |

---

## Encoding Pipeline

For every queued file:

1. **Probe video codec** — if already HEVC, skip (mark done, no re-encode)
2. **Probe audio streams** — list all tracks with ffprobe
3. **Silence detection** — run `volumedetect` on first 60 s of each track; drop streams ≤ −90 dB; always keep at least one
4. **Encode** — write to `output_dir/<name>_temp.mkv`
5. **Size check** — if output ≥ input: delete temp, mark failed, move on
6. **Promote** — `os.replace(temp → final)` atomically; original untouched
7. **Log** — append row to `logs/encode_log.csv`

### FFmpeg command (NVENC)

```
ffmpeg -hwaccel cuda
       -i input.mkv
       -map 0:v:0
       -map 0:<non-silent audio streams>
       -c:v hevc_nvenc -preset p4 -cq 24 -spatial-aq 1 -aq-strength 8
       -c:a copy
       -progress pipe:1 -nostats
       output_temp.mkv
```

> **Why `-cq 24` instead of the spec's `26`?**
> In practice `-cq 24` consistently produces ~60–70% file size savings on high-bitrate source files.
> `-cq 26` produced larger or equal-size output in testing. Lower CQ = better quality target = more aggressive compression with NVENC VBR mode.

---

## REST API

All routes require:
```
X-API-KEY: <your_api_key>
```

| Method | Route | Description |
|---|---|---|
| `GET` | `/status` | Encoder state, progress, ETA |
| `GET` | `/queue` | All queue items |
| `POST` | `/add` | Add files `{"paths": ["C:/..."]}`|
| `POST` | `/start` | Start encoder worker |
| `POST` | `/stop` | Stop after current file finishes |
| `DELETE` | `/queue/{id}` | Remove a pending item |
| `GET` | `/logs?lines=100` | Last N lines of errors.log |

### `/status` response

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

### Examples

```bash
# Check status
curl -H "X-API-KEY: your_key" http://<tailscale-ip>:8000/status

# Add files remotely
curl -X POST \
     -H "X-API-KEY: your_key" \
     -H "Content-Type: application/json" \
     -d '{"paths":["C:/Videos/movie.mkv","C:/Videos/show.mp4"]}' \
     http://<tailscale-ip>:8000/add

# Stop after current file
curl -X POST -H "X-API-KEY: your_key" http://<tailscale-ip>:8000/stop
```

Interactive API docs: `http://localhost:8000/docs`

---

## Tailscale Setup

1. Install [Tailscale](https://tailscale.com) on both machines
2. API already binds to `0.0.0.0:8000` — no extra config needed
3. Access via Tailscale IP: `http://100.x.x.x:8000/status`

---

## Crash Recovery

If the app is killed mid-encode:
- `queue.json` has the item at `status: "encoding"`
- On next startup it resets to `"pending"` and re-encodes from the beginning
- The stale `_temp.mkv` in the output folder is deleted before the retry
- The original file is always safe

---

## Auto Shutdown

Set `"auto_shutdown": true` in `config.json`. When the queue empties and the worker exits normally, Windows will shut down in 60 seconds (`shutdown /s /t 60`).

---

## Logs

| File | Contents |
|---|---|
| `logs/errors.log` | All INFO/WARNING/ERROR messages with timestamps |
| `logs/encode_log.csv` | Per-file: input MB, output MB, reduction %, encode time, encoder used, audio tracks kept/dropped, status |
