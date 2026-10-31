# Operations

Everything an operator needs when something is wrong. Kept short on purpose — the point is that you
can read it at 2 a.m.

---

## Health signals

| Signal | Where | Meaning |
|---|---|---|
| `GET /health` | always 200 | `status` is `ok` or `degraded`; carries storage state, durability, retry countdown, last error, uptime, today's AI usage |
| `GET /ready` | 200 / 503 | 503 means data is **not** on a durable store. Point your uptime monitor at this one |
| `/diag` | Telegram, owner | Storage state, durability, gate readiness, per-channel permissions, today's AI usage |
| `/mongo` | Telegram, owner | Live connection test with the real error mapped to plain language; never prints the password |
| `/audit` | Telegram, owner | The last 25 privileged actions: actor, action, target, chat |

If Render's health check is pointed at `/health` (it is, in `render.yaml`), a degraded bot keeps
running rather than being restarted in a loop. That is intentional: a degraded bot still serves users.
Use `/ready` for alerting.

---

## MongoDB is down — what happens, and what to do

The bot does not stop. It goes `DEGRADED`:

- every write goes to `data/db.json` with `fsync`, so nothing acknowledged is lost to a crash;
- reconnection attempts run with bounded exponential backoff plus jitter (1 s → 60 s cap);
- the owner is messaged with the plain-language cause;
- `/health` reports `degraded` and `/ready` returns 503.

**What you should do**

1. Read the owner message — it names the usual cause (bad auth, DNS, network access, TLS).
2. Fix it in Atlas (password, access list, database user role). No redeploy is needed.
3. Watch `/diag` until the state reads `READY`. The bot then pushes the state written during the outage
   to MongoDB, so nothing is lost.
4. If the process restarts before MongoDB is back, the local JSON file is still the seed, so recovery
   survives a restart too.

**What you should not do** — do not "fix" it by redeploying. A redeploy on Render Free wipes the
ephemeral disk, and the local file is the only copy of the outage's changes.

---

## Restore from backup

Atlas snapshots are the backup. To restore into a clean database:

1. In Atlas, take a snapshot restore into a **new** cluster or database.
2. Point the service at it: set `MONGO_DB` to the restored database name (or swap `MONGO_HOST`).
3. Deploy or restart, then run `/diag` and check the counts: users, chats, posts.
4. Verify a known referral: `/ref` for a test account should show the expected count.
5. Keep the old database until you are satisfied, then delete it.

The state is a single document, so "referential integrity" here means: `users[].ref_by` should point at
an existing user, and `referrals[].users` should be a subset of known ids. A quick check:

```javascript
// Atlas > Browse Collections > mongosh
const s = db.state.findOne({_id: "state"});
print("users:", Object.keys(s.users).length);
print("dangling ref_by:", Object.values(s.users)
      .filter(u => u.ref_by && !s.users[String(u.ref_by)]).length);
print("posts:", Object.keys(s.posts || {}).length);
print("doc bytes:", Object.bsonsize(s));
```

`doc bytes` above 12 MiB means the owner has already been warned; above 16 MiB writes will be pruned.
See `docs/AUDIT-RESPONSE.md` (DB-02) for the real fix.

---

## Rotating a credential

**Bot token** — BotFather → `/revoke`. Then update `BOT_TOKEN` and redeploy. The old webhook secret is
regenerated per boot, so nothing else needs changing.

**MongoDB password** — Atlas → Database Access → edit user → Edit Password. Then update
`MONGO_PASSWORD` and redeploy. Until the redeploy lands, the bot will be `DEGRADED` and writing to the
local file; the reconnect will succeed on its own once the new password is in place.

**AI API key** — create the new key, set `AI_API_KEY`, redeploy, then delete the old key. A bad key is
now classified as fatal rather than retried, so a mistake shows up immediately in the logs instead of
looking like provider downtime.

**Webhook secret** — leave `WEBHOOK_SECRET` unset and the bot generates a fresh random secret on every
boot, so there is nothing to rotate.

---

## AI cost control

Three limits, all in `config.py`:

| Setting | Default | Effect |
|---|---|---|
| `AI_USER_MAX` / `AI_USER_WINDOW` | 15 per 60 s | per-user throttle |
| `AI_GROUP_DAILY_MAX` | 400 | one group cannot consume the whole budget |
| `AI_DAILY_MAX` | 3000 | bot-wide cap per UTC day |

Usage is in `/diag` and in `/health` (`ai_today`). When the daily cap is hit, users get a polite
message with the direct-AI link instead of a failure. Internal AI calls (post episode lines, the
deep-link welcome line) are not counted, so publishing never stops because of the budget.

---

## Scheduled posts

Scheduled posts are re-armed at startup from the stored `post_at`. A post whose time has already
passed is sent immediately rather than being dropped — the previous behaviour lost it silently. If the
service was asleep on Render Free, expect the post to arrive late, not never.

---

## Logs worth knowing

| Log line | Meaning |
|---|---|
| `Storage state: READY -> DEGRADED (...)` | The transition that matters; the owner also gets a DM |
| `MongoDB retry #n, Xs baad` | Backoff in progress |
| `Duplicate update <id> — skip.` | Telegram retried a delivery; the guard caught it |
| `Storage document X MB — 16 MB limit ke kareeb` | Time to look at DB-02 |
| `Thinking level 'minimal' is not supported by ...` | Configuration corrected itself; fix `AI_THINKING_LEVEL` |
| `AI fail (model=... try=...)` | Provider error; a fatal one is not retried |

---

## Deploying a change

1. `pip install -r requirements-dev.txt`
2. `python -m pytest` — must be green
3. `python -m pyflakes bot.py config.py storage.py fonts.py tests/` — must be silent
4. Deploy
5. `/diag` — storage `READY`, gate as expected
6. `/health` — `status: ok`

Never deploy with a failing test to "fix it afterwards"; the persistence path is the one thing you
cannot repair after the fact.
