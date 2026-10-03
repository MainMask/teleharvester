# teleharvester

A console tool for managing many Telegram accounts at once (Telethon):
bulk DM broadcasts with per-recipient stats, chat/comment broadcast campaigns,
joining and inviting, reactions, poll voting, content moderation reports, profile
and session management.

![Python](https://img.shields.io/badge/python-3.11%2B-blue)

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
- **Scrape channel/group** — collect posts, comments, reactions and participants from a
  channel/group into `.parquet`/`.xlsx` (absorbed from scraper; see below).
- **Verify scrape against live channel** — cross-check a scrape for posts it missed.
- **Analyze scraped data** — offline tools over scraped files: combine, comments,
  participants, summary, sample, filter, links, read.

The scraping and analysis features are also available as a command-line tool for
automation and server/Docker use: `python -m scraper <command>` (see
[*Scraping & analysis*](#scraping--analysis) below).

These functions can also be driven from Telegram through a control bot instead of the terminal
menu (see [*Control bot*](#control-bot-telegram) below). Rich broadcast content — media, albums,
custom emoji and text formatting — is supplied through the control bot; the terminal menu sends
plain text.

## Requirements

- Python **3.11+** (some features rely on a recent Telethon; the scraper uses `X | None` syntax).
- Dependencies from [`requirements.txt`](requirements.txt): Telethon, aiogram (for the
  control bot), Rich, toml, phonenumbers, GitPython, python-socks, pyarrow, and — for
  scraping/analysis — pandas, numpy, tqdm, openpyxl and python-dotenv.

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

The optional `[bot]` section configures the Telegram control bot (see
[*Control bot*](#control-bot-telegram)): `token` (from [@BotFather](https://t.me/BotFather))
and `admins` (a list of Telegram user IDs allowed to control it). The first-run setup does not
create it — add it yourself (copy from [`config.toml.example`](config.toml.example)), or set the
`BOT_TOKEN` and `BOT_ADMINS` environment variables instead (they override the file).

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
- `assets/photos/` — images for **Change profile photo**.
- `stats/pm_mailing.json` — mailing stats, created at runtime (git-ignored).
- `stats/account_limits.json` — per-account daily send counts for **Mailing to PM**'s
  `per_account_daily` cap, created at runtime (git-ignored).

## Usage

```bash
python main.py
```

Choose whether to initialize sessions, then pick a function by its number. On startup
the tool checks for updates via git and can pull them automatically.

## Control bot (Telegram)

As an alternative to the terminal menu, teleharvester can be driven from Telegram through an
[aiogram](https://docs.aiogram.dev) 3.x bot. The CLI (`python main.py`) keeps working
unchanged; the bot is a separate entry point.

```bash
# config.toml → [bot] token = "..." and admins = [<your_user_id>]
python -m bot
```

**Host / worker model (anti-ban).** The bot itself is the **host**: being a Bot API account it
cannot (and never does) perform the risky MTProto actions. Every task is **delegated to the
worker accounts** in `sessions/` — the bot only orchestrates them. Access is restricted to the
Telegram user IDs in `[bot].admins` (everyone else is ignored).

Send `/start`, then **📋 Функции** to pick a function by category:

- **📣 Рассылки** — PM mailing (with stats), PM broadcast, comments, instant, trigger-based chat.
- **👤 Профиль** — name, username, bio, photo, 2FA.
- **👥 Аудитория** — invite from chat, add contacts from `.parquet`.
- **⚡ Активность** — join chat, reactions, poll vote.
- **🛡 Модерация** — report message/post, report user.
- **🧹 Сервис** — account status (@SpamBot), phone stats, terminate sessions, clear dialogs.
- **🔎 Скрапинг** — scrape, verify, analyse (runs on one worker's session; results come back as files).
  Bot-launched scrapes run in a background thread that **cannot be stopped from the bot**, hold the single
  job slot for their whole duration, and are **not resumable** — a bot restart (e.g. `systemctl restart`)
  ends the run and the next launch starts it fresh. Use the bot for short, interactive scrapes; run
  **long or multi-day scrapes through the `deploy/teleharvester-scrape@.service` template** (below), which
  passes `--resume` and checkpoints on SIGTERM.

Risky functions are marked ⚠️ and refuse to run with no workers. Only **one task runs at a time**
(the worker pool is shared); long or looping jobs — and the trigger-based chat listener — show a
**⏹ Стоп** button, and `/cancel` aborts an in-progress dialog.

Broadcast content is taken from the message you send the bot — text, media or an album, with
any formatting and custom emoji; it is captured and re-sent as-is by the workers (Bot API caps
each download at ~20 MB). Profile photos for **Сменить фото** come from the local
`assets/photos/` folder.

## Running under systemd

For long-lived server use (the control bot as a daemon, or multi-day scrape runs), unit files are
in [`deploy/`](deploy/). They assume the repo at `/opt/teleharvester` with a venv in `.venv/` and a
`teleharvester` user — edit the `User=`, `WorkingDirectory=` and `ExecStart=` lines to match your install.

**Control bot** — `deploy/teleharvester-bot.service` runs `python -m bot` with `Restart=on-failure`
(so it survives crashes) and logs to journald. aiogram already retries transient polling errors and
stops gracefully on SIGTERM. The worker accounts in `sessions/` are read once at startup, so after
adding or removing a session file restart the service (`systemctl restart teleharvester-bot`) for the
change to take effect.

```bash
sudo cp deploy/teleharvester-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now teleharvester-bot
journalctl -u teleharvester-bot -f
```

**Scrape jobs** — `deploy/teleharvester-scrape@.service` is a template: one instance per job, with its
arguments in `deploy/scrape-<name>.env` (see [`deploy/scrape-example.env`](deploy/scrape-example.env)).
The unit always passes `--resume`, and the scraper turns SIGTERM (`systemctl stop`/`restart`) into a
clean checkpoint, so a restart continues from where it left off instead of re-scraping or losing data.

For multi-day runs keep the default file session (`scraper.session`) rather than `TG_SESSION_STRING`:
Telethon caches every user/chat it sees, and a file session keeps that cache on disk (flat memory),
while a string session keeps it all in RAM and grows unbounded over millions of scraped reactors.

```bash
sudo cp deploy/teleharvester-scrape@.service /etc/systemd/system/
cp deploy/scrape-example.env deploy/scrape-myrun.env   # edit channels/dates/name
sudo systemctl daemon-reload
sudo systemctl start teleharvester-scrape@myrun
```

## Scraping & analysis

teleharvester includes a full Telegram scraper and analyser: scraping Telegram channels,
groups and chats (message content, authors, reactions, views, shares, comments) and
analysing the result, stored as **Apache Parquet** (`.parquet`) or **Excel** (`.xlsx`).

> Scraper and analysis originally by **Ergon Cugler de Moraes Silva** —
> <https://github.com/ergoncugler/web-scraping-telegram/>. See *Citation* below.

Two ways to run it:

- **From the menu** — pick *Scrape channel/group*, *Verify scrape against live channel*,
  or *Analyze scraped data*. These reuse your `config.toml` API credentials and one of
  your existing `sessions/` accounts (you pick which at run time) — no separate login.
  Scrapes default to `assets/databases/`, so a `_participants` file flows straight into
  *Add users to contacts from a .parquet database*.
- **From the command line** — `python -m scraper <command>` (or the `scraper`
  console script after `pip install -e .`). This path is for automation, long resumable
  runs and Docker; it reads credentials from `.env` (`TG_API_ID`, `TG_API_HASH`), falling
  back to `config.toml` (`[sessions]`) when no `.env` is set, and uses its own
  `scraper.session` (run `python -m scraper login` once, or set `TG_SESSION_STRING`).

| Command   | What it does |
|-----------|--------------|
| `scrape`  | scrape channels/groups into `.parquet` or `.xlsx` |
| `verify`  | probe the live channel for posts a scrape missed |
| `login`   | authorise once and save a session (CLI path only) |
| `read`    | print the head of a data file, optionally convert it |
| `combine` | merge many `.parquet` files, drop duplicates, recount comments |
| `comments`| flatten `Comments List` into one row per comment |
| `participants`| unique `ID` + `Username` + `Access Hash` + `Name` of everyone who commented or reacted |
| `summary` | per-group monthly tables (contents / comments / total) |
| `sample`  | proportional per-category sample to `.xlsx` |
| `filter`  | keep rows matching keywords, add one 0/1 column per keyword |
| `links`   | extract and count `t.me` links from `Content` |

Run `python -m scraper <command> --help` for the full flag list.

### Scrape

```bash
python -m scraper scrape \
  --channels "@LulanoTelegram, @jairbolsonarobrasil" \
  --date-min 2024-10-15 --date-max 2025-01-15 \
  --name Test --out-dir output
```

A channel may be `@name`, `t.me/name`, a full `https://t.me/name` URL, a `t.me/+hash`
invite link, or a **numeric ID** such as `-1001629147115` (the account must already be a
member). Dates are `DD.MM.YYYY` or ISO `YYYY-MM-DD`, both inclusive. Output is parquet by
default (`--format excel` only for small runs — Excel truncates cells over 32,767 chars).

Output files are named after `--name` with the scraped post-date span appended:
`<name>_posts_<from>-<to>`, plus `<name>_participants_…` (unless `--no-participants`) and
`<name>_reactors_…` (unless `--no-reactors`; slow — one API call per reacted message).
`FLOOD_WAIT` rate limits are waited out automatically.

The scrape itself is memory-bounded (it checkpoints and frees its buffers as it goes), but
the final `participants` step loads the whole posts + reactors output into RAM at once. On a
very large scrape (millions of reactors) that step can run the machine out of memory **after**
the `_posts`/`_reactors` files are already safely written. If RAM is tight, scrape with
`--no-participants` and build the participants file separately later
(`python -m scraper participants …`) on a machine with more memory.

### Interruptions / resume

A dropped connection is retried for hours. If a run dies (long outage, crash, `Ctrl-C`)
it prints the ready-to-paste command that resumes it — the same arguments plus `--resume`,
which continues from the checkpoint in `<name>_partial/`.

### Verify

```bash
python -m scraper verify --input output/Test_posts_02.01.2024-30.01.2024.parquet \
  --channel @Test --date-min 01.01.2020 --date-max 31.12.2024 --output output/Test_missed.parquet
```

`scrape` walks the channel with `iter_messages`; `verify` cross-checks with
`get_messages(ids=…)` and lists any real message inside the date window the scrape missed.

### Analyse

```bash
python -m scraper combine      --input 'output/*_posts_*.parquet' --output output/unified.parquet
python -m scraper participants --input output/unified.parquet --output output/people.parquet
python -m scraper summary      --input output/unified.parquet --output-base output/resume
python -m scraper read         output/unified.parquet --head 20 --to xlsx
```

`Access Hash` (and `Comment Author Access Hash`) is the user's Telegram `access_hash`:
with the `ID` it forms an `InputPeerUser`, so the user can be addressed without resolving
them again (only from the account that scraped it). This is exactly what *Add users to
contacts from a .parquet database* consumes.

### Output columns

`Type, Group, Author ID, Content, Date, Message ID, Author, Views, Reactions, Shares,
Media, Url, Comments List` (plus a `Comments` count added on write).

### Docker

Deploy the scraper CLI on a server without a local Python setup. The session and every
scraped file live in `./data/`.

```bash
cp .env.example .env        # set TG_API_ID, TG_API_HASH
docker compose build
docker compose run --rm scraper login          # authorise once
docker compose run --rm scraper scrape --channels '@channel' \
  --date-min 01.01.2024 --date-max 31.01.2024 --name Test
```

### Notes

- **Telegram soft ban:** scraping more than ~150–200 communities in one block can trigger a
  24-hour soft ban. There is no practical limit on the number of messages from fewer communities.
- Respect Telegram's Terms of Service and applicable data-protection law.

### Citation

If you use the scraper in research, please cite the original work:

> SILVA, Ergon Cugler de Moraes. *TelegramScrap: A comprehensive tool for scraping Telegram data*.
> (Feb) 2023. Available at: <https://doi.org/10.48550/arXiv.2412.16786>.
