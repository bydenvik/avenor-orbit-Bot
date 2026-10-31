"""
=====================================================================
  storage.py — AVENOR ORBIT ka persistence layer
====================================================================
  Pehle ye saara code bot.py ke andar tha aur usme ek asli defect tha
  (audit DB-01):

      mongo_start() ek hi baar chalta tha. Fail hua to _mongo_ready=False
      ho jata tha, mongo_flush() turant return karta tha, aur mongo_loop()
      sirf flush bulata rehta tha — dobara connect karne ki koshish KABHI
      nahi hoti thi. Matlab ek baar Mongo gir gaya to bot apni poori zindagi
      local JSON par chalta rehta tha (Render par wo disk ephemeral hai),
      chahe Mongo 10 second me wapas aa jaye.

  Ab yahan ek explicit state machine hai:

      CONNECTING ──ok──> READY ──health fail──> DEGRADED
           │              ▲                      │
           │fail          │                      │ backoff (1,2,4,…60s)
           ▼              │                      ▼
      DEGRADED ────────RECONNECTING─────────── (retry)

  Aur doosra, usse bhi zaroori hissa — DEGRADED me data sirf RAM me
  nahi rehta:

      READY      -> memory + dirty flag, 5s me Mongo me write (write-behind)
      DEGRADED   -> memory + TURANT local JSON (fsync ke saath)
                    + _pending_push_full, taaki wapas connect hone par
                      local (naya) state hi Mongo par chadhe, purana na.

  Isse "acknowledged state change RAM me pada reh gaya" wala hole band ho
  jata hai. Render ka disk ephemeral hai — isliye /diag aur /health dono
  par haalat dikhti hai, aur owner ko alert bhi jata hai. Bina batae
  chup-chaap non-durable mode me chalna hi asli problem thi.

  Baaki jo yahan hai:
    • db_* functions — users, referrals, bans, chats, requests, done, posts, links
    • BSON size guard (audit DB-02 / DB-04): 16 MiB limit se pehle warn,
      uske baad bounded caches prune — users/referrals ko kabhi nahi chhedte
    • admin action journal (audit log) — kaun, kya, kisko, kahan
    • delete_user_data() — privacy deletion, saare stores me (PRIV-01)
    • purge_stale() — retention
    • update dedupe (TG-01/TG-02) — ek hi update_id dobara na chale
====================================================================
"""

import asyncio
import copy
import json
import logging
import os
import random
import threading
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bson import BSON

from config import (
    DB_PATH,
    LOCAL_DISK_PERSISTENT,
    MONGO_DB,
    MONGO_ENABLED,
    MONGO_HOST,
    MONGO_URI_SAFE,
    STORAGE_ALERT_BYTES,
    STORAGE_DOC_LIMIT_BYTES,
    STORAGE_FLUSH_EVERY,
    STORAGE_HEALTH_EVERY,
    STORAGE_MAX_BACKOFF,
    STORAGE_MIRROR_EVERY,
    POSTS_KEEP,
    LINKS_KEEP,
    AUDIT_KEEP,
    UPDATE_DEDUPE_SIZE,
    UPDATE_DEDUPE_TTL,
    _clean_host,
    _host_looks_wrong,
)

log = logging.getLogger("storage")

_db_lock = threading.Lock()


# =====================================================================
#  PERSISTENCE STATE MACHINE
# =====================================================================
class State:
    """Storage ki haalat. Sirf yehi 5 values hain — string hi rakhe hain
    taaki logs, /diag aur /health me seedha padhi ja sakein."""

    CONNECTING = "CONNECTING"      # pehli koshish chal rahi hai
    READY = "READY"                # Mongo connected, writes ja rahi hain
    DEGRADED = "DEGRADED"          # Mongo nahi, local JSON par (durable-ish)
    RECONNECTING = "RECONNECTING"  # backoff ke baad dobara koshish
    DISABLED = "DISABLED"          # Mongo configure hi nahi hai
    CLOSED = "CLOSED"              # shutdown


_state = State.CONNECTING if MONGO_ENABLED else State.DISABLED
_dirty = False
_pending_push_full = False     # degraded me likha gaya data Mongo par chadhana hai
_last_ok = 0.0                 # aakhri successful write ka time
_last_err = ""
_last_health = 0.0
_reconnect_attempt = 0
_next_retry_at = 0.0
_last_local_ok = 0.0
_local_write_failed = False
_client = None
_alert_hook = None


def state() -> str:
    return _state


def is_ready() -> bool:
    return _state == State.READY


# Durability ke 4 level — "haan/nahi" se kaam nahi chalta tha (audit AV-003).
DURABILITY_MONGO = "MONGO"                  # Mongo me — restart ke baad bacha
DURABILITY_DISK = "PERSISTENT_DISK"         # asli disk (VPS) — bacha rahega
DURABILITY_EPHEMERAL = "EPHEMERAL"          # Render Free ka disk — redeploy par ud jayega
DURABILITY_NONE = "VOLATILE"                # kuch bhi disk par nahi


def durability() -> str:
    """Data SACH ME kahan tika hai. Pehle is_durable() local write ko hi
    durable maan leta tha — Render Free ke ephemeral disk par wo jhoot tha,
    aur /ready 200 deta rehta tha jabki redeploy par data jaana tha."""
    if _state == State.READY:
        return DURABILITY_MONGO
    if not (_last_local_ok and not _local_write_failed):
        return DURABILITY_NONE
    return DURABILITY_DISK if LOCAL_DISK_PERSISTENT else DURABILITY_EPHEMERAL


def is_durable() -> bool:
    """Restart ke baad bachega? Sirf Mongo ya asli disk par haan.
    Ephemeral local file = kaam chalau, durable nahi."""
    return durability() in (DURABILITY_MONGO, DURABILITY_DISK)


def set_alert_hook(fn):
    """bot.py owner ko DM karne ke liye ek async hook deta hai.
    storage ko telegram ka pata nahi — isliye callback."""
    global _alert_hook
    _alert_hook = fn


async def _alert(text: str):
    log.warning("STORAGE ALERT: %s", text.replace("\n", " | "))
    if _alert_hook is None:
        return
    try:
        await _alert_hook(text)
    except Exception as e:      # alert fail ho to bot na ruke
        log.warning("Storage alert hook fail: %s", e)


def _set_state(new: str, reason: str = ""):
    global _state
    if _state == new:
        return
    log.warning("Storage state: %s -> %s%s", _state, new, f" ({reason})" if reason else "")
    _state = new


