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
import mimetypes
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
from PIL import Image, ImageFilter, ImageOps
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
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "").strip()    # free key from themoviedb.org (posters)
TMDB_API = os.environ.get("TMDB_API_BASE", "https://api.themoviedb.org/3").rstrip("/")   # TMDB block ho to proxy yahan
TMDB_IMG = os.environ.get("TMDB_IMAGE_BASE", "https://image.tmdb.org/t/p").rstrip("/")
OCR_SPACE_KEY = os.environ.get("OCR_SPACE_KEY", "").strip()  # free key ocr.space se (jahan tesseract install nahi hota)
OCR_MIN_SCORE = float(os.environ.get("OCR_MIN_SCORE", "0.8"))   # poster se padhe naam ka TMDB match kitna strict ho
AUTO_RESUME = env_bool("AUTO_RESUME", True)                   # restart/error ke baad adhura kaam khud resume
MAX_AUTO_RESUMES = max(0, int(os.environ.get("MAX_AUTO_RESUMES", "0")))   # bina progress ke max kitni baar khud resume (0 = jab tak kaam poora na ho)
RETRY_ROUNDS = max(0, int(os.environ.get("RETRY_ROUNDS", "4")))             # job ke end me failed files itni baar khud dobara bhejo
BLOCK_WORDS = [w.strip() for w in os.environ.get("BLOCK_WORDS", "").split(",") if w.strip()]
PORT = int(os.environ.get("PORT", "8080"))                   # Render health-check port
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip()   # Render sets this itself (keep-alive ping)
STATE_FILE = os.environ.get("STATE_FILE", "bot_state.json")      # progress + settings yahan save hote hain
TRANSMISSIONS = int(os.environ.get("TRANSMISSIONS", "8"))        # parallel upload/download streams
PREFETCH = max(1, int(os.environ.get("PREFETCH", "3")))          # kitni files aage se download/prepare hon
LIVE_PANEL = env_bool("LIVE_PANEL", False)                              # True => job ke dauran live progress panel dikhao
FRAME_AT = min(max(float(os.environ.get("FRAME_AT", "0.1")), 0.0), 0.95)   # video ke kitne hisse par frame (0.1 = 10%)
MAX_RETRIES = max(1, int(os.environ.get("MAX_RETRIES", "3")))    # network error pe kitni baar retry

# Telegram guideline: ek chat me ~1 message/second se zyada nahi. Isliye delay 1s se kam allowed nahi.
MIN_DELAY = 0.5      # FloodWait aaye to delay khud badh jata hai (safe() dekho), isliye thoda tez rakha
SEND_DELAY = max(float(os.environ.get("SEND_DELAY", "0.7")), MIN_DELAY)
# Live rate-limit state: FloodWait aane par delay khud badhta hai, phir dheere dheere wapas kam hota hai
# FloodWait ke baad delay max itna hi badhega (Telegram ~20 msg/min => ~3s/msg safe hai). Pehle 10s tak jata tha => bahut slow.
FLOOD_MAX_DELAY = max(float(os.environ.get("FLOOD_MAX_DELAY", "3.0")), SEND_DELAY)
rate = {"delay": SEND_DELAY, "floods": 0, "flood_secs": 0.0}

# Runtime settings (/settings se on/off). Env sirf starting default hai.
cfg = {
    "clean_caption": env_bool("CLEAN_CAPTION", True),    # caption se @username / t.me links hatao
    "clean_filename": env_bool("CLEAN_FILENAME", True),  # file ke naam se username hatao (re-upload lagega)
    "set_poster": env_bool("SET_POSTER", True),          # video pe TMDB poster thumbnail (abhi OFF - API key nahi hai)
    "dedupe": env_bool("DEDUPE", True),                  # pehle bheji hui file dobara na bhejo
    "auto": env_bool("AUTO_FORWARD", True),              # source me nayi post aate hi auto forward
    "notify": env_bool("NOTIFY", True),                  # kaam khatam hone par alag message (ping)
    "delete_quality": env_bool("DELETE_QUALITY", False), # CAM/HDTS/HDTC file quality channel me jaane ke baad source se delete
    "auto_caption": env_bool("AUTO_CAPTION", True),      # caption khaali ho to TMDB se khud likho (TMDB_API_KEY chahiye)
    "hd_poster": env_bool("HD_POSTER", False),           # movie ke saath TMDB ka bada (HD) poster alag photo me bhi bhejo
    "name_from_poster": env_bool("NAME_FROM_POSTER", True),  # video me naam na ho to poster (thumbnail) se naam padho (OCR)
    "frame_thumb": env_bool("FRAME_THUMB", True),        # video ka apna frame thumbnail/cover bane (ffmpeg chahiye)
}
SETTING_LABELS = {
    "clean_caption": "🧹 Clean caption",
    "clean_filename": "✂️ Clean file name",
    "set_poster": "🖼 Auto poster",
    "dedupe": "♻️ Skip duplicates",
    "auto": "📡 Auto-forward new posts",
    "notify": "🔔 Notify on finish",
    "delete_quality": "🗑 Delete quality files from source",
    "auto_caption": "📝 Auto caption (jahan caption nahi)",
    "hd_poster": "🖼 HD poster photo (video ke upar alag)",
    "name_from_poster": "🔎 Poster se naam padho (naam na ho to)",
    "frame_thumb": "🎞 Video frame thumbnail",
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
    "resume_tries": 0,   # bina progress ke auto-resume kitni baar hua
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
    for k in ("blocks", "failed", "footer", "last_job", "target", "resume_tries"):
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
    if force or dirty[0] >= 10:
        save_state()


# ======================================================================
#  CLEANING
# ======================================================================
# Hatane wale link domains: mdisk hamesha; aur chahiye to env EXTRA_LINK_DOMAINS="site1.com,site2.net"
_EXTRA_DOMAINS = [d.strip().lower() for d in os.environ.get("EXTRA_LINK_DOMAINS", "").split(",") if d.strip()]
_LINK_DOMAINS = "|".join([r"mdisk\.[a-z]{2,}"] + [re.escape(d) for d in _EXTRA_DOMAINS])
LINK_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/\S+|tg://\S+"
                     r"|(?:https?://)?(?:[\w-]+\.)*(?:" + _LINK_DOMAINS + r")(?:/\S*)?", re.I)
