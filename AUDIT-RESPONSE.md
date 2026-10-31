# Audit response — what changed, and what is still open

This document answers the production audit of 31 October 2026 line by line. Every finding has a
verdict: **Fixed**, **Partly fixed**, or **Open**, with the reason. Nothing is marked fixed unless the
code was changed and a test covers it.

Revision: `main` at `a76ce8e` + this change. Tests: 51, all passing (`python -m pytest`).

---

## Summary

| Finding | Severity | Verdict | Where |
|---|---|---|---|
| DB-01 Mongo recovery permanently disabled | High | **Fixed** | `storage.py`, `tests/test_storage_recovery.py` |
| DB-02 One document can hit the 16 MiB limit | Medium | **Partly fixed** — bounded, measured, alerted; no entity split | `storage.py` |
| DB-03 Write-behind crash window | Medium | **Partly fixed** — degraded path is `fsync`ed; the 5 s window remains | `storage.py` |
| DB-04 Write amplification / single writer | Medium | **Partly fixed** (same work as DB-02) | `storage.py` |
| TG-01 Webhook concurrency | Medium | **Fixed** — per-chat serialisation + dedupe | `bot.py` |
| TG-02 `drop_pending_updates=True` | Medium | **Fixed** — config, default `False` | `config.py`, `bot.py` |
| TG-03 PTB 22.8 vs Bot API 10.3 | Low | **Open** — depends on upstream PTB | — |
| AI-01 `minimal` thinking level rejected | Low | **Fixed** — default `low`, per-model validation | `config.py`, `bot.py` |
| SEC-01 README recommends `0.0.0.0/0` | Medium | **Fixed** in docs | `README.md`, `env.example.txt` |
| PRIV-01 No privacy notice or deletion | Medium | **Fixed** | `bot.py`, `storage.py`, `PRIVACY.md` |
| ARCH-01 Monolithic module | Medium | **Partly fixed** — persistence extracted; handlers not split | `storage.py` |
| QA-01 No tests or CI | Medium | **Fixed** — 51 tests + GitHub Actions | `tests/`, `.github/workflows/ci.yml` |
| OPS-01 Render Free for a stateful bot | Medium | **Partly fixed** — blueprint and docs; the plan is the operator's call | `render.yaml` |
| OPS-02 No lockfile | Low | **Fixed** | `requirements.lock` |
| DOC-01 Stale `.gitignore` warning | Low | **Fixed** | `README.md` |

Four defects the audit did not list were also found and fixed; they are in the last section.

---

## DB-01 — MongoDB recovery was permanently disabled (High)

**What the audit said.** `_mongo_ready` went `False` on a startup failure, `mongo_flush()` returned
immediately, and `mongo_loop()` never retried the connection. The process could stay in JSON-only mode
for its whole lifetime.

**What was actually worse.** `_db_save()` looked like this:

```python
if MONGO_ENABLED:
    _mongo_dirty = True      # and nothing else
    return
_db_write_json(data)
```

So in a Mongo-configured deployment that had fallen back, a write did **not** go to the JSON file
either. It set a flag that `mongo_flush()` then discarded, because `_mongo_ready` was `False`. The only
copy on disk was the ten-minute mirror. The audit called it "JSON-only mode"; in practice it was
"RAM-only mode with a ten-minute checkpoint". A restart in between lost up to ten minutes of
membership, referral and moderation changes, not merely the redeploy risk the audit described.

**What was done.**

1. `storage.py` implements an explicit state machine: `CONNECTING → READY → DEGRADED →
   RECONNECTING → READY`, with `DISABLED` when no MongoDB is configured.
2. `loop()` no longer just flushes. Each tick it: reconnects if the state is not `READY` and the
   backoff has elapsed; runs a `ping` health check every `STORAGE_HEALTH_EVERY` (60 s) to catch a
   connection that died silently; flushes when dirty; and writes the local mirror on its own interval.
3. Backoff is bounded exponential with jitter — 1, 2, 4 … capped at 60 s — so a fleet of bots does not
   hit Atlas in lockstep.
4. **Degraded writes are durable on disk.** In `DEGRADED`, `db_save()` writes the JSON file with
   `fsync` and `os.replace` on every save. Nothing acknowledged is left only in memory.
5. **Reconnect prefers the newer state.** Any write during `DEGRADED` sets `_pending_push_full`; on the
   next successful connect the local state is pushed to MongoDB instead of loading the stale remote
   document over it. Without this the recovery itself would have destroyed the outage's worth of work.
6. A failed `flush()` now degrades the state immediately rather than waiting up to 60 s for the health
   check, because a failed write already proves the connection is gone.
7. The owner is messaged on every transition into `DEGRADED`, with the same plain-language cause
   mapping `/mongo` uses.
8. Observability: `GET /health` reports state, durability, retry countdown and last error; `GET /ready`
   returns 503 while data is not durable; `/diag` shows state and durability.

**Tests** (`tests/test_storage_recovery.py`): connect failure schedules a retry; degraded writes land on
disk; ready writes stay write-behind; reconnect pushes local state over a stale remote document;
first connect loads remote state; empty collection is seeded; a failed flush degrades immediately;
health check detects a dead connection; backoff grows, caps, and jitters.

