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
    LOOP = asyncio.get_event_loop()
except RuntimeError:
    LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(LOOP)

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
# OWNERS = .env ke ADMINS (hamesha admin, doosre admin ko add/remove kar sakte hain). ADMINS = owners + /addadmin wale.
OWNERS = [int(x) for x in required_env("ADMINS").replace(",", " ").split()]
ADMINS = list(OWNERS)


def _env_chat_id(name: str):
    """Channel ID ab .env me ZARURI NAHI. Agar daali ho to sirf pehle-start ka default hai;
    bot ke andar /settings -> Channels se set kiya hua hamesha usse upar rehta hai."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        print(f"{name} ek number hona chahiye (-100...), ignore kar diya: {raw!r}")
        return None


ENV_SOURCE = _env_chat_id("SOURCE_CHANNEL")             # optional
ENV_TARGET = _env_chat_id("TARGET_CHANNEL")             # optional
ENV_QUALITY = _env_chat_id("QUALITY_TARGET_CHANNEL")    # optional (CAM/TS/TC)
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
STARTUP_NOTIFY = env_bool("STARTUP_NOTIFY", True)                # har start/restart par admin ko message
STATE_FILE = os.environ.get("STATE_FILE", "bot_state.json")      # progress + settings yahan save hote hain
TRANSMISSIONS = int(os.environ.get("TRANSMISSIONS", "8"))        # parallel upload/download streams
PREFETCH = max(1, int(os.environ.get("PREFETCH", "3")))          # kitni files aage se download/prepare hon
LIVE_PANEL = env_bool("LIVE_PANEL", True)                               # True => job ke dauran live progress panel dikhao
FRAME_AT = min(max(float(os.environ.get("FRAME_AT", "0.1")), 0.0), 0.95)   # video ke kitne hisse par frame (0.1 = 10%)
MAX_RETRIES = max(1, int(os.environ.get("MAX_RETRIES", "3")))    # network error pe kitni baar retry
WORKERS = max(1, int(os.environ.get("WORKERS", "3")))            # kitni files ek saath (parallel) download/upload hon (1 = bilkul order me)
QUEUE_SIZE = max(PREFETCH, WORKERS * 2)                          # workers ke liye aage se kitne messages tayyar rakhne hain
SHUTDOWN_WAIT = max(5, int(os.environ.get("SHUTDOWN_WAIT", "20")))     # restart/redeploy pe current files ke liye max wait (sec)
COOLDOWN_AFTER = max(3, int(os.environ.get("COOLDOWN_AFTER", "8")))    # lagataar itni files fail => kaam cancel nahi, thoda ruk kar dobara
MAX_UPLOAD_BYTES = int(float(os.environ.get("MAX_UPLOAD_MB", "1990")) * 1024 * 1024)   # bot upload limit ~2GB
USER_SESSION = os.environ.get("USER_SESSION", "").strip()        # Premium account ka Pyrogram session string (2GB-4GB files ke liye)
USER_MAX_BYTES = int(float(os.environ.get("USER_MAX_MB", "3990")) * 1024 * 1024)   # Premium upload limit ~4GB
SILENT = env_bool("SILENT", True)                                # True => target channel me bulk sending bina notification ke
TG_WORKERS = max(4, int(os.environ.get("TG_WORKERS", "8")))      # Pyrogram ke handler workers (sirf start par lagta hai)
BACKUP_HOURS = max(0, int(os.environ.get("BACKUP_HOURS", "0")))    # har itne ghante me state file owner ko DM (0 = band)
ENV_TMDB_KEY = TMDB_API_KEY                                      # /settings se badla hua key isse upar rehta hai
ENV_OCR_KEY = OCR_SPACE_KEY

# Telegram guideline: ek chat me ~1 message/second se zyada nahi. Isliye delay 1s se kam allowed nahi.
MIN_DELAY = 0.5      # FloodWait aaye to delay khud badh jata hai (safe() dekho), isliye thoda tez rakha
SEND_DELAY = max(float(os.environ.get("SEND_DELAY", "0.5")), MIN_DELAY)
# Live rate-limit state: FloodWait aane par delay khud badhta hai, phir dheere dheere wapas kam hota hai
# FloodWait ke baad delay max itna hi badhega (Telegram ~20 msg/min => ~3s/msg safe hai). Pehle 10s tak jata tha => bahut slow.
FLOOD_MAX_DELAY = max(float(os.environ.get("FLOOD_MAX_DELAY", "3.0")), SEND_DELAY)
rate = {"delay": SEND_DELAY, "floods": 0, "flood_secs": 0.0, "flood_until": 0.0}

# Runtime settings (/settings se on/off). Env sirf starting default hai.
cfg = {
    "clean_caption": env_bool("CLEAN_CAPTION", True),    # caption se @username / t.me links hatao
    "clean_filename": env_bool("CLEAN_FILENAME", True),  # file ke naam se username hatao (re-upload lagega)
    "set_poster": env_bool("SET_POSTER", False),         # video pe TMDB poster thumbnail (abhi OFF - API key nahi hai)
    "dedupe": env_bool("DEDUPE", True),                  # pehle bheji hui file dobara na bhejo
    "auto": env_bool("AUTO_FORWARD", True),              # source me nayi post aate hi auto forward
    "notify": env_bool("NOTIFY", True),                  # kaam khatam hone par alag message (ping)
    "delete_quality": env_bool("DELETE_QUALITY", False), # CAM/HDTS/HDTC file quality channel me jaane ke baad source se delete
    "auto_caption": env_bool("AUTO_CAPTION", False),     # caption khaali ho to TMDB se khud likho (TMDB_API_KEY chahiye)
    "hd_poster": env_bool("HD_POSTER", False),           # movie ke saath TMDB ka bada (HD) poster alag photo me bhi bhejo
    "name_from_poster": env_bool("NAME_FROM_POSTER", False),  # video me naam na ho to poster (thumbnail) se naam padho (OCR)
    "frame_thumb": env_bool("FRAME_THUMB", True),        # re-upload hone wali / bina-thumbnail wali video ko frame thumbnail (ffmpeg chahiye)
    "silent": SILENT,                                    # target me bina notification ke bhejo
    "live_panel": LIVE_PANEL,                            # job ke dauran live progress panel
    "auto_resume": AUTO_RESUME,                          # restart/error ke baad adhura kaam khud resume
    "startup_notify": STARTUP_NOTIFY,                    # start/restart par admin ko message
    "error_alerts": env_bool("ERROR_ALERTS", True),      # auto-forward fail hone par admin ko DM
    "auto_video": True, "auto_document": True,           # auto-forward me kaun si types jayein
    "auto_audio": True, "auto_photo": True,
}
ENV_CFG = dict(cfg)         # "Defaults" button isi par wapas jata hai

# Number wale settings (/settings me ➖/➕). g = global ka naam jo bot use karta hai.
NUM_SPECS = {
    "workers":          dict(g="WORKERS", label="🧵 Workers", type=int, min=1, max=12, step=1,
                             hint="ek saath kitni files download/upload hon (1 = bilkul order me)"),
    "prefetch":         dict(g="PREFETCH", label="📥 Prefetch", type=int, min=1, max=10, step=1,
                             hint="aage se kitni files pehle se tayyar rakhni hain"),
    "send_delay":       dict(g="SEND_DELAY", label="⏱ Send delay", type=float, min=0.5, max=10.0, step=0.5, unit="s",
                             hint="do sends ke beech ka gap (kam = tez, par FloodWait zyada)"),
    "flood_max_delay":  dict(g="FLOOD_MAX_DELAY", label="🛡 Flood max delay", type=float, min=1.0, max=60.0, step=1.0, unit="s",
                             hint="FloodWait ke baad delay iske upar nahi jayega"),
    "max_retries":      dict(g="MAX_RETRIES", label="🔁 Retries", type=int, min=1, max=10, step=1,
                             hint="network/send error par ek file ke liye kitni koshish"),
    "retry_rounds":     dict(g="RETRY_ROUNDS", label="♻️ Retry rounds", type=int, min=0, max=10, step=1,
                             hint="job ke end me failed files itni baar khud dobara (0 = band)"),
    "cooldown_after":   dict(g="COOLDOWN_AFTER", label="🧊 Cooldown after", type=int, min=3, max=30, step=1,
                             hint="lagataar itni files fail => thoda ruk kar dobara"),
    "max_auto_resumes": dict(g="MAX_AUTO_RESUMES", label="🔄 Max auto-resume", type=int, min=0, max=50, step=1,
                             hint="bina progress ke max auto-resume (0 = jab tak kaam poora na ho)"),
    "shutdown_wait":    dict(g="SHUTDOWN_WAIT", label="⏳ Shutdown wait", type=int, min=5, max=120, step=5, unit="s",
                             hint="restart par current files ke liye max wait"),
    "frame_at":         dict(g="FRAME_AT", label="🎞 Frame at", type=float, min=0.0, max=0.95, step=0.05, pct=True,
                             hint="video ke kitne % hisse par se thumbnail frame liya jaye"),
    "ocr_min_score":    dict(g="OCR_MIN_SCORE", label="🔎 OCR match score", type=float, min=0.3, max=1.0, step=0.05, pct=True,
                             hint="poster se padhe naam ka TMDB match kitna strict ho"),
    "backup_hours":     dict(g="BACKUP_HOURS", label="💾 Auto-backup", type=int, min=0, max=168, step=6, unit="h",
                             hint="har itne ghante me state file owner ko DM (0 = band)"),
}
nums = {k: globals()[sp["g"]] for k, sp in NUM_SPECS.items()}
ENV_NUMS = dict(nums)
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
    "frame_thumb": "🎞 Video frame thumbnail (jahan zaroori)",
    "silent": "🔕 Silent send (bina notification)",
    "live_panel": "📊 Live progress panel",
    "auto_resume": "🔄 Auto-resume (restart/error ke baad)",
    "startup_notify": "🟢 Start/restart par message",
    "error_alerts": "🚨 Error alerts (auto-forward fail)",
    "auto_video": "🎬 Auto: videos",
    "auto_document": "📄 Auto: documents",
    "auto_audio": "🎵 Auto: audio",
    "auto_photo": "🖼 Auto: photos",
}

app = Client("cleaner_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN,
             max_concurrent_transmissions=max(TRANSMISSIONS, WORKERS), workers=TG_WORKERS)
# Premium user session: bot 2GB se badi file upload nahi kar sakta, Premium account 4GB tak kar sakta hai.
# Is account ko SOURCE channel ka member aur TARGET channel ka admin (Post Messages) hona chahiye.
user_app = (Client("cleaner_user", api_id=API_ID, api_hash=API_HASH, session_string=USER_SESSION, in_memory=True,
                   max_concurrent_transmissions=max(TRANSMISSIONS, WORKERS), no_updates=True)
            if USER_SESSION else None)
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
    "src_last": 0,       # source me dekhi gayi sabse badi message ID (auto-forward se)
    "source": None,      # /settings -> Channels se chuna source (None => env SOURCE_CHANNEL, agar di ho)
    "quality": None,     # None => env QUALITY_TARGET_CHANNEL | 0 => band | id => CAM/TS/TC channel
    "admins": [],        # /addadmin se jude extra admins (OWNERS alag hain, .env se)
    "domains": [],       # extra link domains jo caption/naam se hatane hain
    "texts": {},         # tmdb_key / ocr_key (bot ke andar se set)
    "last_backup": 0,
    "legacy_target": None,   # purani (env wale) target ki duplicate-memory bina prefix ke hai => us target ko pehchano
}
seen: set = set()        # file_unique_id of already-sent files
dirty = [0]


def get_source():
    """Source/database channel: bot ke andar set kiya hua, warna (optional) env."""
    return state.get("source") or ENV_SOURCE


def default_target():
    """Default destination: bot ke andar set kiya hua, warna (optional) env."""
    return state.get("target") or ENV_TARGET


def get_quality():
    """CAM/TS/TC channel. state['quality'] == 0 => jaan-boojh kar band."""
    q = state.get("quality")
    if q is None:
        return ENV_QUALITY
    return q or None


def seen_key(key, target: int):
    """Duplicate check har destination ke liye alag (env TARGET ke keys pehle jaise hi rehte hain)."""
    if not key:
        return None
    legacy = ENV_TARGET if ENV_TARGET is not None else state.get("legacy_target")
    return key if (legacy is not None and target == legacy) else f"{target}:{key}"


def refresh_admins():
    extra = [a for a in state.get("admins", []) if isinstance(a, int) and a not in OWNERS]
    ADMINS[:] = OWNERS + extra


def clamp_num(key: str, value):
    sp = NUM_SPECS[key]
    v = min(max(float(value), sp["min"]), sp["max"])
    return int(round(v)) if sp["type"] is int else round(v, 2)


def apply_runtime():
    """cfg / nums / texts me jo bhi badla ho use bot ke live variables me lagao (restart ki zarurat nahi)."""
    global QUEUE_SIZE, SILENT, LIVE_PANEL, AUTO_RESUME, STARTUP_NOTIFY, TMDB_API_KEY, OCR_SPACE_KEY, FLOOD_MAX_DELAY
    g = globals()
    for k, sp in NUM_SPECS.items():
        g[sp["g"]] = nums[k]
    FLOOD_MAX_DELAY = max(FLOOD_MAX_DELAY, SEND_DELAY)
    QUEUE_SIZE = max(PREFETCH, WORKERS * 2)
    SILENT, LIVE_PANEL = bool(cfg["silent"]), bool(cfg["live_panel"])
    AUTO_RESUME, STARTUP_NOTIFY = bool(cfg["auto_resume"]), bool(cfg["startup_notify"])
    texts = state.get("texts") or {}
    TMDB_API_KEY = (texts.get("tmdb_key") or ENV_TMDB_KEY or "").strip()
    OCR_SPACE_KEY = (texts.get("ocr_key") or ENV_OCR_KEY or "").strip()
    if not running["busy"]:
        rate["delay"] = SEND_DELAY


def set_num(key: str, value):
    nums[key] = clamp_num(key, value)
    apply_runtime()
    save_state()
    return nums[key]


STATE_FOUND = False      # restart ke baad purani progress file mili ya nahi (Render free par disk nahi hoti)


def apply_state_data(data: dict):
    """State dict (file ya /restore backup) ko live state me lagao."""
    for k in ("blocks", "failed", "footer", "last_job", "target", "source", "quality", "admins", "domains",
              "texts", "resume_tries", "src_last", "last_backup", "legacy_target"):
        if k in data:
            state[k] = data[k]
    if not isinstance(state.get("texts"), dict):
        state["texts"] = {}
    for k in ("blocks", "failed", "admins", "domains"):
        if not isinstance(state.get(k), list):
            state[k] = []
    state["admins"] = [int(a) for a in state["admins"] if str(a).lstrip("-").isdigit()]
    state["totals"].update(data.get("totals", {}))
    seen.update(data.get("seen", []))
    for k, v in (data.get("cfg") or {}).items():
        if k in cfg:
            cfg[k] = bool(v)
    for k, v in (data.get("opts") or {}).items():
        if k in NUM_SPECS:
            try:
                nums[k] = clamp_num(k, v)
            except (TypeError, ValueError):
                pass
    refresh_admins()
    rebuild_block_re()
    rebuild_link_re()
    apply_runtime()


def load_state():
    global STATE_FOUND
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        STATE_FOUND = True
    except FileNotFoundError:
        return
    except Exception as e:
        print(f"state load failed: {e}")
        return
    apply_state_data(data)
    print(f"state loaded: {len(seen)} files remembered")


def snapshot() -> dict:
    data = dict(state)
    data["seen"] = list(seen)
    data["cfg"] = dict(cfg)
    data["opts"] = dict(nums)
    return data


def save_state():
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot(), f)
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
LINK_RE = None


def rebuild_link_re():
    """Hatane wale link domains: mdisk hamesha + env EXTRA_LINK_DOMAINS + /settings se jude domains."""
    global LINK_RE
    extra = sorted({d for d in _EXTRA_DOMAINS + [x.lower() for x in state.get("domains", [])] if d})
    doms = "|".join([r"mdisk\.[a-z]{2,}"] + [re.escape(d) for d in extra])
    LINK_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/\S+|tg://\S+"
                         r"|(?:https?://)?(?:[\w-]+\.)*(?:" + doms + r")(?:/\S*)?", re.I)


rebuild_link_re()
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
    q = get_quality()
    if q is not None and has_quality_tag(msg):
        return q, bool(cfg["delete_quality"])
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


def _err_classes(*names):
    return tuple(getattr(tg_errors, n) for n in names if hasattr(tg_errors, n))


# network / server ki aisi galtiyan jo thodi der baad theek ho jati hain
TRANSIENT = (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError) + _err_classes(
    "InternalServerError", "ServiceUnavailable", "BadGateway", "GatewayTimeout", "Timeout")


def flood_seconds(e) -> int:
    v = getattr(e, "value", None)
    if v is None:
        v = getattr(e, "x", 5)
    return int(v)


def is_permanent(e) -> bool:
    """Permission / invalid-media jaisi galti: dobara try karne se kuch nahi badlega."""
    return isinstance(e, tg_errors.RPCError) and getattr(e, "CODE", 0) in (400, 401, 403, 406)


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
            wait = flood_seconds(e) + 1
            rate["floods"] += 1
            rate["flood_secs"] += wait
            rate["delay"] = min(max(rate["delay"] * 1.5, 2.0), FLOOD_MAX_DELAY)
            # sabhi workers ek saath rukenge (throttle() is time tak naya send shuru nahi karta)
            rate["flood_until"] = max(rate["flood_until"], time.monotonic() + wait)
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
    if message is None:
        return
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
        flood_wait = rate["flood_until"] - time.monotonic()
        if flood_wait > 0:                       # FloodWait chal raha hai => sab ruk jao
            await asyncio.sleep(flood_wait)
        wait = last_send[0] + rate["delay"] - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        last_send[0] = time.monotonic()
        rate["delay"] = max(SEND_DELAY, rate["delay"] * 0.95)   # flood ke baad jaldi normal


chan_cache: dict = {}      # chat_id -> title (settings screen me naam dikhane ke liye)


async def resolve_channels(target: int = None) -> dict:
    """Source aur destination ko resolve karo taaki Pyrogram peer cache me save kar le."""
    results = {}
    for name, cid in (("SOURCE", get_source()), ("TARGET", target or default_target())):
        if not cid:
            results[name] = "set nahi hai (/settings -> 📡 Channels)"
            continue
        try:
            chat = await safe(app.get_chat, cid)
            chan_cache[cid] = chat.title or str(cid)
            print(f"{name} OK: {chat.title} ({chat.id})")
            results[name] = None
            if user_app is not None:      # user session ka peer cache (non-fatal)
                try:
                    await safe(user_app.get_chat, cid)
                except Exception as ue:
                    print(f"USER session ko {name} ({cid}) nahi dikha: {type(ue).__name__}: {ue}")
        except Exception as e:
            print(f"{name} FAILED ({cid}): {e}")
            results[name] = f"{type(e).__name__}: {e}"
    q = get_quality()
    if q:                                  # quality channel optional hai => fail hone par job nahi rukta
        try:
            chat = await safe(app.get_chat, q)
            chan_cache[q] = chat.title or str(q)
        except Exception as e:
            print(f"QUALITY FAILED ({q}): {type(e).__name__}: {e}")
    return results


# ======================================================================
#  PROCESSING PIPELINE
#    prepare()  -> download / poster / caption (parallel, aage se)
#    deliver()  -> Telegram pe bhejna (order me, throttle ke saath)
# ======================================================================
running = {"busy": False, "stop": False, "job": None, "gate": None, "io": {"dl": "", "ul": ""}}
async def _is_admin(_, __, update):
    u = getattr(update, "from_user", None)
    return bool(u and u.id in ADMINS)           # ADMINS live list hai (/addadmin turant lagu)


async def _is_owner(_, __, update):
    u = getattr(update, "from_user", None)
    return bool(u and u.id in OWNERS)


admin_filter = filters.create(_is_admin)
owner_filter = filters.create(_is_owner)

FILE_ATTRS = ("document", "video", "audio", "photo", "voice", "animation", "video_note", "sticker")


def Job(**kw):
    base = dict(kind="skip", text=None, caption=None, msg_id=None, key=None, path=None,
                thumb=None, name=None, tmp=None, send_as="document", meta=dict(), info=None, msg=None, uploader=None)
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
    return Job(kind="copy", msg_id=msg.id, msg=msg, caption=caption, key=key, info=info)


async def _prepare(client: Client, msg: Message, target: int = None, dedupe: bool = None,
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
    q = get_quality()
    if q is not None and target == q:
        if msg.text:
            return Job(kind="text", text=msg.text[:4096])
        return Job(kind="copy", msg_id=msg.id, msg=msg, caption=None, key=key)

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
    # Frame thumbnail tabhi lagta hai jab file waise bhi re-upload ho rahi ho (rename). Warna server-side copy (bahut
    # fast) me video ka original thumbnail waisa hi rehta hai. Sirf jis video me thumbnail hi nahi, uske liye re-upload.
    force_frame = wants_frame and not getattr(media, "thumbs", None)

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

    if not media or not (wants_rename or wants_poster or from_poster_name or force_frame):
        return copy_job(msg, key, generated, info if is_video else None)

    if from_poster_name:
        new = build_filename(info, media, old)                 # naam tha hi nahi => poster wala naam save karo
    elif cfg["clean_filename"] and old:
        new = clean_old
    else:
        new = old
    title_src = new or (info["title"] if info else "") or first_line or "file"

    fsize = getattr(media, "file_size", 0) or 0
    big = fsize > MAX_UPLOAD_BYTES      # bot sirf ~2GB tak upload kar sakta hai
    if big and (user_app is None or fsize > USER_MAX_BYTES):
        why = "Premium USER_SESSION set nahi hai" if user_app is None else "4GB limit se badi hai"
        print(f"msg {msg.id}: file {fsize / 1048576:.0f}MB - {why}, copy ho rahi hai (naam nahi badlega)")
        return copy_job(msg, key, generated, info if is_video else None)

    tmp = tempfile.mkdtemp()
    try:
        poster = os.path.join(tmp, "poster.jpg")
        has_poster = bool(wants_poster and not wants_frame and info and await save_poster(
            info, poster, (getattr(media, "width", 0) or 0, getattr(media, "height", 0) or 0)))

        # naam pehle se saaf hai aur naya poster nahi => re-upload ki zarurat nahi
        if new == old and not has_poster and not force_frame:
            shutil.rmtree(tmp, ignore_errors=True)
            return copy_job(msg, key, generated, info if is_video else None)

        await wait_for_disk(tmp, fsize)
        dl_client, dl_msg, dl_media = client, msg, media
        if big:             # badi file: Premium user session se download + upload
            dl_client = user_app
            dl_msg = await safe(user_app.get_messages, get_source(), msg.id)
            dl_media = dl_msg.document or dl_msg.video or dl_msg.audio
            if dl_media is None:
                raise RuntimeError("user session ko source me ye file nahi dikhi (session account source channel me hai?)")
        dl_name = os.path.join(tmp, local_name(new)) if new else tmp + os.sep
        path = await safe(dl_client.download_media, dl_msg, file_name=dl_name,
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
            thumbs = getattr(dl_media, "thumbs", None)       # file_id har session ke liye alag hota hai
            if thumbs:
                thumb = await safe(dl_client.download_media, thumbs[0].file_id,
                                   file_name=os.path.join(tmp, "thumb.jpg"))

        job = Job(kind="upload", path=path, thumb=thumb, name=new, tmp=tmp, key=key,
                  uploader=dl_client if big else None,
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


inflight: set = set()     # abhi process ho rahi files ke keys (parallel workers me duplicate na jaye)


async def wait_for_disk(path: str, size: int):
    """Parallel workers ki temp files se disk na bhare: jagah banne tak ruko (max ~10 min)."""
    need = int(size * 1.15) + 64 * 1024 * 1024
    for _ in range(120):
        try:
            if shutil.disk_usage(path).free >= need:
                return
        except OSError:
            return
        await asyncio.sleep(5)
    raise RuntimeError("disk me jagah nahi hai (temp files bhari hui hain)")


async def prepare(client: Client, msg: Message, target: int = None, dedupe: bool = None,
                  media_only: bool = False) -> SimpleNamespace:
    """_prepare() + duplicate guard: ek hi file do workers ek saath na bhejein."""
    target = target or default_target()
    dedupe = cfg["dedupe"] if dedupe is None else dedupe
    key = None if (msg.empty or msg.service) else seen_key(file_key(msg), target)
    claimed = False
    if dedupe and key:
        if key in seen or key in inflight:
            return Job(kind="dup")
        inflight.add(key)
        claimed = True
    try:
        job = await _prepare(client, msg, target, dedupe, media_only)
    except BaseException:
        if claimed:
            inflight.discard(key)
        raise
    if claimed and job.kind in ("skip", "dup", "text"):
        inflight.discard(key)
    return job


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
    await safe(client.send_photo, target, bio, caption=cap[:1024], parse_mode=ParseMode.DISABLED,
               disable_notification=SILENT)
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
        await safe(client.send_message, target, job.text, parse_mode=ParseMode.DISABLED,
                   disable_notification=SILENT)
    elif k == "copy":
        extra = dict(disable_notification=SILENT)
        if job.caption is not None:
            # reply_markup=None => original ke buttons (jinme telegram link ho sakte hain) copy nahi hote
            extra.update(caption=job.caption, parse_mode=ParseMode.DISABLED, reply_markup=None)
        if job.msg is not None:
            # message pehle se fetch ho chuka hai => copy_message wala extra get_messages call bach jata hai (fast)
            try:
                await safe(job.msg.copy, target, **extra)
            except tg_errors.RPCError as e:
                if "FILE_REFERENCE" not in str(getattr(e, "ID", "")):
                    raise
                await safe(client.copy_message, target, get_source(), job.msg_id, **extra)   # purana message => dobara fetch
        else:
            await safe(client.copy_message, target, get_source(), job.msg_id, **extra)
    elif k == "upload":
        client = job.uploader or client           # badi file => Premium user session se upload
        common = dict(caption=job.caption, parse_mode=ParseMode.DISABLED, thumb=job.thumb,
                      disable_notification=SILENT,
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
                if attempt >= MAX_RETRIES or is_permanent(e):
                    raise
                wait = 2 ** attempt
                print(f"send fail ({type(e).__name__}: {e}) - {wait}s baad retry {attempt}/{MAX_RETRIES}")
                await asyncio.sleep(wait)
    finally:
        if job is not None:
            cleanup(job)
            inflight.discard(job.key)


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


def stop_confirm_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Haan, Stop karo", callback_data="job:stopyes"),
        InlineKeyboardButton("↩️ Nahi, chalne do", callback_data="job:stopno"),
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
    cool_left = js.cool_until - time.monotonic()
    cool_txt = f"  ·  🧊 <b>Cooldown</b> <code>{fmt_secs(cool_left)}</code>" if cool_left > 0 else ""
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
        f"🧵 <b>Workers</b> <code>{WORKERS}</code>{cool_txt}\n"
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
    """Temporary Telegram/network errors par job ko terminate na hone do; retry until success or stop."""
    attempt = 0
    while not running.get("stop"):
        attempt += 1
        try:
            return await safe(client.get_messages, get_source(), chunk)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Backoff capped at 60s. This prevents a single temporary outage from
            # silently ending a long /sendall operation after the old 20-attempt limit.
            wait = min(5 * attempt, 60)
            print(f"get_messages fail ({type(e).__name__}: {e}) - {wait}s baad retry (attempt {attempt})")
            for _ in range(wait):
                if running.get("stop"):
                    raise StopRequested
                await asyncio.sleep(1)
    raise StopRequested


class ChatShim:
    """Auto-resume ke liye: run_job ko Message jaisa object chahiye (reply_text + chat.id)."""
    def __init__(self, client: Client, chat_id: int):
        self._client = client
        self.chat = SimpleNamespace(id=chat_id)

    async def reply_text(self, text, **kw):
        return await self._client.send_message(self.chat.id, text, **kw)


# Background tasks ka strong reference: asyncio sirf weak reference rakhta hai, isliye bina reference wala task
# beech kaam me garbage-collect hokar "apne aap cancel" ho sakta hai. Har lamba kaam yahin se chalta hai.
bg_tasks: set = set()


def spawn(coro, name: str = "task"):
    t = asyncio.create_task(coro, name=name)
    bg_tasks.add(t)

    def _done(task):
        bg_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            print(f"background task '{name}' crashed: {type(exc).__name__}: {exc}")
    t.add_done_callback(_done)
    return t


def launch_job(client: Client, m, ids, kind: str = "range", target: int = None, dedupe: bool = None,
               media_only: bool = False) -> bool:
    """Job ko background me chalao (command handler turant free ho jata hai).
    busy flag yahin, bina await ke set hota hai => do kaam ek saath shuru nahi ho sakte."""
    if running["busy"] or not len(ids):
        return False
    running.update(busy=True, stop=False)
    spawn(run_job(client, m, ids, kind, target, dedupe, media_only), "job")
    return True


async def run_job(client: Client, m, ids, kind: str = "range", target: int = None, dedupe: bool = None,
                  media_only: bool = False):
    """ids: range ya list of message IDs. kind: 'range' (checkpoint banta hai) ya 'retry'.
    Pipeline: 1 producer (messages fetch) + WORKERS parallel workers (prepare -> download -> upload)."""
    target = target or default_target()
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
        consec_fail=0, cool_until=0.0, cool_level=0, last_cd_notify=0.0, stop_skipped=False, last_error="",
    )
    running["job"] = js
    outcome = "done"
    status = None
    done_pos = set()      # poore ho chuke positions
    wm = [0]              # watermark: shuru se itne IDs lagataar poore ho chuke (resume yahin se hoga)

    label = f"📍 Range: <code>{first}</code> → <code>{last}</code>" if kind == "range" \
        else "🔁 <b>Failed IDs dobara try ho rahe hain</b>"
    resume_hint = ("▶️ Aage badhane ke liye: <code>/resume</code>" if kind == "range"
                   else "▶️ Dobara: <code>/retryfailed</code>")

    # ---------------- helpers ----------------
    def record(pos, mid, res, took=0.0):
        if res == "sent":
            js.sent += 1
            js.last_took = took
            js.consec_fail = 0
            js.cool_level = 0
        elif res == "dup":
            js.dup += 1
        elif res == "skip":
            js.skipped += 1
        else:
            js.failed += 1
            js.failed_ids.append(mid)
            js.consec_fail += 1
        if res != "failed":
            bump(res)                       # failed ka total job ke end me (retry ke baad) judta hai
        js.done += 1
        js.last_id = max(js.last_id, mid)
        js.processed.add(mid)
        tick_now = time.monotonic()
        dt = tick_now - js.last_tick
        js.last_tick = tick_now
        js.ewma = dt if not js.ewma else js.ewma * 0.9 + dt * 0.1
        if pos is not None:
            done_pos.add(pos)
            while wm[0] in done_pos:        # sirf lagataar poori hui files ka checkpoint (beech ki file na chhute)
                done_pos.discard(wm[0])
                wm[0] += 1
            if kind == "range" and state["last_job"]:
                state["last_job"]["last_id"] = max(state["last_job"]["last_id"],
                                                   ids[wm[0] - 1] if wm[0] else first - 1)
        dirty[0] += 1
        maybe_save()

    async def process(pos, mid, msg):
        item_target, delete_source = destination_for(msg, target)
        t0 = time.monotonic()
        try:
            res = await handle(client, prepare(client, msg, item_target, dedupe, media_only), item_target)
            # Source se tabhi delete karo jab destination par sending confirm ho chuki ho.
            if res == "sent" and delete_source:
                try:
                    await safe(client.delete_messages, get_source(), mid)
                    print(f"msg {mid}: quality-tagged file sent to {item_target}; source deleted")
                except Exception as de:     # file ja chuki hai - delete fail hone par 'failed' mat gino
                    print(f"msg {mid}: sent but source delete failed: {type(de).__name__}: {de}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            res = "failed"
            js.last_error = f"{type(e).__name__}: {e}"
            print(f"msg {mid} failed: {js.last_error}")
        if res == "sent":
            print(f"msg {mid} sent in {fmt_secs(time.monotonic() - t0)}")
        record(pos, mid, res, time.monotonic() - t0)

    async def tick():
        if not LIVE_PANEL or status is None:
            return
        if time.monotonic() - js.last_edit >= 8:
            js.last_edit = time.monotonic()
            await safe_edit(status, render_panel("🚀 <b>Forwarding in progress</b>"),
                            job_buttons(bool(js.pause_since)))

    async def nap(seconds: float):
        end = time.monotonic() + seconds
        while time.monotonic() < end and not running["stop"]:
            await asyncio.sleep(min(1.0, max(end - time.monotonic(), 0.0)))

    async def arm_cooldown():
        """Lagataar fail (Telegram/network issue) => kaam CANCEL nahi hota, sab workers thoda ruk kar dobara chalte hain."""
        js.cool_level += 1
        wait = min(30 * 2 ** (js.cool_level - 1), 600)
        js.cool_until = time.monotonic() + wait
        js.consec_fail = 0
        print(f"lagataar {COOLDOWN_AFTER} files fail ({js.last_error}) - {wait}s ruk kar dobara chalaunga (kaam cancel nahi)")
        if time.monotonic() - js.last_cd_notify > 600:
            js.last_cd_notify = time.monotonic()
            try:
                await safe(client.send_message, m.chat.id,
                           f"⚠️ Lagataar kai files fail hui: <code>{html.escape(js.last_error[:200])}</code>\n"
                           f"Kaam <b>cancel nahi hua</b> — {fmt_secs(wait)} ruk kar khud dobara chalega.",
                           parse_mode=ParseMode.HTML)
            except Exception as e:
                print(f"cooldown notify failed: {e}")

    async def worker(q):
        while True:
            item = await q.get()
            try:
                if item is None:
                    return
                await gate.wait()                                   # pause
                while time.monotonic() < js.cool_until and not running["stop"]:
                    await asyncio.sleep(1)                           # cooldown
                if running["stop"]:
                    js.stop_skipped = True
                    continue
                try:
                    await process(*item)
                except asyncio.CancelledError:
                    raise
                except Exception as e:                               # worker kabhi marna nahi chahiye
                    print(f"worker error: {type(e).__name__}: {e}")
                    js.last_error = f"{type(e).__name__}: {e}"
                    record(item[0], item[1], "failed")
                await tick()
                if js.consec_fail >= COOLDOWN_AFTER and time.monotonic() >= js.cool_until:
                    await arm_cooldown()
            finally:
                q.task_done()

    async def pipeline(id_seq, track: bool):
        q = asyncio.Queue(maxsize=QUEUE_SIZE)
        workers = [asyncio.create_task(worker(q)) for _ in range(WORKERS)]
        try:
            n = len(id_seq)
            for i in range(0, n, 100):
                chunk = list(id_seq[i:i + 100])
                msgs = await fetch_chunk(client, chunk)
                for j, (mid, msg) in enumerate(zip(chunk, msgs)):
                    if running["stop"]:
                        js.stop_skipped = True
                        break
                    await q.put((i + j if track else None, mid, msg))
                if running["stop"]:
                    break
            for _ in workers:
                await q.put(None)
            await asyncio.gather(*workers)
        finally:
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        if running["stop"] and js.stop_skipped:
            raise StopRequested

    # ---------------- main ----------------
    try:
        if kind == "range":
            state["last_job"] = {"start": first, "end": last, "last_id": first - 1, "done": False,
                                 "target": target, "dedupe": dedupe, "media_only": media_only}
            save_state()

        try:
            status = await safe(
                m.reply_text,
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
                f"⚙️ <code>{WORKERS}</code> files ek saath parallel chalengi\n\n"
                "<i>Baaki time ka andaza kuch messages ke baad dikhega.</i>",
                parse_mode=ParseMode.HTML, reply_markup=job_buttons(False) if LIVE_PANEL else None,
            )
        except Exception as e:          # status message na jaye to bhi kaam chalta rahe
            print(f"status message send failed: {type(e).__name__}: {e}")
            status = None
        js.status = status

        await pipeline(ids, True)

        # jo files fail hui thi unhe khud dobara bhejo (kuch der ruk kar), taaki sab share hone ke baad hi job khatam ho
        for rnd in range(1, RETRY_ROUNDS + 1):
            if not js.failed_ids:
                break
            retry_ids = list(js.failed_ids)
            print(f"auto-retry round {rnd}/{RETRY_ROUNDS}: {len(retry_ids)} failed files")
            js.failed_ids.clear()
            js.failed -= len(retry_ids)
            js.done -= len(retry_ids)
            await nap(min(15 * rnd, 60))
            if running["stop"]:
                js.stop_skipped = True
                raise StopRequested
            await pipeline(retry_ids, False)
        if LIVE_PANEL:
            await safe_edit(status, render_panel("✅ <b>Forwarding complete</b>"))
    except StopRequested:
        outcome = "stopped"
        if kind == "range" and state["last_job"]:
            # user ne /stop kiya => restart pe auto-resume nahi; bot band hone (SIGTERM) pe roka gaya => auto-resume
            state["last_job"]["user_stopped"] = not running.get("shutdown")
        if LIVE_PANEL:
            hint = "🔄 Restart ke baad khud wahin se resume hoga" if running.get("shutdown") else resume_hint
            await safe_edit(status, render_panel("⛔ <b>Forwarding roki gayi</b>", hint))
    except asyncio.CancelledError:
        outcome = "interrupted"         # process band ho raha hai: done mark NAHI hoga => restart par resume
        raise
    except Exception as e:
        outcome = "error"
        error_text = f"{type(e).__name__}: {e}"
        print(f"run_job crashed: {error_text}")
        await safe_edit(status, render_panel(
            "❌ <b>Forwarding error — job ruk gaya</b>",
            f"<code>{html.escape(error_text[:700])}</code>\\n{resume_hint}"))
        try:
            await safe(client.send_message, m.chat.id,
                       "⚠️ <b>Forwarding job error ki wajah se ruk gaya.</b>\\n"
                       f"<code>{html.escape(error_text[:700])}</code>\\n"
                       "Auto-resume enabled ho to bot dobara koshish karega. "
                       "/status se check karein; zarurat par /resume chalayein.",
                       parse_mode=ParseMode.HTML)
        except Exception as notify_error:
            print(f"job error notification failed: {type(notify_error).__name__}: {notify_error}")
    finally:
        if kind == "range" and state["last_job"]:
            state["last_job"]["done"] = (outcome == "done")
        if js.done > 0:
            state["resume_tries"] = 0     # progress hua => auto-resume ka counter reset
        state["failed"] = sorted((set(state["failed"]) - js.processed) | set(js.failed_ids))
        state["totals"]["failed"] += len(js.failed_ids)
        save_state()
        print(f"{outcome.upper()} — sent {js.sent}, dup {js.dup}, skipped {js.skipped}, failed {js.failed}")
        running.update(busy=False, stop=False, job=None, gate=None)
        running["io"] = {"dl": "", "ul": ""}

    # finish notification (naya message => phone pe ping aata hai)
    if cfg["notify"]:
        try:
            restart = outcome == "stopped" and bool(running.get("shutdown"))
            icon = {"done": "✅", "stopped": "🔄" if restart else "⛔", "error": "❌"}[outcome]
            head = ("🔄 <b>Bot restart ho raha hai</b> — kaam restart ke baad khud wahin se resume hoga"
                    if restart else f"{icon} <b>Job {outcome}</b>")
            await safe(client.send_message, m.chat.id,
                       f"{head}\n"
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
        spawn(auto_resume(30, "error"), "resume")


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
            if (AUTO_RESUME and ADMINS and not running["busy"] and not running.get("shutdown") and lj
                    and not lj.get("done") and lj["last_id"] < lj["end"] and not lj.get("user_stopped")):
                await auto_resume(0, "watchdog")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"watchdog error: {type(e).__name__}: {e}")


async def start_range_job(client: Client, m: Message, start_id: int, end_id: int,
                          target: int = None, dedupe: bool = None, media_only: bool = False):
    if start_id < 1 or end_id < start_id:
        return await m.reply_text("Invalid message ID range. Start ID must be >= 1 and end ID must be >= start ID.")
    if not launch_job(client, m, range(start_id, end_id + 1), "range", target, dedupe, media_only):
        await m.reply_text("Ek kaam pehle se chal raha hai. /stop karo ya wait karo.")


# ======================================================================
#  COMMANDS
# ======================================================================
USAGE = (
    "📖 <b>Commands</b>\n"
    "━━━━━━━━━━━━━━━━━━\n"
    "🛠 <b>Setup (sab bot ke andar)</b>\n"
    "▫️ <code>/settings</code> — saari settings (channels, cleaning, poster, speed, alerts, backup...)\n"
    "▫️ <code>/channels</code> — Source / Target / Quality channel set karo (forward ya ID/@username/link se)\n"
    "▫️ <code>/setsource</code> · <code>/setdest</code> · <code>/setquality</code> — shortcut (<code>reset</code> / <code>off</code> bhi)\n"
    "▫️ <code>/dest</code> — abhi ke channels dekho\n\n"
    "🚀 <b>Forward</b>\n"
    "▫️ <code>/sendall</code> — source ki saari files\n"
    "▫️ <code>/sendall 500</code> — ID 500 se aage ki saari files\n"
    "▫️ <code>/sendall -1001234567890</code> — saari files <b>dusre channel</b> me (ya <code>@channelname</code>)\n"
    "▫️ <code>/sendall -1001234567890 files</code> — sirf files (text skip)\n"
    "▫️ <code>/sendall 500 -1001234567890 force</code> — ID 500 se, us channel me, duplicate check ke bina\n"
    "▫️ <code>/clean 100 200 [channel_id] [force]</code> — sirf ek range\n"
    "▫️ <code>/resume</code> · <code>/retryfailed</code> · <code>/status</code> · <code>/pause</code> · <code>/unpause</code> · <code>/stop</code>\n"
    "▫️ <code>/speed 0.5 4</code> — send delay (sec) aur workers\n\n"
    "🧹 <b>Cleaning</b>\n"
    "▫️ <code>/addblock word</code> · <code>/delblock word</code> · <code>/blocklist</code>\n"
    "▫️ <code>/footer text</code> (<code>/footer off</code>)\n"
    "▫️ <code>/poster Jawan 2023</code> — TMDB test (video pe reply karke bhi)\n\n"
    "💾 <b>Data / Admin</b>\n"
    "▫️ <code>/backup</code> — state file DM me · <code>/restore</code> — backup file pe reply karke\n"
    "▫️ <code>/admins</code> · <code>/addadmin id</code> · <code>/deladmin id</code> (sirf owner)\n"
    "▫️ <code>/stats</code> · <code>/resetseen</code> · <code>/cancel</code>\n\n"
    "<i>Bot ko source channel me admin (Post Messages) aur target me admin (Post Messages) banao.</i>"
)


@app.on_message(filters.command("start") & filters.private)
async def start(_, m: Message):
    hint = ""
    if m.from_user and m.from_user.id in ADMINS and (not get_source() or not default_target()):
        hint = ("\n\n⚠️ <b>Channels abhi set nahi hain.</b> /settings → 📡 Channels me jaake "
                "Source aur Target set karo.")
    await m.reply_text(
        "✨ <b>Channel Cleaner &amp; Forwarder</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Source channel ki files ko saaf karke (caption/file name se @username, links hata kar, "
        "poster laga kar) target channel me bhejta hoon.\n\n"
        f"{USAGE}{hint}",
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

    miss = missing_channels(target)
    if miss:
        return await m.reply_text(miss, parse_mode=ParseMode.HTML, reply_markup=channels_shortcut())
    res = await resolve_channels(target)
    bad = {k: v for k, v in res.items() if v}
    if bad:
        lines = "\n".join(f"{k}: {v}" for k, v in bad.items())
        return await m.reply_text(
            "Channel access nahi mil raha:\n"
            f"{lines}\n\n"
            "Check karo:\n"
            "1. /settings → 📡 Channels me Source/Target sahi set hain (🔍 Access test dabao)\n"
            "2. Wahi bot dono channels me admin hai\n"
            "3. Source me 'Post Messages', destination me bhi 'Post Messages' permission di hai"
        )

    if end_id is None:
        try:
            probe = await safe(client.send_message, get_source(), ".", disable_notification=True)
            end_id = probe.id - 1
            await safe(client.delete_messages, get_source(), probe.id)
        except Exception as e:
            if not state.get("src_last"):
                return await m.reply_text(
                    "Source channel me test message nahi bhej paya.\n"
                    "Bot ko source channel me admin + 'Post Messages' permission do.\n"
                    "Ya end ID khud do: <code>/sendall 1 9000</code>\n"
                    f"Error: {html.escape(type(e).__name__ + ': ' + str(e))}", parse_mode=ParseMode.HTML)
            end_id = state["src_last"]
            await m.reply_text(f"⚠️ Test message nahi bhej paya, isliye last dekhi gayi ID <code>{end_id}</code> tak jaunga.",
                               parse_mode=ParseMode.HTML)
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
    miss = missing_channels(target)
    if miss:
        return await m.reply_text(miss, parse_mode=ParseMode.HTML, reply_markup=channels_shortcut())
    res = await resolve_channels(target)
    if any(res.values()):
        return await m.reply_text("Channel access nahi mil raha. /settings → 📡 Channels → 🔍 Access test se check karo.")
    state["resume_tries"] = 0
    await start_range_job(client, m, lj["last_id"] + 1, lj["end"], target, lj.get("dedupe"), lj.get("media_only", False))


@app.on_message(filters.command("retryfailed") & filters.private & admin_filter)
async def retry_cmd(client: Client, m: Message):
    if running["busy"]:
        return await m.reply_text("Ek kaam pehle se chal raha hai.")
    ids = sorted(set(state["failed"]))
    if not ids:
        return await m.reply_text("✅ Koi failed file pending nahi hai.")
    miss = missing_channels()
    if miss:
        return await m.reply_text(miss, parse_mode=ParseMode.HTML, reply_markup=channels_shortcut())
    res = await resolve_channels()
    if any(res.values()):
        return await m.reply_text("Channel access nahi mil raha. /settings → 📡 Channels → 🔍 Access test se check karo.")
    if not launch_job(client, m, ids, "retry"):
        await m.reply_text("Ek kaam pehle se chal raha hai.")


@app.on_message(filters.command(["dest", "channels"]) & filters.private & admin_filter)
async def dest_cmd(_, m: Message):
    text, kb = render_page("channels")
    await m.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


@app.on_message(filters.command(["setsource", "setdest", "settarget", "setquality"]) & filters.private & admin_filter)
async def setchan_cmd(client: Client, m: Message):
    """/setdest -1001234567890 | /setdest @name | /setdest (phir forward) | /setquality off | /setdest reset"""
    role = {"setsource": "source", "setdest": "target", "settarget": "target",
            "setquality": "quality"}[m.command[0].lower()]
    arg = (m.command[1] if len(m.command) > 1 else "").strip()
    if arg.lower() in ("reset", "off"):
        if running["busy"]:
            return await m.reply_text("Ek kaam chal raha hai. Pehle /stop karo ya khatam hone do.")
        if arg.lower() == "off" and role != "quality":
            return await m.reply_text("Sirf Quality channel band (<code>off</code>) ho sakta hai. Source/Target ke liye "
                                      "<code>reset</code> ya naya channel do.", parse_mode=ParseMode.HTML)
        state[role] = 0 if arg.lower() == "off" else None
        save_state()
        return await m.reply_text(f"✅ {CHANNEL_ROLES[role]['label']}: " +
                                  ("band kar diya." if arg.lower() == "off" else
                                   f"reset (env wala: {chan_label(role_value(role))})."), parse_mode=ParseMode.HTML)
    ref = None
    rep_chat = _fwd_chat(m.reply_to_message) if m.reply_to_message else None
    if rep_chat:
        ref = rep_chat.id
    elif arg:
        ref = parse_chat_ref(arg)
        if ref is None:
            return await m.reply_text("Ye channel ID/@username/link nahi lag raha. Aise do: "
                                      "<code>/setdest -1001234567890</code> ya <code>/setdest @channelname</code>",
                                      parse_mode=ParseMode.HTML)
    if ref is None:
        return await ask_channel(m, m.from_user.id, role)
    await do_assign(client, m, role, ref)


@app.on_message(filters.command("speed") & filters.private & admin_filter)
async def speed_cmd(_, m: Message):
    """/speed -> dekho  |  /speed 0.5 -> send delay  |  /speed 0.5 4 -> delay + workers"""
    args = m.command[1:]
    try:
        if args:
            set_num("send_delay", args[0])
        if len(args) > 1:
            set_num("workers", args[1])
    except ValueError:
        return await m.reply_text("Use: <code>/speed 0.5</code> ya <code>/speed 0.5 4</code> (delay sec, workers)",
                                  parse_mode=ParseMode.HTML)
    await m.reply_text(
        "⚡ <b>Speed</b>\n"
        f"⏱ Send delay: <code>{SEND_DELAY:.2f}s</code> (FloodWait aaye to khud badhta hai, max {FLOOD_MAX_DELAY:.1f}s)\n"
        f"🧵 Workers: <code>{WORKERS}</code> (naye kaam / agle retry round se lagu; 1 = bilkul order me)\n"
        "<i>Kam delay = tez, par FloodWait zyada. Set: /speed 0.5 4  ·  Ya /settings → ⚡ Performance</i>",
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


@app.on_callback_query(filters.regex(r"^job:(pause|unpause|stop|stopyes|stopno)$"))
async def job_cb(_, cq: CallbackQuery):
    if cq.from_user.id not in ADMINS:
        return await cq.answer("Sirf admin.", show_alert=True)
    act = cq.data.split(":")[1]
    js = running.get("job")
    if act == "stop":          # galti se dabne par kaam cancel na ho => pehle confirm
        if not (running["busy"] and js):
            return await cq.answer("Abhi koi kaam nahi chal raha.", show_alert=True)
        await cq.answer("Pakka stop karna hai?")
        return await safe_edit(cq.message, render_panel("⚠️ <b>Stop karna hai? Confirm karo</b>"),
                               stop_confirm_buttons())
    if act == "stopno":
        await cq.answer("👍 Kaam chalu hai")
        if js:
            await safe_edit(cq.message, render_panel("🚀 <b>Forwarding in progress</b>"),
                            job_buttons(bool(js.pause_since)))
        return
    ok = {"pause": do_pause, "unpause": do_unpause, "stopyes": do_stop}[act]()
    if not ok:
        return await cq.answer("Abhi koi kaam nahi chal raha.", show_alert=True)
    await cq.answer({"pause": "⏸ Paused", "unpause": "▶️ Resumed", "stopyes": "⛔ Stopping..."}[act])
    js = running.get("job")
    if js and act != "stopyes":
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


# ======================================================================
#  CHANNELS + SETTINGS  (sab kuch bot ke andar: /settings)
#   - Source / Target / Quality channel ab .env me nahi, yahin se set hote hain
#   - Har feature ka on/off + number (➖/➕) + text (footer, block words, TMDB key...) yahin
# ======================================================================
def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


CHANNEL_ROLES = {
    "source": dict(label="📤 Source (database)"),
    "target": dict(label="📥 Target (destination)"),
    "quality": dict(label="🎬 Quality (CAM/TS/TC)"),
}
pending: dict = {}          # admin user_id -> {"kind", "role", "ts"}  (bot abhi us admin se kuch maang raha hai)


def role_value(role: str):
    return {"source": get_source, "target": default_target, "quality": get_quality}[role]()


def chan_label(cid) -> str:
    if not cid:
        return "—"
    title = chan_cache.get(cid)
    return f"{html.escape(title)} (<code>{cid}</code>)" if title else f"<code>{cid}</code>"


def quality_label() -> str:
    if state.get("quality") == 0:
        return "band (off)"
    return chan_label(get_quality())


def missing_channels(target=None) -> str:
    miss = []
    if not get_source():
        miss.append("📤 Source")
    if not (target or default_target()):
        miss.append("📥 Target")
    if not miss:
        return ""
    return "⚠️ Pehle channel set karo: <b>" + ", ".join(miss) + "</b>\n/settings → 📡 Channels"


def channels_shortcut() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn("📡 Channels set karo", "m:channels")]])


def _fwd_chat(m):
    """Forward kiye gaye message ka original chat (Pyrogram + Pyrofork dono ke liye)."""
    if m is None:
        return None
    c = getattr(m, "forward_from_chat", None)
    if c:
        return c
    fo = getattr(m, "forward_origin", None)
    return getattr(fo, "chat", None) if fo else None


def parse_chat_ref(text: str):
    """-100123.. | 123456789 | t.me/c/123/45 | @name | t.me/name  ->  int ya '@name'. Samajh na aaye to None."""
    t = (text or "").strip().split()
    if not t:
        return None
    t = t[0]
    if re.fullmatch(r"-?\d{5,}", t):
        n = int(t)
        return n if n < 0 else int("-100" + t)          # bina -100 ke diya to channel maan lo
    m = re.fullmatch(r"(?:https?://)?t\.me/c/(\d+)(?:/\S*)?", t, re.I)
    if m:
        return int("-100" + m.group(1))
    m = re.fullmatch(r"(?:https?://)?t\.me/(\+\S+|joinchat/\S+)", t, re.I)
    if m:
        return t if t.startswith("http") else "https://" + t
    m = re.fullmatch(r"(?:https?://)?t\.me/(\w{4,})(?:/\S*)?", t, re.I)
    if m:
        return "@" + m.group(1)
    if re.fullmatch(r"@\w{4,}", t):
        return t
    return None


async def bot_rights(chat_id, chat_type: str = "") -> dict:
    try:
        mem = await safe(app.get_chat_member, chat_id, "me")
    except Exception as e:
        return {"ok": False, "err": type(e).__name__}
    status = str(getattr(mem.status, "name", mem.status)).upper()
    admin = status in ("OWNER", "ADMINISTRATOR")
    pr = getattr(mem, "privileges", None)
    channel = str(chat_type).upper().endswith("CHANNEL")
    can_post = admin and (status == "OWNER" or not channel or bool(getattr(pr, "can_post_messages", False)))
    can_del = admin and (status == "OWNER" or bool(getattr(pr, "can_delete_messages", False)))
    return {"ok": True, "admin": admin, "can_post": can_post, "can_delete": can_del, "status": status}


def rights_line(role: str, r: dict) -> str:
    if not r.get("ok"):
        return f"❌ Bot is chat me nahi dikh raha ({html.escape(str(r.get('err')))}) — pehle bot ko admin banao"
    if not r["admin"]:
        return "❌ Bot <b>admin nahi</b> hai — admin banao (Post Messages ke saath)"
    tick = lambda b: "✅" if b else "❌"
    line = f"Admin ✅ · Post {tick(r['can_post'])} · Delete {tick(r['can_delete'])}"
    if role in ("target", "quality") and not r["can_post"]:
        line += "\n⚠️ Yahan bhejne ke liye <b>Post Messages</b> permission chahiye"
    if role == "source" and cfg["delete_quality"] and not r["can_delete"]:
        line += "\n⚠️ 'Delete quality files' ON hai par <b>Delete Messages</b> permission nahi"
    return line


async def access_report() -> str:
    lines = ["🔍 <b>Access check</b>", "━━━━━━━━━━━━━━━━━━"]
    for role in ("source", "target", "quality"):
        cid = role_value(role)
        label = CHANNEL_ROLES[role]["label"]
        if not cid:
            lines.append(f"{label}\n   {'— (optional, band)' if role == 'quality' else '❌ set nahi hai'}")
            continue
        try:
            chat = await safe(app.get_chat, cid)
            chan_cache[cid] = chat.title or str(cid)
            r = await bot_rights(cid, str(getattr(chat.type, "name", chat.type)))
            lines.append(f"{label}\n   {html.escape(chat.title or '')} <code>{cid}</code>\n   {rights_line(role, r)}")
        except Exception as e:
            lines.append(f"{label}\n   <code>{cid}</code> ❌ {html.escape(type(e).__name__)} — bot ko channel me admin banao")
    if user_app is not None:
        lines.append("\n👤 Premium user session: ✅ (badi files ke liye)")
    return "\n".join(lines)


async def assign_channel(role: str, cid: int, title: str = "") -> str:
    if role == "source" and running["busy"]:
        raise ValueError("Ek kaam chal raha hai — source badalne se pehle /stop karo ya khatam hone do.")
    for r in CHANNEL_ROLES:
        if r != role and role_value(r) == cid:
            raise ValueError(f"Ye channel pehle se {CHANNEL_ROLES[r]['label']} hai. Alag channel chuno.")
    prev = role_value(role)
    state[role] = cid
    if title:
        chan_cache[cid] = title
    notes = []
    if (role == "target" and ENV_TARGET is None and state.get("legacy_target") is None
            and any(":" not in str(k) for k in seen)):
        state["legacy_target"] = cid       # .env se hata di gayi purani target ki memory yahi se jud jati hai
    if role == "source" and prev != cid:
        state["src_last"] = 0
        state["failed"] = []
        state["resume_tries"] = 0
        lj = state.get("last_job")
        if lj and not lj.get("done"):
            notes.append("ℹ️ Purane source ka adhura kaam clear kar diya.")
        state["last_job"] = None
    save_state()
    out = f"✅ {CHANNEL_ROLES[role]['label']} ab: {chan_label(cid)}"
    if notes:
        out += "\n" + "\n".join(notes)
    return out


async def do_assign(client: Client, m: Message, role: str, ref) -> bool:
    try:
        chat = await safe(client.get_chat, ref)
    except Exception as e:
        await m.reply_text(
            f"❌ Channel nahi mila ya access nahi: <code>{html.escape(type(e).__name__)}</code>\n\n"
            "• Bot ko pehle us channel me <b>admin</b> banao\n"
            "• Sirf ID kaam na kare to us channel ka <b>koi message yahan forward</b> kar do",
            parse_mode=ParseMode.HTML)
        return False
    ctype = str(getattr(chat.type, "name", chat.type)).upper()
    if ctype not in ("CHANNEL", "SUPERGROUP", "GROUP"):
        await m.reply_text("❌ Ye channel/group nahi hai. Channel ka ID/@username/link do.")
        return False
    rights = await bot_rights(chat.id, ctype)
    try:
        info = await assign_channel(role, chat.id, chat.title or "")
    except ValueError as e:
        await m.reply_text(f"⚠️ {html.escape(str(e))}", parse_mode=ParseMode.HTML)
        return False
    await m.reply_text(f"{info}\n{rights_line(role, rights)}", parse_mode=ParseMode.HTML,
                       reply_markup=InlineKeyboardMarkup([[_btn("📡 Channels", "m:channels"),
                                                           _btn("⚙️ Settings", "m:main")]]))
    return True


async def ask_channel(m: Message, uid: int, role: str):
    pending[uid] = {"kind": "chan", "role": role, "ts": time.time()}
    extra = "\n\n<i>Quality optional hai: CAM/TS/TC-tagged files is channel me jayengi.</i>" if role == "quality" else ""
    await m.reply_text(
        f"{CHANNEL_ROLES[role]['label']} <b>set karo</b>\n\n"
        "1️⃣ Us channel ka <b>koi bhi message yahan forward karo</b> (sabse aasan), ya\n"
        "2️⃣ Channel ka ID (<code>-100...</code>), <code>@username</code> ya <code>t.me/...</code> link bhejo.\n\n"
        "⚠️ Bot ko pehle us channel me <b>admin</b> banao (Post Messages ke saath)."
        f"{extra}\n\n<i>/cancel se wapas.</i>",
        parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[_btn("❌ Cancel", "c:cancel")]]))


# ---------- alerts / backup helpers ----------
alert_last: dict = {}


async def notify_admins(text: str, key: str = "gen", every: float = 120.0):
    """Admins ko DM (same key ka alert 'every' sec me ek hi baar, spam nahi)."""
    if not cfg.get("error_alerts"):
        return
    now = time.monotonic()
    if key in alert_last and now - alert_last[key] < every:
        return
    alert_last[key] = now
    for a in list(ADMINS):
        try:
            await app.send_message(a, text, parse_mode=ParseMode.HTML)
        except Exception as e:
            print(f"alert to {a} failed: {type(e).__name__}: {e}")


async def setup_nag():
    miss = [CHANNEL_ROLES[r]["label"] for r, v in (("source", get_source()), ("target", default_target())) if not v]
    if not miss:
        return
    for a in list(ADMINS):
        try:
            await app.send_message(
                a, "🛠 <b>Setup baaki hai</b>\n" + "\n".join(f"❌ {x}" for x in miss) +
                "\n\nBot ko apne channels me admin banao, phir neeche button dabao — channel ka message forward karna "
                "ya ID bhejna kaafi hai. .env me kuch dalne ki zarurat nahi.",
                parse_mode=ParseMode.HTML, reply_markup=channels_shortcut())
        except Exception as e:
            print(f"setup notify failed for {a}: {type(e).__name__}: {e}")


async def send_backup(chat_id, caption: str = "💾 Backup"):
    save_state()
    bio = BytesIO(json.dumps(snapshot()).encode("utf-8"))
    bio.name = time.strftime("cleaner_backup_%Y%m%d_%H%M.json")
    await app.send_document(chat_id, bio, caption=f"{caption}\n\nRestore: is file par reply karke /restore\n"
                            "⚠️ Isme API keys ho sakti hain — kisi ko forward mat karna.", disable_notification=True)


async def backup_loop():
    while True:
        await asyncio.sleep(600)
        try:
            if BACKUP_HOURS <= 0 or not (seen or get_source()):
                continue
            if time.time() - state.get("last_backup", 0) < BACKUP_HOURS * 3600:
                continue
            state["last_backup"] = time.time()
            for owner in OWNERS:
                await send_backup(owner, "🕒 Auto-backup")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"backup error: {type(e).__name__}: {e}")


# ---------- settings pages ----------
PAGES = {
    "clean": dict(title="🧹 Cleaning", intro="Caption aur file naam se kya hatana hai.",
                  bools=["clean_caption", "clean_filename", "dedupe"], nums=[], prompts=["footer", "blocks", "domains"]),
    "media": dict(title="🎬 Media & Poster",
                  intro="TMDB poster, caption, OCR aur thumbnail. Poster/caption features ke liye TMDB key chahiye.",
                  bools=["set_poster", "hd_poster", "auto_caption", "name_from_poster", "frame_thumb"],
                  nums=["frame_at", "ocr_min_score"], prompts=["tmdb_key", "ocr_key"]),
    "forward": dict(title="📤 Forwarding", intro="Source ki nayi posts kaise aur kaun si jayein.",
                    bools=["auto", "auto_video", "auto_document", "auto_audio", "auto_photo", "silent", "delete_quality"],
                    nums=[], prompts=[]),
    "perf": dict(title="⚡ Performance", intro="Speed vs FloodWait ka balance.",
                 bools=["live_panel"], nums=["workers", "prefetch", "send_delay", "flood_max_delay"], prompts=[]),
    "reliab": dict(title="🔁 Reliability", intro="Error, restart aur retry ka behaviour.",
                   bools=["auto_resume"],
                   nums=["max_retries", "retry_rounds", "cooldown_after", "max_auto_resumes", "shutdown_wait"], prompts=[]),
    "alerts": dict(title="🔔 Alerts & Backup", intro="Kab message aaye aur state ka backup.",
                   bools=["notify", "startup_notify", "error_alerts"], nums=["backup_hours"], prompts=[]),
}
PAGES_ALL = set(PAGES) | {"main", "channels", "system"}
ZERO_TXT = {"retry_rounds": "off", "backup_hours": "off", "max_auto_resumes": "∞"}

PROMPTS = {
    "footer": ("📝 Footer", "Har caption ke neeche jaane wala text bhejo.\nHatane ke liye <code>off</code> bhejo."),
    "blocks": ("🚫 Block words",
               "Jo words/@usernames caption aur file naam se hatane hain, comma se alag karke bhejo.\n"
               "Hatane ke liye <code>-word</code> · list ke liye <code>list</code>."),
    "domains": ("🔗 Extra link domains",
                "Jin sites ke links hatane hain (mdisk pehle se hai), comma se alag: <code>site1.com, site2.net</code>\n"
                "Hatane ke liye <code>-site1.com</code> · list ke liye <code>list</code>."),
    "tmdb_key": ("🔑 TMDB key", "themoviedb.org ki v3 API key ya v4 token bhejo.\nHatane ke liye <code>off</code>. "
                                "(Tumhara message mai delete kar dunga.)"),
    "ocr_key": ("🔑 OCR.space key", "ocr.space ki free key bhejo (Tesseract na ho tab kaam aati hai).\n"
                                    "Hatane ke liye <code>off</code>. (Tumhara message mai delete kar dunga.)"),
}


def prompt_status(kind: str) -> str:
    if kind == "footer":
        return "set ✅" if state["footer"] else "—"
    if kind == "blocks":
        return str(len(set(BLOCK_WORDS + state["blocks"])))
    if kind == "domains":
        return str(len(set(_EXTRA_DOMAINS + state.get("domains", []))))
    if kind == "tmdb_key":
        return "set ✅" if TMDB_API_KEY else "❌"
    if kind == "ocr_key":
        return "set ✅" if OCR_SPACE_KEY else "❌"
    return ""


def fmt_num(k: str) -> str:
    sp, v = NUM_SPECS[k], nums[k]
    if v == 0 and k in ZERO_TXT:
        return ZERO_TXT[k]
    if sp.get("pct"):
        return f"{v * 100:.0f}%"
    return f"{v:g}{sp.get('unit', '')}"


def main_text() -> str:
    on = lambda k: "✅" if cfg[k] else "❌"
    warn = "\n⚠️ <b>Pehle 📡 Channels set karo.</b>\n" if not (get_source() and default_target()) else ""
    return (
        "⚙️ <b>Settings</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"📤 Source: {chan_label(get_source()) if get_source() else '❌ set nahi'}\n"
        f"📥 Target: {chan_label(default_target()) if default_target() else '❌ set nahi'}\n"
        f"🎬 Quality: {quality_label()}\n{warn}\n"
        f"📡 Auto {on('auto')}  🧹 Caption {on('clean_caption')}  ✂️ Naam {on('clean_filename')}  ♻️ Dedupe {on('dedupe')}\n"
        f"⚡ Workers <code>{WORKERS}</code> · Delay <code>{SEND_DELAY:g}s</code>\n"
        f"🖼 TMDB {'✅' if TMDB_API_KEY else '❌'}  📝 Footer {'✅' if state['footer'] else '—'}\n\n"
        "Section chuno. Sab turant lagu hota hai aur restart ke baad bhi yaad rehta hai."
    )


def main_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn("📡 Channels", "m:channels")],
        [_btn("🧹 Cleaning", "m:clean"), _btn("🎬 Media & Poster", "m:media")],
        [_btn("📤 Forwarding", "m:forward"), _btn("⚡ Performance", "m:perf")],
        [_btn("🔁 Reliability", "m:reliab"), _btn("🔔 Alerts & Backup", "m:alerts")],
        [_btn("🛠 System", "m:system"), _btn("✖️ Close", "m:close")],
    ])


def channels_text() -> str:
    return (
        "📡 <b>Channels</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"📤 <b>Source</b> — jahan se files aati hain\n   {chan_label(get_source()) if get_source() else '❌ set nahi'}\n"
        f"📥 <b>Target</b> — jahan bhejni hain\n   {chan_label(default_target()) if default_target() else '❌ set nahi'}\n"
        f"🎬 <b>Quality</b> — CAM/TS/TC alag channel (optional)\n   {quality_label()}\n\n"
        "Button dabao → channel ka <b>koi message forward karo</b> ya ID/@username/link bhejo.\n"
        "Bot ko pehle us channel me admin banana hoga. Naye channel me bot ko admin banaoge to mai khud poochh lunga."
    )


def channels_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn("📤 Source set", "c:source"), _btn("📥 Target set", "c:target")],
        [_btn("🎬 Quality set", "c:quality"), _btn("🚫 Quality off", "c:qoff")],
        [_btn("🔍 Access test", "x:test")],
        [_btn("⬅️ Back", "m:main")],
    ])


def system_text() -> str:
    try:
        fsize = os.path.getsize(STATE_FILE) / 1024
    except OSError:
        fsize = 0
    owners = "\n".join(f"   👑 <code>{a}</code>" for a in OWNERS)
    extra = "\n".join(f"   👤 <code>{a}</code>" for a in ADMINS if a not in OWNERS)
    try:
        ocr = ocr_status()
    except Exception:
        ocr = "?"
    return (
        "🛠 <b>System</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"👥 Admins:\n{owners}{chr(10) + extra if extra else ''}\n"
        f"⏳ Uptime <code>{fmt_secs(time.time() - START_TIME)}</code>\n"
        f"🧠 Yaad rakhi files <code>{len(seen)}</code> · 💾 State <code>{fsize:.0f} KB</code>\n"
        f"🔎 Poster OCR: <code>{html.escape(str(ocr))}</code>\n"
        f"👤 Premium session: <code>{'on' if user_app is not None else 'off'}</code>\n"
        f"🕒 Last backup: <code>{time.strftime('%d %b %H:%M', time.localtime(state['last_backup'])) if state.get('last_backup') else '—'}</code>\n\n"
        "<i>Render free par disk restart pe saaf hoti hai — 💾 Backup le lo ya Alerts me auto-backup chalu karo.</i>"
    )


def system_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [_btn("💾 Backup bhejo", "x:backup"), _btn("🔍 Access test", "x:test")],
        [_btn("⬅️ Back", "m:main")],
    ])


def render_page(page: str):
    if page == "main":
        return main_text(), main_markup()
    if page == "channels":
        return channels_text(), channels_markup()
    if page == "system":
        return system_text(), system_markup()
    sp = PAGES[page]
    lines = [f"{sp['title']}\n━━━━━━━━━━━━━━━━━━\n{sp['intro']}"]
    for k in sp["nums"]:
        lines.append(f"▫️ <b>{NUM_SPECS[k]['label']}</b> — {NUM_SPECS[k]['hint']}")
    for kind in sp["prompts"]:
        lines.append(f"▫️ <b>{PROMPTS[kind][0]}</b>: <code>{prompt_status(kind)}</code>")
    rows = []
    for k in sp["bools"]:
        rows.append([_btn(f"{'✅' if cfg[k] else '❌'} {SETTING_LABELS[k]}", f"t:{k}:{page}")])
    for k in sp["nums"]:
        rows.append([_btn("➖", f"n:{k}:-:{page}"), _btn(f"{NUM_SPECS[k]['label']}: {fmt_num(k)}", f"n:{k}:i:{page}"),
                     _btn("➕", f"n:{k}:+:{page}")])
    for kind in sp["prompts"]:
        rows.append([_btn(f"{PROMPTS[kind][0]}: {prompt_status(kind)}", f"p:{kind}")])
    rows.append([_btn("↩️ Defaults", f"d:{page}"), _btn("⬅️ Back", "m:main")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def show_page(cq: CallbackQuery, page: str):
    text, kb = render_page(page)
    try:
        await cq.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except MessageNotModified:
        pass


@app.on_message(filters.command(["settings", "config"]) & filters.private & admin_filter)
async def settings_cmd(_, m: Message):
    text, kb = render_page("main")
    await m.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


@app.on_callback_query(filters.regex(r"^(?:set|t|n|d|m|c|p|x|aj):"))
async def settings_cb(client: Client, cq: CallbackQuery):
    if cq.from_user.id not in ADMINS:
        return await cq.answer("Sirf admin.", show_alert=True)
    parts = cq.data.split(":")
    act = parts[0]
    uid = cq.from_user.id

    if act == "m":
        page = parts[1] if len(parts) > 1 else "main"
        await cq.answer()
        if page == "close":
            try:
                await cq.message.delete()
            except Exception:
                pass
            return
        return await show_page(cq, page if page in PAGES_ALL else "main")

    if act in ("set", "t"):                         # "set:" purane settings message ke liye
        key = parts[1]
        page = parts[2] if len(parts) > 2 and parts[2] in PAGES_ALL else "main"
        if key not in cfg:
            return await cq.answer("Unknown setting.")
        cfg[key] = not cfg[key]
        apply_runtime()
        save_state()
        await cq.answer(f"{SETTING_LABELS.get(key, key)}: {'ON' if cfg[key] else 'OFF'}")
        return await show_page(cq, page)

    if act == "n":
        key, op = parts[1], parts[2]
        page = parts[3] if len(parts) > 3 and parts[3] in PAGES_ALL else "main"
        if key not in NUM_SPECS:
            return await cq.answer("Unknown setting.")
        sp = NUM_SPECS[key]
        if op == "i":
            return await cq.answer(f"{sp['label']}: {sp['hint']}", show_alert=True)
        old = nums[key]
        new = set_num(key, old + (sp["step"] if op == "+" else -sp["step"]))
        if new == old:
            return await cq.answer("Limit tak pahunch gaye.")
        await cq.answer(f"{sp['label']}: {fmt_num(key)}")
        return await show_page(cq, page)

    if act == "d":
        page = parts[1] if len(parts) > 1 else "main"
        if page not in PAGES:
            return await cq.answer()
        for k in PAGES[page]["bools"]:
            cfg[k] = ENV_CFG[k]
        for k in PAGES[page]["nums"]:
            nums[k] = ENV_NUMS[k]
        apply_runtime()
        save_state()
        await cq.answer("↩️ Defaults par wapas")
        return await show_page(cq, page)

    if act == "c":
        what = parts[1] if len(parts) > 1 else ""
        if what == "cancel":
            pending.pop(uid, None)
            await cq.answer("Cancel")
            try:
                await cq.message.delete()
            except Exception:
                pass
            return
        if what == "qoff":
            state["quality"] = 0
            save_state()
            await cq.answer("Quality channel band")
            return await show_page(cq, "channels")
        if what in CHANNEL_ROLES:
            await cq.answer()
            return await ask_channel(cq.message, uid, what)
        return await cq.answer()

    if act == "p":
        kind = parts[1] if len(parts) > 1 else ""
        if kind not in PROMPTS:
            return await cq.answer()
        pending[uid] = {"kind": kind, "ts": time.time()}
        await cq.answer()
        title, help_text = PROMPTS[kind]
        return await cq.message.reply_text(
            f"<b>{title}</b>\n\n{help_text}\n\n<i>/cancel se wapas.</i>", parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[_btn("❌ Cancel", "c:cancel")]]))

    if act == "x":
        what = parts[1] if len(parts) > 1 else ""
        if what == "test":
            await cq.answer("Check kar raha hu...")
            report = await access_report()
            try:
                await cq.message.edit_text(report, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(
                    [[_btn("🔄 Dobara", "x:test"), _btn("⬅️ Back", "m:channels")]]))
            except MessageNotModified:
                pass
            return
        if what == "backup":
            await cq.answer("Backup bhej raha hu...")
            return await send_backup(cq.message.chat.id)
        return await cq.answer()

    if act == "aj":                                  # bot kisi channel me admin bana => role chuno
        role = parts[1] if len(parts) > 1 else ""
        if role == "no":
            await cq.answer("Theek hai")
            try:
                await cq.message.delete()
            except Exception:
                pass
            return
        if role not in CHANNEL_ROLES or len(parts) < 3:
            return await cq.answer()
        cid = int(parts[2])
        try:
            chat = await safe(client.get_chat, cid)
            info = await assign_channel(role, cid, chat.title or "")
        except ValueError as e:
            return await cq.answer(str(e), show_alert=True)
        except Exception as e:
            return await cq.answer(f"Error: {type(e).__name__}", show_alert=True)
        await cq.answer("Set ho gaya")
        try:
            await cq.message.edit_text(info, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(
                [[_btn("📡 Channels", "m:channels")]]))
        except MessageNotModified:
            pass


# ---------- text input (bot ne kuch maanga tha: channel / footer / block words / API key) ----------
async def _has_pending(_, __, m):
    u = getattr(m, "from_user", None)
    if not u or u.id not in pending:
        return False
    if time.time() - pending[u.id]["ts"] > 600:         # 10 min baad bhool jao
        pending.pop(u.id, None)
        return False
    return not (m.text or "").startswith("/")


has_pending = filters.create(_has_pending)


@app.on_message(filters.private & admin_filter & filters.regex(r"^/\w+"), group=-2)
async def _drop_pending_on_command(_, m: Message):
    cmd = (m.text or "").split()[0].lstrip("/").split("@")[0].lower()
    if cmd != "cancel":
        pending.pop(m.from_user.id, None)


@app.on_message(filters.command("cancel") & filters.private & admin_filter)
async def cancel_cmd(_, m: Message):
    if pending.pop(m.from_user.id, None):
        await m.reply_text("❌ Cancel kar diya.")
    else:
        await m.reply_text("Abhi kuch pending nahi hai.")


def _norm_domain(w: str) -> str:
    return re.sub(r"^(?:https?://)?(?:www\.)?", "", w.strip().lower()).split("/")[0]


async def handle_text_prompt(m: Message, kind: str):
    text = (m.text or "").strip()
    back = InlineKeyboardMarkup([[_btn("⬅️ Settings", "m:main")]])
    if kind == "footer":
        state["footer"] = "" if text.lower() == "off" else text[:200]
        save_state()
        return await m.reply_text("🗑 Footer hata diya." if not state["footer"] else "✅ Footer set ho gaya.",
                                  reply_markup=back)
    if kind in ("blocks", "domains"):
        key = kind
        if text.lower() == "list":
            env_part = BLOCK_WORDS if kind == "blocks" else _EXTRA_DOMAINS
            return await m.reply_text(f"<b>{PROMPTS[kind][0]}</b>\nEnv: <code>{html.escape(', '.join(env_part) or '—')}</code>\n"
                                      f"Bot me: <code>{html.escape(', '.join(state[key]) or '—')}</code>",
                                      parse_mode=ParseMode.HTML, reply_markup=back)
        added, removed = [], []
        for raw in [x.strip() for x in text.split(",") if x.strip()]:
            neg = raw.startswith("-")
            w = raw[1:] if neg else raw
            w = _norm_domain(w) if kind == "domains" else w.strip().lstrip("@")
            if not w:
                continue
            if neg and w in state[key]:
                state[key].remove(w)
                removed.append(w)
            elif not neg and w not in state[key]:
                state[key].append(w)
                added.append(w)
        rebuild_block_re()
        rebuild_link_re()
        save_state()
        return await m.reply_text(
            f"✅ {PROMPTS[kind][0]}\n➕ {', '.join(added) or '—'}\n🗑 {', '.join(removed) or '—'}\n"
            f"Total: {prompt_status(kind)}", reply_markup=back)
    if kind in ("tmdb_key", "ocr_key"):
        if text.lower() == "off":
            state["texts"].pop(kind, None)
        elif text:
            state["texts"][kind] = text
        apply_runtime()
        save_state()
        try:
            await m.delete()                  # key chat me na rahe
        except Exception:
            pass
        return await m.reply_text(f"✅ {PROMPTS[kind][0]}: {prompt_status(kind)} (tumhara message delete kar diya)",
                                  reply_markup=back)


@app.on_message(filters.private & admin_filter & has_pending, group=-1)
async def pending_input(client: Client, m: Message):
    uid = m.from_user.id
    p = pending.get(uid)
    if not p:
        return
    if p["kind"] == "chan":
        chat = _fwd_chat(m)
        ref = chat.id if chat else parse_chat_ref(m.text or "")
        if ref is None:
            return await m.reply_text("Samajh nahi aaya. Channel ka message forward karo, ya ID "
                                      "(<code>-100...</code>) / <code>@username</code> / link bhejo. /cancel se wapas.",
                                      parse_mode=ParseMode.HTML)
        if await do_assign(client, m, p["role"], ref):
            pending.pop(uid, None)
        return
    pending.pop(uid, None)
    await handle_text_prompt(m, p["kind"])


# ---------- bot ko kisi channel me admin banaya => admin se poochho ki kaunsa role dena hai ----------
if hasattr(app, "on_chat_member_updated"):
    @app.on_chat_member_updated()
    async def bot_added(client: Client, upd):
        try:
            new = upd.new_chat_member
            if not new or not getattr(new, "user", None) or not new.user.is_self:
                return
            if str(getattr(new.status, "name", new.status)).upper() not in ("ADMINISTRATOR", "OWNER"):
                return
            old = upd.old_chat_member
            if old and str(getattr(old.status, "name", old.status)).upper() in ("ADMINISTRATOR", "OWNER"):
                return                                    # pehle se admin tha, sirf permission badli
            chat = upd.chat
            if chat is None or str(getattr(chat.type, "name", chat.type)).upper() not in ("CHANNEL", "SUPERGROUP", "GROUP"):
                return
            cid = chat.id
            if cid in (get_source(), default_target(), get_quality()):
                return
            chan_cache[cid] = chat.title or str(cid)
            kb = InlineKeyboardMarkup([
                [_btn("📤 Source banao", f"aj:source:{cid}"), _btn("📥 Target banao", f"aj:target:{cid}")],
                [_btn("🎬 Quality banao", f"aj:quality:{cid}"), _btn("🙅 Ignore", "aj:no:0")],
            ])
            for a in list(ADMINS):
                try:
                    await client.send_message(
                        a, f"🆕 Bot ko <b>{html.escape(chat.title or '')}</b> (<code>{cid}</code>) me admin banaya gaya.\n"
                           "Is channel ko kya banana hai?", parse_mode=ParseMode.HTML, reply_markup=kb)
                except Exception as e:
                    print(f"channel-added notify failed for {a}: {type(e).__name__}: {e}")
        except Exception as e:
            print(f"chat member update error: {type(e).__name__}: {e}")


# ---------- admins ----------
@app.on_message(filters.command("admins") & filters.private & admin_filter)
async def admins_cmd(_, m: Message):
    lines = [f"👑 <code>{a}</code> (owner)" for a in OWNERS] + [f"👤 <code>{a}</code>" for a in ADMINS if a not in OWNERS]
    await m.reply_text("👥 <b>Admins</b>\n" + "\n".join(lines) +
                       "\n\nOwner add/remove: <code>/addadmin 123456789</code> · <code>/deladmin 123456789</code>",
                       parse_mode=ParseMode.HTML)


@app.on_message(filters.command(["addadmin", "deladmin"]) & filters.private & owner_filter)
async def admin_manage_cmd(_, m: Message):
    arg = m.command[1] if len(m.command) > 1 else ""
    if not re.fullmatch(r"\d{5,}", arg):
        return await m.reply_text("Use: <code>/addadmin 123456789</code> (Telegram numeric user ID)", parse_mode=ParseMode.HTML)
    uid = int(arg)
    if m.command[0].lower() == "addadmin":
        if uid not in ADMINS:
            state["admins"].append(uid)
        text = f"✅ <code>{uid}</code> ab admin hai."
    else:
        if uid in OWNERS:
            return await m.reply_text("Owner ko hata nahi sakte (wo .env ADMINS me hai).")
        state["admins"] = [a for a in state["admins"] if a != uid]
        text = f"🗑 <code>{uid}</code> admin nahi raha."
    refresh_admins()
    save_state()
    await m.reply_text(text, parse_mode=ParseMode.HTML)


# ---------- backup / restore ----------
@app.on_message(filters.command("backup") & filters.private & admin_filter)
async def backup_cmd(_, m: Message):
    await send_backup(m.chat.id)


@app.on_message(filters.command("restore") & filters.private & owner_filter)
async def restore_cmd(client: Client, m: Message):
    if running["busy"]:
        return await m.reply_text("Ek kaam chal raha hai. Pehle /stop karo ya khatam hone do.")
    rm = m.reply_to_message
    if not rm or not rm.document:
        return await m.reply_text("Backup file (<code>cleaner_backup_....json</code>) par <b>reply</b> karke "
                                  "<code>/restore</code> bhejo.", parse_mode=ParseMode.HTML)
    if (rm.document.file_size or 0) > 60 * 1024 * 1024:
        return await m.reply_text("File bahut badi hai, ye backup nahi lagti.")
    try:
        bio = await rm.download(in_memory=True)
        data = json.loads(bio.getvalue().decode("utf-8"))
        if not isinstance(data, dict) or not ({"cfg", "seen", "source", "target", "totals"} & set(data)):
            raise ValueError("ye bot ka backup nahi lag raha")
    except Exception as e:
        return await m.reply_text(f"❌ Restore nahi hua: <code>{html.escape(type(e).__name__ + ': ' + str(e))}</code>",
                                  parse_mode=ParseMode.HTML)
    apply_state_data(data)
    save_state()
    await resolve_channels()
    await m.reply_text(
        "✅ <b>Restore ho gaya</b>\n"
        f"📤 Source: {chan_label(get_source()) if get_source() else '—'}\n"
        f"📥 Target: {chan_label(default_target()) if default_target() else '—'}\n"
        f"🧠 Yaad rakhi files: <code>{len(seen)}</code>\n"
        f"🔁 Pending failed: <code>{len(state['failed'])}</code>\n"
        "Adhura kaam ho to /resume.", parse_mode=ParseMode.HTML)


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


async def _from_source(_, __, m):
    s = get_source()                      # dynamic: /settings se source badalte hi naya channel sunna shuru
    return bool(s and getattr(m, "chat", None) and m.chat.id == s)


from_source = filters.create(_from_source)


def _auto_type_ok(m: Message) -> bool:
    if m.video:
        return cfg["auto_video"]
    if m.document:
        return cfg["auto_document"]
    if m.audio:
        return cfg["auto_audio"]
    if m.photo:
        return cfg["auto_photo"]
    return True


@app.on_message(from_source & (filters.document | filters.video | filters.audio | filters.photo))
async def auto(client: Client, m: Message):
    if m.id > state.get("src_last", 0):
        state["src_last"] = m.id
        dirty[0] += 1
    if not cfg["auto"] or auto_queue is None or not default_target() or not _auto_type_ok(m):
        return
    auto_queue.put_nowait(m)


async def process_auto(client: Client, m: Message):
    try:
        target, delete_source = destination_for(m, default_target())
        res = await handle(client, prepare(client, m, target), target)
        if res == "sent" and delete_source:
            await safe(client.delete_messages, get_source(), m.id)
            print(f"auto msg {m.id}: quality-tagged file sent to {target}; source deleted")
        bump(res)
        maybe_save()
    except Exception as e:
        bump("failed")
        state["failed"] = sorted(set(state["failed"]) | {m.id})    # /retryfailed se dobara ho sakta hai
        dirty[0] += 1
        maybe_save(True)
        print(f"auto msg {m.id} failed: {type(e).__name__}: {e}")
        await notify_admins(f"🚨 <b>Auto-forward fail</b> (msg <code>{m.id}</code>)\n"
                            f"<code>{html.escape(type(e).__name__ + ': ' + str(e))[:300]}</code>\n"
                            "Baad me /retryfailed chala sakte ho.", key="auto_fail")


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
    return web.json_response({"ok": True, "configured": bool(get_source() and default_target()),
                              "busy": running["busy"], "sent": js.sent if js else 0,
                              "done": js.done if js else 0, "total": js.total if js else 0,
                              "uptime": int(time.time() - START_TIME)})


async def keep_alive():
    """Render free plan 15 min bina traffic ke so jata hai - khud ko ping karte raho."""
    if not RENDER_URL:
        return
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        while True:
            await asyncio.sleep(240)        # Render 15 min bina traffic ke so jata hai
            try:
                async with s.get(RENDER_URL) as r:
                    await r.read()
            except Exception as e:
                print(f"keep-alive ping failed: {e}")


async def main():
    global auto_queue
    load_state()
    refresh_admins()
    rebuild_block_re()
    rebuild_link_re()
    apply_runtime()
    auto_queue = asyncio.Queue()

    server = web.Application()
    server.add_routes([web.get("/", health)])
    runner = web.AppRunner(server)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    while True:         # login par FloodWait aaye (baar-baar restart) to crash hone ke bajay ruk kar dobara
        try:
            await app.start()
            break
        except FloodWait as e:
            w = flood_seconds(e) + 1
            print(f"Login FloodWait: {w}s ruk raha hu")
            await asyncio.sleep(w)
    global user_app
    if user_app is not None:
        try:
            await user_app.start()
            me = await user_app.get_me()
            if getattr(me, "is_premium", False):
                print(f"Premium user session ready ({me.id}): 4GB tak ki files rename ho sakengi")
            else:
                print("USER_SESSION Premium nahi hai - 2GB se badi file upload nahi hogi, session band kar raha hu")
                await user_app.stop()
                user_app = None
        except Exception as e:
            print(f"USER_SESSION start nahi hua: {type(e).__name__}: {e}")
            user_app = None
    await resolve_channels()    # peer cache (Peer id invalid fix); channel set na ho to bas skip
    ka_task = asyncio.create_task(keep_alive())
    aw_task = asyncio.create_task(auto_worker())
    wd_task = asyncio.create_task(resume_watchdog())
    bk_task = asyncio.create_task(backup_loop())

    # pichla kaam adhura reh gaya tha (restart/redeploy/crash)
    lj = state["last_job"]
    if lj and not lj.get("done") and lj["last_id"] < lj["end"] and get_source() and default_target():
        if AUTO_RESUME and ADMINS and not lj.get("user_stopped"):
            print(f"pichla kaam adhura tha (ID {lj['last_id'] + 1} -> {lj['end']}): auto-resume hoga")
            spawn(auto_resume(5, "startup"), "resume")
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
    await setup_nag()

    # Har start par admin ko batao: pata chale ki bot restart hua tha aur progress bachi ya nahi
    if STARTUP_NOTIFY:
        lj = state["last_job"]
        if lj and not lj.get("done") and lj["last_id"] < lj["end"]:
            extra = (f"Pichla kaam ID <code>{lj['last_id'] + 1}</code> → <code>{lj['end']}</code> adhura tha"
                     + (", khud resume ho raha hai." if (AUTO_RESUME and not lj.get("user_stopped"))
                        else ". /resume se aage badhao."))
        elif STATE_FOUND:
            extra = "Koi adhura kaam nahi tha."
        else:
            extra = ("⚠️ <b>Purani progress/settings file nahi mili</b> — Render par disk persist nahi hoti (ya pehli baar start hua). "
                     "Pichla <code>/backup</code> file mere paas ho to uspe reply karke <code>/restore</code> bhejo — "
                     "channels, settings aur duplicate-memory sab wapas aa jayenge. Warna <code>/settings</code> se dobara set karo.")
        for admin in ADMINS:
            try:
                await app.send_message(admin, f"🟢 <b>Bot start/restart hua</b>\n{extra}", parse_mode=ParseMode.HTML)
            except Exception as e:
                print(f"startup notify failed for {admin}: {e}")

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
    jobs = [t for t in bg_tasks if t.get_name() == "job"]
    if jobs:                        # current files ko khatam hone ka thoda time do
        await asyncio.wait(jobs, timeout=SHUTDOWN_WAIT)
    for t in list(bg_tasks):        # jo abhi bhi chal raha ho use band karo (state finally me save hota hai)
        t.cancel()
    await asyncio.gather(*list(bg_tasks), return_exceptions=True)
    ka_task.cancel()
    aw_task.cancel()
    wd_task.cancel()
    bk_task.cancel()
    save_state()
    if user_app is not None:
        try:
            await user_app.stop()
        except Exception as e:
            print(f"user session stop failed: {e}")
    await app.stop()
    await runner.cleanup()


if __name__ == "__main__":
    LOOP.run_until_complete(main())
