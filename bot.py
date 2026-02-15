#!/usr/bin/env python3
"""
Telegram YouTube Downloader Bot
--------------------------------
- Uses yt-dlp to download videos/audio from YouTube.
- Authenticates via cookies.txt (required to bypass bot checks).
- Falls back to audio if video exceeds Telegram's 50 MB limit.
- Shows download progress (speed, ETA, percent).
- Retries once on failure.
- Auto‑deletes temporary files.
- Fully async and memory‑safe.
- Admin command /updatecookies to upload a new cookies.txt file.
- Optional HTTP health check server for Render (listens on $PORT).
"""

import os
import re
import asyncio
import logging
import tempfile
import signal
from pathlib import Path

import yt_dlp
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ---------- Configuration ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set")

# Admin user IDs (comma-separated, e.g., "123456,789012")
ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_USER_ID", "").split(",") if x.strip()]

# Optional HTTP server for Render health checks
PORT = os.environ.get("PORT")

COOKIES_FILE = "cookies.txt"          # Must be in the same directory
MAX_FILE_SIZE = 50 * 1024 * 1024       # 50 MB Telegram limit
YT_DLP_OPTIONS = {
    "quiet": True,
    "no_warnings": True,
    "cookiefile": COOKIES_FILE,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "extractor_args": {"youtube": {"player_client": ["android", "web"]}}
}

# Setup logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------- Helper functions ----------
def extract_url(text: str) -> str | None:
    """Extract the first YouTube URL from a message."""
    pattern = r'(https?://)?(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/\S+'
    match = re.search(pattern, text)
    return match.group(0) if match else None

def progress_hook(queue, loop):
    """Return a hook function that puts progress into an asyncio.Queue."""
    def hook(d):
        if d['status'] == 'downloading':
            asyncio.run_coroutine_threadsafe(queue.put(d), loop)
    return hook

async def progress_updater(queue, message):
    """
    Coroutine that reads progress from the queue and edits the Telegram message.
    Updates every 2 seconds to avoid hitting rate limits.
    """
    last_update = 0
    while True:
        try:
            progress = await asyncio.wait_for(queue.get(), timeout=2.0)
            if progress['status'] != 'downloading':
                continue

            percent = progress.get('_percent_str', 'N/A').strip()
            speed = progress.get('_speed_str', 'N/A').strip()
            eta = progress.get('_eta_str', 'N/A').strip()
            text = f"⬇️ Downloading…\n{percent} at {speed}\nETA: {eta}"

            now = asyncio.get_event_loop().time()
            if now - last_update > 2.0:
                await message.edit_text(text)
                last_update = now

        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Progress updater error: {e}")
            break