def status_text() -> str:
    """Insaan ki bhasha me storage ki haalat (purana mongo_status ka kaam)."""
    if not MONGO_ENABLED:
        return "local file (MongoDB set nahi hai)"
    if _state == State.READY:
        age = int(time.time() - _last_ok) if _last_ok else -1
        if age < 0:
            return "MongoDB connected"
        return (f"MongoDB connected (last save {age}s pehle)" if age >= 60
                else "MongoDB connected")
    if _state == State.DEGRADED or _state == State.RECONNECTING:
        wait = max(0, int(_next_retry_at - time.time()))
        return (f"MongoDB OFFLINE — local file par chal raha hai "
                f"(retry {wait}s me)" if wait else
                "MongoDB OFFLINE — local file par chal raha hai")
    if _state == State.CONNECTING:
        return "MongoDB connect ho raha hai"
    return f"storage {_state}"


def health() -> dict:
    """/health aur /diag ke liye ek hi jagah se sach."""
    return {
        "state": _state,
        "durability": durability(),
        "durable": is_durable(),
        "mongo_enabled": MONGO_ENABLED,
        "dirty": _dirty,
        "pending_full_push": _pending_push_full,
        "last_write_age_s": int(time.time() - _last_ok) if _last_ok else None,
        "last_local_write_age_s": (int(time.time() - _last_local_ok)
                                   if _last_local_ok else None),
        "local_write_failed": _local_write_failed,
        "reconnect_attempt": _reconnect_attempt,
        "retry_in_s": max(0, int(_next_retry_at - time.time())),
        "last_error": _last_err[:300],
    }


def backoff_delay(attempt: int, cap: int | None = None) -> float:
    """Bounded exponential backoff + jitter.
    attempt=0 -> ~1s, 1 -> ~2s, 2 -> ~4s ... cap par ruk jata hai.
    Jitter zaroori hai: warna 10 bots ek saath hi Atlas ko hit karte hain."""
    cap = STORAGE_MAX_BACKOFF if cap is None else cap
    base = min(2 ** max(0, int(attempt)), max(1, cap))
    return round(base * (0.75 + random.random() * 0.5), 2)


# =====================================================================
#  LOCAL JSON  (fallback + seed + mirror)
# =====================================================================
_db_cache = None


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _today():
    return datetime.now(timezone.utc).strftime("%d %b %Y")


def db_default() -> dict:
    return {"users": {}, "referrals": {}, "banned": [],
            "stats": {"total_started": 0}, "chat_ids": {}, "chats": {},
            "requests": {}, "done": {}, "posts": {}, "links": {},
            "audit": [], "ai_usage": {}}


def _db_load_json():
    """Local JSON file padho (fallback / pehli-baar seed ke liye)."""
    if os.path.exists(DB_PATH):
        try:
            with open(DB_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            base = db_default()
            base.update(data)
            return base
        except Exception as e:
            # Corrupt DB ko CHUPCHAAP reset MAT karo — warna saara data ud jata
            # hai aur pata bhi nahi chalta. File alag rakho + log karo.
            log.error("DB load fail (%s): %s", DB_PATH, e)
            try:
                bad = DB_PATH + ".corrupt-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
                os.replace(DB_PATH, bad)
                log.error("Corrupt DB ko %s par bhej diya", bad)
            except Exception:
                pass
    return db_default()


def db_load() -> dict:
    """State ko memory me cache karo — HAR call par poora JSON parse nahi.
    Caller isi dict ko badalta hai aur db_save() ko deta hai, isliye cache
    hamesha sync rehta hai."""
    global _db_cache
    if _db_cache is None:
        _db_cache = _db_load_json()
    return _db_cache


# Do jagah se likha jata hai: background writer thread, aur (kabhi-kabhi)
# flush_local_now — privacy deletion / shutdown par. Dono ek hi .tmp file
# use karte hain, isliye ek saath chalne par ek doosre ka tmp uda deta tha:
#   os.replace(...) -> FileNotFoundError: db.json.tmp
# (Ye asli bug test ne pakda, jab AV-005 ka commit_now add hua.)
_json_write_lock = threading.Lock()


def db_write_json(data) -> bool:
    """Local JSON file likho. Disk full / permission error par CRASH na ho.
    os.replace + fsync = aadhi likhi file kabhi nahi banti."""
    global _last_local_ok, _local_write_failed
    try:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        with _json_write_lock:
            tmp = DB_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, DB_PATH)
        _last_local_ok = time.time()
        _local_write_failed = False
        return True
    except OSError as e:
        _local_write_failed = True
        log.error("DB save fail (disk full / permission?): %s", e)
    except Exception as e:
        _local_write_failed = True
        log.error("DB save fail: %s", e)
    return False


# ---------------------------------------------------------------------
#  JSON WRITER THREAD  (audit AV-02 — event loop block na ho)
# ---------------------------------------------------------------------
#  DEGRADED mode me har write ko disk par fsync karna padta hai. Wo kaam
#  ~5-100ms leta hai, aur pehle wo EVENT LOOP ke thread me hota tha (aur
#  upar se _db_lock pakde hue) — matlab ek write ke dauraan bot ke saare
#  doosre kaam ruk jaate the: doosri chats, /health, webhook.
#  Ab ek hi background thread likhta hai, aur sirf LATEST snapshot (purane
#  snapshot bekaar hain — state poora replace hota hai).
_writer_slot: list = []
_writer_event = threading.Event()
_writer_thread = None
_writer_writes = 0


def _writer_loop():
    global _writer_writes
    while True:
        _writer_event.wait()
        _writer_event.clear()
        data = _writer_slot.pop() if _writer_slot else None
        if data is None:
            continue
        if db_write_json(data):
            _writer_writes += 1


def _ensure_writer():
    global _writer_thread
    if _writer_thread is not None and _writer_thread.is_alive():
        return
    _writer_thread = threading.Thread(target=_writer_loop, name="json-writer",
                                      daemon=True)
    _writer_thread.start()


def _queue_json_write(data) -> None:
    """Latest snapshot writer ko do. Purana snapshot phenk do — state poora
    replace hota hai, isliye sirf aakhri wala matlab rakhta hai."""
    _writer_slot.clear()
    _writer_slot.append(data)
    _ensure_writer()
    _writer_event.set()


def flush_local_now() -> bool:
    """Abhi, isi thread par JSON likho (fsync ke saath).

    Do jagah chahiye: shutdown par (aakhri baar), aur jab kuch aisa hua ho
    jise user ko "ho gaya" bolne se pehle disk par chahiye (privacy deletion).
    Async code se isse `asyncio.to_thread(...)` me hi bulana — warna event
    loop ruk jayega (wahi AV-02 galti).
    """
    return db_write_json(db_load())


def pending_json_write() -> bool:
    """Koi write queue me padi hai? (shutdown se pehle dekhne ke liye)"""
    return bool(_writer_slot)