**Acceptance criterion from the audit** — "after a transient Mongo outage, the application reconnects
without process restart, and all acknowledged state changes survive restart" — is met by the code and
covered by tests against a fake collection. It has **not** been exercised against a live Atlas cluster
or a real restart; that needs a staging environment.

---

## DB-02 / DB-04 — one document, 16 MiB, write amplification (Medium)

**Not fully fixed.** Splitting into entity collections means a data migration and rewriting roughly
forty call sites that assume one document; doing that blind, with no staging database to verify
against, would risk the very data the audit is worried about.

**What was done instead**, so the failure mode stops being silent:

- Every growing map is bounded: `posts` (`POSTS_KEEP`), `links` (`LINKS_KEEP`), `done` and `requests`
  (5000 keys each, then 2000 on prune), `audit` (`AUDIT_KEEP`), referral id lists (500, with an exact
  counter), AI per-group counters (2000 groups).
- The document is measured with a real BSON encode before every write (`doc_size_bytes`), not
  estimated.
- Above `STORAGE_ALERT_BYTES` (12 MiB) the owner is warned. Above `STORAGE_DOC_LIMIT_BYTES` (16 MiB)
  derived caches are pruned and the owner is told exactly what was pruned — **users, referrals, chats
  and bans are never touched.**
- The plan for the real fix is below, in "Still open".

---

## DB-03 — write-behind crash window (Medium)

**Partly fixed.** The dangerous half is gone: a crash while degraded no longer loses anything, because
degraded writes are `fsync`ed. The `READY` path still batches to every 5 s, so an abrupt kill can lose
up to five seconds of changes.

That is a deliberate trade: flushing on every write cost about 77 ms per write in the original design
and would put a database round trip in front of every handler. If a deployment needs a tighter window,
set `STORAGE_FLUSH_EVERY=1`. A true fix is a per-mutation outbox, which belongs with the entity split.

---

## TG-01 — webhook concurrency (Medium)

**Fixed.** The route used to be:

```python
update = Update.de_json(await request.json(), app.bot)
await app.process_update(update)
```

FastAPI serves webhook requests concurrently, so two updates from the same chat could interleave
across `await` points while both mutate the shared state behind the join gate, referral counts and the
`/post` flow.

Now the handler holds a per-chat `asyncio.Lock` (keyed by `effective_chat.id`, falling back to
`update_id` for updates without a chat), inside a `Semaphore(WEBHOOK_CONCURRENCY)` so different chats
still run in parallel. Locks are dropped when idle, so the map cannot grow without bound.

---

## TG-02 — `drop_pending_updates=True` (Medium)

**Fixed.** It was hardcoded in both the webhook setup and `run_polling`. It is now
`DROP_PENDING_UPDATES`, default `False`, documented in `env.example.txt` and `render.yaml`, and
asserted by a test.

---

## TG-03 — PTB 22.8 targets Bot API 10.0 (Low)

**Open, by design.** `python-telegram-bot` 22.8 is the current release and it targets Bot API 10.0.
Rich messages and the 10.1–10.3 capabilities need a PTB release that exposes them; upgrading the
dependency alone would not add the typed surface. Recorded in the README limits.

---

## AI-01 — `minimal` thinking level (Low)

**Fixed.** The default is now `low`, and `config.thinking_level_for(model)` validates the level against
`AI_THINKING_LEVELS_BY_MODEL` before the first request: `gemini-3.8-flash` accepts `low | medium |
high`; an unsupported value falls back to `low` (or to sending no thinking config at all), with a
warning. The old self-healing retry is still there as a last resort.

The AI retry loop was also tightened: it used to retry on **every** exception, including a bad API key
or a wrong model name, which burned attempts and showed the user "busy" for a configuration mistake.
Retries now happen only on retryable errors (5xx, 429, timeout, connection), with exponential backoff
and jitter; a fatal error moves on to the fallback model immediately.

---

## SEC-01 — `0.0.0.0/0` (Medium)

**Fixed in documentation.** The recommendation is gone from the README and `env.example.txt`, replaced
with an ordered preference: Render's fixed outbound IPs or a private endpoint; then a restricted Atlas
access list; then `0.0.0.0/0` only as a last resort, with a long unique password, TLS, and a database
user scoped to `readWrite` on this one database. The operator's Atlas settings themselves are outside
this repository and cannot be changed from here.

---

## PRIV-01 — privacy notice and deletion (Medium)

**Fixed.**

- `/privacy` — what is stored, what is not, who else sees the text (the AI provider), how long it is
  kept, and how to erase it. `PRIVACY_TEXT` overrides the built-in notice; `PRIVACY_URL` links to the
  published policy.
- `/delete_my_data` — private chats only, with an explicit confirm button. One call removes the
  account record, the referral entry, the user's id from other people's invite lists (and decrements
  their counts), ban entries, verification marks, join-request records, audit entries naming them, and
  every conversation-memory key — both `u<id>` and `g<chat>:u<id>`. The confirmation reports what was
  removed. The deletion is not itself written to the audit log, so no new record of the user is
  created.
