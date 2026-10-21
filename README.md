# teleharvester

A tool for operating pools of Telegram accounts (Telethon), one pool per project: DM mailings with
per-recipient statistics, chat and comment campaigns, joining and inviting, reactions, poll votes,
moderation reports, profile and session management, and a channel/group scraper with offline analysis.
It is controlled from a terminal menu or a Telegram control bot.

![Python](https://img.shields.io/badge/python-3.11%2B-blue)

> **Disclaimer.** For educational use and for accounts and chats you own or are authorized to act
> on. Bulk messaging, campaigns and moderation reports may violate Telegram's Terms of Service and
> local law. You are solely responsible for how you use it.

## Features

The terminal menu and the bot share one menu (Russian labels); functions are discovered from
[`functions/`](functions/). Media, albums and formatted text are sent through the bot only; the
terminal sends plain text. ⚠️ marks actions that may get a worker limited by Telegram.

All the work is a project's: each project has its own worker accounts, proxies, mailing statistics
and contacts ledger, and its card holds everything done with them. The main menu has three items:

| Section | Functions |
|---|---|
| **📁 Проекты** | The projects. A project's card sums it up and opens its screens: on its files — **📥 Сбор** (scrape, top-up, list upload, group members, verification, analysis), **📨 База и рассылка** (add to contacts, DM mailing), **⚙️ Управление** (channels, rename, clear, delete) — and on its workers, below; see [Projects](#projects). |
| **⚙️ Настройки** | Action delay, profile pause, auto-reply, the per-account daily caps (**📏 Дневные лимиты**: DMs, invites, contacts) — for all projects, written back to `config.toml`. The chat campaign's trigger and messages per worker are asked by **🔔 Чат — по слову** itself and saved there. |
| **📊 Задачи** | Every project's running jobs with progress and stop (bot only; same as `/status`). |

A project's screens, on its own workers:

| Screen | Functions |
|---|---|
| **🤖 Аккаунты** | Account list and cards (login codes, removal of personal accounts), adding accounts (by phone, from tdata, or free ones), moving workers to another project, the project's proxies, @SpamBot status check. |
| **👤 Профиль** | Name, username, bio, photo, remove profile channel — applied one account at a time with `profile_pause`. |
| **🔐 Безопасность** | Set 2FA password, terminate other sessions, hide last seen, clear dialogs. |
| **💬 Активность** | Every worker of the project on one target: DM to one recipient, post comments, instant and trigger-based chat broadcasts; join chat, reactions, poll vote, inviting members of one chat to another; report a post or a user. |

## Installation

Requires Python **3.11+**.

```bash
git clone https://github.com/MainMask/teleharvester.git
cd teleharvester
pip install -r requirements.txt
```

## Configuration

| File | Contents |
|---|---|
| `.env` | Secrets: `TG_API_ID`, `TG_API_HASH`, `BOT_TOKEN`. See [`.env.example`](.env.example). Environment variables take precedence. Keep it private (`chmod 600`). |
| `config.toml` | Behaviour. See [`config.toml.example`](config.toml.example). Created on first run, which also asks for the API ID/hash (from <https://my.telegram.org>) if `.env` lacks them. |

Key `config.toml` settings:

| Key | Default | Meaning |
|---|---|---|
| `[broadcast] delay` | — | Pause between a worker's actions, `[min, max]` s. |
| `[broadcast] trigger`, `messages_count` | — | Trigger phrase and per-chat message cap for chat/comment campaigns (`0` = until stopped); the bot asks for them at each run and saves them here. |
| `[limits] per_account_daily` | `30` | DM mailing messages per account per day (`0` = unlimited). |
| `[limits] account_pause` | `[30, 60]` | Pause when the mailing switches accounts. |
| `[limits] profile_pause` | `[60, 180]` | Pause between accounts for profile changes. |
| `[limits] invite_per_account_daily`, `contacts_per_account_daily` | `0` | Daily invite / contact-add caps. Contacts: the next account continues. Invites: each worker gets its own share of the members, and a worker at its cap skips the rest of its share for that run. |
| `[autoreply] enabled`, `text`, `interval` | `true`, `""`, `900` | Bot only: replies once to people who answer a mailing and forwards their messages to the admins. Interval ≥ 60 s; empty text disables it. |
| `[bot] admins` | `[]` | Telegram user IDs allowed to use the bot (`BOT_ADMINS` overrides). |

Every write from the app keeps the previous file as `config.toml.bak`. Legacy configs with
`[sessions]` or `[bot] token` are rejected at startup; move those values to `.env`.

## Accounts

- **Workers** — each project's own, in `projects/<id>/sessions/`: `.session` (Telethon `StringSession`)
  or `.jsession` (JSON with account, app and proxy metadata). An account is in one project only: a number
  or a tdata already a worker elsewhere is refused.
- **Free accounts** — `sessions/`: workers in no project (the shared pool from before projects had their
  own accounts lands here). None of them works until it is moved into a project: **➕ Добавить → 📦 Из
  свободных**, several at once by ticks (✅/⬜, ☑️ Все). **🤖 Аккаунты → ↪️ Перенести** moves the ticked
  workers into another project, or back to the free ones; their @SpamBot restrictions and proxies go with
  them, and one busy with a job stays. The terminal takes numbers (`1,3,5-8`, `all`). Only `.jsession`
  accounts are moved from the menu.
- **Personal accounts** — `personal_sessions/`: same format, used for scraping only, never as workers;
  shared by all projects (one scrape at a time on each).

Adding accounts (bot: a project's **🤖 Аккаунты → ➕ Добавить**; terminal: the same, on the project's card;
or the scripts, which ask for the project):

```bash
python sessions/add_session.py           # worker or personal account by phone, tdata, or a free one
python sessions/login.py [file.jsession] # login codes received in the last 15 minutes
python sessions/import_tdata.py          # import tdata_import/<name>/tdata folders
```

- **tdata import.** Place each Telegram Desktop `tdata` folder in `tdata_import/<name>/`
  (optionally with a `Пароль 2фа*.txt` file containing `<label>: <password>` for 2FA). Each folder is converted to
  the project's `sessions/<phone>.jsession` and marked `.imported`; an account that is already the project's
  worker is left untouched. The bot accepts a ZIP of one `tdata` folder (≤ 10 MB, ≤ 100 MB unpacked).
- **Proxies.** Each project's own list, `projects/<id>/proxies.txt` (set with **🌐 Прокси**), one per line,
  `scheme://[user:pass@]ip:port` (`socks5`, `socks4`, `http`), up to 3 accounts per proxy; the project's
  new accounts take theirs from it. Personal accounts use no proxy.
  Optionally, `projects/<id>/proxies_restricted.txt` (the second step of **🌐 Прокси**): proxies for the
  permanently restricted workers, no cap per proxy, none of the pool's. They stay workers (they still answer
  replies and can scrape) but leave the pool, so its places go to new accounts and no fresh account shares
  an IP with a restricted one. After each status check the bot offers **🔁 Пересадить** for a day (the
  terminal asks): the restricted ones go onto these proxies, the ones clean again back into the pool (or
  stay, reported, while it is full); the other workers keep their proxies, and one busy right now (a scrape,
  a login) is left for the next check. **🗑 Не отделять** at that step
  (in the terminal: `-`) drops the list and brings them back into the pool. Any other worker not on a pool
  proxy (clean again, its list replaced, or moved here from another project) is offered back into it too.
- Session files put in by hand are read when the bot starts (or the terminal opens the project's card).

## Usage

```bash
python main.py   # terminal menu
python -m bot    # Telegram control bot (needs BOT_TOKEN and [bot] admins)
```

- **Terminal menu.** Pick an item by number; a project's card lists its screens in the bot's order: the
  buttons of each one on its files a line, its screens on its workers (🤖 Аккаунты, 💬 Активность) on a
  line of their own; their accounts connect for the work they are in. On startup it checks git for updates
  (30 s timeout).
- **Control bot.** The bot only orchestrates; all MTProto actions are performed by the workers.
  Access is limited to `[bot] admins`. Commands: `/start`, `/status`, `/cancel`.
- **Navigation.** One tree of screens (`modules/registry.py`). A project's screen is titled with its path
  (📁 GreenBase › 🤖 Аккаунты › 👤 Профиль), and each of its buttons carries the project: an older
  message's button acts on its own project, whichever one the chat is in by then (a form under way in
  another project is left). Screens and choices open in place of the message their button is under; a
  prompt for text comes as a new message starting with the function's name, with **✖️ Отмена** — on every
  step, buttons too —, which leads back to the screen the form was started from. Where a value can be left
  out or taken by default, a button says so (**⏭ Без пароля**, **♾ Все**, **♾ Без лимита**,
  **📄 assets/…**); a typed «-» still works. Why a button can't run now (no worker, nothing to work on
  yet, a job in the way, a stale button) is said in a pop-up over its screen.
