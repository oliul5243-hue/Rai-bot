#!/usr/bin/env python3
"""
Telegram Video Downloader Bot - Complete Single File
Supports: YouTube, Facebook, TikTok, Instagram, Twitter, Reddit, Vimeo + 1000+ sites
Features: Cover photo, all qualities, audio extract, progress bar, video preview
"""
import humanize
import os, re, asyncio, logging, time, uuid, hashlib, json
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List, Any, Tuple
from concurrent.futures import ThreadPoolExecutor

import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes,
)
from telegram.error import TelegramError

# =====================================================================
# 📌 তোমার বটের তথ্য এখানে বসাও
# =====================================================================

BOT_TOKEN = " "
API_ID = 28976457           # ⚠️ my.telegram.org থেকে নিয়ে বসাও
API_HASH = "d476a6a7053bea62308834d7091047c223ed9ecc"

# =====================================================================

# সেটিংস
DOWNLOAD_DIR = Path("downloads")
MAX_FILE_SIZE = 2000 * 1024 * 1024  # 2GB
COVER_PHOTO = "cover.jpg"

DOWNLOAD_DIR.mkdir(exist_ok=True)

# লগিং
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# থ্রেড পুল ও সেশন
thread_pool = ThreadPoolExecutor(max_workers=4)
user_sessions: Dict[int, Dict] = {}
download_progress: Dict[str, Dict] = {}

# =====================================================================
# ইউটিলিটি ফাংশন
# =====================================================================

def fmt_size(size):
    if not size: return "Unknown"
    for unit in ['B','KB','MB','GB']:
        if size < 1024: return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"

def fmt_duration(sec):
    if not sec: return "Unknown"
    sec = int(sec)
    h, m = divmod(sec, 3600); m, s = divmod(m, 60)
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"

def extract_url(text):
    m = re.search(r'(https?://[^\s<>"]+)', text)
    return m.group(0) if m else None

def safe_name(name, mx=80):
    s = re.sub(r'[<>:"/\\|?*]', '', name).strip()
    if len(s) > mx:
        n, e = os.path.splitext(s)
        s = n[:mx-len(e)] + e
    return s or "video"

def gen_id():
    return hashlib.md5(f"{uuid.uuid4()}{time.time()}".encode()).hexdigest()[:10]

# =====================================================================
# yt-dlp ফাংশন
# =====================================================================

def get_video_info(url):
    opts = {"quiet": True, "no_warnings": True, "ignoreerrors": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)

def get_video_formats(info):
    fmts = info.get("formats", [])
    res, seen = [], set()
    for f in fmts:
        v, h = f.get("vcodec","none"), f.get("height",0)
        if v != "none" and h >= 144 and h not in seen:
            sz = f.get("filesize") or f.get("filesize_approx",0)
            lbl = f"{h}p"
            if (f.get("fps") or 0) > 30: lbl += f" {f['fps']}fps"
            res.append({
                "id": f["format_id"], "height": h, "label": lbl,
                "ext": f.get("ext","mp4"), "size": sz,
                "size_str": fmt_size(sz),
                "has_audio": f.get("acodec","none") != "none",
            })
            seen.add(h)
    res.sort(key=lambda x: x["height"], reverse=True)
    return res

def get_audio_formats(info):
    fmts = info.get("formats", [])
    res, seen = [], set()
    for f in fmts:
        if f.get("vcodec") == "none" and f.get("acodec") != "none":
            abr = f.get("abr",0)
            if abr and abr not in seen:
                sz = f.get("filesize") or f.get("filesize_approx",0)
                res.append({
                    "id": f["format_id"], "abr": round(abr),
                    "label": f"{round(abr)}kbps", "ext": f.get("ext","m4a"),
                    "size": sz, "size_str": fmt_size(sz),
                })
                seen.add(abr)
    res.sort(key=lambda x: x["abr"], reverse=True)
    return res

