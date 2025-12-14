#!/usr/bin/env python3
import os
import asyncio
import re
from pathlib import Path
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import yt_dlp

# ---------------- Config ----------------
API_ID = int(os.environ.get("API_ID") or 0)
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")

if not (API_ID and API_HASH and BOT_TOKEN):
    raise SystemExit("Set API_ID, API_HASH, and BOT_TOKEN in environment")

BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
SESSION_DIR = BASE_DIR / "session_data"
DOWNLOADS_DIR.mkdir(exist_ok=True)
SESSION_DIR.mkdir(exist_ok=True)

app = Client(
    "youtube_bot_v5",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=str(SESSION_DIR)
)

QUALITY_OPTIONS = ["360", "480", "720"]
QUALITY_EMOJIS = {"360": "🎥", "480": "📺", "720": "💎"}

# ---------------- Temporary storage ----------------
user_url = {}        # store URLs per user
user_tasks = {}      # store active download tasks per user

# ---------------- Helpers ----------------
def create_progress_bar(percent, width=20):
    percent = max(0.0, min(100.0, percent))
    filled = round(width * (percent / 100))
    empty = width - filled
    bar = "█" * filled + "░" * empty
    return f"[{bar}] {percent:.1f}%"

def format_bytes(num):
    if not num:
        return "0 B"
    for unit in ["B","KB","MB","GB","TB"]:
        if num < 1024.0:
            return f"{num:.2f} {unit}"
        num /= 1024.0
    return f"{num:.2f} PB"

def format_duration(seconds):
    try:
        seconds = int(seconds)
    except:
        return "0:00"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h}:{m:02}:{s:02}"
    return f"{m}:{s:02}"

def clean_filename(s: str) -> str:
    return re.sub(r'[\\/:"*?<>|]+', "_", s)

async def run_subprocess(cmd, on_line=None, timeout=6000):
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    assert proc.stdout is not None
    while True:
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("Subprocess timed out (6000 s)")
        if not line:
            break
        txt = line.decode(errors="ignore").rstrip()
        if on_line:
            try:
                on_line(txt)
            except Exception:
                pass
    return await proc.wait()

# ---------------- Bot Commands ----------------
@app.on_message(filters.command("start"))
async def start(_, message):
    await message.reply_text(
        "🎬 **YouTube Video Downloader v5**\n\n"
        "Send any YouTube link to download.\n"
        "Supported resolutions: 360p / 480p / 720p\n"
        "✅ Audio fixed | ✅ Color fixed | ⏱ 6000 s timeout\n"
        "ℹ️ Use /cancel to cancel an ongoing download."
    )

@app.on_message(filters.command("cancel"))
async def cancel_download(_, message):
    task = user_tasks.get(message.from_user.id)
    if task and not task.done():
        task.cancel()
        await message.reply_text("❌ Download cancelled successfully.")
    else:
        await message.reply_text("ℹ️ No active download to cancel.")

# ---------- URL handler ----------
@app.on_message(filters.text & ~filters.command(["start", "cancel"]))
async def handle_url(_, message):
    url = message.text.strip()
    if not ("youtube.com" in url or "youtu.be" in url):
        await message.reply_text("❌ Please send a valid YouTube URL.")
        return

    user_url[message.from_user.id] = url
    buttons = [InlineKeyboardButton(f"{QUALITY_EMOJIS[q]} {q}p", callback_data=q) for q in QUALITY_OPTIONS]
    await message.reply_text(
        "Select your preferred quality:",
        reply_markup=InlineKeyboardMarkup([buttons])  # horizontal buttons
    )