def db_save(data) -> None:
    """State save karo.

    • READY      -> memory + dirty flag. Background flush Mongo me likhta hai.
                    (Per-write JSON nahi — wo ~77ms leta tha.)
    • DEGRADED   -> memory + local JSON, ek background writer thread par.
                    Disk par likhna event loop me NAHI hota (AV-02), par
                    write turant queue hoti hai — data RAM me nahi rehta.
    """
    global _db_cache, _dirty, _pending_push_full
    _db_cache = data
    if _state == State.READY:
        _dirty = True
        return
    if MONGO_ENABLED:
        # Wapas connect hone par ye (naya) state Mongo par chadhna chahiye,
        # purana remote state nahi.
        _pending_push_full = True
    _queue_json_write(data)


# =====================================================================
#  MONGO  (durable store)  —  write-behind, RAM cache hot path
# =====================================================================
#  Motor DEPRECATED hai (May 2026) — isliye pymongo ka naya AsyncMongoClient.
_MONGO_DOC_ID = "state"
MONGO_PASSWORD_SET = bool((os.getenv("MONGO_PASSWORD") or "").strip())


def _coll():
    global _client
    if _client is None:
        from pymongo import AsyncMongoClient
        from pymongo.server_api import ServerApi
        _client = AsyncMongoClient(
            MONGO_URI_SAFE, server_api=ServerApi("1"),
            serverSelectionTimeoutMS=8000, connectTimeoutMS=8000,
            retryWrites=True,
        )
    return _client[MONGO_DB]["state"]


def _reset_client():
    """Dead client ko chhodo — naya banega. Purane par koi call nahi."""
    global _client
    _client = None


def mongo_error_hint(e) -> str:
    """Mongo error ko seedha, kaam ki baat me badlo. 'bad auth' jaisa generic
    error user ko confuse karta hai — asli wajah ye hi hote hain."""
    s = str(e).lower()
    if "bad auth" in s or "authentication failed" in s or "code': 8000" in s:
        return ("Username ya password galat hai.\n"
                "  • Atlas > Database Access me apne DB USER ka password reset kar "
                "— Atlas LOGIN password NAHI, wo alag hota hai.\n"
                "  • Password me @ # $ / ? ! ho to MONGO_PASSWORD alag se daal — "
                "bot khud encode kar dega.")
    if "server selection" in s or "timed out" in s or "timeout" in s:
        return ("Atlas tak pahunch hi nahi paya. Atlas > Network Access me apni "
                "service ka egress range allow hai? (0.0.0.0/0 aakhri raasta hai, "
                "pehla nahi.)")
    if "does not exist" in s or "dns" in s or "nodename" in s:
        return ("Host ka naam nahi mila. Zyadatar wajah: URI me PLACEHOLDER host "
                "(jaise cluster0.xxxxx...) pada hai, ya host type karte waqt "
                "1/l ya 0/o galat ho gaya. Atlas se COPY karo, type mat karo.")
    if "must be escaped" in s or "rfc 3986" in s or "quote_plus" in s:
        return ("URI me koi character escape nahi tha (jaise @ ya space).\n"
                "  • Ye bot khud theek karta hai — MONGO_PASSWORD me password daal.")
    if "ssl" in s or "tls" in s:
        return "TLS problem — poora mongodb+srv:// wala string use kar."
    if "not authorized" in s or "unauthorized" in s:
        return ("Database user ke paas is database ka access nahi. Atlas > Database "
                "Access me user ka role 'Atlas admin' ya readWriteAnyDatabase rakho.")
    return "MONGO_URI / MONGO_PASSWORD check kar (aur Render me redeploy kiya?)"


def _doc(data) -> dict:
    """State ko Mongo document banao (top-level keys, plus _id).

    Update-dedupe ring bhi saath jaati hai (audit AV-004). Pehle wo sirf
    memory me thi: process crash ho jaye side effect ke baad par Telegram ko
    200 milne se pehle, to restart par guard khaali hota aur wahi update
    DOBARA chal jata. Ab last 500 ids document ke saath hi save hote hain.
    """
    doc = {k: v for k, v in data.items() if k != "_id"}
    doc["_id"] = _MONGO_DOC_ID
    doc["_saved"] = _now()
    if _seen_updates:
        # BSON me keys SIRF string ho sakti hain — int key par encode fail
        # hota hai, isliye stringify karke likhte hain.
        doc["_seen_updates"] = {str(k): v for k, v in list(_seen_updates.items())[-500:]}
    return doc


def _restore_dedupe(doc) -> None:
    """Saved dedupe ring wapas memory me (TTL ke andar wale hi)."""
    raw = doc.get("_seen_updates") or {}
    now = time.time()
    for key, ts in raw.items():
        try:
            uid, when = int(key), float(ts)
        except (TypeError, ValueError):
            continue
        if now - when <= UPDATE_DEDUPE_TTL:
            _seen_updates[uid] = when


def _write_mirror() -> None:
    """Local JSON safety copy (fsync ke bina) — thread me chalta hai."""
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with open(DB_PATH + ".tmp", "w", encoding="utf-8") as f:
        json.dump(db_load(), f, ensure_ascii=False)
    os.replace(DB_PATH + ".tmp", DB_PATH)


def doc_size_bytes(data) -> int:
    """BSON me encode hone ke baad kitne bytes — MongoDB ki asli limit yahi
    hai (16 MiB). Estimate nahi, asli encode."""
    try:
        return len(BSON.encode(_doc(data)))
    except Exception:
        return 0


def _shrink(data) -> list:
    """Document 16 MiB ke kareeb pahuncha -> DERIVED caches chhoto karo.

    ZAROORI: users / referrals / banned / chat_ids ko haath NAHI lagate —
    wo asli data hai. Sirf wo cheezein kaatti hain jo dobara ban sakti hain
    ya jinki purani copies ka koi matlab nahi (posts, links, done, requests,
    audit). Har prune log hota hai + owner ko alert jata hai."""
    notes = []
    posts = data.get("posts") or {}
    keep_posts = max(10, min(POSTS_KEEP, 50))
    if len(posts) > keep_posts:
        order = sorted(posts.items(), key=lambda kv: (kv[1] or {}).get("created", ""))
        for code, _ in order[: len(posts) - keep_posts]:
            posts.pop(code, None)
        data["posts"] = posts
        notes.append(f"posts -> {len(posts)}")
    links = data.get("links") or {}
    if len(links) > LINKS_KEEP:
        order = sorted(links.items(), key=lambda kv: (kv[1] or {}).get("last", ""))
        for key, _ in order[: len(links) - LINKS_KEEP]:
            links.pop(key, None)
        data["links"] = links
        notes.append(f"links -> {len(links)}")
    for field, inner in (("done", True), ("requests", True)):
        store = data.get(field) or {}
        if len(store) > 2000:
            for key in list(store.keys())[: len(store) - 2000]:
                store.pop(key, None)
            data[field] = store
            notes.append(f"{field} -> {len(store)}")
    audit = data.get("audit") or []
    if len(audit) > AUDIT_KEEP:
        data["audit"] = audit[-AUDIT_KEEP:]
        notes.append(f"audit -> {len(data['audit'])}")
    return notes


