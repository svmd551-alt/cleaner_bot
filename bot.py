import os
import re
import asyncio
import shutil
import tempfile
from io import BytesIO

# Python 3.12+/3.14 me pyrogram import hote hi event loop maangta hai - pehle se bana do
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import aiohttp
from aiohttp import web
from PIL import Image
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.types import Message


# ---------- CONFIG (environment variables) ----------
def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


API_ID = int(required_env("API_ID"))
API_HASH = required_env("API_HASH")
BOT_TOKEN = required_env("BOT_TOKEN")
ADMINS = [int(x) for x in os.environ.get("ADMINS", "").replace(",", " ").split()]
SOURCE = int(required_env("SOURCE_CHANNEL"))  # source/database channel ID
TARGET = int(required_env("TARGET_CHANNEL"))  # destination channel ID
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")   # free key from themoviedb.org (for posters)
AUTO_FORWARD = os.environ.get("AUTO_FORWARD", "true").lower() == "true"
# Default: file jaisi hai waisi forward (re-upload nahi), bas caption se @username aur telegram links hat jate hain
CLEAN_CAPTION = os.environ.get("CLEAN_CAPTION", "true").lower() == "true"     # caption saaf karo
CLEAN_FILENAME = os.environ.get("CLEAN_FILENAME", "false").lower() == "true"  # file ka naam bhi saaf karo (re-upload lagega)
SET_POSTER = os.environ.get("SET_POSTER", "false").lower() == "true"          # video pe TMDB poster thumbnail (re-upload lagega)
BLOCK_WORDS = [w.strip() for w in os.environ.get("BLOCK_WORDS", "").split(",") if w.strip()]  # e.g. ClipmateMovies
PORT = int(os.environ.get("PORT", "8080"))    # Render health-check port