# link hatane ke baad bacha hua akela label ("Online Play link:") bhi hatao
LINK_LABEL_RE = re.compile(r"^\W*(?:online\s+)?(?:play|watch|stream|download|direct|mdisk)\s*(?:link|url)?s?\W*$", re.I)
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


def clean_text_changed(original: str, lines) -> bool:
    return "\n".join(lines) != "\n".join(l.strip() for l in original.splitlines())


def clean_text(text: str) -> str:
    original = text
    text = clean_quality_tags(text)
    text = LINK_RE.sub("", text)
    text = WWW_RE.sub("", text)
    if block_re:
        text = block_re.sub("", text)
    text = USER_RE.sub("", text)
    lines = [re.sub(r"[ \t]{2,}", " ", l).strip() for l in text.splitlines()]
    lines = [l for l in lines if not LINK_LABEL_RE.match(l)]
    if clean_text_changed(original, lines):      # "❤️ @user ❤️" -> sirf emoji bachte hain, wo line bhi hatao
        lines = [l for l in lines if not l or re.search(r"\w", l)]
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
#  POSTER (TMDB) - best-match + cache, aur "poster se naam" (OCR)
# ======================================================================
JUNK_RE = re.compile(
    r"\b(480p|576p|720p|1080p|2160p|4k|hdrip|webrip|web-dl|webdl|web|bluray|brrip|bdrip|dvdrip|"
    r"hdtv|hdcam|camrip|hdts|x264|x265|hevc|h264|h265|10bit|aac|ddp?5?1|atmos|esubs?|hindi|"
    r"english|tamil|telugu|malayalam|kannada|punjabi|bengali|dual|audio|multi|org|proper|repack|"
    r"complete|season|s\d{1,2}e\d{1,3}|s\d{1,2}|e\d{1,3})\b.*",
    re.I,
)
SERIES_RE = re.compile(r"\bs\d{1,2}\s?e\d{1,3}\b|\bseason\b", re.I)
poster_cache: dict = {}   # poster_path -> jpeg bytes
lookup_cache: dict = {}   # (title, year, min_score) -> info dict | None (match nahi mila)
ocr_cache: dict = {}      # thumbnail file_unique_id -> info dict | None

GENRES = {28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime", 99: "Documentary",
          18: "Drama", 10751: "Family", 14: "Fantasy", 36: "History", 27: "Horror", 10402: "Music",
          9648: "Mystery", 10749: "Romance", 878: "Sci-Fi", 10770: "TV Movie", 53: "Thriller",
          10752: "War", 37: "Western", 10759: "Action & Adventure", 10762: "Kids", 10763: "News",
          10764: "Reality", 10765: "Sci-Fi & Fantasy", 10766: "Soap", 10767: "Talk", 10768: "War & Politics"}
QUALITY_INFO_RE = re.compile(r"\b(480p|576p|720p|1080p|2160p|4k|hdrip|webrip|web-dl|webdl|bluray|brrip|bdrip|"
                             r"dvdrip|hdtv|x264|x265|hevc|10bit|esubs?|dual audio|hindi|english|tamil|telugu|"
                             r"malayalam|kannada|punjabi|bengali)\b", re.I)

# Sirf asli media extension hatao (pehle os.path.splitext tha: "Mr. Bean" ka ". Bean" extension ban jata tha)
MEDIA_EXT_RE = re.compile(r"\.(mkv|mp4|avi|mov|webm|m4v|ts|flv|wmv|mpg|mpeg|3gp|mp3|m4a|flac|aac|ogg|zip|rar|7z)$", re.I)
# "VID_2023", "video", "file (1)" jaise naam = koi asli naam nahi
GENERIC_WORDS = {"vid", "video", "videos", "file", "movie", "document", "doc", "audio", "img", "image", "mov",
                 "untitled", "new", "clip", "film", "download", "telegram", "whatsapp", "screen", "recording"}
# Poster pe aam likha hua text jo title nahi hota
OCR_SKIP_WORDS = {"hindi", "dubbed", "english", "tamil", "telugu", "movie", "movies", "full", "hd", "new",
                  "official", "poster", "trailer", "presents", "starring", "directed", "download", "watch",
                  "only", "theatres", "theaters", "cinemas", "coming", "soon", "dual", "audio", "www", "com",
                  "in", "the", "a", "of", "and", "by", "from", "film"}


def is_generic_title(title: str) -> bool:
    if re.fullmatch(r"\d{1,4}", (title or "").strip()):
        return False                      # "300", "1917" jaisi movies
    words = re.findall(r"[A-Za-z]{2,}", title or "")
    return not [w for w in words if w.lower() not in GENERIC_WORDS]


def guess_title(filename: str):
    name = MEDIA_EXT_RE.sub("", filename or "")
    base = re.sub(r"[._]+", " ", name)
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


def usable_title(text) -> bool:
    """Is text se koi asli movie/series ka naam nikal sakta hai?"""
    if not text:
        return False
    title, _ = guess_title(text)
    return len(title) >= 2 and not is_generic_title(title)


def title_score(a: str, b: str) -> float:
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