async def connect() -> bool:
    """Mongo se state lo (ya degraded state push karo). True = READY.

    Teen soorat:
      1. Degraded me kuch likha gaya tha (_pending_push_full) -> LOCAL state
         Mongo par chadhao. Warna hum purana remote state load kar ke naya
         local data kho dete.
      2. Collection khali -> local JSON seed karo.
      3. Warna remote state load karo.
    """
    global _db_cache, _pending_push_full, _reconnect_attempt, _next_retry_at, _dirty
    if not MONGO_ENABLED:
        _set_state(State.DISABLED)
        return False
    _set_state(State.CONNECTING if _state in (State.DISABLED, State.CLOSED) else State.RECONNECTING)
    _hbad = _host_looks_wrong(_clean_host(MONGO_HOST)) if MONGO_HOST else ""
    if _hbad:
        log.error("MongoDB host me galti: %s", _hbad)
    try:
        coll = _coll()
        await coll.database.command("ping")

        local = db_load()
        if _pending_push_full:
            await coll.replace_one({"_id": _MONGO_DOC_ID}, _doc(local), upsert=True)
            _pending_push_full = False
            _set_state(State.READY, "degraded state pushed to Mongo")
            log.info("MongoDB connected — degraded me likha data Mongo par chadha diya "
                     "(%s users, %s chats).", len(local.get("users", {})),
                     len(local.get("chats", {})))
        else:
            doc = await coll.find_one({"_id": _MONGO_DOC_ID})
            if doc:
                doc.pop("_id", None)
                doc.pop("_saved", None)
                # Update-dedupe ring restart ke baad bhi kaam kare (AV-004).
                _restore_dedupe(doc)
                doc.pop("_seen_updates", None)
                base = db_default()
                base.update(doc)
                _db_cache = base
                _set_state(State.READY, "state loaded")
                log.info("MongoDB connected — state load hui: %s users, %s chats, %s posts.",
                         len(base.get("users", {})), len(base.get("chats", {})),
                         len(base.get("posts", {})))
            else:
                await coll.replace_one({"_id": _MONGO_DOC_ID}, _doc(local), upsert=True)
                _set_state(State.READY, "empty collection seeded")
                log.info("MongoDB khali thi — local data seed kar diya (%s users).",
                         len(local.get("users", {})))
        _reconnect_attempt = 0
        _next_retry_at = 0.0
        _dirty = False
        return True
    except Exception as e:
        global _last_err
        _last_err = str(e)
        _reset_client()
        _set_state(State.DEGRADED, "connect failed")
        _schedule_retry()
        log.error("MongoDB connect fail: %s", e)
        log.error("MongoDB wajah: %s", mongo_error_hint(e).replace("\n", " | "))
        log.error("DEGRADED mode — har write local file (%s) par ja rahi hai.", DB_PATH)
        return False


def _schedule_retry():
    global _reconnect_attempt, _next_retry_at
    delay = backoff_delay(_reconnect_attempt)
    _reconnect_attempt += 1
    _next_retry_at = time.time() + delay
    log.info("MongoDB retry #%s, %ss baad.", _reconnect_attempt, delay)


def _snapshot_for_write():
    """Write ke liye copy + size check + (zaroorat par) prune.

    Ye sab CPU-heavy hai (deepcopy + BSON encode of a multi-MB doc), isliye
    event loop par NAHI — `asyncio.to_thread` se chalaya jata hai.
    Returns (data, size_before, notes).
    """
    # deepcopy ZAROORI hai: db_load() live dict deta hai jise handlers usi
    # waqt badal rahe hote hain. Bina copy ke driver encode karte waqt
    # "dictionary changed size during iteration" aa sakta hai.
    data = copy.deepcopy(db_load())
    size = doc_size_bytes(data)
    notes = []
    if size > STORAGE_DOC_LIMIT_BYTES:
        notes = _shrink(data)
    return data, size, notes


async def flush(force: bool = False) -> bool:
    """Dirty ho to poori state Mongo me likh do."""
    global _dirty, _last_ok, _last_err
    if not MONGO_ENABLED or _state != State.READY:
        return False
    if not _dirty and not force:
        return False
    _dirty = False
    try:
        data, size, notes = await asyncio.to_thread(_snapshot_for_write)

        if size > STORAGE_ALERT_BYTES:
            if size > STORAGE_DOC_LIMIT_BYTES:
                new_size = doc_size_bytes(data)
                await _alert(
                    "⚠️ Storage document 16 MB limit ke paas pahunch gaya tha "
                    f"({size / 1048576:.1f} MB). Purane derived caches prune kiye: "
                    f"{', '.join(notes) or 'kuch nahi'} -> {new_size / 1048576:.1f} MB.\n"
                    "Users / referrals / chats ka data chhua nahi gaya.\n"
                    "Pakka fix: entity-level collections (docs/AUDIT-RESPONSE.md, DB-02).")
                _dirty = True     # pruned state bhi save ho jaye
            else:
                log.warning("Storage document %.1f MB — 16 MB limit ke kareeb.",
                            size / 1048576)

        await _coll().replace_one({"_id": _MONGO_DOC_ID}, _doc(data), upsert=True)
        _last_ok = time.time()
        _last_err = ""
        return True
    except Exception as e:
        _dirty = True              # agla flush phir try karega
        _last_err = str(e)
        log.warning("MongoDB flush fail: %s", e)
        # Write fail = connection shayad mar gaya. Health check ka intezaar
        # mat karo, turant degraded mode me jao (writes local par tikengi).
        _reset_client()
        _set_state(State.DEGRADED, "flush failed")
        _schedule_retry()
        await _alert("⚠️ MongoDB write fail ho gaya — DEGRADED mode.\n"
                     f"{mongo_error_hint(e)}\n"
                     "Ab har change local file par likha ja raha hai; wapas "
                     "connect hone par Mongo par chala jayega.")
        return False


