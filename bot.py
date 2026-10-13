import os
import re
import json
import time
import html
import signal
import shutil
import asyncio
import difflib
import tempfile
from collections import deque
from io import BytesIO
from types import SimpleNamespace

# Python 3.12+/3.14 me pyrogram import hote hi event loop maangta hai - pehle se bana do
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import aiohttp
from aiohttp import web
from PIL import Image
from pyrogram import Client, filters, errors as tg_errors
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.types import (CallbackQuery, InlineKeyboardButton,
                            InlineKeyboardMarkup, Message)


# ======================================================================
#  CONFIG (environment variables)
# ======================================================================
def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


API_ID = int(required_env("API_ID"))
API_HASH = required_env("API_HASH")
BOT_TOKEN = required_env("BOT_TOKEN")
ADMINS = [int(x) for x in os.environ.get("ADMINS", "").replace(",", " ").split()]
SOURCE = int(required_env("SOURCE_CHANNEL"))   # source/database channel ID
TARGET = int(required_env("TARGET_CHANNEL"))   # normal destination channel ID
QUALITY_TARGET = int(os.environ["QUALITY_TARGET_CHANNEL"].strip()) if os.environ.get("QUALITY_TARGET_CHANNEL", "").strip() else None  # CAM/TS/TC etc. destination
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")           # free key from themoviedb.org (posters)
BLOCK_WORDS = [w.strip() for w in os.environ.get("BLOCK_WORDS", "").split(",") if w.strip()]
PORT = int(os.environ.get("PORT", "8080"))                   # Render health-check port
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip()   # Render sets this itself (keep-alive ping)
STATE_FILE = os.environ.get("STATE_FILE", "bot_state.json")      # progress + settings yahan save hote hain
TRANSMISSIONS = int(os.environ.get("TRANSMISSIONS", "4"))        # parallel upload/download streams
PREFETCH = max(1, int(os.environ.get("PREFETCH", "3")))          # kitni files aage se download/prepare hon
LIVE_PANEL = env_bool("LIVE_PANEL", False)                              # True => job ke dauran live progress panel dikhao
MAX_RETRIES = max(1, int(os.environ.get("MAX_RETRIES", "3")))    # network error pe kitni baar retry

# Telegram guideline: ek chat me ~1 message/second se zyada nahi. Isliye delay 1s se kam allowed nahi.
MIN_DELAY = 1.0
SEND_DELAY = max(float(os.environ.get("SEND_DELAY", "1.0")), MIN_DELAY)
# Live rate-limit state: FloodWait aane par delay khud badhta hai, phir dheere dheere wapas kam hota hai
# FloodWait ke baad delay max itna hi badhega (Telegram ~20 msg/min => ~3s/msg safe hai). Pehle 10s tak jata tha => bahut slow.
FLOOD_MAX_DELAY = max(float(os.environ.get("FLOOD_MAX_DELAY", "3.0")), SEND_DELAY)
rate = {"delay": SEND_DELAY, "floods": 0, "flood_secs": 0.0}

# Runtime settings (/settings se on/off). Env sirf starting default hai.
cfg = {
    "clean_caption": env_bool("CLEAN_CAPTION", True),    # caption se @username / t.me links hatao
    "clean_filename": env_bool("CLEAN_FILENAME", True),  # file ke naam se username hatao (re-upload lagega)
    "set_poster": env_bool("SET_POSTER", False),         # video pe TMDB poster thumbnail (abhi OFF - API key nahi hai)
    "dedupe": env_bool("DEDUPE", True),                  # pehle bheji hui file dobara na bhejo
    "auto": env_bool("AUTO_FORWARD", True),              # source me nayi post aate hi auto forward
    "notify": env_bool("NOTIFY", True),                  # kaam khatam hone par alag message (ping)
    "delete_quality": env_bool("DELETE_QUALITY", False), # CAM/HDTS/HDTC file quality channel me jaane ke baad source se delete
}
SETTING_LABELS = {
    "clean_caption": "🧹 Clean caption",
    "clean_filename": "✂️ Clean file name",
    "set_poster": "🖼 Auto poster",
    "dedupe": "♻️ Skip duplicates",
    "auto": "📡 Auto-forward new posts",
    "notify": "🔔 Notify on finish",
    "delete_quality": "🗑 Delete quality files from source",
}

app = Client("cleaner_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN,
             max_concurrent_transmissions=TRANSMISSIONS)
START_TIME = time.time()


# ======================================================================
#  PERSISTENT STATE  (resume, duplicates, block list, settings)
# ======================================================================
state = {
    "blocks": [],        # /addblock se jude words
    "failed": [],        # fail hui message IDs (/retryfailed)
    "footer": "",        # har caption ke neeche jaane wala text
    "target": None,      # /setdest se chuna default destination (None => env TARGET_CHANNEL)
    "cfg": {},
    "last_job": None,    # {"start","end","last_id","done"} - /resume ke liye
    "totals": {"sent": 0, "dup": 0, "failed": 0},
}
seen: set = set()        # file_unique_id of already-sent files
dirty = [0]


def default_target() -> int:
    """Default destination: /setdest se set kiya hua, warna env TARGET_CHANNEL."""
    return state.get("target") or TARGET


def seen_key(key, target: int):
    """Duplicate check har destination ke liye alag (env TARGET ke keys pehle jaise hi rehte hain)."""
    if not key:
        return None
    return key if target == TARGET else f"{target}:{key}"


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    except Exception as e:
        print(f"state load failed: {e}")
        return
    for k in ("blocks", "failed", "footer", "last_job", "target"):
        if k in data:
            state[k] = data[k]
    state["totals"].update(data.get("totals", {}))
    seen.update(data.get("seen", []))
    for k, v in (data.get("cfg") or {}).items():
        if k in cfg:
            cfg[k] = bool(v)
    print(f"state loaded: {len(seen)} files remembered")


def save_state():
    try:
        data = dict(state)
        data["seen"] = list(seen)
        data["cfg"] = dict(cfg)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, STATE_FILE)
        dirty[0] = 0
    except Exception as e:
        print(f"state save failed: {e}")


def maybe_save(force: bool = False):
    if force or dirty[0] >= 25:
        save_state()


# ======================================================================
#  CLEANING
# ======================================================================
LINK_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/\S+|tg://\S+", re.I)
WWW_RE = re.compile(r"\bwww\.[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b", re.I)
USER_RE = re.compile(r"@[A-Za-z0-9_]+")
# Remove low-quality/cinema-recording tags from both captions and filenames.
# Supports CAMRip, CAM-Rip, CAM.Rip and tags inside []/()/{}.
QUALITY_CORE = (r"(?<![A-Za-z0-9])(?:CAM[ \t]*[-_.]?[ \t]*RIP|HDCAM|CAM|TELESYNC|HDTS|TELECINE|HDTC|TS|TC)"
                r"(?![A-Za-z0-9])")
QUALITY_RE = re.compile(QUALITY_CORE, re.I)
# tag + uske aas-paas ka ek separator hi hatao (baaki caption ka spacing/punctuation na chhede)
QUALITY_START_RE = re.compile(r"(?m)^[ \t]*" + QUALITY_CORE + r"[ \t]*[._-]?[ \t]*", re.I)
QUALITY_STRIP_RE = re.compile(r"[ \t]*[._-]?[ \t]*" + QUALITY_CORE, re.I)
block_re = None


def clean_quality_tags(text: str) -> str:
    if not text or not QUALITY_RE.search(text):
        return text                      # tag nahi => text bilkul waisa hi (comma/dash spacing safe)
    text = QUALITY_START_RE.sub("", text)
    text = QUALITY_STRIP_RE.sub("", text)
    text = re.sub(r"[\[({][ \t]*[\])}]", "", text)          # khaali [] () {}
    text = re.sub(r"([\[({])[ \t]+", r"\1", text)           # "[ 720p]" -> "[720p]"
    text = re.sub(r"[ \t]+([\])}])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text


HDTS_HDTC_RE = re.compile(r"(?<![A-Za-z0-9])HD[ \t]*[-_.]?[ \t]*(?:TS|TC)(?![A-Za-z0-9])", re.I)
HDRIP_RE = re.compile(r"(?<![A-Za-z0-9])HD[ \t]*[-_.]?[ \t]*RIP(?![A-Za-z0-9])", re.I)


def has_quality_tag(msg: Message) -> bool:
    """True when filename or caption has a CAM/TS/TC-type tag. HDRip files are always normal."""
    media = msg.document or msg.video or msg.audio or msg.animation
    filename = getattr(media, "file_name", "") if media else ""
    caption = msg.caption or msg.text or ""
    combined = f"{filename} {caption}"
    if HDTS_HDTC_RE.search(combined):
        return True                      # HDTS / HDTC => hamesha quality channel
    if HDRIP_RE.search(combined):
        return False                     # HDRip => hamesha normal channel
    return bool(QUALITY_RE.search(combined))


def destination_for(msg: Message, fallback: int) -> tuple[int, bool]:
    """Return (destination, should_delete_source_after_success). Delete sirf /settings me ON ho tab."""
    if QUALITY_TARGET is not None and has_quality_tag(msg):
        return QUALITY_TARGET, bool(cfg["delete_quality"])
    return fallback, False


def rebuild_block_re():
    global block_re
    words = sorted({w for w in BLOCK_WORDS + state["blocks"] if w}, key=len, reverse=True)
    block_re = re.compile("@?(?:" + "|".join(map(re.escape, words)) + ")", re.I) if words else None


def clean_text(text: str) -> str:
    text = clean_quality_tags(text)
    text = LINK_RE.sub("", text)
    text = WWW_RE.sub("", text)
    if block_re:
        text = block_re.sub("", text)
    text = USER_RE.sub("", text)
    lines = [re.sub(r"[ \t]{2,}", " ", l).strip() for l in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def clean_filename(name: str) -> str:
    base, ext = os.path.splitext(name)
    base = clean_quality_tags(base)
    base = LINK_RE.sub("", base)
    base = WWW_RE.sub("", base)
    if block_re:
        base = block_re.sub("", base)
    base = USER_RE.sub("", base)
    base = re.sub(r"[\[\(\{]\s*[\]\)\}]", "", base)       # empty [] () {}
    base = re.sub(r"\s{2,}", " ", base)
    base = re.sub(r"([._\-])\1+", r"\1", base)            # .. -- __ -> single
    base = base.strip(" ._-[](){}")
    return (base or "file") + ext


def local_name(name: str) -> str:
    """Disk pe save karne ke liye safe naam (Telegram ko original `name` hi bheja jaata hai)."""
    base, ext = os.path.splitext(name)
    base = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", base).strip()[:150] or "file"
    return base + ext[:10]


def build_caption(raw: str, limit: int = 1024, footer: bool = True) -> str:
    text = clean_text(raw) if cfg["clean_caption"] else raw
    if footer and state["footer"]:
        text = f"{text}\n\n{state['footer']}" if text else state["footer"]
    return text[:limit]


# ======================================================================
#  POSTER (TMDB) - best-match + cache
# ======================================================================
JUNK_RE = re.compile(
    r"\b(480p|576p|720p|1080p|2160p|4k|hdrip|webrip|web-dl|webdl|web|bluray|brrip|bdrip|dvdrip|"
    r"hdtv|hdcam|camrip|hdts|x264|x265|hevc|h264|h265|10bit|aac|ddp?5?1|atmos|esubs?|hindi|"
    r"english|tamil|telugu|malayalam|kannada|punjabi|bengali|dual|audio|multi|org|proper|repack|"
    r"complete|season|s\d{1,2}e\d{1,3}|s\d{1,2}|e\d{1,3})\b.*",
    re.I,
)
SERIES_RE = re.compile(r"\bs\d{1,2}\s?e\d{1,3}\b|\bseason\b", re.I)
poster_cache: dict = {}   # (title, year) -> jpeg bytes | None (match nahi mila)


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


async def tmdb_poster_bytes(title: str, year, filename: str):
    """TMDB me search karke sabse milta-julta poster (JPEG bytes). Network error par exception uthata hai."""
    kinds = ("tv", "movie") if SERIES_RE.search(filename) else ("movie", "tv")
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        for kind in kinds:
            for use_year in ((True, False) if year else (False,)):
                params = {"api_key": TMDB_API_KEY, "query": title}
                if use_year:
                    params["year" if kind == "movie" else "first_air_date_year"] = year
                async with s.get(f"https://api.themoviedb.org/3/search/{kind}", params=params) as r:
                    if r.status != 200:
                        raise RuntimeError(f"TMDB HTTP {r.status}")
                    data = await r.json()
                best, best_score = None, 0.0
                for res in data.get("results", [])[:8]:
                    if not res.get("poster_path"):
                        continue
                    names = [res.get("title") or res.get("name") or "",
                             res.get("original_title") or res.get("original_name") or ""]
                    score = max(difflib.SequenceMatcher(None, title.lower(), n.lower()).ratio() for n in names)
                    if score > best_score:
                        best, best_score = res, score
                if best and best_score >= 0.5:    # galat poster se behtar hai koi poster na ho
                    async with s.get(f"https://image.tmdb.org/t/p/w342{best['poster_path']}") as img:
                        raw = await img.read()
                    im = Image.open(BytesIO(raw)).convert("RGB")
                    im.thumbnail((320, 320))
                    buf = BytesIO()
                    im.save(buf, "JPEG", quality=85)   # Telegram: JPEG, <200KB, max 320px
                    return buf.getvalue()
    return None


async def fetch_poster(filename: str, dest: str) -> bool:
    if not TMDB_API_KEY:
        return False
    title, year = guess_title(filename)
    if len(title) < 2:
        return False
    key = (title.lower(), year)
    try:
        if key not in poster_cache:
            data = await tmdb_poster_bytes(title, year, filename)
            if len(poster_cache) > 500:
                poster_cache.pop(next(iter(poster_cache)))
            poster_cache[key] = data
        data = poster_cache[key]
        if not data:
            return False
        with open(dest, "wb") as f:
            f.write(data)
        return True
    except Exception as e:
        print(f"poster fetch failed for '{title}': {e}")
        return False


# ======================================================================
#  HELPERS
# ======================================================================
class StopRequested(Exception):
    """/stop dabane par kaam rokne ke liye."""


TRANSIENT = (asyncio.TimeoutError, TimeoutError, ConnectionError,
             getattr(tg_errors, "InternalServerError", ConnectionError))


def fmt_secs(s: float) -> str:
    s = int(round(s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


def progress_bar(pct: float, width: int = 14) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(round(width * pct / 100))
    return f"{'█' * filled}{'░' * (width - filled)}  {pct:.1f}%"


async def safe(coro_fn, *args, **kwargs):
    """FloodWait par rukta hai, network/server error par backoff ke saath retry karta hai."""
    tries = 0
    while True:
        try:
            return await coro_fn(*args, **kwargs)
        except FloodWait as e:
            wait = int(e.value) + 1
            rate["floods"] += 1
            rate["flood_secs"] += wait
            rate["delay"] = min(max(rate["delay"] * 1.5, 2.0), FLOOD_MAX_DELAY)
            print(f"FloodWait: Telegram ne {wait}s rukne ko kaha (delay ab {rate['delay']:.1f}s)")
            await asyncio.sleep(wait)
        except TRANSIENT as e:
            tries += 1
            if tries >= MAX_RETRIES:
                raise
            wait = 2 ** tries
            print(f"{type(e).__name__}: {wait}s baad dobara try ({tries}/{MAX_RETRIES})")
            await asyncio.sleep(wait)


async def safe_edit(message: Message, text: str, markup=None):
    try:
        await safe(message.edit_text, text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except MessageNotModified:
        pass
    except Exception as e:
        print(f"status edit failed: {e}")


# Saare sends (job + auto) ek hi throttle se guzarte hain => chat me ~1 msg/sec rehta hai
send_lock = asyncio.Lock()
last_send = [0.0]


async def throttle():
    async with send_lock:
        wait = last_send[0] + rate["delay"] - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        last_send[0] = time.monotonic()
        rate["delay"] = max(SEND_DELAY, rate["delay"] * 0.95)   # flood ke baad jaldi normal


async def resolve_channels(target: int = None) -> dict:
    """SOURCE aur destination ko resolve karo taaki Pyrogram peer cache me save kar le."""
    results = {}
    for name, cid in (("SOURCE", SOURCE), ("TARGET", target or default_target())):
        try:
            chat = await safe(app.get_chat, cid)
            print(f"{name} OK: {chat.title} ({chat.id})")
            results[name] = None
        except Exception as e:
            print(f"{name} FAILED ({cid}): {e}")
            results[name] = f"{type(e).__name__}: {e}"
    return results


# ======================================================================
#  PROCESSING PIPELINE
#    prepare()  -> download / poster / caption (parallel, aage se)
#    deliver()  -> Telegram pe bhejna (order me, throttle ke saath)
# ======================================================================
running = {"busy": False, "stop": False, "job": None, "gate": None, "io": {"dl": "", "ul": ""}}
admin_filter = filters.user(ADMINS) if ADMINS else filters.user([])   # ADMINS khaali => koi nahi (fail closed)

FILE_ATTRS = ("document", "video", "audio", "photo", "voice", "animation", "video_note", "sticker")


def Job(**kw):
    base = dict(kind="skip", text=None, caption=None, msg_id=None, key=None, path=None,
                thumb=None, name=None, tmp=None, send_as="document", meta=dict())
    base.update(kw)
    return SimpleNamespace(**base)


def file_key(msg: Message):
    for attr in FILE_ATTRS:
        media = getattr(msg, attr, None)
        if media is not None:
            return getattr(media, "file_unique_id", None)
    return None


def make_progress(label: str, name: str):
    short = (name or "file")[:45]
    last = [0.0]

    def cb(current, total):
        now = time.monotonic()
        if now - last[0] < 2 and current != total:
            return
        last[0] = now
        running["io"][label] = f"{short} — {current * 100 / total:.0f}%" if total else short
    return cb


def copy_job(msg: Message, key) -> SimpleNamespace:
    """File re-upload ke bina: Telegram server-side copy (fast). Sirf caption badalta hai."""
    captionable = bool(msg.photo or msg.document or msg.video or msg.audio or msg.animation or msg.voice)
    caption = None
    if captionable and (cfg["clean_caption"] or state["footer"]):
        caption = build_caption(msg.caption or "")
    return Job(kind="copy", msg_id=msg.id, caption=caption, key=key)


async def prepare(client: Client, msg: Message, target: int = None, dedupe: bool = None,
                  media_only: bool = False) -> SimpleNamespace:
    if msg.empty or msg.service:
        return Job(kind="skip")
    if media_only and not file_key(msg):     # 'files' mode: text/poll/etc. skip, sirf file wale messages
        return Job(kind="skip")
    target = target or default_target()
    dedupe = cfg["dedupe"] if dedupe is None else dedupe
    key = seen_key(file_key(msg), target)
    if dedupe and key and key in seen:
        return Job(kind="dup")

    # Quality channel (CAM/TS/TC): file, naam aur caption bilkul original rehte hain (no clean, no footer, no poster)
    if QUALITY_TARGET is not None and target == QUALITY_TARGET:
        if msg.text:
            return Job(kind="text", text=msg.text[:4096])
        return Job(kind="copy", msg_id=msg.id, caption=None, key=key)

    if msg.text:
        t = (clean_text(msg.text) if cfg["clean_caption"] else msg.text)[:4096]
        return Job(kind="text", text=t) if t else Job(kind="skip")

    media = msg.document or msg.video or msg.audio
    is_video = bool(msg.video) or (bool(msg.document) and (msg.document.mime_type or "").startswith("video/"))
    wants_poster = cfg["set_poster"] and is_video and bool(TMDB_API_KEY)
    wants_rename = cfg["clean_filename"] and bool(media)

    if not media or not (wants_rename or wants_poster):
        return copy_job(msg, key)

    old = media.file_name
    new = clean_filename(old) if (cfg["clean_filename"] and old) else old
    raw_caption = (msg.caption or "").strip()
    first_line = raw_caption.splitlines()[0] if raw_caption else ""
    title_src = new or first_line     # poster search ke liye naam (naam na ho to caption ki pehli line)

    tmp = tempfile.mkdtemp()
    try:
        poster = os.path.join(tmp, "poster.jpg")
        has_poster = bool(wants_poster and title_src and await fetch_poster(title_src, poster))

        # naam pehle se saaf hai aur naya poster nahi => re-upload ki zarurat nahi
        if new == old and not has_poster:
            shutil.rmtree(tmp, ignore_errors=True)
            return copy_job(msg, key)

        dl_name = os.path.join(tmp, local_name(new)) if new else tmp + os.sep
        path = await safe(client.download_media, msg, file_name=dl_name,
                          progress=make_progress("dl", title_src or "file"))
        running["io"]["dl"] = ""
        if not path:
            raise RuntimeError("download_media ne koi file nahi di")

        thumb = None
        if has_poster:
            thumb = poster
        else:
            thumbs = getattr(media, "thumbs", None)
            if thumbs:
                thumb = await safe(client.download_media, thumbs[0].file_id,
                                   file_name=os.path.join(tmp, "thumb.jpg"))

        job = Job(kind="upload", path=path, thumb=thumb, name=new, tmp=tmp, key=key,
                  caption=build_caption(msg.caption or "") if (cfg["clean_caption"] or state["footer"])
                  else (msg.caption or ""))
        if msg.video:
            job.send_as = "video"
            job.meta = dict(duration=msg.video.duration, width=msg.video.width, height=msg.video.height)
        elif msg.audio:
            job.send_as = "audio"
            job.meta = dict(duration=msg.audio.duration, performer=msg.audio.performer, title=msg.audio.title)
        return job
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def cleanup(job):
    tmp = getattr(job, "tmp", None)
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)


def mark_seen(key):
    if key:
        seen.add(key)
        dirty[0] += 1


async def deliver(client: Client, job, target: int) -> str:
    k = job.kind
    if k in ("skip", "dup"):
        return k
    await throttle()
    if k == "text":
        await safe(client.send_message, target, job.text, parse_mode=ParseMode.DISABLED)
    elif k == "copy":
        extra = {}
        if job.caption is not None:
            # reply_markup=None => original ke buttons (jinme telegram link ho sakte hain) copy nahi hote
            extra = dict(caption=job.caption, parse_mode=ParseMode.DISABLED, reply_markup=None)
        await safe(client.copy_message, target, SOURCE, job.msg_id, **extra)
    elif k == "upload":
        common = dict(caption=job.caption, parse_mode=ParseMode.DISABLED, thumb=job.thumb,
                      progress=make_progress("ul", job.name or os.path.basename(job.path)))
        if job.name:
            common["file_name"] = job.name
        if job.send_as == "video":
            await safe(client.send_video, target, job.path, supports_streaming=True, **job.meta, **common)
        elif job.send_as == "audio":
            await safe(client.send_audio, target, job.path, **job.meta, **common)
        else:
            await safe(client.send_document, target, job.path, **common)
        running["io"]["ul"] = ""
    mark_seen(job.key)
    return "sent"


async def handle(client: Client, work, target: int = None) -> str:
    """prepare() ka result lo, retry ke saath bhejo, temp files saaf karo.
    Returns: sent | skip | dup. Final failure par exception uthata hai."""
    job = None
    try:
        job = await work
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return await deliver(client, job, target or default_target())
            except Exception as e:
                if attempt >= MAX_RETRIES:
                    raise
                wait = 2 ** attempt
                print(f"send fail ({type(e).__name__}: {e}) - {wait}s baad retry {attempt}/{MAX_RETRIES}")
                await asyncio.sleep(wait)
    finally:
        if job is not None:
            cleanup(job)


def bump(res: str):
    if res in state["totals"]:
        state["totals"][res] += 1


# ======================================================================
#  JOB RUNNER (pipeline + pause/stop + checkpoint + live panel)
# ======================================================================
def job_buttons(paused: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("▶️ Resume" if paused else "⏸ Pause", callback_data="job:unpause" if paused else "job:pause"),
        InlineKeyboardButton("⛔ Stop", callback_data="job:stop"),
    ]])


def job_elapsed(js) -> float:
    now = time.monotonic()
    extra = (now - js.pause_since) if js.pause_since else 0.0
    return max(now - js.t_start - js.paused_total - extra, 0.0)


def render_panel(title: str, footer: str = "") -> str:
    js = running.get("job")
    if js is None:
        return title
    elapsed = job_elapsed(js)
    left = max(js.total - js.done, 0)
    pct = js.done * 100 / js.total if js.total else 100.0
    avg_file = elapsed / js.sent if js.sent else 0
    speed = js.sent * 60 / elapsed if elapsed > 0 and js.sent else 0
    if not left:
        eta_txt = "0s"
    elif js.ewma:
        eta_txt = fmt_secs(js.ewma * left)
    else:
        eta_txt = "calculate ho raha hai..."
    if js.pause_since:
        title += "  ⏸ <b>PAUSED</b>"
    io_lines = ""
    for icon, k in (("⬇️", "dl"), ("⬆️", "ul")):
        v = running["io"].get(k)
        if v:
            io_lines += f"\n{icon} <code>{html.escape(v)}</code>"
    text = (
        f"{title}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<code>{progress_bar(pct)}</code>\n\n"
        f"📊 <b>Total</b>   <code>{js.total}</code>\n"
        f"✅ <b>Done</b>    <code>{js.done}</code>   ⏳ <b>Left</b> <code>{left}</code>\n\n"
        f"📤 <b>Sent</b> <code>{js.sent}</code>  ⏭ <b>Skip</b> <code>{js.skipped}</code>  "
        f"♻️ <b>Dup</b> <code>{js.dup}</code>  ❌ <b>Failed</b> <code>{js.failed}</code>\n\n"
        f"⚡ <b>Speed</b>    <code>{speed:.1f} files/min</code>\n"
        f"⏱ <b>Last file</b> <code>{fmt_secs(js.last_took)}</code>  ·  <b>Avg</b> <code>{fmt_secs(avg_file)}</code>\n"
        f"🛡 <b>FloodWait</b> <code>{rate['floods']}</code> baar  ·  <b>Wait</b> <code>{fmt_secs(rate['flood_secs'])}</code>"
        f"  ·  <b>Delay</b> <code>{rate['delay']:.1f}s</code>\n"
        f"🕒 <b>Elapsed</b>  <code>{fmt_secs(elapsed)}</code>  ·  <b>ETA</b> <code>{eta_txt}</code>\n"
        f"📍 <b>Current ID</b> <code>{js.last_id}</code> / <code>{js.end_id}</code>"
        f"{io_lines}"
    )
    if footer:
        text += f"\n\n{footer}"
    return text


def do_pause() -> bool:
    js, gate = running.get("job"), running.get("gate")
    if not (running["busy"] and js and gate) or js.pause_since:
        return False
    gate.clear()
    js.pause_since = time.monotonic()
    return True


def do_unpause() -> bool:
    js, gate = running.get("job"), running.get("gate")
    if not (running["busy"] and js and gate) or not js.pause_since:
        return False
    now = time.monotonic()
    js.paused_total += now - js.pause_since
    js.pause_since = None
    js.last_tick = now
    gate.set()
    return True


def do_stop() -> bool:
    if not running["busy"]:
        return False
    do_unpause()                 # pause me ho to pehle chalu karo taaki loop stop dekh sake
    running["stop"] = True
    return True


async def run_job(client: Client, m: Message, ids, kind: str = "range", target: int = None, dedupe: bool = None,
                  media_only: bool = False):
    """ids: range ya list of message IDs. kind: 'range' (checkpoint banta hai) ya 'retry'."""
    target = target or default_target()
    running.update(busy=True, stop=False)
    rate.update(delay=SEND_DELAY, floods=0, flood_secs=0.0)
    gate = asyncio.Event()
    gate.set()
    running["gate"] = gate
    running["io"] = {"dl": "", "ul": ""}

    first, last = ids[0], ids[-1]
    now = time.monotonic()
    js = SimpleNamespace(
        total=len(ids), done=0, sent=0, dup=0, skipped=0, failed=0, failed_ids=[], processed=set(),
        t_start=now, paused_total=0.0, pause_since=None, last_tick=now, last_edit=0.0,
        ewma=0.0, last_took=0.0, first_id=first, end_id=last, last_id=first - 1, status=None,
    )
    running["job"] = js
    if kind == "range":
        state["last_job"] = {"start": first, "end": last, "last_id": first - 1, "done": False,
                             "target": target, "dedupe": dedupe, "media_only": media_only}
        save_state()

    label = f"📍 Range: <code>{first}</code> → <code>{last}</code>" if kind == "range" \
        else "🔁 <b>Failed IDs dobara try ho rahe hain</b>"
    status = await m.reply_text(
        "🚀 <b>Forwarding shuru ho gayi</b>\n"
        f"📥 Destination: <code>{target}</code> · 📊 Total message: <code>{js.total}</code>\n"
        "Khatam hone par aapko ek message aayega.\n"
        "<i>/status · /pause · /stop</i>"
        if not LIVE_PANEL else
        "🚀 <b>Forwarding shuru ho gayi</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"{label}\n"
        f"📥 Destination: <code>{target}</code>{' (sirf files)' if media_only else ''}\n"
        f"📊 Total message: <code>{js.total}</code>\n"
        f"⚙️ Pipeline: <code>{PREFETCH}</code> files aage se prepare hongi\n\n"
        "<i>Baaki time ka andaza kuch messages ke baad dikhega.</i>",
        parse_mode=ParseMode.HTML, reply_markup=job_buttons(False) if LIVE_PANEL else None,
    )
    js.status = status
    pending = deque()   # (message_id, prepare-task) - order me

    async def consume(item):
        mid, task, item_target, delete_source = item
        t0 = time.monotonic()
        try:
            res = await handle(client, task, item_target)
            # Source se tabhi delete karo jab destination par sending confirm ho chuki ho.
            if res == "sent" and delete_source:
                try:
                    await safe(client.delete_messages, SOURCE, mid)
                    print(f"msg {mid}: quality-tagged file sent to {item_target}; source deleted")
                except Exception as de:     # file ja chuki hai - delete fail hone par 'failed' mat gino
                    print(f"msg {mid}: sent but source delete failed: {type(de).__name__}: {de}")
        except Exception as e:
            res = "failed"
            print(f"msg {mid} failed: {type(e).__name__}: {e}")
        if res == "sent":
            js.sent += 1
            js.last_took = time.monotonic() - t0
            print(f"msg {mid} sent in {fmt_secs(js.last_took)}")
        elif res == "dup":
            js.dup += 1
        elif res == "skip":
            js.skipped += 1
        else:
            js.failed += 1
            js.failed_ids.append(mid)
        bump(res)
        js.done += 1
        js.last_id = mid
        js.processed.add(mid)
        tick_now = time.monotonic()
        dt = tick_now - js.last_tick
        js.last_tick = tick_now
        js.ewma = dt if not js.ewma else js.ewma * 0.9 + dt * 0.1
        if kind == "range" and state["last_job"]:
            state["last_job"]["last_id"] = mid
        dirty[0] += 1
        maybe_save()

    async def tick():
        if not LIVE_PANEL:
            return
        if time.monotonic() - js.last_edit >= 8:
            js.last_edit = time.monotonic()
            await safe_edit(status, render_panel("🚀 <b>Forwarding in progress</b>"),
                            job_buttons(bool(js.pause_since)))

    outcome = "done"
    resume_hint = ("▶️ Aage badhane ke liye: <code>/resume</code>" if kind == "range"
                   else "▶️ Dobara: <code>/retryfailed</code>")
    try:
        for i in range(0, len(ids), 100):
            chunk = list(ids[i:i + 100])
            msgs = await safe(client.get_messages, SOURCE, chunk)
            for mid, msg in zip(chunk, msgs):
                await gate.wait()                       # pause
                if running["stop"]:
                    raise StopRequested
                item_target, delete_source = destination_for(msg, target)
                pending.append((mid, asyncio.create_task(prepare(client, msg, item_target, dedupe, media_only)),
                                item_target, delete_source))
                while len(pending) >= PREFETCH:         # window bhar gayi => sabse purani bhejo
                    await consume(pending.popleft())
                    await tick()
        while pending:
            await gate.wait()
            if running["stop"]:
                raise StopRequested
            await consume(pending.popleft())
            await tick()
        if LIVE_PANEL:
            await safe_edit(status, render_panel("✅ <b>Forwarding complete</b>"))
    except StopRequested:
        outcome = "stopped"
        if LIVE_PANEL:
            await safe_edit(status, render_panel("⛔ <b>Forwarding roki gayi</b>", resume_hint))
    except Exception as e:
        outcome = "error"
        print(f"run_job crashed: {type(e).__name__}: {e}")
        await safe_edit(status, render_panel(
            "❌ <b>Error aa gaya</b>",
            f"<code>{html.escape(type(e).__name__ + ': ' + str(e))}</code>\n{resume_hint}"))
    finally:
        for item in pending:
            item[1].cancel()
        leftovers = await asyncio.gather(*(item[1] for item in pending), return_exceptions=True)
        for r in leftovers:
            if isinstance(r, SimpleNamespace):
                cleanup(r)
        pending.clear()
        if kind == "range" and state["last_job"]:
            state["last_job"]["done"] = (outcome == "done")
        state["failed"] = sorted((set(state["failed"]) - js.processed) | set(js.failed_ids))
        save_state()
        summary = (f"{outcome.upper()} — sent {js.sent}, dup {js.dup}, skipped {js.skipped}, failed {js.failed}")
        print(summary)
        running.update(busy=False, stop=False, job=None, gate=None)
        running["io"] = {"dl": "", "ul": ""}

    # finish notification (naya message => phone pe ping aata hai)
    if cfg["notify"]:
        try:
            icon = {"done": "✅", "stopped": "⛔", "error": "❌"}[outcome]
            await safe(client.send_message, m.chat.id,
                       f"{icon} <b>Job {outcome}</b>\n"
                       f"📤 Sent <code>{js.sent}</code> · ♻️ Dup <code>{js.dup}</code> · "
                       f"⏭ Skip <code>{js.skipped}</code> · ❌ Failed <code>{js.failed}</code>\n"
                       f"🕒 <code>{fmt_secs(time.monotonic() - js.t_start - js.paused_total)}</code>"
                       + (f" · 🛡 FloodWait <code>{rate['floods']}</code> baar ({fmt_secs(rate['flood_secs'])})"
                          if rate["floods"] else "")
                       + ("\n\n🔁 Failed files ke liye: <code>/retryfailed</code>" if js.failed_ids else ""),
                       parse_mode=ParseMode.HTML)
            if js.failed_ids:
                f = BytesIO("\n".join(map(str, js.failed_ids)).encode())
                f.name = "failed_ids.txt"
                await safe(client.send_document, m.chat.id, f, caption="Failed message IDs")
        except Exception as e:
            print(f"notify failed: {e}")


async def start_range_job(client: Client, m: Message, start_id: int, end_id: int,
                          target: int = None, dedupe: bool = None, media_only: bool = False):
    if start_id < 1 or end_id < start_id:
        return await m.reply_text("Invalid message ID range. Start ID must be >= 1 and end ID must be >= start ID.")
    await run_job(client, m, range(start_id, end_id + 1), "range", target, dedupe, media_only)


# ======================================================================
#  COMMANDS
# ======================================================================
USAGE = (
    "📖 <b>Commands</b>\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "▫️ <code>/sendall</code> — source ki saari files\n"
    "▫️ <code>/sendall 500</code> — ID 500 se aage ki saari files\n"
    "▫️ <code>/sendall -1001234567890</code> — saari files <b>dusre folder/channel</b> me\n"
    "▫️ <code>/sendall @channelname</code> — public channel ko username/link se bhi de sakte ho\n"
    "▫️ <code>QUALITY_TARGET_CHANNEL</code> — CAM/TS/TC-tagged files ka separate channel (/settings me <b>Delete quality files</b> ON ho to successful send ke baad source se delete; HDRip normal channel me jata hai)\n"
    "▫️ <code>/sendall -1001234567890 files</code> — sirf files (text messages skip)\n"
    "▫️ <code>/sendall 500 -1001234567890 force</code> — ID 500 se, us channel me, duplicate check ke bina\n"
    "▫️ <code>/clean 100 200 [channel_id] [force]</code> — sirf ek range (start, end ID)\n"
    "▫️ <code>/setdest -100...</code> — default destination badlo (<code>/setdest reset</code> = env wala) · <code>/dest</code> — dekho\n"
    "▫️ <code>/resume</code> — roka/adhura kaam wahin se aage\n"
    "▫️ <code>/retryfailed</code> — fail hui files dobara\n"
    "▫️ <code>/status</code> · <code>/pause</code> · <code>/unpause</code> · <code>/stop</code>\n"
    "▫️ <code>/settings</code> — features on/off (buttons)\n"
    "▫️ <code>/addblock word</code> · <code>/delblock word</code> · <code>/blocklist</code>\n"
    "▫️ <code>/footer text</code> — har caption ke neeche text (<code>/footer off</code>)\n"
    "▫️ <code>/stats</code> · <code>/resetseen</code>\n\n"
    "<i>Saari files ke liye bot ko source channel me admin + Post Messages permission chahiye.</i>"
)


@app.on_message(filters.command("start") & filters.private)
async def start(_, m: Message):
    await m.reply_text(
        "✨ <b>Channel Cleaner &amp; Forwarder</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Source channel ki files ko saaf karke (caption/file name se @username, links hata kar, "
        "poster laga kar) target channel me bhejta hoon.\n\n"
        f"{USAGE}",
        parse_mode=ParseMode.HTML,
    )


@app.on_message(filters.command("help") & filters.private)
async def help_cmd(_, m: Message):
    await m.reply_text(USAGE, parse_mode=ParseMode.HTML)


@app.on_message(filters.command("status") & filters.private & admin_filter)
async def status_cmd(_, m: Message):
    if running["busy"] and running.get("job"):
        js = running["job"]
        await m.reply_text(render_panel("🚀 <b>Forwarding in progress</b>"), parse_mode=ParseMode.HTML,
                           reply_markup=job_buttons(bool(js.pause_since)))
    else:
        await m.reply_text("😴 Abhi koi kaam nahi chal raha.\n\n/sendall se shuru karo.")


@app.on_message(filters.command("stats") & filters.private & admin_filter)
async def stats_cmd(_, m: Message):
    t = state["totals"]
    lj = state["last_job"]
    lj_txt = f"{lj['last_id']}/{lj['end']} ({'done' if lj['done'] else 'adhura'})" if lj else "—"
    await m.reply_text(
        "📈 <b>Stats</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"📤 Total sent <code>{t['sent']}</code>  ♻️ Dup <code>{t['dup']}</code>  ❌ Failed <code>{t['failed']}</code>\n"
        f"🧠 Yaad rakhi files <code>{len(seen)}</code>\n"
        f"🔁 Pending failed IDs <code>{len(state['failed'])}</code>\n"
        f"📍 Last job <code>{lj_txt}</code>\n"
        f"🖼 Poster cache <code>{len(poster_cache)}</code>\n"
        f"⏳ Uptime <code>{fmt_secs(time.time() - START_TIME)}</code>",
        parse_mode=ParseMode.HTML)


@app.on_message(filters.command(["clean", "sendall"]) & filters.private & admin_filter)
async def clean_cmd(client: Client, m: Message):
    if running["busy"]:
        return await m.reply_text("Ek kaam pehle se chal raha hai. /stop karo ya wait karo.")

    args = m.command[1:]
    force = any(a.lower() in ("force", "-f") for a in args)
    media_only = any(a.lower() in ("files", "file", "-files") for a in args)
    ids_given = [a for a in args if re.fullmatch(r"-?\d+", a)]
    # -100... ya koi negative number => destination channel; baaki positive numbers => message IDs
    dest = next((int(a) for a in ids_given if a.startswith("-")), None)
    # @username ya t.me/username link bhi destination ho sakta hai (public channel)
    if dest is None:
        ref = next((a for a in args if re.fullmatch(r"@\w{4,}|(?:https?://)?t\.me/\w{4,}/?", a, re.I)), None)
        if ref:
            uname = "@" + re.sub(r"^(?:https?://)?t\.me/|/$", "", ref.lstrip("@"), flags=re.I) if not ref.startswith("@") else ref
            try:
                dest = (await safe(client.get_chat, uname)).id
            except Exception as e:
                return await m.reply_text(f"Destination <code>{html.escape(ref)}</code> nahi mila: "
                                          f"{html.escape(type(e).__name__)}", parse_mode=ParseMode.HTML)
    nums = [int(a) for a in ids_given if not a.startswith("-")]
    try:
        if m.command[0] == "sendall" or (args and args[0].lower() == "all"):
            start_id = nums[0] if nums else 1
            end_id = nums[1] if len(nums) > 1 else None   # None => neeche probe se nikalenge
        else:
            start_id, end_id = nums[0], nums[1]
    except IndexError:
        return await m.reply_text(USAGE, parse_mode=ParseMode.HTML)
    target = dest or default_target()
    dedupe = False if force else None

    res = await resolve_channels(target)
    bad = {k: v for k, v in res.items() if v}
    if bad:
        lines = "\n".join(f"{k}: {v}" for k, v in bad.items())
        return await m.reply_text(
            "Channel access nahi mil raha:\n"
            f"{lines}\n\n"
            "Check karo:\n"
            "1. SOURCE_CHANNEL / destination channel ID sahi hai (-100 se shuru)\n"
            "2. Wahi bot (wahi BOT_TOKEN) dono channels me admin hai\n"
            "3. Source me 'Post Messages', destination me bhi 'Post Messages' permission di hai"
        )

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
    await start_range_job(client, m, start_id, end_id, target, dedupe, media_only)


@app.on_message(filters.command("resume") & filters.private & admin_filter)
async def resume_cmd(client: Client, m: Message):
    if running["busy"]:
        return await m.reply_text("Ek kaam pehle se chal raha hai.")
    lj = state["last_job"]
    if not lj or lj.get("done") or lj["last_id"] >= lj["end"]:
        return await m.reply_text("Aisa koi adhura kaam nahi mila. /sendall se naya shuru karo.")
    target = lj.get("target") or default_target()
    res = await resolve_channels(target)
    if any(res.values()):
        return await m.reply_text("Channel access nahi mil raha. Bot dono channels me admin hai ya nahi check karo.")
    await start_range_job(client, m, lj["last_id"] + 1, lj["end"], target, lj.get("dedupe"), lj.get("media_only", False))


@app.on_message(filters.command("retryfailed") & filters.private & admin_filter)
async def retry_cmd(client: Client, m: Message):
    if running["busy"]:
        return await m.reply_text("Ek kaam pehle se chal raha hai.")
    ids = sorted(set(state["failed"]))
    if not ids:
        return await m.reply_text("✅ Koi failed file pending nahi hai.")
    res = await resolve_channels()
    if any(res.values()):
        return await m.reply_text("Channel access nahi mil raha. Bot dono channels me admin hai ya nahi check karo.")
    await run_job(client, m, ids, "retry")


@app.on_message(filters.command("dest") & filters.private & admin_filter)
async def dest_cmd(_, m: Message):
    t = default_target()
    src = "env TARGET_CHANNEL" if not state.get("target") else "/setdest"
    await m.reply_text(f"📥 Default destination: <code>{t}</code> ({src})\n"
                       f"📤 Source (database): <code>{SOURCE}</code>", parse_mode=ParseMode.HTML)


@app.on_message(filters.command("setdest") & filters.private & admin_filter)
async def setdest_cmd(_, m: Message):
    if running["busy"]:
        return await m.reply_text("Ek kaam chal raha hai. Pehle /stop karo ya khatam hone do.")
    arg = (m.command[1] if len(m.command) > 1 else "").strip()
    if arg.lower() == "reset":
        state["target"] = None
        save_state()
        return await m.reply_text(f"♻️ Destination wapas env wala: <code>{TARGET}</code>", parse_mode=ParseMode.HTML)
    if not re.fullmatch(r"-?\d+", arg):
        return await m.reply_text("Use: <code>/setdest -1001234567890</code>  |  <code>/setdest reset</code>",
                                  parse_mode=ParseMode.HTML)
    cid = int(arg)
    res = await resolve_channels(cid)
    if res.get("TARGET"):
        return await m.reply_text(f"Is destination tak pahunch nahi paya: {res['TARGET']}\n"
                                  "Bot ko us channel me admin + Post Messages permission do.")
    state["target"] = cid
    save_state()
    await m.reply_text(f"✅ Default destination ab <code>{cid}</code>. /sendall se saari files wahan jayengi.",
                       parse_mode=ParseMode.HTML)


@app.on_message(filters.command("stop") & filters.private & admin_filter)
async def stop_cmd(_, m: Message):
    if do_stop():
        await m.reply_text("⛔ <b>Stop request bhej di.</b>\nCurrent file khatam hote hi kaam ruk jayega.",
                           parse_mode=ParseMode.HTML)
    else:
        await m.reply_text("😴 Abhi koi kaam nahi chal raha.")


@app.on_message(filters.command("pause") & filters.private & admin_filter)
async def pause_cmd(_, m: Message):
    await m.reply_text("⏸ Pause ho gaya. /unpause se chalu karo." if do_pause()
                       else "Koi chalta hua kaam nahi (ya pehle se paused hai).")


@app.on_message(filters.command("unpause") & filters.private & admin_filter)
async def unpause_cmd(_, m: Message):
    await m.reply_text("▶️ Dobara chalu." if do_unpause() else "Kaam paused nahi hai.")


@app.on_callback_query(filters.regex(r"^job:(pause|unpause|stop)$"))
async def job_cb(_, cq: CallbackQuery):
    if cq.from_user.id not in ADMINS:
        return await cq.answer("Sirf admin.", show_alert=True)
    act = cq.data.split(":")[1]
    ok = {"pause": do_pause, "unpause": do_unpause, "stop": do_stop}[act]()
    if not ok:
        return await cq.answer("Abhi koi kaam nahi chal raha.", show_alert=True)
    await cq.answer({"pause": "⏸ Paused", "unpause": "▶️ Resumed", "stop": "⛔ Stopping..."}[act])
    js = running.get("job")
    if js and act != "stop":
        await safe_edit(cq.message, render_panel("🚀 <b>Forwarding in progress</b>"),
                        job_buttons(bool(js.pause_since)))


# ---------- settings ----------
def settings_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{'✅' if cfg[k] else '❌'} {label}", callback_data=f"set:{k}")]
        for k, label in SETTING_LABELS.items()
    ])


def settings_text() -> str:
    footer = html.escape(state["footer"]) if state["footer"] else "—"
    words = len(set(BLOCK_WORDS + state["blocks"]))
    return (
        "⚙️ <b>Settings</b>\n━━━━━━━━━━━━━━━━━━\n"
        "Button dabake on/off karo (turant lagu, restart ke baad bhi yaad rehta hai).\n\n"
        f"📝 Footer: <code>{footer}</code>\n"
        f"🚫 Block words: <code>{words}</code>\n"
        f"🖼 TMDB key: <code>{'set' if TMDB_API_KEY else 'NOT SET (poster nahi lagega)'}</code>"
    )


@app.on_message(filters.command("settings") & filters.private & admin_filter)
async def settings_cmd(_, m: Message):
    await m.reply_text(settings_text(), parse_mode=ParseMode.HTML, reply_markup=settings_markup())


@app.on_callback_query(filters.regex(r"^set:"))
async def settings_cb(_, cq: CallbackQuery):
    if cq.from_user.id not in ADMINS:
        return await cq.answer("Sirf admin.", show_alert=True)
    key = cq.data.split(":", 1)[1]
    if key not in cfg:
        return await cq.answer("Unknown setting.")
    cfg[key] = not cfg[key]
    save_state()
    await cq.answer(f"{SETTING_LABELS[key]}: {'ON' if cfg[key] else 'OFF'}")
    try:
        await cq.message.edit_text(settings_text(), parse_mode=ParseMode.HTML, reply_markup=settings_markup())
    except MessageNotModified:
        pass


# ---------- block words / footer / maintenance ----------
def _arg_text(m: Message) -> str:
    parts = (m.text or "").split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


@app.on_message(filters.command("addblock") & filters.private & admin_filter)
async def addblock_cmd(_, m: Message):
    words = [w.strip() for w in _arg_text(m).split(",") if w.strip()]
    if not words:
        return await m.reply_text("Use: <code>/addblock ClipmateMovies, @MyChannel</code>", parse_mode=ParseMode.HTML)
    for w in words:
        w = w.lstrip("@")
        if w and w not in state["blocks"]:
            state["blocks"].append(w)
    rebuild_block_re()
    save_state()
    await m.reply_text(f"✅ Block list me {len(words)} word add hue. Ab total: {len(set(BLOCK_WORDS + state['blocks']))}")


@app.on_message(filters.command("delblock") & filters.private & admin_filter)
async def delblock_cmd(_, m: Message):
    words = [w.strip().lstrip("@") for w in _arg_text(m).split(",") if w.strip()]
    removed = [w for w in words if w in state["blocks"]]
    state["blocks"] = [w for w in state["blocks"] if w not in removed]
    rebuild_block_re()
    save_state()
    await m.reply_text(f"🗑 Removed: {', '.join(removed) or 'kuch nahi (env wale words yahan se nahi hatte)'}")