# ---------- TMDB API ----------
def tmdb_auth():
    """v3 'API Key' ya v4 'API Read Access Token' (eyJ...) - dono chalte hain."""
    key = TMDB_API_KEY.strip()
    if key.startswith("eyJ") or len(key) > 60:
        return {}, {"Authorization": f"Bearer {key}"}
    return {"api_key": key}, {}


async def tmdb_get(session, path: str, **params):
    base, headers = tmdb_auth()
    last = None
    for attempt in range(2):
        try:
            async with session.get(f"{TMDB_API}/{path}", params={**base, **params}, headers=headers) as r:
                if r.status == 401:
                    raise RuntimeError("TMDB HTTP 401 - TMDB_API_KEY galat hai (v3 API Key ya v4 Read Access Token dalo)")
                if r.status == 429:
                    last = RuntimeError("TMDB HTTP 429 (rate limit)")
                    await asyncio.sleep(2)
                    continue
                if r.status != 200:
                    raise RuntimeError(f"TMDB HTTP {r.status}")
                return await r.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last = e
            await asyncio.sleep(1)
    raise RuntimeError(f"TMDB network error: {type(last).__name__}: {last}")


def info_from(res: dict, kind: str, title: str, year, score: float) -> dict:
    date = res.get("release_date") or res.get("first_air_date") or ""
    return dict(
        title=res.get("title") or res.get("name") or title,
        year=date[:4] or year or "",
        rating=res.get("vote_average") or 0,
        genres=[GENRES[g] for g in res.get("genre_ids", []) if g in GENRES][:3],
        overview=(res.get("overview") or "").strip(),
        lang=(res.get("original_language") or "").upper(),
        tv=(kind == "tv"),
        poster_path=res.get("poster_path"),
        score=score,
    )


async def tmdb_find(session, title: str, year, filename: str = "", min_score: float = 0.5):
    kinds = ("tv", "movie") if SERIES_RE.search(filename or title) else ("movie", "tv")
    for kind in kinds:
        for use_year in ((True, False) if year else (False,)):
            params = {"query": title, "include_adult": "false"}
            if use_year:
                params["year" if kind == "movie" else "first_air_date_year"] = year
            data = await tmdb_get(session, f"search/{kind}", **params)
            best, best_score = None, 0.0
            for res in data.get("results", [])[:8]:
                if not res.get("poster_path"):
                    continue
                names = [n for n in (res.get("title") or res.get("name") or "",
                                     res.get("original_title") or res.get("original_name") or "") if n]
                if not names:
                    continue
                score = max(title_score(title, n) for n in names)
                if score > best_score:
                    best, best_score = res, score
            if best and best_score >= min_score:    # galat poster se behtar hai koi poster na ho
                return info_from(best, kind, title, year, best_score)
    return None


async def tmdb_lookup(title: str, year=None, filename: str = "", min_score: float = 0.5):
    """TMDB me naam se best match (info dict) ya None. Network/key error par exception uthata hai."""
    if not TMDB_API_KEY:
        return None
    key = (title.lower(), year, round(min_score, 2))
    if key in lookup_cache:
        return lookup_cache[key]
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        info = await tmdb_find(s, title, year, filename, min_score)
    if len(lookup_cache) > 500:
        lookup_cache.pop(next(iter(lookup_cache)))
    lookup_cache[key] = info
    return info


async def tmdb_poster_raw(poster_path: str, size: str = "w500") -> bytes:
    """TMDB se poster, jaisa hai waisa (w500 = thumbnail ke liye, w780 = HD photo post ke liye)."""
    ckey = size + poster_path
    if ckey in poster_cache:
        return poster_cache[ckey]
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.get(f"{TMDB_IMG}/{size}{poster_path}") as r:
            if r.status != 200:
                raise RuntimeError(f"TMDB image HTTP {r.status}")
            raw = await r.read()
    if len(poster_cache) > 100:
        poster_cache.pop(next(iter(poster_cache)))
    poster_cache[ckey] = raw
    return raw


def _jpeg_under_limit(im) -> bytes:
    data = b""
    for q in (95, 90, 82, 72, 60):          # Telegram thumbnail: JPEG, <200KB
        buf = BytesIO()
        im.save(buf, "JPEG", quality=q, subsampling=0, optimize=True)
        data = buf.getvalue()
        if len(data) < 195_000:
            break
    return data


