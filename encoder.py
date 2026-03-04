"""
encoder.py — FFmpeg worker thread with NVENC encoding and smart stream analysis.

Key behaviours:
- Inspects every file before encoding: skips HEVC files, drops silent audio tracks.
- Writes to a _temp.mkv file first; only renames over the original on exit code 0.
- Falls back to libx265 if hevc_nvenc is unavailable.
- Parses FFmpeg -progress output for real-time percent / ETA.
- Logs per-file stats to logs/encode_log.csv.
"""

import csv
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from queue_manager import QueueManager

logger = logging.getLogger("ffqueue.encoder")

LOGS_DIR = Path("logs")
ENCODE_LOG = LOGS_DIR / "encode_log.csv"

_CSV_FIELDS = [
    "timestamp", "input_path", "input_size_mb", "output_size_mb",
    "reduction_pct", "encode_seconds", "encoder_used",
    "audio_kept", "audio_dropped", "status",
]


def _write_csv_row(row: dict) -> None:
    write_header = not ENCODE_LOG.exists()
    with ENCODE_LOG.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow(row)


# ---------------------------------------------------------------------------
# ffprobe / ffmpeg analysis helpers
# ---------------------------------------------------------------------------

def probe_video_codec(path: str, ffprobe: str = "ffprobe") -> str:
    """Return the codec name of the first video stream (e.g. 'hevc', 'h264')."""
    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout.strip().lower()
    except Exception as exc:
        logger.warning(f"probe_video_codec failed for {path!r}: {exc}")
        return ""


def probe_audio_streams(path: str, ffprobe: str = "ffprobe") -> list[dict]:
    """Return a list of audio stream dicts: {index, codec_name, channels}."""
    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-select_streams", "a",
                "-show_entries", "stream=index,codec_name,channels",
                "-of", "csv=p=0",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        streams = []
        for line in result.stdout.strip().splitlines():
            parts = line.strip().split(",")
            if len(parts) >= 1:
                try:
                    streams.append({
                        "index":      int(parts[0]),
                        "codec_name": parts[1] if len(parts) > 1 else "unknown",
                        "channels":   int(parts[2]) if len(parts) > 2 else 0,
                    })
                except (ValueError, IndexError):
                    continue
        return streams
    except Exception as exc:
        logger.warning(f"probe_audio_streams failed for {path!r}: {exc}")
        return []