@app.on_message(filters.command("blocklist") & filters.private & admin_filter)
async def blocklist_cmd(_, m: Message):
    env_words = ", ".join(BLOCK_WORDS) or "—"
    added = ", ".join(state["blocks"]) or "—"
    await m.reply_text(f"🚫 <b>Block words</b>\nEnv: <code>{html.escape(env_words)}</code>\n"
                       f"Added: <code>{html.escape(added)}</code>", parse_mode=ParseMode.HTML)


@app.on_message(filters.command("footer") & filters.private & admin_filter)
async def footer_cmd(_, m: Message):
    text = _arg_text(m)
    if not text:
        return await m.reply_text(f"Current footer: {state['footer'] or '—'}\n\n"
                                  "Set: /footer text  |  Hatao: /footer off")
    state["footer"] = "" if text.lower() == "off" else text[:200]
    save_state()
    await m.reply_text("🗑 Footer hata diya." if not state["footer"] else "✅ Footer set ho gaya.")


@app.on_message(filters.command("resetseen") & filters.private & admin_filter)
async def resetseen_cmd(_, m: Message):
    if "yes" not in (m.command[1:] or []):
        return await m.reply_text(f"⚠️ Ye {len(seen)} yaad rakhi files bhula dega (duplicate check reset).\n"
                                  "Pakka ho to: <code>/resetseen yes</code>", parse_mode=ParseMode.HTML)
    seen.clear()
    save_state()
    await m.reply_text("♻️ Duplicate memory saaf.")