# ---------------- Callback Query ----------------
@app.on_callback_query()
async def download_video(_, query):
    await query.answer()
    user_id = query.from_user.id
    url = user_url.get(user_id)
    if not url:
        await query.edit_message_text("❌ URL not found. Please send the link again.")
        return

    try:
        quality = int(query.data)
    except:
        await query.edit_message_text("❌ Invalid selection.")
        return

    header = f"🎬 YouTube Downloader\n✅ Selected: {quality}p\n"
    msg = await query.edit_message_text(header + "⏳ Extracting video info...")

    # ---------------- Extract info ----------------
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        await msg.edit_text(header + f"❌ Failed to extract video info.\n{e}")
        return

    title_raw = info.get("title", "video")
    duration = info.get("duration", 0)
    clean_title = clean_filename(title_raw)
    final_path = DOWNLOADS_DIR / f"{clean_title}.mp4"
    temp_path = DOWNLOADS_DIR / f"{clean_title}_temp.mp4"

    # ---------------- Integrated Final Design ----------------
    caption = (
        f"╔═════════════════════\n\n"
        f" 🎬 {title_raw}\n\n"
        f"║ ⏱ Duration: {format_duration(duration)}\n"
        f"║ 📦 Size: {format_bytes(0)}\n"
        f"║ ✅ Quality: {quality}p\n"
        f"╚═════════════════════\n\n"
        f"━━━━━✦ DEVU ❣️✦━━━━━"
    )
    await msg.edit_text(caption + "\n\n⏳ Downloading...")

    # ---------------- Integrated Final Design ----------------
    ytdlp_cmd = [
        "yt-dlp",
        "-f", f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]",
        "-o", str(temp_path),
        "--no-warnings",
        "--newline",
        "--no-playlist",
        "--merge-output-format", "mp4",
        "--external-downloader", "aria2c",
        "--external-downloader-args", "-x 4 -k 1M",
        url,
    ]

    progress = {"pct": -1}
    def parse_progress(line):
        # Robust regex for yt-dlp + aria2c output
        m = re.search(r"(\d{1,3}\.\d+)%.*?ETA\s+([0-9:]+)", line)
        speed_match = re.search(r"at\s+([0-9\.]+[KMG]?B/s)", line)
        if m:
            pct = int(float(m.group(1)))
            speed = speed_match.group(1) if speed_match else "?"
            eta = m.group(2)
            if pct % 5 == 0 and pct != progress["pct"]:
                progress["pct"] = pct
                asyncio.create_task(
                    msg.edit_text(
                        f"╔═════════════════════\n\n"
                        f" 🎬 {title_raw}\n\n"
                        f"║ 📦 {create_progress_bar(pct)}\n"
                        f"║ ⏱ {speed} ETA {eta}\n"
                        f"╚═════════════════════\n\n"
                        f"━━━━━✦ DEVU ❣️✦━━━━━"
                    )
                )

    # ---------------- Run download task with cancellation support ----------------
    async def run_download():
        try:
            ret = await run_subprocess(ytdlp_cmd, parse_progress)
            if ret != 0:
                await msg.edit_text("❌ Download failed.")
                return
        except asyncio.CancelledError:
            await msg.edit_message_text("❌ Download cancelled by user.")
            try:
                if temp_path.exists():
                    os.remove(temp_path)
            except:
                pass
            return

        # ---------------- Fix color + merge ----------------
        await msg.edit_text("🎨 Fixing video color & merging safely...")
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-i", str(temp_path),
            "-c:v", "libx264",
            "-crf", "23",
            "-preset", "fast",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
            str(final_path)
        ]
        proc = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=6000)
        except asyncio.TimeoutError:
            proc.kill()
            await msg.edit_text("❌ FFmpeg merge timed out.")
            return
        if proc.returncode != 0:
            await msg.edit_text("❌ FFmpeg merge failed.")
            return

        # ---------------- Extract thumbnail ----------------
        thumb_path = DOWNLOADS_DIR / f"{clean_title}_thumb.jpg"
        ffmpeg_thumb_cmd = [
            "ffmpeg", "-y",
            "-i", str(final_path),
            "-ss", "00:00:01",
            "-vframes", "1",
            str(thumb_path)
        ]
        proc_thumb = await asyncio.create_subprocess_exec(
            *ffmpeg_thumb_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc_thumb.communicate()
        if not thumb_path.exists():
            thumb_path = None

        size = os.path.getsize(final_path) if os.path.exists(final_path) else 0
        caption_final = (
            f"╔═════════════════════\n\n"
            f" 🎬 {title_raw}\n\n"
            f"║ ⏱ Duration: {format_duration(duration)}\n"
            f"║ 📦 Size: {format_bytes(size)}\n"
            f"║ ✅ Quality: {quality}p\n"
            f"╚═════════════════════\n\n"
            f"━━━━━✦ DEVU ❣️✦━━━━━"
        )

        # ---------------- Upload with progress ----------------
        last_update = {"pct": -1}
        async def progress_upload(current, total):
            if not total:
                return
            pct = int((current / total) * 100)
            if pct % 5 == 0 and pct != last_update["pct"]:
                last_update["pct"] = pct
                try:
                    bar = create_progress_bar(pct)
                    await msg.edit_text(
                        f"╔═════════════════════\n\n"
                        f" 🎬 Uploading\n\n"
                        f"║ {bar}\n"
                        f"║ {format_bytes(current)}/{format_bytes(total)}\n"
                        f"╚═════════════════════\n\n"
                        f"━━━━━✦ DEVU ❣️✦━━━━━"
                    )
                except Exception as e:
                    if "MESSAGE_NOT_MODIFIED" not in str(e):
                        print("Progress update error:", e)

        for attempt in range(3):
            try:
                await _.send_video(
                    query.message.chat.id,
                    video=str(final_path),
                    caption=caption_final,
                    supports_streaming=True,
                    thumb=str(thumb_path) if thumb_path else None,
                    progress=progress_upload,
                    progress_args=(),
                    duration=int(duration)
                )
                break
            except Exception as e:
                print(f"Upload attempt {attempt+1} failed:", e)
                await asyncio.sleep(5)
        else:
            await msg.edit_text("❌ Upload failed after multiple retries.")
            return

        await msg.edit_text("✅ Uploaded successfully. Cleaning up...")
        for f in (temp_path, final_path, thumb_path):
            try:
                if f:
                    os.remove(f)
            except:
                pass
        await msg.edit_text("✅ Done. File deleted locally.")

    # ------
    # ---------- Schedule download task ----------------
    task = asyncio.create_task(run_download())
    user_tasks[user_id] = task

# ---------------- Run Bot ----------------
if __name__ == "__main__":
    print("Bot v5 starting... (Ultra-Stable 6000 s Timeout with aria2c + Thumbnail + Cancel + Mosaic Fix)")
    app.run()

