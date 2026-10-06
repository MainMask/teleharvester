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
menu on startup, grouped into the same sections as the control bot, with the same Russian
titles and a one-line description under each (the prompts inside a function stay in English):

- **Mailing to PM (with stats)** — broadcast a message (text or media) to a list of
  recipients and keep persistent per-recipient success counts with dates. Each person in a
  scraped base goes to the worker that has them in contacts; workers are checked with
  @SpamBot first (see [*Workers*](#workers-contacts-restrictions-and-order)).
- **Broadcast to PM** — send a message to a single user by username or phone number.
- **Broadcast to chat** — trigger-based campaigns: text, media, reply, or stickers.
- **Instant broadcast (no trigger)** — broadcast to a chat immediately, without a trigger.
- **Broadcast to channel comments** — post to the comment section of a post.
- **Join chat** — join channels/groups, optionally solving captchas and broadcasting.
- **Invite users from supergroup** — parse members of one chat and invite them to another.
- **Add users to contacts from a .parquet database** — bulk-add users by `user_id` +
  `access_hash`, with per-account rotation when an account hits a limit. Who was added by
  which worker is recorded, so the mailing later writes to each person from that worker.
- **Set reactions to message/post** — put reactions on a post.
- **Vote in poll** — vote in a poll from multiple accounts.
- **Moderation report (message/post)** and **Moderation report (user)** — submit
  reports from multiple accounts.
  Reactions, poll votes and reports go one account at a time with a pause between them (the
  `delay` setting), so they don't all land on the same post within the same second.
- **Change names**, **Change usernames**, **Change bio**, **Change profile photo** — bulk profile edits,
  run one account at a time with a pause between them (`profile_pause`) so identical edits don't land
  on every account at once: one name for all or a random one from a list; a base username with a random
  suffix per account or one per account from a list; one bio for all or a random one from a list; one
  photo for all (a file path) or a random one per account from `assets/photos/`.
- **Set two-step verification password to accounts** — bulk 2FA setup.
- **Clear all dialogs** — wipe dialogs / leave channels.
- **Terminate other authorized sessions** — reset other logins on each account.
- **Check accounts status** — check each account against @SpamBot: permanently restricted
  workers are left out of mailing/contacts until they are clean again, dead sessions are moved
  to `sessions/inactive/`.
- **Accounts list** — name, username, phone, proxy and status of every worker, by username.
- **Set proxies** — distribute the proxies from a file across all accounts (3 per proxy) and
  reconnect them through the new proxies.
- **Statistics (phone numbers)** — breakdown of accounts by country code.
- **Scrape channel/group** — collect messages, their authors, comments, reactions and
  participants from channels and groups into `.parquet` (see below).
- **Group members** — a group's whole member list, the silent members too, as a mailing base.
- **Verify scrape against live channel** — cross-check a scrape for posts it missed; the
  channel and the scrape's date window are filled in from the posts file.
- **Analyze scraped data** — offline tools over scraped files: t.me links, keyword search,
  view a file, combine, comments, monthly activity, sample, rebuild participants.

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

Settings are split in two git-ignored files:

- **`.env` — secrets**: `TG_API_ID`, `TG_API_HASH`, `BOT_TOKEN`. See [`.env.example`](.env.example).
  Real environment variables override the file. It holds secrets — keep it private
  (`chmod 600 .env`, readable only by the user that runs the bot).
- **`config.toml` — behaviour**: `[broadcast]`, `[limits]` and `[bot] admins`. See
  [`config.toml.example`](config.toml.example).

On the first run the app creates `config.toml` and asks for:

- **API ID** and **API hash** (only if they are not in `.env` yet; they are saved to `.env`) —
  get them at <https://my.telegram.org> → *API development tools*.
- **Broadcast messages**, **delay** (e.g. `1-3`) and a **trigger** phrase used by the
  trigger-based broadcast functions. The delay between a worker's actions can later be changed
  from the bot (**🤖 Воркеры → ⏱ Задержка**); it is written back to `config.toml`, and the
  terminal menu offers it as the default. The trigger and `messages_count` (how many messages
  each worker sends per chat / post, `0` = until stopped) are asked in the bot's chat, instant
  and comments broadcasts, with the current value one tap away; the chosen value is written
  back to `config.toml` when the job starts.

Older configs with a `[sessions]` section or `[bot] token` are rejected at startup with a
message to move those values into `.env`.

The optional `[limits]` section throttles the **Mailing to PM** function: `per_account_daily`
caps how many messages each account sends per day (`0` = unlimited; rotating to the next account when reached),
and `account_pause` (`[min, max]` seconds) is the pause taken when switching accounts.
`profile_pause` (`[min, max]` seconds) is the pause between accounts for profile-wide changes
(name, bio, username, 2FA, photo, channel, last seen) — they run one account at a time so the
same edit doesn't land on every account at once. `invite_per_account_daily` and
`contacts_per_account_daily` cap how many invites / contact-adds each account makes per day
(`0` = unlimited; when reached, the person is handed to the next account). If the section is
missing, defaults (`30`, `[30, 60]`, `[60, 180]`, `0`, `0`) apply.

`profile_pause` can also be changed from the bot (**🤖 Воркеры → ⏳ Пауза профиля**), like the
delay; it is written back to `config.toml`.

The optional `[autoreply]` section is for the bot only: every `interval` seconds (default
`900`, 15 min; at least `60`) it checks each worker's unread private chats, answers once anyone who replies to a
worker that wrote to them first with `text` (e.g. "for further contact write to @MainMask"),
marks the chat read and forwards what they wrote to the admins (name, username, phone if known,
the worker, and an "open profile" button). What an answered person writes later is forwarded
too, without another reply. `enabled = false`, an empty `text` or no section turns it off.
On/off and the text can be changed from the bot (**🤖 Воркеры → 💬 Автоответ**, written back
to `config.toml`, effective from the next check); the same screen shows how many people
replied — in all, in the last day / week and per worker.

The Telegram control bot (see [*Control bot*](#control-bot-telegram)) needs `BOT_TOKEN` in
`.env` (from [@BotFather](https://t.me/BotFather)) and `admins` in the `[bot]` section of
`config.toml` (a list of Telegram user IDs allowed to control it). The first-run setup does not
create either — add them yourself. The `BOT_ADMINS` environment variable overrides `admins`.

## Adding accounts

Put session files into the `sessions/` folder. Two formats are supported:

- `.session` — a Telethon `StringSession` (a 353-character auth key).
- `.jsession` — a JSON session with account/app/proxy metadata.

Helper scripts (run from inside `sessions/`):

```bash
cd sessions
python add_session.py   # log in a new account and save it as .jsession (optional proxy; a 2FA password typed at sign-in is stored)
python login.py <file.jsession>   # connect a session and print service messages
```

The control bot does both without a terminal: **🤖 Воркеры → 📲 Добавить по номеру** (phone,
then the code — typed with spaces, `1 2 3 4 5`, as Telegram expires a code sent through Telegram
whole — and the 2FA password if set; the account gets a proxy from `assets/proxies.txt` and joins
the pool live) and **🤖 Воркеры → 🔑 Код входа** (a worker's login codes from the last 15 minutes,
shown spaced, with 🔄 to refresh).

### Importing Telegram Desktop `tdata`

Drop each account into `tdata_import/<name>/` as a Telegram Desktop `tdata` folder:

```
tdata_import/
  account1/
    tdata/
    Пароль 2фа dark.txt   # optional: `<label>: <password>` — the account's 2FA password
```

On startup `main.py` converts every new folder to `sessions/<phone>.jsession`
(authorizing over the network, so the account's proxy must be reachable) and marks
the folder with a `.imported` file so it is not converted again. An account that is already a
worker (matched offline by its user id against `sessions/*.jsession`) is not connected at all — its
key must not show up from another proxy — and its file is left untouched (its stored 2FA password
and proxy are kept). Run it
manually (from any directory) with:

```bash
python sessions/import_tdata.py
```

Proxies are assigned from `assets/proxies.txt` (one per line), shared by up to 3 accounts
each. If that file exists but has fewer proxies than needed for that ratio, the import
stops. With no `proxies.txt`, accounts are imported without a proxy. The 2FA
password is stored in the `.jsession`, which lets **Set two-step verification password**
change it later even when a password is already set.

## Assets

- `assets/databases/` — scraped files (`<name>_posts_…`, `<name>_participants_…`, …); the
  default scrape output. The menu and the bot offer the files here to pick from.
- `assets/names.txt` — pool of names for **Change names** (one per line, `First Last`); the bot
  also takes such a list as a `.txt` sent to it.
- `assets/usernames.txt` — usernames for **Change usernames** file mode (one per account); same
  in the bot. Without a list, a base is suffixed with a random 2-4 digit number per account
  (e.g. `CitadelCurator24`).
- `assets/bios.txt` — pool of bio variants for **Change bio** in the bot (one per line); each
  account gets a random one. The bot also takes such a list as a `.txt`, or a single bio typed in;
  the CLI asks for one bio for all accounts.
- `assets/targets.txt` — recipients for **Mailing to PM** (one username/phone per line).
- `assets/contacts.parquet` — users database for **Add users to contacts**
  (columns `user_id`, `access_hash`; optional `first_name`, `last_name`, `phone`).
- `assets/photos/` — images for **Change profile photo**.
- `assets/proxies.txt` — proxies for tdata import (one per line, shared by up to 3 accounts
  each), `scheme://[user:pass@]ip:port` where `scheme` is `socks5`, `socks4` or `http` (git-ignored).
- `stats/pm_mailing.json` — mailing stats, created at runtime (git-ignored).
- `stats/account_limits.json` — per-account daily send counts for **Mailing to PM**'s
  `per_account_daily` cap, created at runtime (git-ignored).
- `stats/contacts.json` — which worker added whom to contacts (`{user_id: worker id}`),
  written by **Add users to contacts** and read by the mailing (git-ignored).
- `stats/restricted.json` — session files the last @SpamBot check found permanently
  restricted; rewritten on every check (git-ignored).
- `stats/auto_replies.json` — who the `[autoreply]` already answered (per worker), so no
  one is answered twice (git-ignored).

## Usage

```bash
python main.py
```

Choose whether to initialize sessions, then pick a function by its number. The menu lists
the sections (📣 Рассылки, 💬 Активность, 🎯 Аудитория, 🤖 Воркеры, 👤 Профиль,
🔐 Безопасность, 🩺 Проверка и статистика) with a description of every item; ⚠️ marks the
actions that workers perform in other people's chats. On startup
the tool checks for updates via git and can pull them automatically (only when run from a git
checkout; otherwise the check is skipped).

## Control bot (Telegram)

As an alternative to the terminal menu, teleharvester can be driven from Telegram through an
[aiogram](https://docs.aiogram.dev) 3.x bot. The CLI (`python main.py`) keeps working
unchanged; the bot is a separate entry point.

```bash
# .env → BOT_TOKEN=...   config.toml → [bot] admins = [<your_user_id>]
python -m bot
```

**Host / worker model (anti-ban).** The bot itself is the **host**: being a Bot API account it
cannot (and never does) perform the risky MTProto actions. Every task is **delegated to the
worker accounts** in `sessions/` — the bot only orchestrates them. Access is restricted to the
Telegram user IDs in `[bot].admins` (everyone else is ignored).

Send `/start` and pick a section from the keyboard. Every section message lists its items with a
one-line description of what each does:

- **📣 Рассылки** — PM mailing (with stats), PM to one recipient, post comments, instant and
  trigger-based chat broadcasts. PM mailing offers the scraped bases from `assets/databases/` as
  buttons, or takes an uploaded `.txt`, a pasted list (one per line) or a file path.
- **💬 Активность** — join chat, reactions, poll vote, report message/post, report user.
- **🎯 Аудитория** — scrape, verify, data analysis, invite from chat, add to contacts:
  - **Scrape** shows a summary before starting: **▶️ Запустить**, or **⚙️ Дополнительно** for a
    keyword filter and a faster scrape without reactors. Output is always parquet.
  - **Verify** offers the posts files as buttons; a channel button runs it at once over the
    scrape's own date window (stored in the file). Older files ask for the dates.
  - **Анализ данных** — 🔗 t.me links (top list in chat + table), 🔎 keyword search, 👀 view a
    file, and under ➕ Ещё: combine posts files, comments as a table, monthly activity, sample.
    The file is picked by button; results are named automatically next to it and sent back.
- **🤖 Воркеры** — 📋 account list (name, `@username`, ID, polled live, by username), three groups:
  **👤 Профиль** (name, username, bio, photo, hide last seen, remove the profile channel — names
  and usernames can come from a `.txt`), **🔐 Безопасность** (2FA, terminate other sessions),
  **🩺 Проверка и статистика** (@SpamBot status, phone stats, clear dialogs); and
  **🌐 Прокси** (a `.txt` or pasted list, one proxy per 3 accounts, applied live and saved to
  `assets/proxies.txt`), **⏱ Задержка** (the delay between a worker's actions) and
  **📥 Загрузить tdata** (a ZIP of one Telegram Desktop `tdata` folder; its 2FA password is asked
  in chat and the account is imported live), **📲 Добавить по номеру** and **🔑 Код входа**
  (see [*Adding accounts*](#adding-accounts)).

Scraper jobs (scrape, group members, verify) run in **a separate process** and **their own job
slot**, so even a multi-day scrape doesn't hold up mailings or the other functions. One account never
does both at once: while a worker scrapes, new bot jobs run without it (and say so), and a scrape
won't start on a worker a running bot job uses — pick another account or wait. In the mailing and
adding to contacts, the people of a worker busy scraping wait for it instead of going to other workers. The memory a big
scrape's final step takes goes back to the system when it ends, and running out of memory ends only
that job (what a scrape checkpointed is kept), never the bot. Their status messages have
**📊 Прогресс** and **⏹ Стоп** — a scrape stops at a checkpoint (continued later), *group members*
saves the members listed so far. A bot restart (e.g.
`systemctl restart`) checkpoints the running scrape and **continues it automatically** on the next
start; after a long flood ban start *Скрап канала/группы* again with the same name and output folder
and the bot offers to **continue** it (the terminal menu does the same). Prompts with a default take
`-` for it.

Risky functions are marked ⚠️ and refuse to run with no workers. Only **one task runs at a time**
(the worker pool is shared; the scraper's jobs have a slot of their own, see above); long or looping jobs — and the trigger-based chat listener — show a
**⏹ Стоп** button, and `/cancel` aborts an in-progress dialog. When a task ends (done, stopped
or error) the bot sends a **summary report** — task name, workers used, success/error counts and
elapsed time — and returns the main menu. Scrape/verify/analyse send their result files, then the menu.

Broadcast content is taken from the message you send the bot — text, media or an album, with
any formatting and custom emoji; it is captured and re-sent as-is by the workers (Bot API caps
each download at ~20 MB). **Сменить фото** takes a photo sent to the bot (one for every
account) or picks a random one per account from the local `assets/photos/` folder.

## Workers: contacts, restrictions and order

The same rules apply in the terminal menu and in the bot.

- **Contacts ledger.** *Add users to contacts* records which worker added each person
  (`stats/contacts.json`). The mailing then writes to that person only from that worker — to its
  own contact, not to a stranger, which is what keeps it out of the spam filter. People without a
  username in a scraped base go to the account that scraped it (only its access hashes are valid,
  see `Owner ID` below); everyone else is a shared queue for all workers. In the terminal menu,
  the workers left out by *how many accounts to use?* keep their people too: those wait for them,
  they don't go to the workers picked for the run (*Add users to contacts* doesn't re-add them either).
- **@SpamBot check before every run.** The mailing and *Add users to contacts* first ask @SpamBot
  about their workers (a few seconds). A dead session (banned or logged out) is moved to
  `sessions/inactive/`; a permanently restricted one is left out (`stats/restricted.json`). The
  people of a worker that is out become shared: other workers take them in the mailing, and the
  next *Add users to contacts* re-adds them to live workers. Each check rewrites the list, so a
  worker that is clean again is back in the next run. **Check accounts status** does the same on
  demand.
- **Mid-run.** If a worker stops during a mailing, it is asked about at once: out for good
  (permanent restriction or dead) → its remaining people are handed to the other workers in the
  same run; a daily cap or a temporary limit → they wait for it until the next run (another
  worker would be writing to a stranger).
- **Dead workers in other functions.** A worker whose session is dead is skipped with
  `get_me failed: session is dead (banned or logged out)`; the other workers go on.
- **Order.** Workers are ordered by username (`name1, name2 … name10`), in the pool and in every
  report; workers running at once still report in that order. The username is remembered in the
  `.jsession` the first time it is seen (a status check, a run, the account list).

## Running under systemd

For long-lived server use (the control bot as a daemon), the unit file is in [`deploy/`](deploy/). It assumes the repo at `/opt/teleharvester` with a venv in `.venv/` and a
`teleharvester` user — edit the `User=`, `WorkingDirectory=` and `ExecStart=` lines to match your install.

**Control bot** — `deploy/teleharvester-bot.service` runs `python -m bot` with `Restart=on-failure`
(so it survives crashes) and logs to journald. Five failed starts within 5 minutes (e.g. a config
error) leave the unit failed instead of restarting forever: fix it, then
`systemctl reset-failed teleharvester-bot`. aiogram already retries transient polling errors and
stops gracefully on SIGTERM. Every start is announced to the admins in the bot ("🔄 Бот запущен"),
and a start after a crash or an out-of-memory kill says so ("⚠️ … после сбоя"), so a silent restart
or a crash loop doesn't go unnoticed. The worker accounts in `sessions/` are read once at startup, so after
adding or removing a session file restart the service (`systemctl restart teleharvester-bot`) for the
change to take effect.

```bash
sudo cp deploy/teleharvester-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now teleharvester-bot
journalctl -u teleharvester-bot -f
```

## Scraping & analysis

teleharvester includes a full Telegram scraper and analyser: scraping Telegram channels,
groups and chats (message content, authors, reactions, views, shares, comments) and
analysing the result, stored as **Apache Parquet** (`.parquet`) or **Excel** (`.xlsx`).

Everything runs from the terminal menu or the control bot, in the **🎯 Аудитория** section, on an
account you pick: a worker from `sessions/` or a **personal account from `personal_sessions/`**
(the same `.jsession` format; personal accounts are never workers — mailings and the other
functions don't see them — but the scraper only reads, so it may run on one). Each runs with that
account's own API credentials, proxy and device — no separate login. A scrape is continued on the
account it was started on: the scraped access hashes are valid for that account only. For the same
reason a base scraped by a personal account reaches, through the workers' mailing, only the people
with a username (the rest are skipped as *another account's base*) — the bot and the menu say so
when a personal account is picked; scrape with a worker for a full base. The output is parquet in `assets/databases/`, so a `_participants`
base flows straight into *Add to contacts* and the mailing; files are picked by number, verify
fills in the channel and the scrape window, and the analysis tools suggest an output name next to
the input.

- **Скрап канала/группы** — messages, their authors, comments and reactions of channels and groups
  over a date window. A source may be a channel, a supergroup (forum topics included) or a basic
  group, given as `@name`, `t.me/name`, a full `https://t.me/name` URL, a `t.me/+hash` invite link,
  or a **numeric ID** such as `-1001629147115` (a basic group's is `-123456`). A private source
  (an invite link, `t.me/c/…`, a numeric ID) is read only if the account is already a member. Dates are `DD.MM.YYYY` or ISO `YYYY-MM-DD`, both inclusive. In a group the
  people are the message authors, so they go into the participants base too (bots and deleted
  accounts never do); a basic group's
  messages have no links, so their `Url` is empty. In a forum every post gets its `Topic ID`
  (1 = General); a topic link (`t.me/name/42`, `t.me/c/<id>/42`) scrapes that topic alone into the
  group `@name-topic42` (verify then checks just that topic). A private chat is given as
  `@username`; its messages have no links either. The files are named after the scrape with the
  post-date span appended: `<name>_posts_<from>-<to>`, `<name>_participants_…` and
  `<name>_reactors_…` (reactors are slow — one API call per reacted message). `FLOOD_WAIT` rate
  limits are waited out automatically. In the bot, *⚙️ Дополнительно* toggles comments, reactors
  and the participants base, and sets a keyword filter and a post limit.
- **Участники чата** — a group's whole member list, the silent members too, into
  `<name>_participants_members.parquet` (the same columns as a participants base). Telegram lists
  up to ~10,000 members; past that the rest is reached by searching names letter by letter. A
  channel's subscribers, or a group with a hidden member list, are visible to admins only
  (reported, then skipped), and a private chat has no member list. A topic link lists the whole
  group: a forum's members are shared by its topics.
- **Верификация скрапа** — the scrape walks a channel with `iter_messages`; verify cross-checks with
  `get_messages(ids=…)` and lists any real message inside the date window the scrape missed. It
  assumes a full scrape: for a keyword scrape every non-matching post is reported as missed.
- **Анализ данных** — combine posts files, flatten comments, rebuild participants, monthly
  summaries, samples, keyword filters, `t.me` links (all of them in the menu and in the bot).

**Interruptions.** A dropped connection is retried for hours. If a run still stops (a long outage,
a flood ban, a restart, `Ctrl-C`, ⏹), it keeps a checkpoint in `<name>_partial/`: start the same
scrape again — the same name and output folder — and it offers to continue from there with the
interrupted run's account, channels, dates and settings.

**Giant runs.** The scrape is memory-bounded: it checkpoints and frees its buffers as it goes, and
Telethon's cache of every user/chat it meets lives in a SQLite file in the checkpoint folder
(`entities.session`, removed on a clean finish) instead of RAM — on millions of scraped reactors an
in-memory cache would grow without bound. The file holds the cache only, never the account's login.
The outputs are built from the checkpoint shards one shard at a time — the `_posts` and
`_reactors` files, the per-channel `_until_` snapshots and the participants base (one entry per
person, the posts file read in batches) — so a scrape's memory doesn't grow with its size. The
posts in `_posts` go channel by channel, newest first within each.

`Access Hash` (and `Comment Author Access Hash`, `Author Access Hash`) is the user's Telegram
`access_hash`: with the `ID` it forms an `InputPeerUser`, so the user can be addressed without
resolving them again (only from the account that scraped it). This is exactly what *Add to
contacts* and the mailing consume.

The posts file also stores, in its parquet metadata, the scrape's own date window and the
account that scraped it. *Verify* uses the window as its default dates (the saved posts' span
would hide a scrape cut short), and rebuilding the participants from the posts file keeps the
`Owner ID` column — without it the mailing can't tell which worker may write to people without
a username.

### Output columns

`Type, Group, Author ID, Author Username, Author Access Hash, Author Name, Content, Date,
Message ID, Author, Views, Reactions, Shares, Media, Url, Comments List` (plus a `Comments`
count added on write).

### Notes

- **Telegram soft ban:** scraping more than ~150–200 communities in one block can trigger a
  24-hour soft ban. There is no practical limit on the number of messages from fewer communities.
- Respect Telegram's Terms of Service and applicable data-protection law.

### Citation

If you use the scraper in research, please cite the original work:

