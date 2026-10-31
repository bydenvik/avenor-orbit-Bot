# Changelog

## 2026-10-31 — second pass (two external audits + two user reports)

Response to the Qwen audit (`AV-02`) and the second ChatGPT audit (`AV-001`–`AV-007`), plus two
reported symptoms: a button tapped twice doing the work twice, and AI replies sometimes never
arriving. Full verdicts in [AUDIT-RESPONSE.md](AUDIT-RESPONSE.md).

### Fixed

- **Event loop blocked by disk writes (Qwen AV-02).** In degraded mode every save ran `fsync` on the
  event loop thread while holding the state lock, so one slow write stalled every other chat, the
  health endpoint and webhook delivery. JSON writes now go through a single background writer thread
  (latest snapshot only), and the Mongo flush's deepcopy + BSON sizing moved off the loop too.
- **Readiness overstated durability (AV-003).** A successful local write was treated as durable.
  Durability now has four explicit levels — `MONGO`, `PERSISTENT_DISK`, `EPHEMERAL`, `VOLATILE` — and
  `/ready` returns 503 unless data survives a restart. New setting: `LOCAL_DISK_PERSISTENT`.
- **Deletion acknowledged before it was durable (AV-005).** `/delete_my_data` now commits, verifies the
  record is gone, and only then reports success — and says plainly if the write had to wait.
- **Webhook acknowledged failures as success (AV-006).** A processing exception returned HTTP 200, so
  Telegram never retried and the update was silently lost. It now returns 500 so Telegram redelivers,
  and after `UPDATE_MAX_ATTEMPTS` records a dead letter instead of looping forever.
- **Retry dedupe lost on restart (AV-004).** The processed-`update_id` ring is now saved with the state
  document and restored on connect.
- **Duplicate taps (user report).** Tapping the same button twice — or ten times — ran the handler
  twice; "Post now" published two posts. Callbacks now have a per-user, per-button cooldown
  (`CB_COOLDOWN`), and the `/post` flow is cleared *before* publishing.
- **Duplicate messages.** Sending the same text twice produced two AI answers. The same text from the
  same person in the same chat within `MSG_DEDUPE_WINDOW` is answered once.
- **AI replies that never arrived (user report).** There was no timeout on the provider call, so a
  hung request left the user waiting forever. Every attempt now has a deadline (`AI_TIMEOUT`),
  concurrency is bounded (`AI_CONCURRENCY`), the "typing…" indicator is kept alive for the whole wait,
  and rate-limit or budget refusals are now announced once per cooldown instead of silently ignored.
- **Render manifest contradicted the docs (AV-001, AV-002).** `render.yaml` now installs
  `requirements.lock` and states `plan: free` honestly, with the reason Free is unsuitable.
- **Audit journal kept message text (Qwen AV-03).** `/del` records the message id, not its content, and
  deletion scrubs the user's id from any remaining journal entry.

### Added

- `/diag` now shows durability, the real `/ready` status, document size against the 16 MiB limit, and
  AI counters (calls, ok, fail, timeout, rate-limited, budget-blocked) — the "no reply" complaint had
  no evidence to inspect before this.
- Tests: 77 (up from 51), covering every fix above — including a test that a hung AI call still
  answers the user.
- Settings: `LOCAL_DISK_PERSISTENT`, `AI_TIMEOUT`, `AI_CONCURRENCY`, `AI_TYPING_EVERY`,
  `AI_NOTICE_COOLDOWN`, `CB_COOLDOWN`, `MSG_DEDUPE_WINDOW`, `UPDATE_MAX_ATTEMPTS`.

### Found while fixing

- A real bug the new tests caught: `flush_local_now()` and the writer thread both used `db.json.tmp`,
  so a deletion commit racing a background write could fail with `FileNotFoundError`. Writes are now
  serialised with a dedicated lock.

### Also in this revision (from the previous pass, unchanged here)

- Badge line (`UPDATE LIVE`) is centred using padding — Telegram has no text alignment — tunable with
  `POST_BADGE_CENTER` / `POST_BADGE_WIDTH`.
- The assistant is told what the bot can actually do, generated from its own command table.

### Still open

