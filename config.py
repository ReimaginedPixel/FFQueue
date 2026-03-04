"""
config.py — Load and persist application configuration.

On first run, config.json is created with a randomly generated API key.
The key is printed to the console so the user can copy it.
"""

import json
import secrets
from pathlib import Path

CONFIG_FILE = Path("config.json")

_DEFAULTS: dict = {
    "api_key": "",
    "ffmpeg_path": "ffmpeg",
    "ffprobe_path": "ffprobe",
    "auto_shutdown": False,
    "api_host": "0.0.0.0",
    "api_port": 8000,
    "silence_threshold_db": -90.0,
    "silence_sample_seconds": 60,
    "output_dir": "",   # Move encoded files here after success; "" = keep in place
}


def load_config() -> dict:
    """Load config from disk, creating it with safe defaults if missing."""
    if CONFIG_FILE.exists():
        try:
            stored = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            config = {**_DEFAULTS, **stored}
            if not config.get("api_key"):
                config["api_key"] = secrets.token_hex(24)
                CONFIG_FILE.write_text(json.dumps(config, indent=2), encoding="utf-8")
                print(f"[config] Generated new API key: {config['api_key']}")
            return config
        except Exception as exc:
            print(f"[config] Failed to read config.json ({exc}) — rebuilding with defaults.")

    config = {**_DEFAULTS, "api_key": secrets.token_hex(24)}
    CONFIG_FILE.write_text(json.dumps(config, indent=2), encoding="utf-8")
    print(
        f"[config] Created config.json\n"
        f"         API key : {config['api_key']}\n"
        f"         API URL : http://{config['api_host']}:{config['api_port']}"
    )
    return config
