import asyncio
import io
import logging
import time
import unicodedata
from collections import defaultdict, deque
from datetime import timedelta

import aiohttp
import discord
import numpy as np
from paddleocr import PaddleOCR
from PIL import Image

import config

log = logging.getLogger(__name__)

for _name in ("paddleocr", "ppocr", "paddle", "PaddleOCR"):
    logging.getLogger(_name).setLevel(logging.ERROR)

# enable_mkldnn=False: disables oneDNN which has a known Windows bug in PaddlePaddle 3.3.x
ocr = PaddleOCR(
    use_textline_orientation=True,
    lang=config.OCR_LANG,
    device="cpu",
    enable_mkldnn=False,
)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
client = discord.Client(intents=intents)

MAX_IMAGE_BYTES = 15 * 1024 * 1024  # 15 MB

_http: aiohttp.ClientSession | None = None
_ocr_sem: asyncio.Semaphore | None = None

# Sliding-window rate limiter: user_id → deque of (timestamp, n_images, channel_id, message_id)
_user_message_log: dict[int, deque[tuple[float, int, int, int]]] = defaultdict(deque)


def _consume_rate_limit(
    user_id: int, n: int, channel_id: int, message_id: int
) -> tuple[bool, list[tuple[int, int]]]:
    """Return (blocked, prior_message_refs).

    If blocked, prior_message_refs contains (channel_id, message_id) of every
    message still in the window so callers can delete them too.
    """
    now = time.monotonic()
    dq = _user_message_log[user_id]
    while dq and now - dq[0][0] > config.RATE_LIMIT_WINDOW_SECS:
        dq.popleft()
    total = sum(entry[1] for entry in dq)
    if total + n > config.RATE_LIMIT_MAX_IMAGES:
        return True, [(entry[2], entry[3]) for entry in dq]
    dq.append((now, n, channel_id, message_id))
    return False, []


def sanitize_ocr(text: str) -> str:
    # Normalize unicode so homoglyph substitutions (e.g. ℬitcoin) are collapsed
    text = unicodedata.normalize("NFKC", text)
    # Strip control characters (keep ordinary whitespace)
    text = "".join(c for c in text if unicodedata.category(c)[0] != "C" or c in " \t\n")
    # Collapse whitespace
    text = " ".join(text.split())
    # Neutralize Discord mention triggers (@everyone, @here, user/role pings)
    text = text.replace("@", "[@]")
    # Prevent backtick sequences from breaking Discord code block fences
    text = text.replace("`", "ˋ")
    return text


def _timeout_exempt(member: discord.Member) -> bool:
    if member.id == member.guild.owner_id:
        return True
    if member.guild_permissions.administrator:
        return True
    bot_member = member.guild.get_member(client.user.id)
    if bot_member and member.top_role >= bot_member.top_role:
        return True
    return False


def is_scam(text: str) -> tuple[bool, list[str]]:
    hits = []
    for pattern in config.COMPILED_PATTERNS:
        match = pattern.search(text)
        if match:
            hits.append(match.group(0))
    return bool(hits), hits


def _parse_predict_result(result) -> str:
    lines = []
    for res in result or []:
        if res is None:
            continue
        if not hasattr(res, "json"):
            log.debug("Unexpected OCR result shape: %r", type(res))
            continue
        data = res.json.get("res", {})
        texts = data.get("rec_texts", [])
        scores = data.get("rec_scores", [])
        if not texts:
            log.debug(
                "OCR result missing rec_texts; keys present: %s", list(data.keys())
            )
        for text, score in zip(texts, scores):
            if score >= config.OCR_CONFIDENCE:
                lines.append(text)
    return " ".join(lines)


_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=30)


