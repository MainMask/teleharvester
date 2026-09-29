# teleharvester

A console tool for managing many Telegram accounts at once (Telethon):
bulk DM broadcasts with per-recipient stats, chat/comment broadcast campaigns,
joining and inviting, reactions, poll voting, content moderation reports, profile
and session management.

![Python](https://img.shields.io/badge/python-3.8%2B-blue)

> **Disclaimer.** This project is provided for educational purposes and for use on
> accounts and chats you own or are authorized to act on. Bulk messaging, broadcast
> campaigns and content moderation reports may violate Telegram's Terms of Service
> and local laws. You are solely responsible for how you use it.

## Features

Functions are auto-discovered from [`functions/`](functions/) and shown as a numbered
menu on startup (the in-app menu is in English):

- **Mailing to PM (with stats)** — broadcast a message (text or media) to a list of
  recipients and keep persistent per-recipient success counts with dates.
- **Broadcast to PM** — send a message to a single user by username or phone number.
- **Broadcast to chat** — trigger-based campaigns: text, media, reply, or stickers.
- **Instant broadcast (no trigger)** — broadcast to a chat immediately, without a trigger.
- **Broadcast to channel comments** — post to the comment section of a post.
- **Join chat** — join channels/groups, optionally solving captchas and broadcasting.
- **Invite users from supergroup** — parse members of one chat and invite them to another.
- **Add users to contacts from a .parquet database** — bulk-add users by `user_id` +
  `access_hash`, with per-account rotation when an account hits a limit.
- **Set reactions to message/post** — put reactions on a post.
- **Vote in poll** — vote in a poll from multiple accounts.
- **Moderation report (message/post)** and **Moderation report (user)** — submit
  reports from multiple accounts.
- **Change names**, **Change usernames**, **Change bio**, **Change profile photo** — bulk profile edits.
- **Set two-step verification password to accounts** — bulk 2FA setup.
- **Clear all dialogs** — wipe dialogs / leave channels.
- **Terminate other authorized sessions** — reset other logins on each account.
- **Check accounts status** — check each account against @SpamBot for account restrictions.
- **Statistics (phone numbers)** — breakdown of accounts by country code.

## Requirements

- Python **3.8+** (some features rely on a recent Telethon).
- Dependencies from [`requirements.txt`](requirements.txt): Telethon, Rich, toml,
  phonenumbers, GitPython, python-socks, and pyarrow (for the `.parquet` database).

## Installation

```bash
git clone https://github.com/MainMask/teleharvester.git
cd teleharvester
pip install -r requirements.txt
```

## Configuration

On the first run the app creates `config.toml` and asks for:

- **API ID** and **API hash** — get them at <https://my.telegram.org> → *API development tools*.
- **Broadcast messages**, **delay** (e.g. `1-3`) and a **trigger** phrase used by the
  trigger-based broadcast functions.

The optional `[limits]` section throttles the **Mailing to PM** function: `per_account_daily`
caps how many messages each account sends per day (rotating to the next account when reached),
and `account_pause` (`[min, max]` seconds) is the pause taken when switching accounts. If the
section is missing, defaults (`30` and `[30, 60]`) apply.

See [`config.toml.example`](config.toml.example) for the full structure. `config.toml` holds
your credentials and is git-ignored.

## Adding accounts

Put session files into the `sessions/` folder. Two formats are supported:

- `.session` — a Telethon `StringSession` (a 353-character auth key).
- `.jsession` — a JSON session with account/app/proxy metadata.

Helper scripts (run from inside `sessions/`):

```bash
cd sessions
python add_session.py   # log in a new account and save it as .jsession (optional proxy)
python login.py <file.jsession>   # connect a session and print service messages
```

## Assets

- `assets/names.txt` — pool of names for **Change names** (one per line, `First Last`).
- `assets/usernames.txt` — usernames for **Change usernames** file mode (one per account).
- `assets/targets.txt` — recipients for **Mailing to PM** (one username/phone per line).
- `assets/contacts.parquet` — users database for **Add users to contacts**
  (columns `user_id`, `access_hash`; optional `first_name`, `last_name`, `phone`).
- `media/` — media files picked at random for media broadcasts.
- `assets/photos/` — images for **Change profile photo**.
- `stats/pm_mailing.json` — mailing stats, created at runtime (git-ignored).

## Usage

```bash
python main.py
```

Choose whether to initialize sessions, then pick a function by its number. On startup
the tool checks for updates via git and can pull them automatically.
