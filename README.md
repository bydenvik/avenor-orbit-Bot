# Avenor Orbit

A Telegram bot that manages a community and publishes to channels from one place. It greets and
moderates members, answers questions as an AI assistant, builds and schedules channel posts with a
tracked deep link, and keeps its state in MongoDB so nothing is lost on redeploy.

- **Bot:** [@AvenorOrbitBot](https://t.me/AvenorOrbitBot)
- **Channel:** [Avenor Apps AI](https://t.me/AvenorAppsAI)
- **Group:** [Avenor AI Community](https://t.me/AvenorAICommunity)
- **Repository:** [github.com/bydenvik/avenor-orbit-Bot](https://github.com/bydenvik/avenor-orbit-Bot)
- **Developer:** [bydenvik](https://github.com/bydenvik) · [X](https://x.com/bydenvik) · [Reddit](https://www.reddit.com/u/bydenvik)

---

## What it does

**Community management.** Welcomes new members with a banner, serves `/rules`, handles reports,
gives every member a referral link with a live count, and lets admins rename the group, create invite
links, kick, delete, pin, and promote. Every privileged action is written to an action log, so
"who banned whom, and why" has an answer later.

**AI assistant.** Replies to any private message without needing a command, and answers in a group
when it is @mentioned. It mirrors the language the person writes in, remembers the last six turns per
person per chat, and never claims to be human.

**Guest mode.** Telegram lets a bot answer in a group it has *not* joined when someone mentions it.
With Guest Mode enabled in BotFather, `@AvenorOrbitBot` works in any group without being added — the
bot replies in place, with your channel buttons attached. This is the only supported way for a bot to
reach a chat it is not a member of; a bot cannot add itself anywhere.

**Post builder.** `/post` walks through a type, the target channels, an optional banner, and that
type's fields, then publishes a photo post with a caption and a *Download & Watch* button. The button
carries a deep link back to the bot, so every click is attributable. Posts can be scheduled in IST.

**Privacy.** `/privacy` explains exactly what is stored and where it goes; `/delete_my_data` erases
the record, the referral entry, and the conversation memory in one step. See [PRIVACY.md](PRIVACY.md).

**Storage.** State lives in memory and is flushed to MongoDB every five seconds, with a local JSON
copy as a safety net. If MongoDB is unavailable the bot does not silently continue in a mode that
loses data — see [Storage](#storage) below.

---

## Stack

| Layer | Choice |
|---|---|
| Language | Python 3.12 |
| Telegram | python-telegram-bot 22.8 (Bot API 10.0) |
| Web server | FastAPI + Uvicorn, webhook mode |
| AI | `google-genai` — `gemini-3.8-flash`, falling back to `gemini-3.5-flash-lite` |
| Storage | In-memory cache, write-behind to MongoDB Atlas (`pymongo` async client) |
| Hosting | Render |

The code is three modules, split along the one boundary that matters:

| File | What lives there |
|---|---|
| `bot.py` | Telegram handlers, commands, post builder, webhook server |
| `storage.py` | Persistence: state machine, Mongo, JSON fallback, referrals, audit log, deletion |
| `config.py` | Every setting, with defaults |

---

## Quick start

```bash
git clone https://github.com/bydenvik/avenor-orbit-Bot.git
cd avenor-orbit-Bot
pip install -r requirements.lock        # exact versions
cp env.example.txt .env                 # then fill in the values below
python bot.py
```

With no `WEBHOOK_URL` set the bot long-polls, which is what you want locally. On Render it switches to
webhook mode automatically.

The test suite is optional. If you add `tests/`, `pytest.ini` and `requirements-dev.txt`, run it with:

```bash
pip install -r requirements-dev.txt
python -m pytest
```

---

## Configuration

Seven variables are needed. Everything else has a working default in `config.py`.

| Variable | What to put |
|---|---|
| `BOT_TOKEN` | Token from [@BotFather](https://t.me/BotFather) |
| `OWNER_ID` | Your Telegram user ID (`/id` gives it to you) |
| `AI_API_KEY` | Google AI Studio key |
| `MONGO_HOST` | `cluster0.xxxxx.mongodb.net` — the host only |
| `MONGO_USER` | Atlas database user |
| `MONGO_PASSWORD` | That user's password |
| `BOT_USERNAME` | `@AvenorOrbitBot` |

**Why the host is separate from a connection string.** A full `mongodb+srv://user:pass@host/` string
has to be hand-carried between three apps, and it breaks in four different ways: a typo in the host
(`1` vs `l`, `0` vs `o`), a placeholder left in, a password containing `@` or a space, or a bad copy.
Three short fields cannot break that way, and the bot assembles and percent-encodes the URI itself.
`MONGO_URI` is still accepted if you prefer one string.

Optional: `MONGO_DB` (default `avenor_orbit`), `ADMIN_IDS`, `ADMIN_NAMES`, `BOT_BRAND`, `GUEST_MODE`,
`AUTO_APPROVE_JOIN`, `NOTIFY_OWNER`, `AI_THINKING_LEVEL`, `AI_DAILY_MAX`, `PRIVACY_URL`,
`CACHE_RETENTION_DAYS`, `DROP_PENDING_UPDATES`. The full list with defaults is in `env.example.txt`.

---

## Commands

**Everyone** — `/start` `/help` `/id` `/ai` `/rules` `/report` `/ref` `/privacy` `/delete_my_data`

**Admins** — `/admins` `/settitle` `/invitelink` `/kick` `/unkick` `/del` `/pin` `/promote` `/post` `/posts`

**Owner** — `/stats` `/diag` `/mongo` `/chats` `/perms` `/getid` `/setid` `/broadcast` `/ban` `/unban` `/audit` `/links` `/btnstyle`

Group commands need the bot to be an admin in that group. `/help` shows each role only its own
commands, so a member never sees a list they cannot use.

---

## `/post`

| Type | Fields |
|---|---|
| APK | Name, Version, Size, Android, Link |
| Update | Name, Version, Whats New, Link |
| AI Tool | Name, What It Does, Pricing, Link |
| News | Headline, Summary, Source, Link |
| Tip | Title, Tip, Link |

Targets are set in `POST_CHANNELS`; both the channel and the group are configured. The bot must be an
admin where it posts.

The caption is built from the type's fields, so adding a category is a two-line change in `config.py`:
add it to `POST_TYPES` and give it an entry in `POST_FIELDS_BY_TYPE`. An optional `POST_BADGE_BY_TYPE`
entry controls the badge line (`NEW APK`, `APP UPDATED`, and so on).

If a channel rejects the HTML caption, the bot retries that channel once with a plain caption. The
button still carries the link, so the post is not lost.

---

## Storage

Storage is a small state machine, not a boolean. `storage.py` reports one of:

| State | Meaning |
|---|---|
| `CONNECTING` | First connection attempt is running |
| `READY` | MongoDB connected; writes are write-behind (flushed every 5 s) |
| `DEGRADED` | MongoDB is down; every write goes to the local JSON file **with `fsync`** |
| `RECONNECTING` | Backoff elapsed, trying again |
| `DISABLED` | No MongoDB configured |

What happens, in order:

1. **Boot** — connect and `ping`. If a previous degraded period wrote data, that newer local state is
   pushed to MongoDB (the stale remote copy is not loaded over it). If the collection is empty, the
   local `data/db.json` is uploaded as the seed.
2. **While running** — every write updates memory and sets a dirty flag. No handler waits on the
   network.
3. **Every five seconds** — a background task writes the state if it is dirty.
4. **Every sixty seconds** — a `ping` health check. A connection that died silently (TCP open, server
   gone) is caught here and the bot drops to `DEGRADED` instead of failing writes one by one.
5. **While degraded** — reconnect attempts run with bounded exponential backoff plus jitter
   (1 s, 2 s, 4 s … capped at 60 s). No process restart is needed, and the owner is messaged.
6. **Every ten minutes** — a local JSON copy is written as a safety net.
7. **Shutdown** — one final forced flush.

Because the fallback write is `fsync`ed, an abrupt stop does not lose what the bot acknowledged. That
still is not durable storage on a Render Free service, whose disk is ephemeral — so treat `DEGRADED`
as a warning, not as a working mode. `GET /ready` returns 503 while it is degraded; `/diag` shows the
state and whether data is durable.

Free-tier Atlas limits worth knowing: 0.5 GB storage, 500 connections, roughly 100 operations per
second, and the cluster pauses after 30 days with no connections. A post record is about 360 bytes and
a user record about 160 bytes, so 0.5 GB holds roughly 1.4 million posts.

### Network access for MongoDB

The old advice in this repository — "add `0.0.0.0/0`, it is required" — was wrong and is now removed.
Opening Atlas to the whole internet leaves the password as the only barrier. In order of preference:

1. Render's fixed outbound IPs (paid plans) or your database provider's private endpoint / VPC peering.
2. An Atlas access-list entry limited to that range.
3. `0.0.0.0/0` only when nothing else is possible — and then with a long unique password, TLS (default
   with `mongodb+srv`), and a database user scoped to this one database with `readWrite`, not
   `Atlas admin`.

---

## Deploy on Render

- **Build command:** `pip install -r requirements.lock`
- **Start command:** `python bot.py` — note `bot.py`, not `bor.py`
- **Health check path:** `/health`

Set the seven variables above under Environment, then deploy. `render.yaml` is included for Blueprint
deploys; with a normal Web Service Render ignores it and the Dashboard settings apply.

`render.yaml` ships with `plan: starter`. Render's Free plan is not suitable for this bot: the disk is
ephemeral, the service sleeps after ~15 minutes idle, and the first request after a pause takes
30–50 seconds. Use Free only to try it out, and keep MongoDB configured.

After the first deploy, run `/diag` — it reports storage state, whether data is durable, whether the
gate is ready, and today's AI usage. `/mongo` runs a live connection test and prints the real reason
if it fails, without ever printing the password.

**Enable Guest Mode** in BotFather → Bot Settings. Without it the bot never receives guest mentions.

### Endpoints

| Path | Purpose |
|---|---|
| `GET /health` | Always 200. `status` is `ok` or `degraded`; includes storage state and uptime. |
| `GET /ready` | 200 when data is on a durable store, 503 when degraded. Point your uptime monitor here. |
| `POST /tg/<secret>` | The Telegram webhook. Rejects a bad `X-Telegram-Bot-Api-Secret-Token` with 403. |

---

## Update delivery

Telegram retries a webhook delivery when the reply is slow or fails, and it exposes `update_id` for
exactly this reason. The webhook handler therefore:

- **deduplicates** — an `update_id` that already completed is skipped, so one referral is never
  counted twice (a 15-minute in-memory guard);
- **serialises per chat** — two updates from the same chat never run at the same time, so the shared
  state behind the gate, referrals and `/post` cannot interleave; different chats still run
  concurrently, up to `WEBHOOK_CONCURRENCY`;
- **preserves pending updates** — `drop_pending_updates` is now `False` by default, so a restart no
  longer throws away messages and join requests that arrived during the restart.

---

## Privacy

`/privacy` shows what is stored and who sees it; `/delete_my_data` erases the user's record, their
referral count, their id from other people's invite lists, and the assistant's memory of the
conversation. Retention is configurable (`CACHE_RETENTION_DAYS`, `USER_RETENTION_DAYS`) and a daily
job clears old caches.

The published policy is [PRIVACY.md](PRIVACY.md). Register its URL in BotFather → Bot Settings →
Privacy Policy, and set `PRIVACY_URL` so `/privacy` links to it.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `can't open file 'bor.py'` | Typo in the Start command | Set it to `python bot.py` |
| `bad auth : Authentication failed` | Wrong password, or the Atlas account password instead of the database user's | Reset the **database user's** password, not your Atlas login |
| `Username and password must be escaped` | An unescaped character in the URI | Use `MONGO_PASSWORD`; the bot encodes it |
| `DNS query name does not exist` | Placeholder or typo in the host | Copy the host from Atlas, never type it |
| `/diag` shows `DEGRADED` | MongoDB unreachable | Check Atlas Network Access; the bot reconnects on its own and the owner is messaged |
| `/ready` returns 503 | Data is not on a durable store | Fix MongoDB; do not ignore this on a Free Render service |
| Buttons have no colour | Telegram client older than 9 Feb 2026 | Button colour is a client feature; check on web.telegram.org |
| A channel shows no member count | The bot is not an admin there | Make it an admin, or bind the ID with `/setid` |
| `400 Bad Request: can't parse entities` on a post | An unescaped character in a caption field | The bot retries once with a plain caption; if it persists, check the field for stray HTML |

---

## Notes and limits

- The bot cannot join a chat by itself. Telegram has no such method for bots.
- A channel sends no "member joined" event, so welcomes work in groups only.
- Conversation memory is per-process; it resets on restart.
- All state still lives in one MongoDB document. It is bounded (posts, links, cached marks, audit log
  are all capped, and the document is measured before every write, with the owner alerted at 12 MiB
  and derived caches pruned at 16 MiB), but a real fix is entity-level collections — see
  [AUDIT-RESPONSE.md](AUDIT-RESPONSE.md), finding DB-02.
- On Render's free tier the service sleeps after about 15 minutes idle, so the first request after a
  pause takes 30–50 seconds. A scheduled post may fire late for the same reason.
- `python-telegram-bot` 22.8 targets Bot API 10.0. Rich messages and the 10.1–10.3 features are not
  available until PTB ships them.

---

## Documentation

- [AUDIT-RESPONSE.md](AUDIT-RESPONSE.md) — every audit finding, what was changed, and what is
  deliberately still open
- [OPERATIONS.md](OPERATIONS.md) — degraded mode, restore, credential rotation, monitoring
- [PRIVACY.md](PRIVACY.md) — the published policy
- [CHANGELOG.md](CHANGELOG.md) — what changed in this revision

---

## License

MIT — see [LICENSE](LICENSE).