def probe_stream_silence(
    path: str,
    stream_index: int,
    ffmpeg: str = "ffmpeg",
    threshold_db: float = -90.0,
    sample_seconds: int = 60,
) -> bool:
    """
    Return True if the audio stream at stream_index is silent.
    Runs volumedetect on the first `sample_seconds` of the stream.
    On any error, returns False (conservative: assume NOT silent).
    """
    try:
        result = subprocess.run(
            [
                ffmpeg,
                "-t", str(sample_seconds),
                "-i", path,
                "-map", f"0:{stream_index}",
                "-af", "volumedetect",
                "-f", "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=sample_seconds + 30,
        )
        # volumedetect writes to stderr
        match = re.search(r"max_volume:\s*([-\d.]+)\s*dB", result.stderr)
        if match:
            max_vol = float(match.group(1))
            return max_vol <= threshold_db
    except Exception as exc:
        logger.warning(f"probe_stream_silence failed (stream {stream_index}): {exc}")
    return False


def probe_duration(path: str, ffprobe: str = "ffprobe") -> Optional[float]:
    """Return duration in seconds, or None on failure."""
    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        val = result.stdout.strip()
        return float(val) if val else None
    except Exception as exc:
        logger.warning(f"probe_duration failed for {path!r}: {exc}")
        return None


def _parse_out_time(kv: dict) -> Optional[float]:
    """Extract elapsed output seconds from an FFmpeg -progress key/value block."""
    # out_time_us is microseconds in modern FFmpeg builds
    if "out_time_us" in kv:
        try:
            val = int(kv["out_time_us"])
            if val >= 0:
                return val / 1_000_000
        except ValueError:
            pass
    # Fallback: parse human-readable "HH:MM:SS.ffffff"
    out_time = kv.get("out_time", "")
    if out_time and out_time != "N/A":
        try:
            h, m, s = out_time.split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
        except (ValueError, AttributeError):
            pass
    return None


# ---------------------------------------------------------------------------
# Shared encoder state (thread-safe)
# ---------------------------------------------------------------------------

class EncoderState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.status: str = "idle"          # idle | encoding | stopping | error
        self.current_file: Optional[str] = None
        self.current_id: Optional[str] = None
        self.progress_percent: float = 0.0
        self.eta_seconds: Optional[float] = None
        self.queue_remaining: int = 0
        self.phase: str = ""              # "probing" | "encoding" | ""

    def update(self, **kwargs) -> None:
        with self._lock:
            for k, v in kwargs.items():
                setattr(self, k, v)

    def snapshot(self) -> dict:
        with self._lock:
            eta_min = (
                round(self.eta_seconds / 60, 1)
                if self.eta_seconds is not None
                else None
            )
            return {
                "status":           self.status,
                "current_file":     self.current_file,
                "phase":            self.phase,
                "progress_percent": round(self.progress_percent, 1),
                "eta_minutes":      eta_min,
                "queue_remaining":  self.queue_remaining,
            }


# ---------------------------------------------------------------------------
# Encoder worker
# ---------------------------------------------------------------------------

class EncoderWorker:
    def __init__(
        self,
        queue: QueueManager,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
        auto_shutdown: bool = False,
        silence_threshold_db: float = -90.0,
        silence_sample_seconds: int = 60,
        output_dir: str = "",
        on_update: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self._queue = queue
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe
        self._auto_shutdown = auto_shutdown
        self._silence_threshold = silence_threshold_db
        self._silence_sample = silence_sample_seconds
        self._output_dir = Path(output_dir) if output_dir else None
        self._on_update = on_update

        if self._output_dir:
            try:
                self._output_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.error(f"Cannot create output_dir {self._output_dir!r}: {exc}")
                self._output_dir = None

        self.state = EncoderState()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc_lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None

        # Session counters (reset each worker start)
        self._session_done = 0
        self._session_failed = 0
        self._session_bytes_reclaimed = 0

    # ------------------------------------------------------------------
    # Public control

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._session_done = 0
        self._session_failed = 0
        self._session_bytes_reclaimed = 0
        self._thread = threading.Thread(
            target=self._worker_loop,
            name="EncoderWorker",
            daemon=True,
        )
        self._thread.start()
        logger.info("Encoder worker started.")

    def request_stop(self) -> None:
        """Finish the current encode, then stop the worker loop."""
        self._stop_event.set()
        if self.state.status == "encoding":
            self.state.update(status="stopping")
            self._notify()
        logger.info("Stop requested — will stop after current file.")

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # Internal

    def _notify(self) -> None:
        if self._on_update:
            try:
                self._on_update(self.state.snapshot())
            except Exception:
                pass

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            item = self._queue.get_next_pending()
            if item is None:
                self.state.update(
                    status="idle",
                    current_file=None,
                    current_id=None,
                    progress_percent=0.0,
                    eta_seconds=None,
                    queue_remaining=0,
                    phase="",
                )
                self._notify()
                time.sleep(1.5)
                continue
            self._encode_item(item)

        self.state.update(
            status="idle",
            current_file=None,
            current_id=None,
            progress_percent=0.0,
            eta_seconds=None,
            phase="",
        )
        self._notify()
        self._log_session_summary()
        logger.info("Encoder worker stopped.")

        if self._auto_shutdown and self._queue.get_pending_count() == 0:
            logger.info("All done — scheduling system shutdown in 60 s.")
            subprocess.run(
                ["shutdown", "/s", "/t", "60", "/c", "FFQueue: encoding complete."],
                check=False,
            )

    def _encode_item(self, item: dict) -> None:
        file_path = item["file_path"]
        item_id   = item["id"]

        # ---- 1. Probe video codec ----------------------------------------
        self.state.update(
            status="encoding",
            current_file=file_path,
            current_id=item_id,
            progress_percent=0.0,
            eta_seconds=None,
            queue_remaining=self._queue.get_pending_count(),
            phase="probing",
        )
        self._notify()
        self._queue.mark_encoding(item_id)

        video_codec = probe_video_codec(file_path, self._ffprobe)
        if video_codec == "hevc":
            logger.info(f"Skipping {file_path!r} — already HEVC.")
            input_size = Path(file_path).stat().st_size if Path(file_path).exists() else 0
            self._queue.mark_done(
                item_id,
                encoder_used="skipped (already HEVC)",
                input_size_bytes=input_size,
                output_size_bytes=input_size,
            )
            _write_csv_row({
                "timestamp":      _ts(),
                "input_path":     file_path,
                "input_size_mb":  round(input_size / 1_048_576, 2),
                "output_size_mb": round(input_size / 1_048_576, 2),
                "reduction_pct":  0.0,
                "encode_seconds": 0,
                "encoder_used":   "skipped",
                "audio_kept":     "",
                "audio_dropped":  "",
                "status":         "skipped",
            })
            self._session_done += 1
            return

        # ---- 2. Probe audio streams + silence ----------------------------
        audio_streams = probe_audio_streams(file_path, self._ffprobe)
        kept: list[int] = []
        dropped: list[int] = []

        for stream in audio_streams:
            idx = stream["index"]
            silent = probe_stream_silence(
                file_path,
                idx,
                self._ffmpeg,
                self._silence_threshold,
                self._silence_sample,
            )
            if silent:
                logger.info(f"  Stream {idx} is silent (≤ {self._silence_threshold} dB) — dropping.")
                dropped.append(idx)
            else:
                kept.append(idx)

        # Safety: always keep at least one audio track
        if not kept and audio_streams:
            fallback = audio_streams[0]["index"]
            logger.warning(f"  All audio streams silent — keeping stream {fallback} as fallback.")
            kept.append(fallback)
            dropped = [s["index"] for s in audio_streams if s["index"] != fallback]

        # ---- 3. Probe duration for progress calculation ------------------
        duration = probe_duration(file_path, self._ffprobe)

        # ---- 4. Build output paths ---------------------------------------
        src = Path(file_path)
        temp_path = src.with_name(f"{src.stem}_temp.mkv")

        # Remove stale temp from a previous failed run
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass

        input_size = src.stat().st_size if src.exists() else 0

        # ---- 5. Run FFmpeg -----------------------------------------------
        self.state.update(phase="encoding", progress_percent=0.0)
        self._notify()

        success, encoder_used, stderr_tail = self._run_ffmpeg(
            file_path, str(temp_path), kept, duration, use_nvenc=True
        )

        # NVENC fallback
        if not success and self._nvenc_unavailable(stderr_tail):
            logger.warning("hevc_nvenc unavailable — retrying with libx265.")
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
            self.state.update(progress_percent=0.0, eta_seconds=None)
            success, encoder_used, stderr_tail = self._run_ffmpeg(
                file_path, str(temp_path), kept, duration, use_nvenc=False
            )

        # ---- 6. Finalise -------------------------------------------------
        if success:
            output_size = temp_path.stat().st_size if temp_path.exists() else 0

            # Replace original with encoded temp
            try:
                os.replace(str(temp_path), file_path)
            except OSError as exc:
                logger.error(f"Rename failed for {file_path!r}: {exc}")
                temp_path.unlink(missing_ok=True)
                self._queue.mark_failed(item_id, f"Rename failed: {exc}")
                self._session_failed += 1
                return

            # Move to output_dir if configured
            final_path = file_path
            if self._output_dir:
                dest = self._output_dir / Path(file_path).name
                try:
                    shutil.move(file_path, str(dest))
                    final_path = str(dest)
                    logger.info(f"Moved to {dest!r}")
                except OSError as exc:
                    logger.error(f"Move to output_dir failed for {file_path!r}: {exc} — file stays in place.")

            reduction = (1 - output_size / max(input_size, 1)) * 100
            logger.info(
                f"Done: {Path(file_path).name}  "
                f"{input_size/1_048_576:.1f} MB → {output_size/1_048_576:.1f} MB  "
                f"({reduction:.1f}% reduction)  →  {final_path!r}"
            )

            reclaimed = max(input_size - output_size, 0)
            self._session_bytes_reclaimed += reclaimed
            self._session_done += 1

            self._queue.mark_done(
                item_id,
                encoder_used=encoder_used,
                audio_kept=kept,
                audio_dropped=dropped,
                input_size_bytes=input_size,
                output_size_bytes=output_size,
                final_path=final_path,
            )
            self.state.update(progress_percent=100.0, eta_seconds=0.0)
            self._notify()

            _write_csv_row({
                "timestamp":      _ts(),
                "input_path":     file_path,
                "input_size_mb":  round(input_size / 1_048_576, 2),
                "output_size_mb": round(output_size / 1_048_576, 2),
                "reduction_pct":  round(reduction, 1),
                "encode_seconds": round(self._last_encode_seconds, 1),
                "encoder_used":   encoder_used,
                "audio_kept":     ",".join(map(str, kept)),
                "audio_dropped":  ",".join(map(str, dropped)),
                "status":         "done",
            })

        else:
            temp_path.unlink(missing_ok=True)
            error_msg = f"FFmpeg failed:\n{stderr_tail}"
            logger.error(f"Encode failed for {file_path!r}:\n{stderr_tail}")
            self._queue.mark_failed(item_id, error_msg[:2000])
            self.state.update(status="error")
            self._notify()
            self._session_failed += 1

            _write_csv_row({
                "timestamp":      _ts(),
                "input_path":     file_path,
                "input_size_mb":  round(input_size / 1_048_576, 2),
                "output_size_mb": 0,
                "reduction_pct":  0,
                "encode_seconds": round(getattr(self, "_last_encode_seconds", 0), 1),
                "encoder_used":   encoder_used,
                "audio_kept":     "",
                "audio_dropped":  "",
                "status":         "failed",
            })

    def _run_ffmpeg(
        self,
        input_path: str,
        output_path: str,
        audio_indices: list[int],
        duration: Optional[float],
        use_nvenc: bool,
    ) -> tuple[bool, str, str]:
        """
        Run FFmpeg.  Returns (success, encoder_name, stderr_tail).
        Updates self.state with live progress.
        """
        if use_nvenc:
            # -hwaccel must come before -i; the codec flags come after
            pre_input  = ["-hwaccel", "cuda"]
            video_args = [
                "-c:v", "hevc_nvenc",
                "-preset", "p4",
                "-cq", "24",
                "-spatial-aq", "1",
                "-aq-strength", "8",
            ]
            encoder_name = "hevc_nvenc"
        else:
            pre_input  = []
            video_args = [
                "-c:v", "libx265",
                "-crf", "24",
                "-preset", "medium",
            ]
            encoder_name = "libx265"

        map_args: list[str] = ["-map", "0:v:0"]
        for idx in audio_indices:
            map_args += ["-map", f"0:{idx}"]

        cmd = (
            [self._ffmpeg]
            + pre_input
            + ["-i", input_path]
            + map_args
            + video_args
            + ["-c:a", "copy", "-progress", "pipe:1", "-nostats", output_path]
        )

        stderr_lines: list[str] = []
        encode_start = time.monotonic()

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
            )
            with self._proc_lock:
                self._proc = proc

            def _drain_stderr() -> None:
                for line in proc.stderr:
                    stderr_lines.append(line)

            stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
            stderr_thread.start()

            kv: dict[str, str] = {}
            for raw in proc.stdout:
                line = raw.strip()
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                kv[key.strip()] = val.strip()
                if kv.get("progress") in ("continue", "end"):
                    self._push_progress(kv, duration, encode_start)
                    kv = {}

            proc.wait()
            stderr_thread.join(timeout=15)

            with self._proc_lock:
                self._proc = None

            self._last_encode_seconds = time.monotonic() - encode_start
            stderr_tail = "".join(stderr_lines[-40:]).strip()
            return proc.returncode == 0, encoder_name, stderr_tail

        except FileNotFoundError:
            msg = f"ffmpeg binary not found at {self._ffmpeg!r}"
            logger.error(msg)
            with self._proc_lock:
                self._proc = None
            self._last_encode_seconds = 0.0
            return False, encoder_name, msg

        except Exception as exc:
            logger.error(f"Exception running FFmpeg: {exc}", exc_info=True)
            with self._proc_lock:
                self._proc = None
            self._last_encode_seconds = time.monotonic() - encode_start
            return False, encoder_name, str(exc)

    def _push_progress(
        self,
        kv: dict,
        duration: Optional[float],
        encode_start: float,
    ) -> None:
        out_s = _parse_out_time(kv)
        if out_s is None or out_s < 0:
            return
        if not duration or duration <= 0:
            return
        pct = min(out_s / duration * 100.0, 99.9)
        elapsed = time.monotonic() - encode_start
        eta_s: Optional[float] = None
        if pct > 0.5:
            total_est = elapsed / (pct / 100.0)
            eta_s = max(total_est - elapsed, 0.0)
        self.state.update(progress_percent=pct, eta_seconds=eta_s)
        self._notify()

    @staticmethod
    def _nvenc_unavailable(stderr_tail: str) -> bool:
        lower = stderr_tail.lower()
        return any(
            phrase in lower
            for phrase in ("no capable devices found", "nvenc", "not support", "cannot load")
        )

    def _log_session_summary(self) -> None:
        gb = self._session_bytes_reclaimed / 1_073_741_824
        summary = (
            f"Session summary — "
            f"done: {self._session_done}, "
            f"failed: {self._session_failed}, "
            f"reclaimed: {gb:.2f} GB"
        )
        logger.info(summary)


def _ts() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")
