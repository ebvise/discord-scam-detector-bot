import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_CONFIG_PATH = Path(__file__).parent / "config.json"


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val


with open(_CONFIG_PATH) as _f:
    _data = json.load(_f)

# Discord — secrets stay in .env
TOKEN: str = _require("DISCORD_TOKEN")

# Moderation
_mod = _data["moderation"]
TIMEOUT_MINUTES: int = _mod["timeout_minutes"]
LOG_CHANNEL_ID: int | None = _mod.get("log_channel_id") or None
MONITORED_CHANNEL_IDS: set[int] = set(_mod.get("monitored_channel_ids", []))

# OCR
_ocr = _data["ocr"]
OCR_LANG: str = _ocr["lang"]
OCR_CONFIDENCE: float = _ocr["confidence"]
OCR_MAX_CONCURRENT: int = _ocr.get("max_concurrent", 3)

# Rate limiting
_rl = _data.get("rate_limit", {})
RATE_LIMIT_MAX_IMAGES: int = _rl.get("max_images", 5)
RATE_LIMIT_WINDOW_SECS: int = _rl.get("window_secs", 30)

# Scam patterns
SCAM_PATTERNS: list[str] = _data["scam_patterns"]
COMPILED_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE) for p in SCAM_PATTERNS
]