async def _health_check() -> bool:
    """Chup-chaap mari hui connection pakdo (TCP khula ho par server dead)."""
    global _last_health
    _last_health = time.time()
    try:
        await _coll().database.command("ping")
        return True
    except Exception as e:
        _reset_client()
        _set_state(State.DEGRADED, f"health check fail: {type(e).__name__}")
        _schedule_retry()
        await _alert("⚠️ MongoDB health check fail — DEGRADED mode.\n"
                     f"{mongo_error_hint(e)}")
        return False


async def loop():
    """Background task: reconnect, flush, health check, local mirror.

    Pehle ye loop SIRF flush bulata tha — isliye DB-01 kabhi theek nahi hota.
    Ab teeno kaam isi loop me hain."""
    last_mirror = 0.0
    while True:
        await asyncio.sleep(max(1, STORAGE_FLUSH_EVERY))
        try:
            if not MONGO_ENABLED:
                continue
            now = time.time()

            if _state != State.READY:
                if now >= _next_retry_at:
                    await connect()
                continue

            if now - _last_health >= STORAGE_HEALTH_EVERY:
                if not await _health_check():
                    continue

            await flush()

            if now - last_mirror > STORAGE_MIRROR_EVERY:
                last_mirror = now
                # Mongo hi asli store hai — ye sirf safety copy. Disk par
                # likhna thread me, taaki event loop na ruke (AV-02).
                try:
                    await asyncio.to_thread(_write_mirror)
                except Exception as e:
                    log.warning("Local mirror fail: %s", e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("storage loop error: %s", e)


async def commit_now() -> tuple[bool, str]:
    """State ko TURANT durable karo (privacy deletion ke liye — audit AV-005).

    Pehle deletion sirf memory + dirty flag thi, aur user ko "delete ho gaya"
    bol diya jata tha. Us 5 second ke window me crash hua to record Mongo me
    pada reh jata — matlab jhoot. Ab commit karke sach batate hain.

    Returns (ok, kahan) — kahan: "mongo" | "disk" | "failed".
    """
    if MONGO_ENABLED and _state == State.READY:
        ok = await flush(force=True)
        return ok, ("mongo" if ok else "failed")
    ok = await asyncio.to_thread(flush_local_now)
    return ok, ("disk" if ok else "failed")


def user_present(uid) -> bool:
    """Delete ke baad verify karne ke liye."""
    with _db_lock:
        return str(int(uid)) in db_load().get("users", {})


async def doc_size_now() -> int:
    """Abhi ka document size (bytes). /diag dikhata hai."""
    return await asyncio.to_thread(doc_size_bytes, db_load())


async def close():
    """Shutdown: aakhri flush. State CLOSED."""
    if MONGO_ENABLED and _state == State.READY:
        ok = await flush(force=True)
        log.info("Shutdown: MongoDB final save %s.", "OK" if ok else "FAIL")
    elif MONGO_ENABLED:
        # Degraded tha — memory ka state local file par likh do (thread me,
        # taaki shutdown bhi event loop ko block na kare).
        ok = await asyncio.to_thread(flush_local_now)
        log.info("Shutdown: DEGRADED tha — local file par likh diya: %s.", ok)
    _set_state(State.CLOSED)


# =====================================================================
#  UPDATE DEDUPE  (TG-01 / TG-02)
# =====================================================================
#  Telegram webhook ka jawab na mile to wahi update DOBARA bhejta hai. Aur
#  restart ke baad bhi pending update aa sakta hai. Ek hi update_id par
#  referral count ya ban DOBARA lag jana asli bug hai.
#
#  Ye memory me rehta hai (disk par nahi) — soch-samajh kar: ye ek
#  short-lived retry guard hai (15 min), permanent ledger nahi. Restart par
#  ye khaali hota hai, aur tab Telegram bhi sirf wahi updates bhejta hai
#  jo usne accept nahi kiye the. Isliye restart ke baad duplicate ka
#  risk nahi banta — par webhook retry ka banta hai, aur wahi yahan rukta hai.
_seen_updates: dict = {}


def update_seen(update_id) -> bool:
    """True = ye update pehle aa chuka hai (skip karo)."""
    try:
        uid = int(update_id)
    except (TypeError, ValueError):
        return False
    ts = _seen_updates.get(uid)
    if ts is None:
        return False
    if time.time() - ts > UPDATE_DEDUPE_TTL:
        _seen_updates.pop(uid, None)
        return False
    return True


def mark_update_seen(update_id):
    try:
        uid = int(update_id)
    except (TypeError, ValueError):
        return
    _seen_updates[uid] = time.time()
    if len(_seen_updates) > UPDATE_DEDUPE_SIZE:
        for key in list(_seen_updates.keys())[: len(_seen_updates) - UPDATE_DEDUPE_SIZE]:
            _seen_updates.pop(key, None)


def reset_dedupe():
    """Tests ke liye."""
    _seen_updates.clear()


# =====================================================================
#  CHAT IDS / CHATS
# =====================================================================
_chat_ids_cache = None


def prune(cache: dict, keep: int):
    """Cache ko bounded rakho — memory leak se bachne ke liye."""
    if len(cache) > keep:
        for k in list(cache.keys())[: len(cache) - keep]:
            cache.pop(k, None)


def _cid_key(chat_id) -> str:
    """Chat id ka dict-key. Numeric (-100...) ya @username — dono chalte hain."""
    try:
        return str(int(chat_id))
    except (TypeError, ValueError):
        return str(chat_id or "").strip()


def learned_ids() -> dict:
    """Runtime me seekhi gayi chat ids ({block name: chat id})."""
    global _chat_ids_cache
    if _chat_ids_cache is None:
        with _db_lock:
            _chat_ids_cache = dict(db_load().get("chat_ids", {}))
    return _chat_ids_cache


# Purana naam — bot.py ke bahut call sites isse use karte hain.
_learned_ids = learned_ids


def db_replace_chat_id(old_id, new_id):
    """Group -> supergroup migration: purani chat id ko nayi se badal do."""
    global _chat_ids_cache
    old_id, new_id = int(old_id), int(new_id)
    with _db_lock:
        data = db_load()
        changed = False
        for name, cid in list(data.get("chat_ids", {}).items()):
            if _cid_key(cid) == _cid_key(old_id):
                data["chat_ids"][name] = new_id
                changed = True
        chats = data.setdefault("chats", {})
        if str(old_id) in chats:
            chats[str(new_id)] = chats.pop(str(old_id))
            chats[str(new_id)]["id"] = new_id
            changed = True
        if changed:
            db_save(data)
            _chat_ids_cache = dict(data["chat_ids"])


def db_set_chat_id(name, chat_id):
    """Block name -> numeric chat id yaad rakho (config edit ki zaroorat nahi)."""
    global _chat_ids_cache
    with _db_lock:
        data = db_load()
        try:
            data.setdefault("chat_ids", {})[name] = int(chat_id)
        except (TypeError, ValueError):
            data.setdefault("chat_ids", {})[name] = str(chat_id).strip()
        db_save(data)
        _chat_ids_cache = dict(data["chat_ids"])


def db_upsert_chat(chat_id, title, ctype, status=None):
    """Bot jis bhi chat me hai uski detail yaad rakho (id, title, type, status)."""
    with _db_lock:
        data = db_load()
        c = data.setdefault("chats", {}).get(str(chat_id), {})
        c.update({"id": int(chat_id), "title": title or "", "type": ctype or "",
                  "seen": _now()})
        if status:
            c["status"] = status
        data["chats"][str(chat_id)] = c
        db_save(data)


def db_get_chats():
    with _db_lock:
        return dict(db_load().get("chats", {}))


# =====================================================================
#  USERS / REFERRALS / BANS
# =====================================================================
_seen_cache: dict = {}


def db_add_user(user, bump_start=False):
    """Naya user ho to True return karega.
    bump_start=True (/start) -> 'Total started' bhi badhao, USI ek write me.
    Normal messages par 5 min tak write skip hoti hai (speed)."""
    if not bump_start and time.time() - _seen_cache.get(user.id, 0) < 300:
        return False
    _seen_cache[user.id] = time.time()
    prune(_seen_cache, 10000)
    with _db_lock:
        data = db_load()
        uid = str(user.id)
        is_new = uid not in data["users"]
        entry = data["users"].get(uid, {})
        entry.update({
            "id": user.id,
            "first_name": user.first_name or "",
            "username": user.username or "",
            "last_seen": _now(),
        })
        if is_new:
            entry["joined_at"] = _now()
        data["users"][uid] = entry
        if bump_start:
            data["stats"]["total_started"] = data["stats"].get("total_started", 0) + 1
        db_save(data)
        return is_new


def db_user_exists(uid) -> bool:
    with _db_lock:
        return str(uid) in db_load()["users"]


def db_all_users():
    with _db_lock:
        return list(db_load()["users"].values())


_ban_cache = None


def db_is_banned(uid):
    """In-memory cache — har message par poora dict read na ho."""
    global _ban_cache
    if _ban_cache is None:
        with _db_lock:
            _ban_cache = set(db_load()["banned"])
    return int(uid) in _ban_cache


def db_set_ban(uid, value=True):
    global _ban_cache
    with _db_lock:
        data = db_load()
        uid = int(uid)
        if value and uid not in data["banned"]:
            data["banned"].append(uid)
        if not value and uid in data["banned"]:
            data["banned"].remove(uid)
        db_save(data)
        _ban_cache = set(data["banned"])


def db_add_referral(referrer_id, new_user_id):
    """Naye user ko referrer ke khaate mein jodo (ek user sirf ek baar)."""
    with _db_lock:
        data = db_load()
        new_uid = str(new_user_id)
        entry = data["users"].get(new_uid, {})
        if entry.get("ref_by"):
            return False
        entry["ref_by"] = int(referrer_id)
        data["users"][new_uid] = entry
        ref = data["referrals"].get(str(referrer_id), {"count": 0, "users": []})
        if int(new_user_id) not in ref["users"]:
            ref["users"].append(int(new_user_id))
            # count ko id-list se ALAG rakho. Pehle `count = len(users)` tha,
            # aur list 500 par trim hoti hai — isliye 501 ke baad count hamesha
            # 501 par jam jata tha (asli bug, test ne pakda).
            ref["count"] = max(int(ref.get("count", 0) or 0) + 1, len(ref["users"]))
            # Id list ko bound rakho — warna ek popular referrer hazaron ids
            # jama kar leta aur document 16 MB limit ke kareeb pahunch jata.
            if len(ref["users"]) > 500:
                del ref["users"][: len(ref["users"]) - 500]
        data["referrals"][str(referrer_id)] = ref
        db_save(data)
        return True


def db_ref_count(uid):
    with _db_lock:
        ref = db_load()["referrals"].get(str(uid))
        return ref["count"] if ref else 0


def db_ref_rank(uid):
    with _db_lock:
        refs = db_load()["referrals"]
        ordered = sorted(refs.items(), key=lambda kv: kv[1].get("count", 0), reverse=True)
        for i, (k, _) in enumerate(ordered, start=1):
            if k == str(uid):
                return i
        return 0


def db_stats():
    with _db_lock:
        data = db_load()
        return {
            "total_users": len(data["users"]),
            "total_started": data["stats"].get("total_started", 0),
            "banned": len(data["banned"]),
            "referrals": sum(v.get("count", 0) for v in data["referrals"].values()),
        }


# =====================================================================
#  DONE / JOIN-REQUEST CACHES
# =====================================================================
_done_cache = None
_DONE_TTL = 24 * 3600


def _dones():
    global _done_cache
    if _done_cache is None:
        with _db_lock:
            _done_cache = dict(db_load().get("done", {}))
    return _done_cache


def db_mark_done(user_id, key):
    global _done_cache
    with _db_lock:
        d = db_load()
        r = d.setdefault("done", {})
        r.setdefault(str(user_id), {})[str(key)] = time.time()
        for k in list(r.keys())[: max(0, len(r) - 5000)]:
            r.pop(k, None)
        db_save(d)
        _done_cache = dict(r)


def is_done(user_id, key) -> bool:
    per = _dones().get(str(user_id)) or {}
    ts = per.get(str(key))
    try:
        return bool(ts) and (time.time() - float(ts)) < _DONE_TTL
    except (TypeError, ValueError):
        return False


_requests_cache = None

# Join request sirf itni der valid hai. Iske baad sirf ASLI membership ginegi.
_REQUEST_TTL = 24 * 3600


def _requests():
    """{user_id: {chat_id: timestamp}} — jinhone join REQUEST bhej di hai."""
    global _requests_cache
    if _requests_cache is None:
        with _db_lock:
            _requests_cache = dict(db_load().get("requests", {}))
    return _requests_cache


def _per_user(per):
    """Purana format (list) ko naye {chat_id: ts} me badlo."""
    if isinstance(per, list):
        return {str(c): 0 for c in per}
    return dict(per or {})


def db_add_request(user_id, chat_id):
    global _requests_cache
    with _db_lock:
        d = db_load()
        r = d.setdefault("requests", {})
        per = _per_user(r.get(str(user_id)))
        per[_cid_key(chat_id)] = time.time()
        r[str(user_id)] = per
        for k in list(r.keys())[: max(0, len(r) - 5000)]:
            r.pop(k, None)                      # bound rakho
        db_save(d)
        _requests_cache = dict(r)


def db_del_request(user_id, chat_id):
    """User ne join kar liya ya LEAVE kar diya -> request record hata do."""
    global _requests_cache
    with _db_lock:
        d = db_load()
        r = d.setdefault("requests", {})
        if str(user_id) not in r:
            return
        per = _per_user(r.get(str(user_id)))
        per.pop(_cid_key(chat_id), None)
        if per:
            r[str(user_id)] = per
        else:
            r.pop(str(user_id), None)
        db_save(d)
        _requests_cache = dict(r)


def has_request(user_id, chat_id) -> bool:
    """User ne is chat me join request bhej di thi? Sirf _REQUEST_TTL tak valid."""
    per = _requests().get(str(user_id))
    if not per:
        return False
    if isinstance(per, list):                   # purana format
        return _cid_key(chat_id) in {_cid_key(c) for c in per}
    ts = per.get(_cid_key(chat_id))
    try:
        return bool(ts) and (time.time() - float(ts)) < _REQUEST_TTL
    except (TypeError, ValueError):
        return False


# =====================================================================
#  LINKS  (ek hi link do baar save na ho)
# =====================================================================
def norm_link(url) -> str:
    """Link ko normalize karo taaki same link EK maana jaye.
    - scheme/host lowercase, www. hatao
    - trailing slash hatao
    - tracking params (utm_*, fbclid, gclid, si, igshid) hatao"""
    u = str(url or "").strip()
    if not u:
        return ""
    if not u.lower().startswith(("http://", "https://")):
        u = "https://" + u
    try:
        s = urlsplit(u)
    except Exception:
        return u.lower()
    host = (s.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    drop = ("fbclid", "gclid", "si", "igshid", "mc_cid", "mc_eid")
    q = [(k, v) for k, v in parse_qsl(s.query, keep_blank_values=True)
         if not k.lower().startswith("utm_") and k.lower() not in drop]
    path = s.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, urlencode(q), ""))