- Single-document MongoDB state (DB-02 / AV-01) — bounded and measured, not split.
- Handler layer still in `bot.py`.
- No mocked-`Update` integration tests for the Telegram handlers themselves.
- Bot API 10.1–10.3 — waiting on `python-telegram-bot`.

## 2026-10-31 — reliability and privacy pass

Response to the production audit of the same date. Full mapping in
[docs/AUDIT-RESPONSE.md](docs/AUDIT-RESPONSE.md).

### Fixed

- **Persistence recovery (DB-01).** A failed MongoDB startup used to disable database writes for the
  life of the process. Storage is now a state machine (`CONNECTING → READY → DEGRADED →
  RECONNECTING → READY`) with bounded exponential backoff, a periodic health check, and automatic
  reconnection without a restart.
- **Data loss while degraded.** Writes made with MongoDB unavailable went nowhere but a dirty flag —
  only a ten-minute mirror touched the disk. Degraded writes now go to the local JSON file with
  `fsync` on every save, and the state written during the outage is pushed to MongoDB when the
  connection returns (the stale remote document is not loaded over it).
- **Duplicate Telegram updates (TG-01).** The webhook deduplicated nothing, so a retried delivery could
  count a referral twice. Updates are now keyed by `update_id`, marked only after successful
  processing, and serialised per chat so two updates from the same chat cannot interleave.
- **`drop_pending_updates` (TG-02).** Was hardcoded `True`; now `DROP_PENDING_UPDATES`, default `False`.
- **AI thinking level (AI-01).** The default `minimal` is not accepted by `gemini-3.8-flash`, so the
  first request always failed and the retry disabled thinking. The default is `low`, and the level is
  validated per model before the first request.
- **AI retry classification.** Fatal errors (bad key, wrong model, permission) are no longer retried;
  retryable ones use exponential backoff with jitter.
- **Referral count froze at 501.** The counter was derived from a list capped at 500 ids, so it stopped
  moving after the 501st invite. It is now maintained independently.
- **Privacy (PRIV-01).** Added `/privacy`, `/delete_my_data` (with confirmation, private chats only,
  covering every store including both conversation-memory key shapes), configurable retention with a
  daily purge job, and a published [PRIVACY.md](PRIVACY.md).
- **`/health` reported a constant.** It now reports storage state, durability, uptime and AI usage; a
  new `/ready` returns 503 while data is not durable.
- **Documentation.** Removed the stale `.gitignore` note, the `0.0.0.0/0` MongoDB advice, and three
  contradictions in `config.py` / `env.example.txt` (auto-approve behaviour, default model,
  `AI_MAX_CHARS`).

### Added

- `storage.py` — the persistence layer, extracted from `bot.py`: state machine, Mongo, JSON fallback,
  users, referrals, bans, chats, posts, links, admin action log, AI usage ledger, deletion, retention,
  update dedupe.
- `/audit` — the last 25 privileged actions (who, what, to whom, in which chat). Moderation commands
  now write to it.
- AI cost controls: per-user window plus a bot-wide daily cap and a per-group daily cap.
- BSON size measurement before every write, an owner alert at 12 MiB, and derived-cache pruning at
  16 MiB. User and referral data is never pruned.
- `tests/` — 51 tests, and `.github/workflows/ci.yml` running `pyflakes`, `compileall`, an import smoke
  test, `pytest`, and a committed-secrets check.
- `requirements.lock` (exact resolved versions), `requirements-dev.txt`.
- `PRIVACY.md`, `docs/AUDIT-RESPONSE.md`, `docs/OPERATIONS.md`.

### Changed

- `render.yaml` — `plan: starter` with an explanation of why Free is unsuitable, health check path,
  `autoDeploy`, and the new durability and budget variables.
- README rewritten around the storage states, the privacy commands, update delivery and the new
  endpoints.
- `bot.py` — 3,871 → 3,615 lines after the persistence extraction; handlers unchanged in behaviour.

### Not changed (deliberate)

- One MongoDB document (DB-02 / DB-04). Bounded and measured, not split; a migration needs a staging
  database.
- The 5 s write-behind window (DB-03). Set `STORAGE_FLUSH_EVERY=1` for a tighter one.
- Handler layer still in `bot.py` (ARCH-01) — needs mocked-update tests first.
- Bot API 10.1–10.3 features (TG-03) — waiting on `python-telegram-bot`.