- **Jobs.** Each project runs one account-based job at a time, its scraper jobs and analysis in slots of
  their own; projects run side by side, and an account is never used by two jobs at once. Long jobs show
  **📊 Прогресс** and **⏹ Стоп**; each job is named as its button (in **📊 Задачи** too) and ends with a
  summary (workers, successes, errors, elapsed time) and **⬅️** back to the screen it was started from, in
  its own project. A message sent while the project's slot is busy is not lost: the step stays.
- Only one teleharvester process runs per folder (lock in `tmp/`); stop the service before using
  the terminal menu.

## Worker rules

Applied identically in the bot and the terminal, within each project.

- **Contacts ledger.** *Add to contacts* records which worker added each person
  (the project's `contacts.json`); the mailing writes to that person only from that worker. People without
  a username go to the account whose scrape listed them (access hashes are per account). A base row
  mailed or added to contacts by its username is skipped if that username now belongs to someone else.
- **@SpamBot check** before every mailing and contact run: dead sessions move to
  `inactive/` next to them and their people are reassigned; permanently restricted workers are excluded
  and their people wait until the admin releases them (**Передать контакты**); temporarily
  restricted ones sit the run out.
- **Mid-run failures.** A dead worker's remaining people go to other workers in the same run; a
  restriction or daily cap defers them to the next run. A worker out of contact imports (a phone
  recipient) sits the run out and the next worker takes the number.
- **Unreachable recipients.** A deleted (or Telegram-banned) account and a username nobody has are kept
  in the project's `pm_mailing.json` (`"gone"`, no sends) and skipped by every later mailing, with
  «skip those already messaged» or without. Not kept: privacy refusals, an invalid peer (an older base's
  hash may be another account's), a phone not found, a base row's username nobody has (renamed, not gone). A recipient who blocked the worker can't be told
  apart: Telegram accepts the message, and it counts as sent.
- **Order.** Workers are ordered by username in the pool and in reports.
- **Settings** (`config.toml`: delays, daily caps, auto-reply) are shared by all projects; the daily caps
  count per account.

## Projects

A project is a named audience — a set of channels, or none when its base comes from group members or
an uploaded list — with its own workers, proxies, mailing statistics and contacts ledger in
`projects/<id>/` (named by its id, so a rename moves none of them). Its scraped files live in
`assets/databases/` as `<name>_posts_…`, `<name>_reactors_…` and `<name>_participants_…`; the list is
stored in `stats/projects.json` (backup in `stats/projects.json.bak`). Project names may not contain
`/`, `\` or the file markers (`_posts`, `_reactors`, `_participants`, `_partial`, `_previous`, `_combined`).

**Upgrading.** At the first start after the update, each existing project gets an id and a copy of the
shared `stats/pm_mailing.json` and `stats/contacts.json` (so no project writes again to someone already
written to); the shared files stay as they are. The workers from `sessions/` become free accounts and
are moved into projects by hand. Proxies are not copied: set each project's with 🌐 Прокси.

The card shows the project's workers, channels count, an unfinished scrape or the top-up to make, and its
files count; **📥 Сбор** lists its channels and files.

| Card screen → button | Description |
|---|---|
| **📥 Сбор → ▶️ Скрап проекта** | Scrapes all project channels; resumes an unfinished scrape. |
| **📥 Сбор → 🔄 Дособрать** | Updates the newest scrape from one week before its end to today and merges the result into the project files; replaced files go to `<name>_previous/`. Must run on the account that made the scrape. |
| **📥 Сбор → 📥 Загрузить список** | Adds usernames/phones to `<name>_participants_list.txt` (mailing only). |
| **📥 Сбор → 👥 Участники групп** | Adds a group's full member list to `<name>_participants_members.parquet`; a previous base that can't be read is moved to `<name>_previous/`, not overwritten. |
| **📥 Сбор → ✅ Верификация** | Checks a scrape against the live channel for missed posts. |
| **📥 Сбор → 📈 Анализ** | t.me links, keyword search, file view, combine, comments table, monthly activity, sample. Combine merges the project's scrapes into `<name>_posts_combined.parquet`, offered on the card like its other posts files (🔄 still continues the newest scrape). |
| **📨 База и рассылка → 📇 В контакты** / **📨 ЛС по базе** | Add the base to the project's workers' contacts, then mail it. With «skip those already messaged» (asked before each mailing), each person is mailed once within the project. |
| **⚙️ Управление** | Edit channels, rename (files are renamed too), clear the members base or list (moved to `<name>_previous/`), delete (files are kept; a project with workers is not deleted — move them first). |
| **🤖 Аккаунты** / **💬 Активность** | The project's accounts and the work of its workers on one target (see [Features](#features)). |

## Scraping & analysis

- **Sources:** `@name`, `t.me/name`, `t.me/+hash`, numeric IDs, forum topic links
  (`t.me/name/42`). Private sources are read only if the account is a member. Sources are
  validated on the chosen account before the scrape starts.
- **Dates:** `DD.MM.YYYY` or `YYYY-MM-DD`, inclusive.
- **Output:** Parquet in `assets/databases/` (the analysis tools can also export Excel): posts, reactors (slow: one call per
  reacted message) and a participants base that feeds *Add to contacts* and the mailing. Options:
  comments, reactors, participants, keyword filter, post limit.
- **Access hashes** are valid only for the account that scraped them; scrape with one of the project's
  workers for a base fully usable by its mailing.
- **Resilience.** `FLOOD_WAIT` is waited out; connection drops are retried. An interrupted run keeps
  a checkpoint in `<name>_partial/` and resumes on the same account (automatically after a bot
  restart). Memory use is bounded regardless of scrape size.
- **Group members** lists up to ~10,000 members directly, the rest via name search; hidden member
  lists and channel subscribers require admin rights.
- **Verification** requires a full (non-keyword) scrape.

Output columns: `Type, Group, Topic ID, Author ID, Author Username, Author Access Hash, Author Name, Content,
Date, Message ID, Author, Views, Reactions, Shares, Media, Url, Comments List` (+ `Comments` count).

Scraping more than ~150–200 communities in one block may trigger a 24-hour soft ban.

## Files

| Path | Purpose |
|---|---|
| `assets/databases/` | Scraped bases (default output). |
| `assets/names.txt`, `usernames.txt`, `bios.txt`, `photos/` | Pools for profile changes. |
| `assets/targets.txt` | Mailing recipients (username/phone per line). |
| `assets/contacts.parquet` | Base for *Add to contacts* (`user_id`, `access_hash`). |
| `projects/<id>/sessions/` | The project's workers (`inactive/`, `restricted/` inside). |
| `projects/<id>/proxies.txt`, `pm_mailing.json`, `contacts.json` | The project's proxy pool, mailing statistics and contacts ledger. |
| `projects/<id>/proxies_restricted.txt` | Optional: the project's proxies for its permanently restricted workers. |
| `sessions/` | Free accounts: workers in no project. |
| `stats/account_limits.json`, `invite_limits.json`, `contacts_limits.json` | Daily per-account counters (mailing, invites, contact adds). |
| `stats/released.json` | Released workers. |
| `stats/restricted.json`, `spambot_status.json` | Results of the last @SpamBot check. |
| `stats/auto_replies.json` | People already auto-replied to. |
| `stats/projects.json` | Projects. |
| `stats/profile_done.json` | Which profile function last changed which worker (the pickers tick the rest). |
| `stats/bot_scrape.json`, `bot_running` | Each project's bot scrape to continue after a restart; a marker telling a crash from a clean stop. |

Git-ignored: `.env`, `config.toml`, session files, `projects/`, `stats/` and everything in `assets/`
except `names.txt`.

## Running under systemd

The unit file is [`deploy/teleharvester-bot.service`](deploy/teleharvester-bot.service). It assumes
`/opt/teleharvester`, a venv in `.venv/` and a `teleharvester` user; adjust `User=`,
`WorkingDirectory=` and `ExecStart=` as needed.

```bash
sudo cp deploy/teleharvester-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now teleharvester-bot
journalctl -u teleharvester-bot -f
```

- `Restart=on-failure`; five failed starts within 5 minutes stop retries
  (`systemctl reset-failed teleharvester-bot` after fixing the cause).
- Watchdog (`WatchdogSec=120`) restarts a hung process.
- Each start is reported to the admins, including restarts after a crash or OOM kill.