_norm_link = norm_link


def db_link_add(url, note="") -> tuple:
    """Link save karo. Pehle se hai to WAHI wapas — naya entry nahi banta.
    Returns (key, created)."""
    key = norm_link(url)
    if not key:
        return "", False
    with _db_lock:
        d = db_load()
        links = d.setdefault("links", {})
        hit = links.get(key)
        if hit:
            hit["uses"] = int(hit.get("uses", 1)) + 1
            hit["last"] = _now()
            if note and not hit.get("note"):
                hit["note"] = note
            db_save(d)
            return key, False
        links[key] = {"url": str(url).strip(), "note": note or "",
                      "uses": 1, "first": _now(), "last": _now()}
        if len(links) > LINKS_KEEP:
            order = sorted(links.items(), key=lambda kv: kv[1].get("last", ""))
            for k, _ in order[: len(links) - LINKS_KEEP]:
                links.pop(k, None)
        db_save(d)
        return key, True


def db_links() -> dict:
    return dict(db_load().get("links", {}))


def db_links_dedupe() -> int:
    """Purani entries ko normalize karke duplicate hatao — ek hi rakho."""
    with _db_lock:
        d = db_load()
        links = d.get("links") or {}
        seen, keep, removed = {}, {}, 0
        for key, v in links.items():
            nk = norm_link(v.get("url") or key) or key
            if nk in seen:
                keep[seen[nk]]["uses"] = int(keep[seen[nk]].get("uses", 1)) + int(v.get("uses", 1))
                removed += 1
            else:
                seen[nk] = nk
                keep[nk] = dict(v)
        if removed:
            d["links"] = keep
            db_save(d)
        return removed


