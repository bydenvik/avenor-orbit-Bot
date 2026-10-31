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
links, kick, delete, pin, and promote.

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

**Storage.** State lives in memory and is flushed to MongoDB every five seconds. A free Atlas cluster
never expires, so posts, chat IDs, and user records survive redeploys. Without MongoDB configured the
bot falls back to a local JSON file.

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

---

## Quick start

```bash
git clone https://github.com/bydenvik/avenor-orbit-Bot.git
cd avenor-orbit-Bot
pip install -r requirements.txt
cp env.example.txt .env      # then fill in the values below
python bot.py
```

With no `WEBHOOK_URL` set the bot long-polls, which is what you want locally. On Render it switches to
webhook mode automatically.

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
`AUTO_APPROVE_JOIN`, `NOTIFY_OWNER`. The full list with defaults is in `env.example.txt`.

---

## Commands

**Everyone** — `/start` `/help` `/id` `/ai` `/rules` `/report` `/ref`

**Admins** — `/admins` `/settitle` `/invitelink` `/kick` `/unkick` `/del` `/pin` `/promote` `/post` `/posts`

**Owner** — `/stats` `/diag` `/mongo` `/chats` `/perms` `/getid` `/setid` `/broadcast` `/ban` `/unban`

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

1. **Boot** — `mongo_start()` pings Atlas and loads the state document into memory. If the collection
   is empty, the existing `data/db.json` is uploaded as the seed.
2. **While running** — every write updates memory and sets a dirty flag. No handler ever waits on the
   network.
3. **Every five seconds** — a background task writes the state if it is dirty.
4. **Every ten minutes** — a local JSON copy is written as a safety net.
5. **Shutdown** — one final forced flush.

Free-tier Atlas limits worth knowing: 0.5 GB storage, 500 connections, roughly 100 operations per
second, and the cluster pauses after 30 days with no connections. A post record is about 360 bytes and
a user record about 160 bytes, so 0.5 GB holds roughly 1.4 million posts.

**Network Access must include `0.0.0.0/0`.** Render's outbound IP is not fixed, so an allow-list of
specific addresses will break on the next deploy.

---

## Deploy on Render

- **Build command:** `pip install -r requirements.txt`
- **Start command:** `python bot.py` — note `bot.py`, not `bor.py`
- **Health check path:** `/health`

Set the seven variables above under Environment, then deploy. `render.yaml` is included for Blueprint
deploys; with a normal Web Service Render ignores it and the Dashboard settings apply.

After the first deploy, run `/diag` — it reports whether storage is connected and whether the gate is
ready. `/mongo` runs a live connection test and prints the real reason if it fails, without ever
printing the password.

**Enable Guest Mode** in BotFather → Bot Settings. Without it the bot never receives guest mentions.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `can't open file 'bor.py'` | Typo in the Start command | Set it to `python bot.py` |
| `bad auth : Authentication failed` | Wrong password, or the Atlas account password instead of the database user's | Reset the **database user's** password, not your Atlas login |
| `Username and password must be escaped` | An unescaped character in the URI | Use `MONGO_PASSWORD`; the bot encodes it |
| `DNS query name does not exist` | Placeholder or typo in the host | Copy the host from Atlas, never type it |
| Buttons have no colour | Telegram client older than 9 Feb 2026 | Button colour is a client feature; check on web.telegram.org |
| A channel shows no member count | The bot is not an admin there | Make it an admin, or bind the ID with `/setid` |

---

## Notes and limits

- The bot cannot join a chat by itself. Telegram has no such method for bots.
- A channel sends no "member joined" event, so welcomes work in groups only.
- Conversation memory is per-process; it resets on restart.
- On Render's free tier the service sleeps after about 15 minutes idle, so the first request after a
  pause takes 30–50 seconds. A scheduled post may fire late for the same reason.
- `gitignore.txt` in the repository is inert. The file must be named `.gitignore` for GitHub to apply
  it; otherwise `.env` and `data/` can be committed.

---

## License

MIT — see [LICENSE](LICENSE).
