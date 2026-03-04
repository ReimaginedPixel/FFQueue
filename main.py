"""
main.py — FFQueue entry point.

Start order:
  1. Create logs/ directory and configure logging
  2. Load config (creates config.json on first run)
  3. Create QueueManager + EncoderWorker
  4. Inject into FastAPI app
  5. Start uvicorn in a daemon thread
  6. Start encoder worker daemon thread
  7. Run Tkinter GUI in the main thread (blocks until window is closed)
"""

import logging
import threading
from pathlib import Path

# ---------------------------------------------------------------------------
# Logging must be configured before importing any project modules
# ---------------------------------------------------------------------------
_LOGS = Path("logs")
_LOGS.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(_LOGS / "errors.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

# ---------------------------------------------------------------------------
# Project imports (after logging is set up)
# ---------------------------------------------------------------------------
import uvicorn

from api import app as fastapi_app
from api import init as api_init
from config import load_config
from encoder import EncoderWorker
from gui import App
from queue_manager import QueueManager


def main() -> None:
    config = load_config()

    queue   = QueueManager()
    encoder = EncoderWorker(
        queue=queue,
        ffmpeg=config["ffmpeg_path"],
        ffprobe=config["ffprobe_path"],
        auto_shutdown=config["auto_shutdown"],
        silence_threshold_db=config["silence_threshold_db"],
        silence_sample_seconds=config["silence_sample_seconds"],
    )

    api_init(queue, encoder, config["api_key"])

    # --- Start API server in a daemon thread ---
    uvi_config = uvicorn.Config(
        fastapi_app,
        host=config["api_host"],
        port=config["api_port"],
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(uvi_config)
    server.install_signal_handlers = lambda: None  # prevent uvicorn from stealing signals

    api_thread = threading.Thread(target=server.run, name="UvicornServer", daemon=True)
    api_thread.start()

    logging.getLogger("ffqueue").info(
        f"API listening on http://{config['api_host']}:{config['api_port']} — "
        f"use X-API-KEY header to authenticate"
    )

    # --- Auto-start encoder (picks up pending items from queue.json) ---
    encoder.start()

    # --- Run GUI in the main thread (Tkinter requirement) ---
    App(queue=queue, encoder=encoder).mainloop()


if __name__ == "__main__":
    main()