async def download_and_upload(url: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Core logic:
    - Extract video info
    - Download with progress
    - If video > 50MB, fall back to audio
    - Upload to Telegram
    """
    chat_id = update.effective_chat.id
    status_msg = await context.bot.send_message(chat_id, "🔍 Fetching video information...")

    # Create a temporary directory for this download
    with tempfile.TemporaryDirectory() as tmpdir:
        # ---------- Step 1: Get video info ----------
        ydl_opts = YT_DLP_OPTIONS.copy()
        ydl_opts.update({
            "format": "best",           # Just to get info
            "skip_download": True,
        })
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = await asyncio.to_thread(ydl.extract_info, url, download=False)
                if info is None:
                    await status_msg.edit_text("❌ Could not retrieve video info.")
                    return
                title = info.get('title', 'Unknown')
        except Exception as e:
            logger.exception("Info extraction failed")
            await status_msg.edit_text(f"❌ Failed to get video info: {str(e)[:200]}")
            return

        # ---------- Step 2: Decide format ----------
        video_downloaded = False
        audio_downloaded = False
        file_path = None

        # ----- Try video download -----
        await status_msg.edit_text(f"🎬 Downloading video: {title}\nThis may take a while...")

        queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        ydl_opts = YT_DLP_OPTIONS.copy()
        ydl_opts.update({
            "format": "best[ext=mp4]/best",   # Prefer mp4 for Telegram
            "outtmpl": os.path.join(tmpdir, "%(title)s.%(ext)s"),
            "progress_hooks": [progress_hook(queue, loop)],
        })

        progress_task = asyncio.create_task(progress_updater(queue, status_msg))

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                await asyncio.to_thread(ydl.download, [url])

            files = list(Path(tmpdir).glob("*"))
            if not files:
                raise Exception("No file downloaded")
            file_path = files[0]
            file_size = file_path.stat().st_size

            progress_task.cancel()
            try:
                await progress_task
            except asyncio.CancelledError:
                pass

            if file_size <= MAX_FILE_SIZE:
                video_downloaded = True
                await status_msg.edit_text("✅ Download complete. Uploading video...")
                with open(file_path, 'rb') as f:
                    await context.bot.send_video(
                        chat_id=chat_id,
                        video=f,
                        caption=title,
                        read_timeout=60,
                        write_timeout=60,
                        connect_timeout=60,
                        pool_timeout=60,
                    )
                await status_msg.delete()
                return
            else:
                await status_msg.edit_text("📹 Video exceeds 50MB. Falling back to audio...")
                file_path.unlink()
                video_downloaded = False

        except Exception as e:
            logger.exception("Video download failed")
            progress_task.cancel()
            try:
                await progress_task
            except asyncio.CancelledError:
                pass
            if file_path and file_path.exists():
                file_path.unlink()
            await status_msg.edit_text("⚠️ Video download failed, retrying with audio...")

        # ----- Fallback to audio -----
        if not video_downloaded:
            await status_msg.edit_text("🎵 Downloading audio...")
            queue = asyncio.Queue()
            progress_task = asyncio.create_task(progress_updater(queue, status_msg))

            ydl_opts = YT_DLP_OPTIONS.copy()
            ydl_opts.update({
                "format": "bestaudio/best",
                "outtmpl": os.path.join(tmpdir, "%(title)s.%(ext)s"),
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
                "progress_hooks": [progress_hook(queue, loop)],
            })

            retries = 1
            for attempt in range(retries + 1):
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        await asyncio.to_thread(ydl.download, [url])

                    files = list(Path(tmpdir).glob("*.mp3"))
                    if not files:
                        files = list(Path(tmpdir).glob("*"))
                    if not files:
                        raise Exception("No audio file downloaded")
                    file_path = files[0]

                    progress_task.cancel()
                    try:
                        await progress_task
                    except asyncio.CancelledError:
                        pass

                    await status_msg.edit_text("✅ Audio ready. Uploading...")
                    with open(file_path, 'rb') as f:
                        await context.bot.send_audio(
                            chat_id=chat_id,
                            audio=f,
                            caption=title,
                            title=title,
                            performer="YouTube",
                            read_timeout=60,
                            write_timeout=60,
                            connect_timeout=60,
                            pool_timeout=60,
                        )
                    await status_msg.delete()
                    return

                except Exception as e:
                    logger.exception(f"Audio download attempt {attempt+1} failed")
                    if attempt < retries:
                        await status_msg.edit_text(f"⚠️ Audio download failed, retrying... ({attempt+1}/{retries+1})")
                        if file_path and file_path.exists():
                            file_path.unlink()
                    else:
                        progress_task.cancel()
                        try:
                            await progress_task
                        except asyncio.CancelledError:
                            pass
                        await status_msg.edit_text(f"❌ Download failed after retries: {str(e)[:200]}")
                        return

# ---------- Update cookies command ----------
async def update_cookies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allow admin to upload a new cookies.txt file."""
    user = update.effective_user
    if not user or (ADMIN_IDS and user.id not in ADMIN_IDS):
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return

    if not update.message.document:
        await update.message.reply_text("Please attach a `cookies.txt` file.", parse_mode="Markdown")
        return

    doc = update.message.document
    if doc.file_size > 1024 * 1024:  # 1 MB limit
        await update.message.reply_text("❌ File too large (max 1 MB).")
        return

    file = await context.bot.get_file(doc.file_id)
    try:
        with tempfile.NamedTemporaryFile(mode='wb', suffix='.txt', delete=False) as tmp:
            await file.download_to_drive(tmp.name)
            tmp_path = tmp.name

        with open(tmp_path, 'r', encoding='utf-8') as f:
            first_line = f.readline().strip()
            if not first_line.startswith("# Netscape HTTP Cookie File"):
                raise ValueError("Invalid cookies file format (first line must be '# Netscape HTTP Cookie File')")

        dest = Path(COOKIES_FILE)
        dest.unlink(missing_ok=True)
        Path(tmp_path).rename(dest)

        await update.message.reply_text("✅ Cookies file updated successfully.")
        logger.info(f"Cookies file updated by user {user.id}")
    except Exception as e:
        logger.exception("Cookie update failed")
        await update.message.reply_text(f"❌ Failed to update cookies: {str(e)[:200]}")
    finally:
        if 'tmp_path' in locals() and Path(tmp_path).exists():
            Path(tmp_path).unlink()

# ---------- Bot Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 Send me a YouTube link and I'll download it for you.\n"
        "I'll send the video if under 50MB, otherwise I'll send the audio."
    )
    user = update.effective_user
    if user and ADMIN_IDS and user.id in ADMIN_IDS:
        text += "\n\n🔧 *Admin command:* `/updatecookies` – upload a new `cookies.txt` file."
    await update.message.reply_text(text, parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    url = extract_url(update.message.text)
    if not url:
        await update.message.reply_text("❌ Please send a valid YouTube link.")
        return
    asyncio.create_task(download_and_upload(url, update, context))

# ---------- HTTP Server for Render ----------
async def run_http_server():
    """Run a simple HTTP server on $PORT (if set) to satisfy Render health checks."""
    if not PORT:
        return
    try:
        from aiohttp import web
    except ImportError:
        logger.error("aiohttp not installed. HTTP server cannot start.")
        return

    async def handle(request):
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get('/', handle)
    app.router.add_get('/health', handle)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(PORT))
    await site.start()
    logger.info(f"HTTP health check server running on port {PORT}")

# ---------- Main entry point (fixed event loop) ----------
async def main():
    if not Path(COOKIES_FILE).exists():
        logger.warning(f"Cookies file '{COOKIES_FILE}' not found. Authentication may fail.")

    # Build the application
    app = Application.builder().token(BOT_TOKEN).build()

    # Add handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("updatecookies", update_cookies))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Initialize and start the bot
    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    logger.info("Bot started. Press Ctrl+C to stop.")

    # Start HTTP server if PORT is set
    if PORT:
        asyncio.create_task(run_http_server())

    # Keep the bot running until interrupted
    stop_signal = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_event_loop().add_signal_handler(sig, stop_signal.set)

    await stop_signal.wait()  # Wait for shutdown signal

    # Graceful shutdown
    logger.info("Shutting down...")
    await app.updater.stop()
    await app.stop()
    await app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