# =====================================================================
#  POSTS
# =====================================================================
def db_post_save(code, data):
    with _db_lock:
        d = db_load()
        posts = d.setdefault("posts", {})
        posts[code] = data
        # Posts kabhi delete nahi hote the — document hamesha badhta jaata aur
        # ek din 16 MB limit par pahunch kar save hi fail ho jata.
        if len(posts) > POSTS_KEEP:
            order = sorted(posts.items(), key=lambda kv: kv[1].get("created", ""))
            for old_code, _ in order[: len(posts) - POSTS_KEEP]:
                posts.pop(old_code, None)
        db_save(d)


def db_post_get(code):
    with _db_lock:
        return db_load().get("posts", {}).get(code)


def db_post_all():
    with _db_lock:
        return dict(db_load().get("posts", {}))


def db_post_update(code, **kw):
    with _db_lock:
        d = db_load()
        p = d.setdefault("posts", {}).get(code)
        if not p:
            return False
        p.update(kw)
        db_save(d)
        return True


# =====================================================================
#  ADMIN ACTION JOURNAL  (kaun, kya, kisko, kahan)
# =====================================================================
def audit_add(actor_id, action, target=None, chat_id=None, detail=""):
    """Har privileged action ek line me yaad rakho.
    Iske bina "kisne kisko ban kiya" ka koi record hi nahi tha — appeal ya
    galti sudhaarne ka koi raasta nahi bachta."""
    with _db_lock:
        d = db_load()
        journal = d.setdefault("audit", [])
        journal.append({
            "ts": _now(),
            "at": time.time(),
            "actor": int(actor_id) if str(actor_id).lstrip("-").isdigit() else str(actor_id),
            "action": str(action)[:40],
            "target": (int(target) if str(target).lstrip("-").isdigit() else str(target or "")) or None,
            "chat": (int(chat_id) if str(chat_id).lstrip("-").isdigit() else str(chat_id or "")) or None,
            "detail": str(detail)[:200],
        })
        if len(journal) > AUDIT_KEEP:
            del journal[: len(journal) - AUDIT_KEEP]
        db_save(d)


def audit_tail(n: int = 20) -> list:
    with _db_lock:
        return list(db_load().get("audit", []))[-max(1, int(n)):]


# =====================================================================
#  AI USAGE LEDGER  (global + per-group daily budget)
# =====================================================================
def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def ai_usage() -> dict:
    with _db_lock:
        d = db_load()
        usage = d.setdefault("ai_usage", {})
        key = _today_key()
        if usage.get("day") != key:
            # Naya din -> counters reset. Sirf ek chhota dict, poori history nahi.
            usage.clear()
            usage.update({"day": key, "total": 0, "groups": {}})
            db_save(d)
        return usage


def ai_budget_ok(group_id=None, daily_max=0, group_max=0) -> bool:
    """Roz ka budget bacha hai? (0 = cap nahi)"""
    usage = ai_usage()
    if daily_max and int(usage.get("total", 0)) >= int(daily_max):
        return False
    if group_id is not None and group_max:
        got = int((usage.get("groups") or {}).get(str(group_id), 0))
        if got >= int(group_max):
            return False
    return True