app = Client("cleaner_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)


# ---------- CLEANING ----------
LINK_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/\S+|tg://\S+", re.I)
USER_RE = re.compile(r"@[A-Za-z0-9_]+")
BLOCK_RE = re.compile("@?(?:" + "|".join(map(re.escape, BLOCK_WORDS)) + ")", re.I) if BLOCK_WORDS else None


def clean_text(text: str) -> str:
    text = LINK_RE.sub("", text)
    if BLOCK_RE:
        text = BLOCK_RE.sub("", text)
    text = USER_RE.sub("", text)
    lines = [re.sub(r"[ \t]{2,}", " ", l).strip() for l in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def clean_filename(name: str) -> str:
    base, ext = os.path.splitext(name)
    base = LINK_RE.sub("", base)
    if BLOCK_RE:
        base = BLOCK_RE.sub("", base)
    base = USER_RE.sub("", base)
    base = re.sub(r"[\[\(\{]\s*[\]\)\}]", "", base)       # empty [] () {}
    base = re.sub(r"\s{2,}", " ", base)
    base = re.sub(r"([._\-])\1+", r"\1", base)            # .. -- __ -> single
    base = base.strip(" ._-[](){}")
    return (base or "file") + ext


# ---------- POSTER (TMDB) ----------
JUNK_RE = re.compile(
    r"\b(480p|576p|720p|1080p|2160p|4k|hdrip|webrip|web-dl|webdl|web|bluray|brrip|bdrip|dvdrip|"
    r"hdtv|hdcam|camrip|hdts|x264|x265|hevc|h264|h265|10bit|aac|ddp?5?1|atmos|esubs?|hindi|"
    r"english|tamil|telugu|malayalam|kannada|punjabi|bengali|dual|audio|multi|org|proper|repack|"
    r"complete|season|s\d{1,2}e\d{1,3}|s\d{1,2}|e\d{1,3})\b.*",
    re.I,
)
SERIES_RE = re.compile(r"\bs\d{1,2}\s?e\d{1,3}\b|\bseason\b", re.I)


def guess_title(filename: str):
    base = re.sub(r"[._]+", " ", os.path.splitext(filename)[0])
    base = LINK_RE.sub("", base)
    base = USER_RE.sub("", base)
    year = None
    m = re.search(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)", base)
    if m and m.start() > 0:
        year = m.group(1)
        base = base[:m.start()]
    base = re.sub(r"[\[\(\{][^\]\)\}]*[\]\)\}]", " ", base)   # drop [tags] (tags)
    base = JUNK_RE.sub("", base)
    base = re.sub(r"[^\w\s:'&-]", " ", base)
    base = re.sub(r"\s{2,}", " ", base).strip(" -:")
    return base, year


async def fetch_poster(filename: str, dest: str) -> bool:
    """Search TMDB by the file name and save the poster as a Telegram-ready JPEG thumb."""
    if not TMDB_API_KEY:
        return False
    title, year = guess_title(filename)
    if len(title) < 2:
        return False
    kinds = ("tv", "movie") if SERIES_RE.search(filename) else ("movie", "tv")
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            for kind in kinds:
                params = {"api_key": TMDB_API_KEY, "query": title}
                if year:
                    params["year" if kind == "movie" else "first_air_date_year"] = year
                async with s.get(f"https://api.themoviedb.org/3/search/{kind}", params=params) as r:
                    data = await r.json()
                for res in data.get("results", []):
                    if not res.get("poster_path"):
                        continue
                    async with s.get(f"https://image.tmdb.org/t/p/w342{res['poster_path']}") as img:
                        raw = await img.read()
                    im = Image.open(BytesIO(raw)).convert("RGB")
                    im.thumbnail((320, 320))
                    im.save(dest, "JPEG", quality=85)   # Telegram: JPEG, <200KB, max 320px
                    return True
    except Exception as e:
        print(f"poster fetch failed for '{title}': {e}")
    return False


# ---------- HELPERS ----------
class StopRequested(Exception):
    """/stop dabane par kaam rokne ke liye."""


async def safe(coro_fn, *args, **kwargs):
    while True:
        try:
            return await coro_fn(*args, **kwargs)
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)


async def safe_edit(message: Message, text: str):
    """Status message edit karo; fail ho to kaam na ruke."""
    try:
        await safe(message.edit_text, text)
    except MessageNotModified:
        pass
    except Exception as e:
        print(f"status edit failed: {e}")


async def resolve_channels() -> dict:
    """SOURCE aur TARGET ko resolve karo taaki Pyrogram peer cache me save kar le.
    Returns {name: error_or_None}."""
    results = {}
    for name, cid in (("SOURCE", SOURCE), ("TARGET", TARGET)):
        try:
            chat = await safe(app.get_chat, cid)
            print(f"{name} OK: {chat.title} ({chat.id})")
            results[name] = None
        except Exception as e:
            print(f"{name} FAILED ({cid}): {e}")
            results[name] = f"{type(e).__name__}: {e}"
    return results


# ---------- PROCESSING ----------
async def process(client: Client, msg: Message) -> bool:
    """Clean one message and post it to TARGET. Returns True if something was sent."""
    if msg.empty or msg.service:
        return False

    media = msg.document or msg.video or msg.audio
    is_video = bool(msg.video) or (bool(msg.document) and (msg.document.mime_type or "").startswith("video/"))
    wants_poster = SET_POSTER and is_video

    # DEFAULT: file download/upload nahi hoti (fast). Sirf caption saaf hota hai.
    if not CLEAN_FILENAME and not wants_poster:
        if msg.text:
            t = clean_text(msg.text) if CLEAN_CAPTION else msg.text
            if not t:
                return False
            await safe(client.send_message, TARGET, t, parse_mode=ParseMode.DISABLED)
        elif CLEAN_CAPTION:
            # plain-text caption => caption ke andar chhupe hue (hyperlink) t.me links bhi hat jate hain,
            # reply_markup=None => original ke buttons (jinme telegram link ho sakte hain) copy nahi hote
            await safe(client.copy_message, TARGET, SOURCE, msg.id,
                       caption=clean_text(msg.caption or ""),
                       parse_mode=ParseMode.DISABLED, reply_markup=None)
        else:
            await safe(client.copy_message, TARGET, SOURCE, msg.id)
        return True

    caption = clean_text(msg.caption or "") if CLEAN_CAPTION else (msg.caption or "")

    # plain text / photo
    if msg.text:
        t = clean_text(msg.text) if CLEAN_CAPTION else msg.text
        if t:
            await safe(client.send_message, TARGET, t, parse_mode=ParseMode.DISABLED)
            return True
        return False
    if msg.photo:
        await safe(client.send_photo, TARGET, msg.photo.file_id, caption=caption, parse_mode=ParseMode.DISABLED)
        return True

    if not media:
        # voice, sticker, animation etc. - seedha copy kar do
        await safe(client.copy_message, TARGET, SOURCE, msg.id,
                   caption=caption, parse_mode=ParseMode.DISABLED, reply_markup=None)
        return True

    old = media.file_name or "file"
    new = clean_filename(old) if CLEAN_FILENAME else old

    tmp = tempfile.mkdtemp()
    try:
        # poster only for videos (a thumbnail can be set only while uploading)
        poster = os.path.join(tmp, "poster.jpg")
        has_poster = wants_poster and await fetch_poster(new, poster)

        # name already clean and no new poster -> reuse file_id, no re-upload
        if new == old and not has_poster:
            await safe(client.send_cached_media, TARGET, media.file_id, caption=caption, parse_mode=ParseMode.DISABLED)
            return True

        path = await safe(client.download_media, msg, file_name=os.path.join(tmp, new))

        if has_poster:
            thumb = poster
        else:
            thumb = None
            thumbs = getattr(media, "thumbs", None)
            if thumbs:
                thumb = await safe(client.download_media, thumbs[0].file_id,
                                   file_name=os.path.join(tmp, "thumb.jpg"))

        if msg.video:
            await safe(client.send_video, TARGET, path, caption=caption, parse_mode=ParseMode.DISABLED, file_name=new,
                       duration=msg.video.duration, width=msg.video.width,
                       height=msg.video.height, thumb=thumb, supports_streaming=True)
        elif msg.audio:
            await safe(client.send_audio, TARGET, path, caption=caption, parse_mode=ParseMode.DISABLED, file_name=new,
                       duration=msg.audio.duration, performer=msg.audio.performer,
                       title=msg.audio.title, thumb=thumb)
        else:
            await safe(client.send_document, TARGET, path, caption=caption, parse_mode=ParseMode.DISABLED,
                       file_name=new, thumb=thumb)
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- COMMANDS ----------
running = {"stop": False, "busy": False}
# Fail closed: if ADMINS is empty, nobody can run history-processing commands.
admin_filter = filters.user(ADMINS) if ADMINS else filters.user([])

USAGE = (
    "Use:\n"
    "/sendall - source channel ki saari files\n"
    "/sendall <start_id> - kisi id se aage ki saari files\n"
    "/clean <start_id> <end_id> - ek range\n\n"
    "(Saari files ke liye bot ko source channel me admin + Post Messages permission chahiye)"
)


@app.on_message(filters.command("start") & filters.private)
async def start(_, m: Message):
    await m.reply_text(
        "Main source channel ki files ko target channel mai forward karta hoon.\n\n"
        "/sendall - source channel ki saari files target me bhejo\n"
        "/clean <start_id> <end_id> - sirf ek range bhejo\n"
        "/stop - chalte hue kaam ko roko"
    )


@app.on_message(filters.command(["clean", "sendall"]) & filters.private & admin_filter)
async def clean_cmd(client: Client, m: Message):
    if running["busy"]:
        return await m.reply_text("Ek kaam pehle se chal raha hai. /stop karo ya wait karo.")

    # 1) Channels access check (peer cache)
    res = await resolve_channels()
    bad = {k: v for k, v in res.items() if v}
    if bad:
        lines = "\n".join(f"{k}: {v}" for k, v in bad.items())
        return await m.reply_text(
            "Channel access nahi mil raha:\n"
            f"{lines}\n\n"
            "Check karo:\n"
            "1. SOURCE_CHANNEL / TARGET_CHANNEL ID sahi hai (-100 se shuru)\n"
            "2. Wahi bot (wahi BOT_TOKEN) dono channels me admin hai\n"
            "3. Source me 'Post Messages' permission di hai"
        )

    # 2) Args parse
    args = m.command[1:]
    try:
        if m.command[0] == "sendall" or (args and args[0].lower() == "all"):
            nums = [a for a in args if a.isdigit()]
            start_id = int(nums[0]) if nums else 1
            end_id = None  # neeche probe se nikalenge
        else:
            start_id, end_id = int(args[0]), int(args[1])
    except (ValueError, IndexError):
        return await m.reply_text(USAGE)

    # 3) Last message id nikalo (sendall ke liye)
    if end_id is None:
        try:
            probe = await safe(client.send_message, SOURCE, ".")
            end_id = probe.id - 1
            await safe(client.delete_messages, SOURCE, probe.id)
        except Exception as e:
            return await m.reply_text(
                "Source channel me test message nahi bhej paya.\n"
                "Bot ko source channel me admin + 'Post Messages' permission do.\n"
                f"Error: {type(e).__name__}: {e}"
            )

    if start_id < 1 or end_id < start_id:
        return await m.reply_text("Invalid message ID range. Start ID must be >= 1 and end ID must be >= start ID.")

    running.update(busy=True, stop=False)
    status = await m.reply_text("Shuru ho gaya...")
    sent = skipped = failed = 0
    last_id = start_id - 1

    try:
        for chunk_start in range(start_id, end_id + 1, 100):
            ids = list(range(chunk_start, min(chunk_start + 100, end_id + 1)))
            msgs = await safe(client.get_messages, SOURCE, ids)
            for msg in msgs:
                if running["stop"]:
                    raise StopRequested
                last_id = getattr(msg, "id", last_id)
                try:
                    if await process(client, msg):
                        sent += 1
                    else:
                        skipped += 1
                except Exception as e:
                    failed += 1
                    print(f"msg {getattr(msg, 'id', '?')} failed: {e}")
                await asyncio.sleep(1)  # flood se bachne ke liye
            await safe_edit(status, f"Chal raha hai... ID {ids[-1]}/{end_id}\n"
                                    f"Sent: {sent} | Skipped: {skipped} | Failed: {failed}")
        await safe_edit(status, f"Ho gaya ✅\nSent: {sent} | Skipped: {skipped} | Failed: {failed}")
    except StopRequested:
        await safe_edit(
            status,
            f"Roka gaya ⛔ (last ID: {last_id})\n"
            f"Sent: {sent} | Skipped: {skipped} | Failed: {failed}\n"
            f"Aage badhane ke liye: /sendall {last_id + 1}"
        )
    except Exception as e:
        print(f"clean_cmd crashed: {e}")
        await safe_edit(
            status,
            f"Error aa gaya ❌ (last ID: {last_id})\n{type(e).__name__}: {e}\n"
            f"Sent: {sent} | Skipped: {skipped} | Failed: {failed}\n"
            f"Dobara shuru: /sendall {last_id + 1}"
        )
    finally:
        running["busy"] = False


@app.on_message(filters.command("stop") & filters.private & admin_filter)
async def stop_cmd(_, m: Message):
    running["stop"] = True
    await m.reply_text("Stop request bhej di.")


# new posts in the source channel get cleaned and shared automatically
if AUTO_FORWARD:
    @app.on_message(filters.chat(SOURCE) & (filters.document | filters.video | filters.audio | filters.photo))
    async def auto(client: Client, m: Message):
        try:
            await process(client, m)
        except Exception as e:
            print(f"auto msg {m.id} failed: {e}")


# ---------- tiny web server (Render needs an open port) ----------
async def health(_):
    return web.Response(text="ok")


async def main():
    server = web.Application()
    server.add_routes([web.get("/", health)])
    runner = web.AppRunner(server)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    await app.start()

    # Channels ko startup pe resolve karo taaki peer cache ho jaye (Peer id invalid fix)
    await resolve_channels()

    print("Bot started")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
