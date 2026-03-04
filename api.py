"""
api.py — FastAPI application for remote queue management via Tailscale.

All routes require the X-API-KEY header.
Bind on 0.0.0.0:8000 so Tailscale can forward requests from other devices.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

if TYPE_CHECKING:
    from encoder import EncoderWorker
    from queue_manager import QueueManager

logger = logging.getLogger("ffqueue.api")

app = FastAPI(title="FFQueue", version="1.0.0", docs_url="/docs", redoc_url=None)

_key_header = APIKeyHeader(name="X-API-KEY", auto_error=False)

# Injected by main.py before the server starts
_queue: "QueueManager | None" = None
_encoder: "EncoderWorker | None" = None
_api_key: str = ""

LOGS_DIR = Path("logs")
ERROR_LOG = LOGS_DIR / "errors.log"


def init(queue: "QueueManager", encoder: "EncoderWorker", api_key: str) -> None:
    """Inject shared instances.  Call before starting uvicorn."""
    global _queue, _encoder, _api_key
    _queue = queue
    _encoder = encoder
    _api_key = api_key


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

async def _auth(key: str = Depends(_key_header)) -> str:
    if not key or key != _api_key:
        raise HTTPException(status_code=403, detail="Invalid or missing X-API-KEY header.")
    return key


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class AddRequest(BaseModel):
    paths: list[str]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/status", dependencies=[Depends(_auth)])
async def get_status() -> dict:
    """Current encoder state including progress and ETA."""
    return _encoder.state.snapshot()  # type: ignore[union-attr]


@app.get("/queue", dependencies=[Depends(_auth)])
async def get_queue() -> list:
    """All queue items (pending, encoding, done, failed)."""
    return _queue.get_all()  # type: ignore[union-attr]


@app.post("/add", dependencies=[Depends(_auth)])
async def add_files(body: AddRequest) -> dict:
    """Add file paths to the encode queue."""
    added = _queue.add_files(body.paths)  # type: ignore[union-attr]
    return {"added": added, "paths": body.paths}


@app.post("/start", dependencies=[Depends(_auth)])
async def start_encoding() -> dict:
    """Start the encoder worker (safe to call when already running)."""
    _encoder.start()  # type: ignore[union-attr]
    return {"message": "Encoder started."}


@app.post("/stop", dependencies=[Depends(_auth)])
async def stop_encoding() -> dict:
    """Finish current file then stop."""
    _encoder.request_stop()  # type: ignore[union-attr]
    return {"message": "Stop requested — will finish current file first."}


@app.delete("/queue/{item_id}", dependencies=[Depends(_auth)])
async def remove_item(item_id: str) -> dict:
    """Remove a pending item from the queue by ID."""
    removed = _queue.remove_item(item_id)  # type: ignore[union-attr]
    if not removed:
        raise HTTPException(status_code=404, detail="Item not found or not removable.")
    return {"removed": item_id}


@app.get("/logs", dependencies=[Depends(_auth)])
async def get_logs(lines: int = 100) -> dict:
    """Return the last N lines of errors.log."""
    if not ERROR_LOG.exists():
        return {"lines": []}
    text = ERROR_LOG.read_text(encoding="utf-8", errors="replace")
    return {"lines": text.splitlines()[-lines:]}