async def _fetch_image(url: str) -> bytes | None:
    try:
        async with _http.get(url, timeout=_FETCH_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            try:
                content_length = int(resp.headers.get("Content-Length", 0) or 0)
            except (ValueError, TypeError):
                content_length = 0
            if content_length > MAX_IMAGE_BYTES:
                log.warning(
                    "Skipping image: Content-Length %d exceeds 15 MB limit",
                    content_length,
                )
                return None
            data = await resp.read()
            if len(data) > MAX_IMAGE_BYTES:
                log.warning(
                    "Skipping image: downloaded %d bytes exceeds 15 MB limit", len(data)
                )
                return None
            return data
    except asyncio.TimeoutError:
        log.warning("Timed out fetching image: %s", url)
        return None
    except aiohttp.ClientError as exc:
        log.warning("Network error fetching image %s: %s", url, exc)
        return None


async def _run_ocr(image_bytes: bytes) -> str:
    loop = asyncio.get_running_loop()

    def _ocr():
        try:
            img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            max_side = max(img.width, img.height)
            if max_side > 1200:
                scale = 1200 / max_side
                img = img.resize(
                    (round(img.width * scale), round(img.height * scale)),
                    Image.LANCZOS,
                )
            result = ocr.predict(np.array(img))
            return _parse_predict_result(result)
        except Exception as exc:
            log.warning("OCR processing failed: %s", exc)
            return ""

    async with _ocr_sem:
        return await loop.run_in_executor(None, _ocr)


async def _process_attachment(attachment: discord.Attachment) -> str:
    image_bytes = await _fetch_image(attachment.url)
    if image_bytes is None:
        return ""
    return await _run_ocr(image_bytes)


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if (
        config.MONITORED_CHANNEL_IDS
        and message.channel.id not in config.MONITORED_CHANNEL_IDS
    ):
        return

    image_attachments = [
        a
        for a in message.attachments
        if a.content_type and a.content_type.startswith("image/")
    ]

    if not image_attachments:
        return

    blocked, prior_refs = _consume_rate_limit(
        message.author.id, len(image_attachments), message.channel.id, message.id
    )
    if blocked:
        log.info(
            "Rate-limited %s (%d): attempted %d image(s) within window",
            message.author,
            message.author.id,
            len(image_attachments),
        )
        # Delete the triggering message and all prior uncleared messages in the window
        delete_targets = [(message.channel.id, message.id)] + prior_refs
        for ch_id, msg_id in delete_targets:
            channel = client.get_channel(ch_id)
            if channel is None:
                continue
            try:
                prior_msg = await channel.fetch_message(msg_id)
                await prior_msg.delete()
            except discord.NotFound:
                pass
            except (discord.Forbidden, discord.HTTPException) as e:
                log.warning(
                    "Could not delete message %d during rate-limit cleanup: %s",
                    msg_id,
                    e,
                )
        try:
            await message.channel.send(
                f"{message.author.mention} You're uploading images too quickly, please slow down.",
                delete_after=10,
            )
        except discord.HTTPException as e:
            log.warning(
                "Could not send rate-limit notice in %s: %s", message.channel, e
            )
        return

    # Process all attachments concurrently; OCR concurrency is capped by _ocr_sem
    results = await asyncio.gather(
        *(_process_attachment(a) for a in image_attachments),
        return_exceptions=True,
    )
    results = [r if isinstance(r, str) else "" for r in results]

    all_hits: list[str] = []
    all_text_parts: list[str] = []

    for raw_text in results:
        if not raw_text:
            continue
        extracted = sanitize_ocr(raw_text)
        print(f"[OCR] #{message.channel} | {message.author}: {extracted}")
        all_text_parts.append(extracted)
        flagged, hits = is_scam(extracted)
        if flagged:
            all_hits.extend(hits)

    if not all_hits:
        return

    try:
        await message.delete()
    except discord.Forbidden:
        log.warning("Missing permissions to delete message %d", message.id)
        return
    except discord.HTTPException as e:
        log.warning("Failed to delete message %d: %s", message.id, e)

    exempt = _timeout_exempt(message.author)

    if not exempt:
        try:
            await message.author.timeout(
                timedelta(minutes=config.TIMEOUT_MINUTES),
                reason=f"Scam image detected. Matched: {', '.join(all_hits[:3])}",
            )
        except discord.Forbidden:
            log.warning("Missing permissions to timeout %s", message.author)

        try:
            await message.channel.send(
                f"{message.author.mention} Your message was removed as it appeared to contain scam content. Contact a moderator if this is incorrect.",
                delete_after=10,
            )
        except discord.HTTPException:
            pass
    else:
        log.info(
            "Scam image from exempt user %s. Deleted silently, no timeout",
            message.author,
        )

    if config.LOG_CHANNEL_ID:
        log_channel = client.get_channel(config.LOG_CHANNEL_ID)
        if log_channel:
            unique_hits = list(dict.fromkeys(all_hits))
            combined_text = " | ".join(all_text_parts)
            try:
                await log_channel.send(
                    f"**Scam Image Detected**\n"
                    f"User: {message.author} (`{message.author.id}`)\n"
                    f"Channel: {message.channel.mention}\n"
                    f"Matched: `{', '.join(unique_hits)}`\n"
                    f"OCR: ```{combined_text[:500]}```"
                )
            except discord.HTTPException as exc:
                log.warning(
                    "Failed to send to log channel %d: %s", config.LOG_CHANNEL_ID, exc
                )


async def main():
    global _http, _ocr_sem
    _ocr_sem = asyncio.Semaphore(config.OCR_MAX_CONCURRENT)
    async with aiohttp.ClientSession() as session:
        _http = session
        async with client:
            await client.start(config.TOKEN)


asyncio.run(main())