def ai_budget_spend(group_id=None):
    with _db_lock:
        d = db_load()
        usage = d.setdefault("ai_usage", {})
        key = _today_key()
        if usage.get("day") != key:
            usage.clear()
            usage.update({"day": key, "total": 0, "groups": {}})
        usage["total"] = int(usage.get("total", 0)) + 1
        if group_id is not None:
            groups = usage.setdefault("groups", {})
            gk = str(group_id)
            groups[gk] = int(groups.get(gk, 0)) + 1
            if len(groups) > 2000:
                for k in list(groups.keys())[: len(groups) - 2000]:
                    groups.pop(k, None)
        db_save(d)


# =====================================================================
#  PRIVACY: DELETE + RETENTION  (PRIV-01)
# =====================================================================
def delete_user_data(uid) -> dict:
    """Ek user ka SAB kuch hatao — saare stores se, ek hi write me.

    Ye function jaan-boojh kar har jagah dekhta hai, kyunki aadhi-adhoori
    deletion (jo audit me GAP thi) user ko jhoota bharosa deti hai:
      users, referrals (dono taraf), banned, done, requests, audit entries
      jahan wo target tha.
    AI conversation memory bot.py ke process me hai — wo bot.py hatata hai.
    """
    uid = int(uid)
    key = str(uid)
    out = {"user": False, "referrals": 0, "referred_by": 0, "banned": False,
           "done": 0, "requests": 0, "audit": 0}
    with _db_lock:
        d = db_load()
        users = d.setdefault("users", {})
        if users.pop(key, None) is not None:
            out["user"] = True

        refs = d.setdefault("referrals", {})
        mine = refs.pop(key, None)
        if mine:
            out["referrals"] = int(mine.get("count", 0) or 0)
        # Jinko isne refer kiya, unke "ref_by" bhi hatao (warna adhoora link
        # reh jata hai aur analytics jhoot bolti hai).
        for other_id, entry in users.items():
            if str(entry.get("ref_by") or "") == key:
                entry.pop("ref_by", None)
        # Iska id kisi aur ke referral list me ho to hatao + count ghatao.
        for rk, rv in list(refs.items()):
            lst = rv.get("users") or []
            if uid in lst:
                lst.remove(uid)
                rv["users"] = lst
                rv["count"] = max(int(rv.get("count", 1)) - 1, len(lst))
                out["referred_by"] += 1

        banned = d.setdefault("banned", [])
        if uid in banned:
            banned.remove(uid)
            out["banned"] = True

        done = d.setdefault("done", {})
        if done.pop(key, None) is not None:
            out["done"] = 1

        requests_ = d.setdefault("requests", {})
        if requests_.pop(key, None) is not None:
            out["requests"] = 1

        journal = d.setdefault("audit", [])
        before = len(journal)
        d["audit"] = [a for a in journal
                      if str(a.get("actor")) != key and str(a.get("target")) != key]
        out["audit"] = before - len(d["audit"])
        # Bachi hui entries me bhi iska id text me na bacha rahe (audit AV-03):
        # detail me kabhi message text ya id chipak sakti hai.
        for a in d["audit"]:
            detail = str(a.get("detail") or "")
            if key in detail:
                a["detail"] = detail.replace(key, "[removed]")

        db_save(d)

    # Caches bhi saaf karo — warna delete ke baad bhi "banned" ya "verified"
    # purani memory se chalta rahega.
    if _ban_cache is not None:
        _ban_cache.discard(uid)
    if _done_cache is not None:
        _done_cache.pop(key, None)
    if _requests_cache is not None:
        _requests_cache.pop(key, None)
    _seen_cache.pop(uid, None)
    return out


def purge_stale(cache_days: int = 30, user_days: int = 0) -> dict:
    """Retention: purane cache hatao (aur chaho to inactive users bhi).
    Ye periodic chalta hai — warna done/requests/audit hamesha badhte rehte."""
    removed = {"done": 0, "requests": 0, "audit": 0, "users": 0}
    now = time.time()
    cache_cut = now - max(1, int(cache_days)) * 86400 if cache_days else 0
    if not cache_cut and not user_days:
        return removed
    with _db_lock:
        d = db_load()
        changed = False
        if cache_cut:
            # done / requests: {user: {chat_or_name: ts}} — andar ke timestamps dekho
            done = d.setdefault("done", {})
            for k in list(done.keys()):
                per = done.get(k) or {}
                if isinstance(per, dict):
                    fresh = {n: t for n, t in per.items()
                             if not cache_cut or float(t or 0) >= cache_cut}
                    if len(fresh) != len(per):
                        removed["done"] += len(per) - len(fresh)
                        changed = True
                        if fresh:
                            done[k] = fresh
                        else:
                            done.pop(k, None)
            req = d.setdefault("requests", {})
            for k in list(req.keys()):
                per = req.get(k) or {}
                if isinstance(per, dict):
                    fresh = {n: t for n, t in per.items()
                             if not cache_cut or float(t or 0) >= cache_cut}
                    if len(fresh) != len(per):
                        removed["requests"] += len(per) - len(fresh)
                        changed = True
                        if fresh:
                            req[k] = fresh
                        else:
                            req.pop(k, None)
            journal = d.setdefault("audit", [])
            fresh = [a for a in journal if float(a.get("at") or 0) >= cache_cut]
            if len(fresh) != len(journal):
                removed["audit"] = len(journal) - len(fresh)
                d["audit"] = fresh
                changed = True
        if user_days:
            cut = now - int(user_days) * 86400
            users = d.setdefault("users", {})
            for k in list(users.keys()):
                stamp = str(users[k].get("last_seen") or "")
                try:
                    ts = datetime.strptime(stamp, "%Y-%m-%d %H:%M UTC").replace(
                        tzinfo=timezone.utc).timestamp()
                except Exception:
                    continue
                if ts < cut:
                    users.pop(k, None)
                    removed["users"] += 1
                    changed = True
        if changed:
            db_save(d)
    return removed


# =====================================================================
#  LIVE CONNECTION TEST  (/mongo)
# =====================================================================
async def ping():
    """Abhi connect karke dekho. Fail hone par exception phenko —
    /mongo uska asli reason dikhata hai. Password kabhi print nahi hota."""
    await _coll().database.command("ping")


def reset_client():
    """Purana (shayad dead) client chhod do — agla call naya banayega."""
    _reset_client()


# ---------------------------------------------------------------------
#  Purane naam — bot.py ke call sites inhi se chalte hain
# ---------------------------------------------------------------------
_db_load = db_load
_db_save = db_save
_db_default = db_default
_db_write_json = db_write_json
_mongo_error_hint = mongo_error_hint