- Retention: `CACHE_RETENTION_DAYS` (default 30) and `USER_RETENTION_DAYS` (default off), applied by a
  daily job and at startup.
- `PRIVACY.md` is the published policy, ready to register in BotFather.

**One decision for you.** The built-in notice and `PRIVACY.md` name Google as the AI processor, because
Telegram's Bot Developer Terms require telling users where their text goes. The bot's system prompt
still forbids the assistant from naming any provider in conversation. If you would rather not name it
in the notice either, set `PRIVACY_TEXT` — but then the notice is weaker than the terms expect.

---

## ARCH-01 — monolithic module (Medium)

**Partly fixed.** The persistence layer moved out of `bot.py` into `storage.py` (1,319 lines including
comments), and `bot.py` went from 3,871 to 3,615 lines. That is the boundary that mattered most: it is
the part with the recovery defect, and it is now testable on its own — which is why DB-01 has real
tests. Handlers, commands and the post builder are still in `bot.py`.

Extracting them safely needs tests around the Telegram handlers first (mocked `Update` objects), which
is the next step rather than something to attempt in the same change.

---

## QA-01 — no tests, no CI (Medium)

**Fixed.** 51 tests in `tests/`, and `.github/workflows/ci.yml` which runs `pyflakes`, `compileall`, an
import smoke test, `pytest`, and a check that `.env` or `data/` was never committed.

The suite covers: the recovery state machine and backoff; degraded durability; reconnect reconciliation;
BSON measurement and pruning; update dedupe; AI thinking-level validation; AI budgets; referral
integrity and bounding; gate counting and title matching; deletion across every store; retention;
the audit log; caption escaping and tag balancing; link normalisation; deep-link codes; chat-id
migration; join-request expiry; corrupt-JSON handling.

Not covered, and honestly so: the Telegram handler flows themselves (they need mocked updates), the
webhook route end to end, and anything against a live MongoDB. The audit's minimum release gate still
needs a staging environment.

---

## OPS-01 — Render Free (Medium)

**Partly fixed.** `render.yaml` now ships `plan: starter`, a `healthCheckPath`, `autoDeploy`, the
durability and AI-budget variables, and a comment block explaining why Free is unsuitable (ephemeral
disk, idle sleep, 30–50 s cold start). Whether the service actually runs on a paid plan is the
operator's account decision.

---

## OPS-02 — no lockfile (Low)

**Fixed.** `requirements.lock` pins the exact resolved set (generated 31 Oct 2026 on Python 3.12.13,
linux/amd64), and both `render.yaml` and CI install from it. `requirements-dev.txt` adds the test
tooling.

---

## DOC-01 — stale documentation (Low)

**Fixed, and three more contradictions found while reading the file:**

- The `.gitignore` warning is gone.
- `config.py` claimed join requests are *not* auto-approved and that the owner gets an Approve button;
  the code auto-approves. The comment now matches the code.
- `config.py` said the default model was `gemini-3.5-flash`; the code defaults to `gemini-3.8-flash`.
- `env.example.txt` said `AI_MAX_CHARS=1500`; the default is 3800.

---

## Four defects the audit did not list

1. **Referral count froze at 501.** `db_add_referral` set `count = len(ref["users"])` *after* capping
   the id list at 500, so from the 501st invite onward the count never moved. Anyone with more than 500
   invites saw a wrong number, and `/ref` reported it as truth. The counter is now maintained
   independently. Caught by `test_referral_id_list_is_bounded`.
2. **Degraded mode wrote nowhere.** Described under DB-01 — worse than the audit's description.
3. **AI retried fatal errors.** A wrong API key or model name produced two wasted attempts per model
   before the user saw "busy". Now classified properly.
4. **`/health` returned a constant.** `{"status": "ok"}` regardless of reality, so an external monitor
   could not tell a healthy bot from one running without durable storage. Now reports storage state,
   durability, uptime and today's AI usage, with `/ready` as the strict signal.

---

## Still open

| Item | Why it is open | What it needs |
|---|---|---|
| DB-02 / DB-04 — entity collections | Data migration plus ~40 call sites; no staging database here to verify against | A staging Atlas database, a migration script, and a dual-read period |
| DB-03 — 5 s flush window | Deliberate latency trade-off | A per-mutation outbox, best done with DB-02 |
| ARCH-01 — handler extraction | Needs mocked-update tests first, otherwise it is a blind rewrite | `tests/` for handlers, then split `bot.py` into `telegram/`, `community/`, `posts/` |
| TG-03 — Bot API 10.3 | Upstream PTB release | Upgrade when PTB ships 10.1+ |
| Handler-level regression tests | Not written yet | Mocked `Update`/`Context`, then the audit's negative authorisation matrix |
| Live outage drill | Needs a real database and a real restart | Staging run: stop MongoDB, mutate state, restart the database, confirm recovery and no data loss |
| Historical secret scan | Cannot be done from a snapshot | `git log -p` scan or a dedicated secret scanner on the full clone |
| Product features (RAG, moderation monitoring, appeals, multimodal) | Out of scope for a reliability pass | Stage 3–4 of the audit's plan, after the foundations above |
