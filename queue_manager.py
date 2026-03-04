"""
queue_manager.py — Thread-safe, JSON-backed encode queue.

Every mutation immediately persists to queue.json so the queue survives
application restarts and crashes.  Any item found in the 'encoding' state
at load time is reset to 'pending' so it will be re-processed.
"""

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

QUEUE_FILE = Path("queue.json")

PENDING  = "pending"
ENCODING = "encoding"
DONE     = "done"
FAILED   = "failed"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class QueueManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[dict] = []
        self._load()

    # ------------------------------------------------------------------
    # Persistence

    def _load(self) -> None:
        if not QUEUE_FILE.exists():
            return
        try:
            data: list[dict] = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
            for item in data:
                if item.get("status") == ENCODING:
                    item["status"] = PENDING
                    item["started_at"] = None
            self._items = data
        except Exception as exc:
            print(f"[queue] Failed to load {QUEUE_FILE}: {exc} — starting empty.")

    def _save(self) -> None:
        """Write queue to disk.  Must be called while holding self._lock."""
        QUEUE_FILE.write_text(
            json.dumps(self._items, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Mutations

    def add_files(self, paths: list[str]) -> int:
        """Queue new files. Skips exact path duplicates already pending."""
        with self._lock:
            added = 0
            for path in paths:
                if any(
                    i["file_path"] == path and i["status"] == PENDING
                    for i in self._items
                ):
                    continue
                self._items.append({
                    "id":                str(uuid.uuid4()),
                    "file_path":         path,
                    "status":            PENDING,
                    "added_at":          _now(),
                    "started_at":        None,
                    "completed_at":      None,
                    "error":             None,
                    "encoder_used":      None,
                    "audio_kept":        None,
                    "audio_dropped":     None,
                    "input_size_bytes":  None,
                    "output_size_bytes": None,
                    "final_path":        None,
                })
                added += 1
            self._save()
            return added

    def mark_encoding(self, item_id: str) -> None:
        with self._lock:
            for item in self._items:
                if item["id"] == item_id:
                    item["status"] = ENCODING
                    item["started_at"] = _now()
                    break
            self._save()

    def mark_done(
        self,
        item_id: str,
        *,
        encoder_used: str = "",
        audio_kept: list[int] | None = None,
        audio_dropped: list[int] | None = None,
        input_size_bytes: int = 0,
        output_size_bytes: int = 0,
        final_path: str = "",
        note: str = "",
    ) -> None:
        with self._lock:
            for item in self._items:
                if item["id"] == item_id:
                    item["status"]            = DONE
                    item["completed_at"]      = _now()
                    item["encoder_used"]      = encoder_used or note
                    item["audio_kept"]        = audio_kept or []
                    item["audio_dropped"]     = audio_dropped or []
                    item["input_size_bytes"]  = input_size_bytes
                    item["output_size_bytes"] = output_size_bytes
                    item["final_path"]        = final_path or item.get("file_path", "")
                    break
            self._save()

    def mark_failed(self, item_id: str, error: str) -> None:
        with self._lock:
            for item in self._items:
                if item["id"] == item_id:
                    item["status"]       = FAILED
                    item["completed_at"] = _now()
                    item["error"]        = error[:2000]
                    break
            self._save()

    def remove_item(self, item_id: str) -> bool:
        with self._lock:
            before = len(self._items)
            self._items = [i for i in self._items if i["id"] != item_id]
            changed = len(self._items) < before
            if changed:
                self._save()
            return changed

    def clear_finished(self) -> None:
        with self._lock:
            self._items = [
                i for i in self._items if i["status"] in (PENDING, ENCODING)
            ]
            self._save()

    # ------------------------------------------------------------------
    # Queries

    def get_next_pending(self) -> Optional[dict]:
        with self._lock:
            for item in self._items:
                if item["status"] == PENDING:
                    return dict(item)
        return None

    def get_pending_count(self) -> int:
        with self._lock:
            return sum(1 for i in self._items if i["status"] == PENDING)

    def get_all(self) -> list[dict]:
        with self._lock:
            return [dict(i) for i in self._items]