# ---------- new posts in the source channel get cleaned and shared automatically ----------
@app.on_message(filters.chat(SOURCE) & (filters.document | filters.video | filters.audio | filters.photo))
async def auto(client: Client, m: Message):
    if not cfg["auto"]:
        return
    try:
        target, delete_source = destination_for(m, default_target())
        res = await handle(client, prepare(client, m, target), target)
        if res == "sent" and delete_source:
            await safe(client.delete_messages, SOURCE, m.id)
            print(f"auto msg {m.id}: quality-tagged file sent to {target}; source deleted")
        bump(res)
        maybe_save()
    except Exception as e:
        bump("failed")
        print(f"auto msg {m.id} failed: {e}")


# ======================================================================
#  tiny web server (Render needs an open port) + keep-alive
# ======================================================================
async def health(_):
    js = running.get("job")
    return web.json_response({"ok": True, "busy": running["busy"], "sent": js.sent if js else 0})


async def keep_alive():
    """Render free plan 15 min bina traffic ke so jata hai - khud ko ping karte raho."""
    if not RENDER_URL:
        return
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        while True:
            await asyncio.sleep(600)
            try:
                async with s.get(RENDER_URL) as r:
                    await r.read()
            except Exception as e:
                print(f"keep-alive ping failed: {e}")


