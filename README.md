# Discord Scam Detector Bot

A Discord bot that uses OCR to detect scam images posted in your server. When a scam image is detected, the message is deleted and the user is timed out.

## How it works

1. Watches for image attachments in monitored channels
2. Runs PaddleOCR on each image to extract text
3. Matches extracted text against configurable scam patterns
4. Deletes the message, times out the user, and logs to a log channel

## Dependencies

| Package | Purpose |
|---------|---------|
| `discord.py` | Discord API client |
| `paddleocr` | OCR engine for extracting text from images |
| `paddlepaddle` | PaddleOCR backend |
| `Pillow` | Image processing |
| `aiohttp` | Async HTTP for fetching image attachments |
| `python-dotenv` | Loading secrets from `.env` |

## Setup

**Requirements:** Python 3.11+, [uv](https://docs.astral.sh/uv/)

```bash
git clone https://github.com/ebvise/discord-scam-detector-bot
cd discord-scam-detector-bot
uv sync
```

Create a `.env` file:

```
DISCORD_TOKEN=your_bot_token_here
```

Edit `config.json` from the example and fill in your channel IDs and other settings:

```json
{
  "moderation": {
    ...
    "log_channel_id": 123456789012345678,
    "monitored_channel_ids": [123456789012345678, 987654321098765432]
  },
  ...
}
```

- **`log_channel_id`** — the channel where the bot posts detection logs. Right-click a channel in Discord → *Copy Channel ID*.
- **`monitored_channel_ids`** — list of channels to watch for scam images. Leave the list empty (`[]`) to watch all channels.

## Running

```bash
uv run bot.py
```

## Configuration

All settings live in `config.json`:

| Key | Description |
|-----|-------------|
| `moderation.timeout_minutes` | How long to timeout offenders |
| `moderation.log_channel_id` | Channel ID to log detections (optional) |
| `moderation.monitored_channel_ids` | Channels to watch (empty = all) |
| `ocr.lang` | OCR language (e.g. `en`) |
| `ocr.confidence` | Minimum OCR confidence score |
| `ocr.max_concurrent` | Max parallel OCR jobs |
| `rate_limit.max_images` | Max images per user per window |
| `rate_limit.window_secs` | Rate limit sliding window in seconds |
| `scam_patterns` | List of regex patterns to match against OCR output |

## Bot permissions required

- Read Messages / View Channels
- Manage Messages (to delete)
- Moderate Members (to timeout)
- Send Messages
