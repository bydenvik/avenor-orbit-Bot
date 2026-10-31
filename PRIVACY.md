# Privacy Policy — Avenor Orbit

Last updated: 31 October 2026

Avenor Orbit (`@AvenorOrbitBot`) is a Telegram community bot. This page explains what it stores,
why, who else sees it, and how to have it deleted. The same notice is available inside the bot with
the `/privacy` command.

Contact for any privacy request: the bot owner listed in the repository
[github.com/bydenvik/avenor-orbit-Bot](https://github.com/bydenvik/avenor-orbit-Bot), or the community
group [@AvenorAICommunity](https://t.me/AvenorAICommunity).

---

## What the bot stores

| Data | Why | Where |
|---|---|---|
| Telegram user ID, first name, username | To recognise you, count invites, apply bans | MongoDB |
| First-seen and last-seen timestamps | Basic activity, retention | MongoDB |
| Your invite count, and the IDs of people who joined through your link | The referral feature | MongoDB |
| Chat IDs and titles of groups and channels the bot is in | To verify channel membership | MongoDB |
| Join-request records and verification marks | So the join gate works | MongoDB, expires after 24 hours |
| Channel posts you create with `/post` | To publish and to resolve the post's deep link | MongoDB, last 200 |
| Admin action log: who did what, to whom, in which chat | So moderation can be reviewed | MongoDB, last 300 actions |
| Daily AI request counter (total and per group) | Cost control | MongoDB, reset each UTC day |
| The last few turns of a conversation | So the assistant can follow the thread | Process memory only, cleared on restart |

## What the bot does not store

- Your phone number, email address, contacts, or Telegram password.
- Any photo, document, voice note, or other file you send. A banner image sent during `/post` is read
  once to prefill the form and is then referenced by Telegram's file ID; the file itself is not stored
  by the bot.
- Messages in groups where the bot is not mentioned. It only sees what Telegram delivers to it.
- Payment data — there is none.

## Who else sees your text

When you ask the assistant a question, that text — together with the recent turns of that
conversation and a short context line saying whether you are in a private chat or a group — is sent to
our AI provider, **Google (Gemini API)**, for the sole purpose of generating the reply. It is not sold,
and it is not used by us to train models. Google's handling of API data is described in Google's own
privacy terms.

Nothing else leaves the bot. Messages are not forwarded to any other third party.

## How long it is kept

- Account and referral data: until you delete it, or until the operator removes it.
- Join-request records and verification marks: 24 hours, and they are also cleared when you leave the
  group.
- Admin action log and other temporary caches: `CACHE_RETENTION_DAYS` (default 30 days), after which a
  daily job removes them.
- Conversation memory: only in the running process; it disappears on restart.
- Optional: the operator may set `USER_RETENTION_DAYS` to delete inactive accounts automatically. The
  default is off.

## Your control

Send **`/delete_my_data`** to the bot in a private chat. It deletes, in one step:

- your account record (ID, name, username, timestamps);
- your invite count, and your ID from other people's invite lists;
- your verification marks and join-request records;
- the assistant's memory of your conversations.

You will see a confirmation with what was removed. The deletion is immediate. A new record is created
only if you use the bot again.

To ask for deletion of a whole group's data, contact the operator through the links at the top.

## Security

- Credentials are supplied through environment variables and are never committed to the repository.
- The Telegram webhook is authenticated with a per-boot secret token that is compared in constant time.
- Administrative commands check a configured numeric user ID, never a username or display name.
- Database access should be restricted to the service's network range; see the README for the
  recommended setup.

## Children

The bot is not directed at children and does not knowingly collect data from them.

## Changes

Material changes to this policy will be published in this file, and the "last updated" date above will
change.