async def main():
    load_state()
    rebuild_block_re()

    server = web.Application()
    server.add_routes([web.get("/", health)])
    runner = web.AppRunner(server)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    await app.start()
    await resolve_channels()    # peer cache (Peer id invalid fix)
    ka_task = asyncio.create_task(keep_alive())

    # pichla kaam adhura reh gaya tha (restart/redeploy) => admins ko ek baar batao
    lj = state["last_job"]
    if lj and not lj.get("done") and lj["last_id"] < lj["end"] and not lj.get("announced"):
        lj["announced"] = True
        save_state()
        for admin in ADMINS:
            try:
                await app.send_message(
                    admin, f"🟢 Bot online. Pichla kaam adhura tha (ID {lj['last_id'] + 1} → {lj['end']}).\n"
                           "▶️ /resume se wahin se aage badhao.")
            except Exception as e:
                print(f"startup notify failed for {admin}: {e}")

    print("Bot started")

    stop_evt = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_evt.set)
        except (NotImplementedError, RuntimeError):
            pass
    await stop_evt.wait()

    print("Shutting down...")
    if running["busy"]:
        do_stop()
        for _ in range(50):    # max ~25s current file khatam hone ka wait
            if not running["busy"]:
                break
            await asyncio.sleep(0.5)
    ka_task.cancel()
    save_state()
    await app.stop()
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