def make_thumb(raw: bytes, vw: int = 0, vh: int = 0) -> bytes:
    """Poster ko video ke aspect ratio ke canvas me rakho (Telegram thumbnail ko video ke size me fit/crop karta hai,
    to seedha portrait poster lagane par beech ka hissa kat jata hai). Max 320px - ye Telegram ki hard limit hai."""
    poster = Image.open(BytesIO(raw)).convert("RGB")
    pw, ph = poster.size
    if not (vw and vh):
        poster.thumbnail((320, 320), Image.LANCZOS)
        return _jpeg_under_limit(poster)
    if vw >= vh:
        cw, ch = 320, max(2, round(320 * vh / vw))
    else:
        cw, ch = max(2, round(320 * vw / vh)), 320
    if abs((cw / ch) - (pw / ph)) < 0.12:          # ratio pehle se milta hai
        return _jpeg_under_limit(poster.resize((cw, ch), Image.LANCZOS))
    bg = ImageOps.fit(poster, (cw, ch), Image.LANCZOS).filter(ImageFilter.GaussianBlur(10))
    bg = Image.blend(bg, Image.new("RGB", (cw, ch), (0, 0, 0)), 0.45)
    fg_h = ch if ch <= cw else ch
    fg_w = max(1, round(pw * fg_h / ph))
    if fg_w > cw:
        fg_w, fg_h = cw, max(1, round(ph * cw / pw))
    fg = poster.resize((fg_w, fg_h), Image.LANCZOS)    # poster poora dikhe (title kate nahi)
    bg.paste(fg, ((cw - fg_w) // 2, (ch - fg_h) // 2))
    return _jpeg_under_limit(bg)


async def extract_frame(video_path: str, dest: str, duration: int = 0, vw: int = 0, vh: int = 0) -> bool:
    """Video ke andar se ek frame nikalo (ffmpeg) -> Telegram thumbnail (JPEG, max 320px, <200KB)."""
    if not shutil.which("ffmpeg"):
        return False
    ts = 1.0 if not duration else min(max(duration * FRAME_AT, 1.0), max(duration - 1, 0.0))
    raw = dest + ".raw.jpg"
    try:
        for t in (ts, 0.0):      # frame na mile (chhota video) to shuru ka frame
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", video_path, "-frames:v", "1",
                "-vf", "scale=320:320:force_original_aspect_ratio=decrease", "-q:v", "3", raw,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            try:
                await asyncio.wait_for(proc.wait(), 60)
            except asyncio.TimeoutError:
                proc.kill()
                continue
            if os.path.exists(raw) and os.path.getsize(raw) > 0:
                break
        else:
            return False
        with open(raw, "rb") as f:
            data = _jpeg_under_limit(Image.open(BytesIO(f.read())).convert("RGB"))
        with open(dest, "wb") as f:
            f.write(data)
        return True
    except Exception as e:
        print(f"frame thumbnail failed: {type(e).__name__}: {e}")
        return False
    finally:
        try:
            os.remove(raw)
        except OSError:
            pass


async def tmdb_poster_by_path(poster_path: str, size=(0, 0)) -> bytes:
    return make_thumb(await tmdb_poster_raw(poster_path), *size)


async def save_poster(info, dest: str, size=(0, 0)) -> bool:
    if not info or not info.get("poster_path"):
        return False
    try:
        data = await tmdb_poster_by_path(info["poster_path"], size)
        with open(dest, "wb") as f:
            f.write(data)
        return True
    except Exception as e:
        print(f"poster download failed for '{info.get('title')}': {e}")
        return False


async def identify_from_text(texts):
    """File ke naam / caption ki pehli line se TMDB match. Pehla jo mil jaye wahi."""
    for t in texts:
        if not usable_title(t):
            continue
        title, year = guess_title(t)
        try:
            info = await tmdb_lookup(title, year, t)
        except Exception as e:
            print(f"TMDB lookup failed for '{title}': {e}")
            continue
        if info:
            return info
    return None


# ---------- poster (thumbnail) se naam padhna: OCR ----------
def _ocr_prep(path: str):
    im = Image.open(path).convert("L")
    w, h = im.size
    scale = max(1, 1100 // max(w, h, 1))      # chhota thumbnail bada karo, warna OCR kharab padhta hai
    if scale > 1:
        im = im.resize((w * scale, h * scale), Image.LANCZOS)
    return ImageOps.autocontrast(im)


def _ocr_local(path: str):
    """Server pe tesseract installed ho to. Returns [(line_text, text_height)]."""
    import pytesseract
    base = _ocr_prep(path)
    lines = []
    for img in (base, ImageOps.invert(base)):         # kaala aur safed, dono tarah ka text
        d = pytesseract.image_to_data(img, config="--psm 11", output_type=pytesseract.Output.DICT)
        groups = {}
        for i, word in enumerate(d["text"]):
            word = (word or "").strip()
            try:
                conf = float(d["conf"][i])
            except (TypeError, ValueError):
                conf = -1
            if not word or conf < 40:
                continue
            g = groups.setdefault((d["block_num"][i], d["par_num"][i], d["line_num"][i]), {"w": [], "h": 0})
            g["w"].append(word)
            g["h"] = max(g["h"], int(d["height"][i]))
        lines += [(" ".join(g["w"]), g["h"]) for g in groups.values()]
    return lines


async def _ocr_space(path: str):
    """Free OCR.space API (OCR_SPACE_KEY chahiye) - Render jaisi jagah jahan tesseract install nahi hota."""
    img = _ocr_prep(path)
    buf = BytesIO()
    img.save(buf, "JPEG", quality=90)
    form = aiohttp.FormData()
    form.add_field("apikey", OCR_SPACE_KEY)
    form.add_field("isOverlayRequired", "true")
    form.add_field("OCREngine", "2")
    form.add_field("scale", "true")
    form.add_field("file", buf.getvalue(), filename="poster.jpg", content_type="image/jpeg")
    timeout = aiohttp.ClientTimeout(total=45)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post("https://api.ocr.space/parse/image", data=form) as r:
            data = await r.json(content_type=None)
    if data.get("IsErroredOnProcessing"):
        err = data.get("ErrorMessage")
        raise RuntimeError(str(err[0] if isinstance(err, list) and err else err or "OCR.space error")[:200])
    lines = []
    for pr in data.get("ParsedResults") or []:
        overlay = (pr.get("TextOverlay") or {}).get("Lines") or []
        for ln in overlay:
            lines.append((ln.get("LineText") or "", int(float(ln.get("MaxHeight") or 0))))
        if not overlay:
            lines += [(t, 0) for t in (pr.get("ParsedText") or "").splitlines()]
    return lines


def ocr_status() -> str:
    if shutil.which("tesseract"):
        try:
            import pytesseract  # noqa: F401
            return "local tesseract"
        except ImportError:
            pass
    return "OCR.space" if OCR_SPACE_KEY else "NOT available"


_ocr_warned = [False]


async def get_ocr_lines(path: str):
    loop = asyncio.get_running_loop()
    if ocr_status() == "local tesseract":
        try:
            return await loop.run_in_executor(None, _ocr_local, path)
        except Exception as e:
            print(f"local OCR failed: {type(e).__name__}: {e}")
    if OCR_SPACE_KEY:
        try:
            return await _ocr_space(path)
        except Exception as e:
            print(f"OCR.space failed: {type(e).__name__}: {e}")
    elif not _ocr_warned[0]:
        _ocr_warned[0] = True
        print("Poster OCR available nahi: server pe tesseract install karo ya OCR_SPACE_KEY set karo")
    return []


def title_candidates(lines, limit: int = 7):
    """OCR lines [(text, height)] -> TMDB me try karne layak naam. Sabse bada likha hua text (title) pehle."""
    good = []
    for idx, (text, h) in enumerate(lines):
        t = re.sub(r"[^A-Za-z0-9:'&\- ]+", " ", text or "")
        t = re.sub(r"\s{2,}", " ", t).strip(" -:'")
        letters = sum(c.isalpha() for c in t)
        if letters < 3 or letters / len(t) < 0.6:
            continue
        if all(w in OCR_SKIP_WORDS for w in re.findall(r"[a-z]+", t.lower())):
            continue
        good.append((idx, t, h))
    uniq = {}                              # normal + inverted OCR pass ek hi line do baar de sakte hain
    for idx, t, h in good:
        k = t.lower()
        if k not in uniq or h > uniq[k][2]:
            uniq[k] = (idx, t, h)
    by_height = sorted(uniq.values(), key=lambda x: (-x[2], x[0]))
    cands = [t for _, t, _ in by_height[:5]]
    for n in (2, 3):                      # title do-teen lines me ho sakta hai ("THE DARK" + "KNIGHT")
        top = sorted(by_height[:n], key=lambda x: x[0])
        if len(top) == n:
            cands.append(" ".join(t for _, t, _ in top))
    out, seen_c = [], set()
    for c in cands:
        if c.lower() not in seen_c:
            seen_c.add(c.lower())
            out.append(c)
    return out[:limit]


async def identify_from_thumb(client: Client, media, debug: dict = None):
    """Video ke thumbnail (poster) pe likha naam OCR se padho, phir TMDB me confirm karo."""
    if not (TMDB_API_KEY and cfg["name_from_poster"]):
        return None
    thumbs = getattr(media, "thumbs", None)
    if not thumbs:
        return None
    t = max(thumbs, key=lambda x: (getattr(x, "width", 0) or 0) * (getattr(x, "height", 0) or 0))
    ukey = getattr(t, "file_unique_id", None) or t.file_id
    if debug is None and ukey in ocr_cache:
        return ocr_cache[ukey]
    tmp = tempfile.mkdtemp()
    try:
        path = await safe(client.download_media, t.file_id, file_name=os.path.join(tmp, "thumb.jpg"))
        if not path:
            return None
        lines = await get_ocr_lines(path)
        cands = title_candidates(lines)
        print(f"poster OCR lines={[x[0] for x in lines][:8]} candidates={cands}")
        if debug is not None:
            debug["lines"], debug["cands"] = lines, cands
        best = None
        for c in cands:
            try:
                info = await tmdb_lookup(c, None, "", min_score=OCR_MIN_SCORE)
            except Exception as e:
                print(f"TMDB lookup (OCR) failed for '{c}': {e}")
                continue
            if info and (best is None or info["score"] > best["score"]):
                best = info
                if info["score"] >= 0.95:
                    break
        if len(ocr_cache) > 500:
            ocr_cache.pop(next(iter(ocr_cache)))
        ocr_cache[ukey] = best
        return best
    except Exception as e:
        print(f"poster OCR crashed: {type(e).__name__}: {e}")
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def guess_ext(media) -> str:
    mt = (getattr(media, "mime_type", None) or "").lower()
    known = {"video/x-matroska": ".mkv", "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov",
             "video/x-msvideo": ".avi", "audio/mpeg": ".mp3", "audio/mp4": ".m4a"}
    return known.get(mt) or mimetypes.guess_extension(mt) or ".mp4"


def build_filename(info: dict, media, old=None) -> str:
    """TMDB ke title+saal se file ka naam, jaise 'Jawan (2023).mkv'."""
    title = re.sub(r'[\\/:*?"<>|]+', " ", info["title"])
    title = re.sub(r"\s{2,}", " ", title).strip()[:120] or "video"
    year = f" ({info['year']})" if info.get("year") else ""
    ext = ""
    if old:
        m = MEDIA_EXT_RE.search(old)
        ext = m.group(0).lower() if m else ""
    return f"{title}{year}{ext or guess_ext(media)}"


def caption_from_info(info: dict, fname: str = "") -> str:
    """Caption khaali ho to TMDB info se title/saal/rating/genre/story likho."""
    head = f"🎬 {info['title']}" + (f" ({info['year']})" if info["year"] else "")
    lines = [head]
    meta = []
    if info["rating"]:
        meta.append(f"⭐ {info['rating']:.1f}/10")
    if info["genres"]:
        meta.append("🎭 " + ", ".join(info["genres"]))
    if info["lang"]:
        meta.append(f"🌐 {info['lang']}")
    if meta:
        lines.append(" · ".join(meta))
    quality = []
    for q in QUALITY_INFO_RE.findall(fname or ""):
        if q.lower() not in [x.lower() for x in quality]:
            quality.append(q)
    if quality:
        lines.append("📀 " + " | ".join(quality))
    if info["overview"]:
        story = info["overview"]
        lines.append("")
        lines.append("📖 " + (story[:380].rsplit(" ", 1)[0] + "..." if len(story) > 380 else story))
    return "\n".join(lines)


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
                thumb=None, name=None, tmp=None, send_as="document", meta=dict(), info=None)
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


def with_footer(text: str, limit: int = 1024) -> str:
    if state["footer"]:
        text = f"{text}\n\n{state['footer']}" if text else state["footer"]
    return text[:limit]


def copy_job(msg: Message, key, generated: str = "", info=None) -> SimpleNamespace:
    """File re-upload ke bina: Telegram server-side copy (fast). Sirf caption badalta hai."""
    captionable = bool(msg.photo or msg.document or msg.video or msg.audio or msg.animation or msg.voice)
    caption = None
    if captionable and generated:
        caption = with_footer(generated)          # khud likha caption (original me caption tha hi nahi)
    elif captionable and (cfg["clean_caption"] or state["footer"]):
        caption = build_caption(msg.caption or "")
    return Job(kind="copy", msg_id=msg.id, caption=caption, key=key, info=info)


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
    need_caption = bool(media) and cfg["auto_caption"] and bool(TMDB_API_KEY) and not (msg.caption or "").strip()
    wants_ocr = bool(media) and is_video and cfg["name_from_poster"] and bool(TMDB_API_KEY)
    wants_frame = bool(media) and is_video and cfg["frame_thumb"] and bool(shutil.which("ffmpeg"))

    old = media.file_name if media else None
    clean_old = clean_filename(old) if old else None          # block words/@username hata kar (TMDB search saaf rahe)
    raw_caption = (msg.caption or "").strip()
    first_line = clean_text(raw_caption.splitlines()[0]) if raw_caption else ""
    file_name_missing = not usable_title(clean_old)                       # file ka apna naam nahi / generic hai
    name_missing = file_name_missing and not usable_title(first_line)     # caption se bhi naam nahi nikalta

    # Pehchan: pehle file ka naam / caption se. Na mile (ya video me naam hi nahi) to video ke poster
    # (thumbnail) pe likha naam OCR se padho, phir TMDB me confirm karke naam + poster + caption lagao.
    info, from_poster_name = None, False
    if media and TMDB_API_KEY and (need_caption or wants_poster or (wants_ocr and file_name_missing)):
        info = await identify_from_text([clean_old, first_line])
        if info is None and wants_ocr and (name_missing or wants_poster or need_caption):
            info = await identify_from_thumb(client, media)
        # file ka naam nahi tha => TMDB title+saal se naam do (caption se mila ho ya poster se)
        from_poster_name = bool(info) and file_name_missing and wants_ocr

    generated = caption_from_info(info, old or "") if (need_caption and info) else ""

    if not media or not (wants_rename or wants_poster or from_poster_name or wants_frame):
        return copy_job(msg, key, generated, info if is_video else None)

    if from_poster_name:
        new = build_filename(info, media, old)                 # naam tha hi nahi => poster wala naam save karo
    elif cfg["clean_filename"] and old:
        new = clean_old
    else:
        new = old
    title_src = new or (info["title"] if info else "") or first_line or "file"

    tmp = tempfile.mkdtemp()
    try:
        poster = os.path.join(tmp, "poster.jpg")
        has_poster = bool(wants_poster and not wants_frame and info and await save_poster(
            info, poster, (getattr(media, "width", 0) or 0, getattr(media, "height", 0) or 0)))

        # naam pehle se saaf hai aur naya poster nahi => re-upload ki zarurat nahi
        if new == old and not has_poster and not wants_frame:
            shutil.rmtree(tmp, ignore_errors=True)
            return copy_job(msg, key, generated, info if is_video else None)

        dl_name = os.path.join(tmp, local_name(new)) if new else tmp + os.sep
        path = await safe(client.download_media, msg, file_name=dl_name,
                          progress=make_progress("dl", title_src))
        running["io"]["dl"] = ""
        if not path:
            raise RuntimeError("download_media ne koi file nahi di")

        thumb = None
        frame = os.path.join(tmp, "frame.jpg")
        if wants_frame and await extract_frame(path, frame, getattr(media, "duration", 0) or 0,
                                               getattr(media, "width", 0) or 0, getattr(media, "height", 0) or 0):
            thumb = frame
        elif has_poster:
            thumb = poster
        else:
            thumbs = getattr(media, "thumbs", None)
            if thumbs:
                thumb = await safe(client.download_media, thumbs[0].file_id,
                                   file_name=os.path.join(tmp, "thumb.jpg"))

        job = Job(kind="upload", path=path, thumb=thumb, name=new, tmp=tmp, key=key,
                  info=info if is_video else None,
                  caption=with_footer(generated) if generated
                  else build_caption(msg.caption or "") if (cfg["clean_caption"] or state["footer"])
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


async def send_hd_poster(client: Client, target: int, info: dict):
    """TMDB ka bada poster photo ki tarah (har title ke liye har destination me sirf ek baar)."""
    pkey = f"hd:{target}:{info['poster_path']}"
    if pkey in seen:
        return
    raw = await tmdb_poster_raw(info["poster_path"], "w780")
    bio = BytesIO(raw)
    bio.name = "poster.jpg"
    meta = []
    if info.get("rating"):
        meta.append(f"⭐ {info['rating']:.1f}/10")
    if info.get("genres"):
        meta.append(", ".join(info["genres"]))
    cap = f"🎬 {info['title']}" + (f" ({info['year']})" if info.get("year") else "")
    if meta:
        cap += "\n" + " · ".join(meta)
    await throttle()
    await safe(client.send_photo, target, bio, caption=cap[:1024], parse_mode=ParseMode.DISABLED)
    seen.add(pkey)
    dirty[0] += 1


async def deliver(client: Client, job, target: int) -> str:
    k = job.kind
    if k in ("skip", "dup"):
        return k
    if k in ("copy", "upload") and cfg["hd_poster"] and getattr(job, "info", None) and job.info.get("poster_path"):
        try:
            await send_hd_poster(client, target, job.info)
        except Exception as e:                # poster fail hone se video na ruke
            print(f"HD poster send failed: {type(e).__name__}: {e}")
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


async def fetch_chunk(client: Client, chunk):
    """get_messages network/server error par poora job gira dene ke bajay retry karo."""
    for attempt in range(1, 21):
        try:
            return await safe(client.get_messages, SOURCE, chunk)
        except Exception as e:
            if attempt >= 20:
                raise
            wait = min(5 * attempt, 60)
            print(f"get_messages fail ({type(e).__name__}: {e}) - {wait}s baad retry {attempt}/20")
            await asyncio.sleep(wait)


class ChatShim:
    """Auto-resume ke liye: run_job ko Message jaisa object chahiye (reply_text + chat.id)."""
    def __init__(self, client: Client, chat_id: int):
        self._client = client
        self.chat = SimpleNamespace(id=chat_id)

    async def reply_text(self, text, **kw):
        return await self._client.send_message(self.chat.id, text, **kw)


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
        js.last_id = max(js.last_id, mid)
        js.processed.add(mid)
        tick_now = time.monotonic()
        dt = tick_now - js.last_tick
        js.last_tick = tick_now
        js.ewma = dt if not js.ewma else js.ewma * 0.9 + dt * 0.1
        if kind == "range" and state["last_job"]:
            state["last_job"]["last_id"] = max(state["last_job"]["last_id"], mid)
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
            msgs = await fetch_chunk(client, chunk)
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
        # jo files fail hui thi unhe khud dobara bhejo (kuch der ruk kar), taaki sab share hone ke baad hi job khatam ho
        for rnd in range(1, RETRY_ROUNDS + 1):
            if not js.failed_ids:
                break
            retry_ids = list(js.failed_ids)
            print(f"auto-retry round {rnd}/{RETRY_ROUNDS}: {len(retry_ids)} failed files")
            js.failed_ids.clear()
            js.failed -= len(retry_ids)
            js.done -= len(retry_ids)
            await asyncio.sleep(min(15 * rnd, 60))
            for i in range(0, len(retry_ids), 100):
                chunk = retry_ids[i:i + 100]
                msgs = await fetch_chunk(client, chunk)
                for mid, msg in zip(chunk, msgs):
                    await gate.wait()
                    if running["stop"]:
                        raise StopRequested
                    item_target, delete_source = destination_for(msg, target)
                    await consume((mid, asyncio.create_task(prepare(client, msg, item_target, dedupe, media_only)),
                                   item_target, delete_source))
                    await tick()
        if LIVE_PANEL:
            await safe_edit(status, render_panel("✅ <b>Forwarding complete</b>"))
    except StopRequested:
        outcome = "stopped"
        if kind == "range" and state["last_job"]:
            # user ne /stop kiya => restart pe auto-resume nahi; bot band hone (SIGTERM) pe roka gaya => auto-resume
            state["last_job"]["user_stopped"] = not running.get("shutdown")
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
        if js.done > 0:
            state["resume_tries"] = 0     # progress hua => auto-resume ka counter reset
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

    if outcome == "error" and kind == "range" and AUTO_RESUME:
        asyncio.create_task(auto_resume(30, "error"))


async def auto_resume(delay: float, reason: str):
    """Restart ya error ke baad adhura kaam khud wahin se aage badhao (user ne /stop kiya ho to nahi)."""
    await asyncio.sleep(delay)
    if running["busy"] or running.get("resuming") or not AUTO_RESUME or not ADMINS:
        return
    running["resuming"] = True      # error-path aur watchdog dono ek saath job shuru na karein
    try:
        await _auto_resume(reason)
    finally:
        running["resuming"] = False


async def _auto_resume(reason: str):
    lj = state["last_job"]
    if not lj or lj.get("done") or lj["last_id"] >= lj["end"] or lj.get("user_stopped"):
        return
    tries = state.get("resume_tries", 0)
    if MAX_AUTO_RESUMES and tries >= MAX_AUTO_RESUMES:
        if not state.get("resume_limit_notified"):
            state["resume_limit_notified"] = True
            save_state()
            print(f"auto-resume limit ({MAX_AUTO_RESUMES}) pahunch gaya, ruk raha hu")
            for admin in ADMINS:
                try:
                    await app.send_message(admin, f"⚠️ Auto-resume {MAX_AUTO_RESUMES} baar try ho chuka, har baar error aaya.\n"
                                                  "Logs check karo, phir /resume se manually chalao.")
                except Exception as e:
                    print(f"auto-resume notify failed for {admin}: {e}")
        return
    state["resume_limit_notified"] = False
    state["resume_tries"] = tries + 1
    save_state()
    target = lj.get("target") or default_target()
    res = await resolve_channels(target)
    if any(res.values()):
        print(f"auto-resume: channel access nahi mila: {res}")
        return
    print(f"auto-resume ({reason}): ID {lj['last_id'] + 1} -> {lj['end']} (try {tries + 1})")
    try:
        await start_range_job(app, ChatShim(app, ADMINS[0]), lj["last_id"] + 1, lj["end"], target,
                              lj.get("dedupe"), lj.get("media_only", False))
    except Exception as e:      # job shuru hi na ho paye to bhi watchdog dobara koshish karega
        print(f"auto-resume start failed: {type(e).__name__}: {e}")


async def resume_watchdog():
    """Har minute check: adhura kaam pada hai aur bot khali hai => khud aage badhao (user ka /stop chhodkar)."""
    while True:
        await asyncio.sleep(60)
        try:
            lj = state["last_job"]
            if (AUTO_RESUME and ADMINS and not running["busy"] and lj and not lj.get("done")
                    and lj["last_id"] < lj["end"] and not lj.get("user_stopped")):
                await auto_resume(0, "watchdog")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"watchdog error: {type(e).__name__}: {e}")


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
    "▫️ <code>/poster Jawan 2023</code> — TMDB test · kisi video pe reply karke <code>/poster</code> — poster se naam padhna test\n"
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
    state["resume_tries"] = 0
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
    state["resume_tries"] = 0
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


@app.on_message(filters.command("poster") & filters.private & admin_filter)
async def poster_cmd(client: Client, m: Message):
    """Test: /poster Jawan 2023   ya   kisi video pe reply karke /poster"""
    if not TMDB_API_KEY:
        return await m.reply_text("❌ TMDB_API_KEY set nahi hai (themoviedb.org se free key lo).")
    rep_msg = m.reply_to_message
    media = rep_msg and (rep_msg.video or rep_msg.document or rep_msg.audio)
    arg = _arg_text(m)
    if not media and not arg:
        return await m.reply_text("Use: <code>/poster Jawan 2023</code>  ya kisi video pe reply karke <code>/poster</code>",
                                  parse_mode=ParseMode.HTML)
    notes, info = [], None
    try:
        if media:
            old = media.file_name
            cap_line = (rep_msg.caption or "").strip().splitlines()[:1]
            notes.append(f"File naam: {old or '—'}")
            info = await identify_from_text([clean_filename(old) if old else None,
                                             clean_text(cap_line[0]) if cap_line else ""])
            if info:
                notes.append("Match: file naam / caption se")
            else:
                dbg = {}
                info = await identify_from_thumb(client, media, dbg)
                lines = ", ".join(t for t, _ in dbg.get("lines", [])[:8]) or "— (kuch nahi padha)"
                notes.append(f"OCR ({ocr_status()}) ne padha: {lines}")
                notes.append(f"Candidates: {', '.join(dbg.get('cands', [])) or '—'}")
                if not getattr(media, "thumbs", None):
                    notes.append("⚠️ Is video me thumbnail (poster) hi nahi hai")
                if info:
                    notes.append("Match: poster ke text (OCR) se")
        else:
            title, year = guess_title(arg)
            notes.append(f"Search: {title} {year or ''}")
            info = await tmdb_lookup(title, year, arg)
    except Exception as e:
        return await m.reply_text(f"❌ Error: <code>{html.escape(type(e).__name__ + ': ' + str(e))}</code>",
                                  parse_mode=ParseMode.HTML)
    body = "\n".join(html.escape(n) for n in notes)
    if not info:
        return await m.reply_text(f"😕 TMDB me match nahi mila.\n\n{body}", parse_mode=ParseMode.HTML)
    caption = (f"✅ <b>{html.escape(info['title'])}</b> ({info['year'] or '?'}) · match {info['score']:.0%}\n\n{body}")[:1024]
    try:
        bio = BytesIO(await tmdb_poster_by_path(info["poster_path"]))
        bio.name = "poster.jpg"
        await m.reply_photo(bio, caption=caption, parse_mode=ParseMode.HTML)
    except Exception as e:
        await m.reply_text(f"{caption}\n\n⚠️ Poster download fail: {html.escape(str(e))}", parse_mode=ParseMode.HTML)


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
        f"🖼 TMDB key: <code>{'set' if TMDB_API_KEY else 'NOT SET (poster nahi lagega)'}</code>\n"
        f"🔎 Poster OCR: <code>{ocr_status()}</code>"
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
# Pehle har post ka kaam seedha Pyrogram ke handler worker me chalta tha: bahut saari/badi files aane par saare
# workers busy ho jate the aur bot "ruk gaya" lagta tha (commands tak jawab nahi dete). Ab handler sirf queue me
# daalta hai, ek alag worker ek-ek karke (order me) bhejta hai.
auto_queue = None


@app.on_message(filters.chat(SOURCE) & (filters.document | filters.video | filters.audio | filters.photo))
async def auto(client: Client, m: Message):
    if not cfg["auto"] or auto_queue is None:
        return
    auto_queue.put_nowait(m)


async def process_auto(client: Client, m: Message):
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
        state["failed"] = sorted(set(state["failed"]) | {m.id})    # /retryfailed se dobara ho sakta hai
        dirty[0] += 1
        maybe_save(True)
        print(f"auto msg {m.id} failed: {type(e).__name__}: {e}")


async def auto_worker():
    while True:
        m = await auto_queue.get()
        try:
            await process_auto(app, m)
        except asyncio.CancelledError:
            raise
        except Exception as e:      # worker kabhi marna nahi chahiye
            print(f"auto worker error: {type(e).__name__}: {e}")
        finally:
            auto_queue.task_done()


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
    global auto_queue
    load_state()
    rebuild_block_re()
    auto_queue = asyncio.Queue()

    server = web.Application()
    server.add_routes([web.get("/", health)])
    runner = web.AppRunner(server)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    await app.start()
    await resolve_channels()    # peer cache (Peer id invalid fix)
    ka_task = asyncio.create_task(keep_alive())
    aw_task = asyncio.create_task(auto_worker())
    wd_task = asyncio.create_task(resume_watchdog())

    # pichla kaam adhura reh gaya tha (restart/redeploy/crash)
    lj = state["last_job"]
    if lj and not lj.get("done") and lj["last_id"] < lj["end"]:
        if AUTO_RESUME and ADMINS and not lj.get("user_stopped"):
            print(f"pichla kaam adhura tha (ID {lj['last_id'] + 1} -> {lj['end']}): auto-resume hoga")
            asyncio.create_task(auto_resume(5, "startup"))
        elif not lj.get("announced"):
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
    running["shutdown"] = True      # restart ke baad auto-resume ho sake (ye user ka /stop nahi hai)
    if running["busy"]:
        do_stop()
        for _ in range(50):    # max ~25s current file khatam hone ka wait
            if not running["busy"]:
                break
            await asyncio.sleep(0.5)
    ka_task.cancel()
    aw_task.cancel()
    wd_task.cancel()
    save_state()
    await app.stop()
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