def download_video(dl_id, url, fmt, out_dir):
    def hook(d):
        if d["status"] == "downloading":
            tot = d.get("total_bytes") or d.get("total_bytes_estimate",0)
            dld = d.get("downloaded_bytes",0)
            download_progress[dl_id] = {
                "percent": round((dld/tot)*100,1) if tot else 0,
                "downloaded": dld, "total": tot,
                "speed": d.get("speed",0), "eta": d.get("eta",0),
                "status": "downloading",
            }
        elif d["status"] == "finished":
            download_progress[dl_id] = {"status": "processing", "percent": 100}
    opts = {
        "format": fmt, "quiet": True, "no_warnings": True,
        "outtmpl": str(out_dir / "%(id)s.%(ext)s"),
        "progress_hooks": [hook],
        "merge_output_format": "mp4", "restrictfilenames": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            fp = info.get("filepath","")
            if fp and os.path.exists(fp): return (True, fp, "")
            for item in out_dir.iterdir():
                if item.is_file() and item.stat().st_size > 0 and (time.time() - item.stat().st_mtime) < 300:
                    return (True, str(item), "")
            return (False, "", "File not found")
    except Exception as e:
        return (False, "", str(e))

# =====================================================================
# টেলিগ্রাম হ্যান্ডলার
# =====================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if os.path.exists(COVER_PHOTO):
        with open(COVER_PHOTO, "rb") as f:
            await update.message.reply_photo(
                photo=InputFile(f, filename="cover.jpg"),
                caption=f"👋 **Welcome, {user.first_name}!**\n\n"
                        f"📥 **Send any video link** and I'll show all download options!\n\n"
                        f"🔹 Supported: YouTube · Facebook · TikTok · Instagram · Twitter · Reddit\n"
                        f"🔹 All qualities: 144p → 8K\n"
                        f"🔹 Audio extraction\n\n"
                        f"**➡️ Just paste your link below!**",
                parse_mode="Markdown",
            )
    else:
        await update.message.reply_text(
            f"🎬 **Video Downloader Bot**\n\n👋 Welcome {user.first_name}!\n\n📥 Send any video link to start.\n\nSupported: YouTube, Facebook, TikTok, Instagram, Twitter & 1000+ sites.",
            parse_mode="Markdown",
        )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 **How to Use:**\n\n1️⃣ Send a video link\n2️⃣ See preview with quality options\n3️⃣ Tap a quality button\n4️⃣ Video sent to you!\n\n**Commands:**\n/start - Restart\n/help - Help\n/cancel - Cancel\n\n**Note:** Max 2GB per file",
        parse_mode="Markdown",
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid in user_sessions: del user_sessions[uid]
    await update.message.reply_text("✅ Cancelled.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    url = extract_url(update.message.text.strip())
    if not url:
        await update.message.reply_text("⚠️ **Send a valid video link**\nExample: `https://youtube.com/watch?v=...`", parse_mode="Markdown")
        return

    msg = await update.message.reply_text("⏳ **Fetching video info...**", parse_mode="Markdown")

    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(thread_pool, get_video_info, url)
        if not info: raise ValueError("Could not get video info")

        title = info.get("title","Untitled")
        uploader = info.get("uploader") or info.get("channel","Unknown")
        dur = info.get("duration")
        views = info.get("view_count")
        dur_str = fmt_duration(dur)

        vf = get_video_formats(info)
        af = get_audio_formats(info)

        user_sessions[uid] = {"url": url, "title": title, "info": info, "vf": vf, "af": af}

        keyboard = []
        row = []
        for v in vf:
            btn = v["label"]
            if v["size_str"] != "Unknown": btn += f" [{v['size_str']}]"
            if v["has_audio"]: btn += " 🎵"
            row.append(InlineKeyboardButton(btn, callback_data=f"v:{v['id']}"))
            if len(row) == 2: keyboard.append(row); row = []
        if row: keyboard.append(row)

        if af: keyboard.append([InlineKeyboardButton(f"🎵 Audio ({len(af)} options)", callback_data="show_audio")])
        keyboard.append([InlineKeyboardButton("⚡ Best Quality", callback_data="dl_best")])

        await msg.edit_text(
            f"🎬 **{title}**\n👤 {uploader} | ⏱ {dur_str} | 👁 {humanize.intword(views) if views else 'N/A'} views\n\n📥 **Select quality below:**",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        await msg.edit_text(f"❌ **Error:** `{str(e)[:250]}`\n\nTry another link.", parse_mode="Markdown")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data

    if uid not in user_sessions:
        await q.edit_message_text("⏰ Session expired. Send link again.")
        return

    s = user_sessions[uid]

    # Audio menu
    if data == "show_audio":
        af = s.get("af", [])
        if not af:
            await q.edit_message_text("No audio formats available.")
            return
        kb = [[InlineKeyboardButton(f"🎵 {a['label']} ({a['ext']}) [{a['size_str']}]" if a['size_str']!='Unknown' else f"🎵 {a['label']} ({a['ext']})", callback_data=f"a:{a['id']}")] for a in af]
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="back_main")])
        await q.edit_message_text("🎵 **Select Audio Quality:**", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
        return

    # Back to main
    if data == "back_main":
        vf, af = s["vf"], s["af"]
        keyboard = []
        row = []
        for v in vf:
            btn = v["label"]
            if v["size_str"] != "Unknown": btn += f" [{v['size_str']}]"
            if v["has_audio"]: btn += " 🎵"
            row.append(InlineKeyboardButton(btn, callback_data=f"v:{v['id']}"))
            if len(row) == 2: keyboard.append(row); row = []
        if row: keyboard.append(row)
        if af: keyboard.append([InlineKeyboardButton(f"🎵 Audio ({len(af)} options)", callback_data="show_audio")])
        keyboard.append([InlineKeyboardButton("⚡ Best Quality", callback_data="dl_best")])
        await q.edit_message_text(
            f"🎬 **{s['title']}**\n\n📥 **Select quality below:**",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # Determine format
    if data == "dl_best":
        fmt, mtype = "bestvideo+bestaudio/best", "best"
    elif data.startswith("v:"):
        fmt, mtype = data.split(":",1)[1], "video"
    elif data.startswith("a:"):
        fmt, mtype = data.split(":",1)[1], "audio"
    else:
        return

    dl_id = gen_id()
    await q.edit_message_text(
        f"⏳ **Downloading...**\n🎬 {s['title'][:40]}...\n📥 Format: `{fmt[:25]}`\n▰▱▱▱▱▱▱▱▱▱ 0%",
        parse_mode="Markdown",
    )

    try:
        loop = asyncio.get_event_loop()
        task = asyncio.create_task(progress_updater(q.message.chat_id, q.message.message_id, dl_id, context))
        success, fp, err = await loop.run_in_executor(thread_pool, download_video, dl_id, s["url"], fmt, DOWNLOAD_DIR)
        task.cancel()

        if not success:
            await q.edit_message_text(f"❌ Download failed: `{err[:200]}`", parse_mode="Markdown")
            if dl_id in download_progress: del download_progress[dl_id]
            return

        sz = os.path.getsize(fp)
        if sz > MAX_FILE_SIZE:
            os.remove(fp)
            await q.edit_message_text(f"⚠️ File too large ({fmt_size(sz)}). Try lower quality.", parse_mode="Markdown")
            if dl_id in download_progress: del download_progress[dl_id]
            return

        await q.edit_message_text(f"📤 **Uploading to Telegram...**\n📦 {fmt_size(sz)}", parse_mode="Markdown")

        if mtype == "audio":
            with open(fp, "rb") as f:
                await context.bot.send_audio(
                    chat_id=q.message.chat_id,
                    audio=InputFile(f, filename=f"{safe_name(s['title'])}.mp3"),
                    title=s['title'][:60],
                    performer=s['info'].get('uploader',''),
                    duration=s['info'].get('duration'),
                    caption=f"🎵 **{s['title'][:100]}**",
                    parse_mode="Markdown",
                    read_timeout=300, write_timeout=300,
                )
        else:
            ext = os.path.splitext(fp)[1] or ".mp4"
            with open(fp, "rb") as f:
                await context.bot.send_video(
                    chat_id=q.message.chat_id,
                    video=InputFile(f, filename=f"{safe_name(s['title'])}{ext}"),
                    caption=f"🎬 **{s['title'][:100]}**\n📦 `{fmt_size(sz)}`",
                    parse_mode="Markdown",
                    supports_streaming=True,
                    read_timeout=600, write_timeout=600,
                )

        await context.bot.delete_message(chat_id=q.message.chat_id, message_id=q.message.message_id)
        os.remove(fp)
        if dl_id in download_progress: del download_progress[dl_id]

    except Exception as e:
        logger.error(f"Download error: {e}", exc_info=True)
        try: await q.edit_message_text(f"❌ Error: `{str(e)[:200]}`", parse_mode="Markdown")
        except: pass
        if dl_id in download_progress: del download_progress[dl_id]

async def progress_updater(chat_id, msg_id, dl_id, context):
    last = -1
    while True:
        await asyncio.sleep(2)
        p = download_progress.get(dl_id)
        if not p: continue
        if p.get("status") == "processing":
            try: await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text="🔄 **Processing...**\nMerging video & audio...", parse_mode="Markdown")
            except: pass
            break
        if p.get("status") == "cancelled": break
        pct = p.get("percent",0)
        if abs(pct - last) < 3: continue
        last = pct
        bar = "▰"*int(10*pct/100) + "▱"*(10-int(10*pct/100))
        dld = fmt_size(p.get("downloaded",0)); tot = fmt_size(p.get("total",0))
        spd = f"{fmt_size(p.get('speed',0))}/s" if p.get('speed') else "..."
        eta = fmt_duration(p.get('eta',0)) if p.get('eta') else "..."
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=msg_id,
                text=f"⏳ **Downloading...**\n`{bar}` **{pct}%**\n📦 `{dld}` / `{tot}`\n⚡ {spd} | ⏱ ETA: {eta}",
                parse_mode="Markdown")
        except: pass

async def error_handler(update, context):
    logger.error(f"Update {update} caused error {context.error}")

# =====================================================================
# মেইন ফাংশন
# =====================================================================

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_error_handler(error_handler)

    print("🤖 Bot is running! Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()