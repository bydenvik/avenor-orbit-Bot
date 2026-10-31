# Changelog

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
