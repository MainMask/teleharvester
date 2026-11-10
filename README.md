# teleharvester

A tool for operating pools of Telegram accounts (Telethon), one pool per project: DM mailings with
per-recipient statistics, chat and comment campaigns, joining and inviting, reactions, poll votes,
moderation reports, profile and session management, and a channel/group scraper with offline analysis.
It is controlled from a Telegram control bot; a few session scripts add accounts and read login codes from the terminal.

![Python](https://img.shields.io/badge/python-3.11%2B-blue)

> **Disclaimer.** For educational use and for accounts and chats you own or are authorized to act
> on. Bulk messaging, campaigns and moderation reports may violate Telegram's Terms of Service and
> local law. You are solely responsible for how you use it.

## Features

The bot's menu (Russian labels) is described in [`modules/registry.py`](modules/registry.py); its functions
are discovered from [`functions/`](functions/). ⚠️ marks actions that may get a worker limited by Telegram, in the
screen's description of its buttons (the buttons themselves stay short).

All the work is a project's: each project has its own worker accounts, proxies, mailing statistics
and contacts ledger, and its card holds everything done with them. The main menu has three items, in one row:

| Section | Functions |
|---|---|
| **📁 Проекты** | The projects. A project's card sums it up and opens its screens: on its files — **📥 Сбор** (channels, scrape, top-up, group members, verification, analysis), **🎯 База и рассылка** (the base's state, list upload, the whole base as a file, clear, add to contacts, DM mailing, auto-reply), **🛠 Управление** (region, rename, delete) — and on its workers, below; see [Projects](#projects). |
| **📊 Задачи** | Every project's running jobs in one message: progress, **⏹** for each that can be stopped, **🔄 Обновить** (bot only; same as `/status`). |
| **⚙️ Настройки** | Under **Паузы** the action delay and the profile pause, under **Дневные лимиты на аккаунт** the per-account daily caps (DMs, invites, contacts, comments), each value on its button — for all projects, written back to `config.toml`. The chat campaign's trigger and messages per worker are asked by **🔔 Чат — по слову** itself and saved there. |

A project's screens, on its own workers:

| Screen | Functions |
|---|---|
| **🤖 Аккаунты** | The workers' count and warmup, the project's proxies, moving workers to another project; its screens below. |
| **➕ Добавить** | Adding accounts: **🤖 Воркер по номеру**, **👤 Личный по номеру** (all the projects', scrapes only), **📥 Из файлов** (tdata, `.session`), **📦 Из свободных** (accounts in no project); details under «Adding accounts» below. Each flow ends with ⬅️ back here. |
| **📋 Список** | The accounts' checks, with workers only: **📡 Опросить** (every worker asked for its profile: name, username, Premium, its last @SpamBot status, the numbers' countries) and **🩺 SpamBot** (the @SpamBot status check). Then the project's accounts by category — 🆕 new, 🌡 warming, ✅ warmed, ❌ failed (with the error), ⛔ permanently restricted, 💀 dead (`sessions/inactive/`): a category's accounts with their cards (login codes; a dead one has none), **📦 Скачать ZIP** of their session files, **🗑 Удалить** (the files and their state; the accounts are not logged out; busy ones are skipped). **👤 Личные** — the personal accounts' cards (login codes, removal); **📦 Все аккаунты** — a ZIP of all the session files. A ZIP asks its format first: **.session + .json** (Telethon's SQLite session and the app, device, account and 2FA password it was used with — a `.session` worker's has the session only — what other software takes, and what **➕ Добавить → 📥 Из файлов** reads back) or **.jsession** (the bot's own, with the proxy: a backup for this bot). Either holds the bot's own keys: an account run here and elsewhere at once may have its session ended by Telegram. Only what there is is offered (a category with accounts, the ZIP with files). |
| **🔥 Прогрев** | Warming the project's accounts up before work, day by day; see [Warmup](#warmup). |
| **👤 Профиль** | Name, username, bio, photo, remove profile channel — applied one account at a time with `profile_pause`; the lists offered are the project's region's ([Content](#content)). |
| **🔐 Безопасность** | Set 2FA password, terminate other sessions, hide last seen, clear dialogs. |
| **💬 Активность** | Every worker of the project on one target: DM to one recipient; comments, a pair as the chat's: **🗨 Комментарии — к посту** (every worker under one post) and **🆕 Комментарии — к новым постам** (under the new posts of watched channels: the project's channels or a typed list; N comments a post, each by another worker, a pause of X–Y minutes before each; a job until ⏹ — the project's other jobs wait meanwhile, and a channel whose discussion is for members only needs 🚪 Вступить в чат first; kept in the project's `watch.json`, continued after a bot restart, the posts missed meanwhile commented only if under 3 hours old, the comments still waiting their pause at the stop not written; the comments' message may be a .txt of variants, one a line, both in 🗨 and 🆕: each comment takes one, none twice under a post before every one has); instant and trigger-based chat broadcasts; join chat, reactions (set, or each worker's own taken off), poll vote, inviting members of one chat to another; report a post or a user. |

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
| `config.toml` | Behaviour. See [`config.toml.example`](config.toml.example). Copy it to `config.toml`, or run a session script once: it creates it and asks for the API ID/hash (from <https://my.telegram.org>) if `.env` lacks them. |

Key `config.toml` settings:

| Key | Default | Meaning |
|---|---|---|
| `[broadcast] delay` | — | Pause between a worker's actions, `[min, max]` s. |
| `[broadcast] trigger`, `messages_count` | — | Trigger phrase and per-chat message cap for chat/comment campaigns (`0` = until stopped); the bot asks for them at each run and saves them here. |
| `[limits] per_account_daily` | `30` | DM mailing messages per account per day (`0` = unlimited). |
| `[limits] account_pause` | `[30, 60]` | Pause when the mailing switches accounts. |
| `[limits] profile_pause` | `[60, 180]` | Pause between accounts for profile changes. |
| `[limits] invite_per_account_daily`, `contacts_per_account_daily` | `0` | Daily invite / contact-add caps. Contacts: the next account continues. Invites: each worker gets its own share of the members, and a worker at its cap skips the rest of its share for that run. |
| `[limits] comments_per_account_daily` | `0` | Daily post-comment cap, 🗨 and 🆕 counted together (`stats/comments_limits.json`). 🗨: a worker at its cap stops; 🆕: it is skipped under the posts until the next day, the next worker comments. A change in ⚙️ applies to the runs started after it, and to a running 🆕 from its next check. |
| `[autoreply] enabled`, `text`, `interval` | `true`, `""`, `[600, 1200]` | Bot only: replies once to people who answer a mailing (their messages are forwarded to the admins either way, once the project has mailed anyone). `enabled` and `text` are the default of a project that has not set its own (**🎯 База и рассылка → 📬 Автоответ**, `projects/<id>/autoreply.json`); `interval` ([sec] or [min, max], ≥ 60 s) is shared: each worker is checked on a timer of its own, a random time within it each time (a bare number: a fixed period); the projects are checked side by side, so one's dead proxies don't delay another's replies. Empty text disables it. |
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
  them, and one busy with a job stays. Only `.jsession` accounts are moved from the menu.
- **Personal accounts** — `personal_sessions/`: same format, used for scraping only, never as workers;
  shared by all projects (one scrape at a time on each).

Adding accounts (bot: a project's **🤖 Аккаунты → ➕ Добавить**; or the scripts, which ask for the project and
don't run while the bot does):

```bash
python sessions/add_session.py           # worker or personal account by phone, tdata, or a free one
python sessions/login.py [file.jsession] # login codes received in the last 15 minutes
```

- **From files** (bot: **➕ Добавить → 📥 Из файлов**; `sessions/add_session.py` → [3]): accounts as sellers give them,
  each made the project's `sessions/<phone>.jsession` on one of its proxies. An account already the project's is
  left as is; one that is a worker elsewhere or a personal account is refused.
  - **tdata** — a Telegram Desktop `tdata` folder: in the bot a ZIP of it (≤ 20 MB, ≤ 100 MB unpacked), for the
    script `tdata_import/<name>/tdata` (marked `.imported` once done). Its 2FA password, optionally, in a `.txt`
    beside it: one named so — `Пароль 2фа.txt`, `2fa.txt`, `twoFA.txt`, `password.txt`… — or the only `.txt`
    there, holding `<label>: <password>` or the password alone; without one the bot asks for it. A changed
    password is written back into that `.txt`.
  - **.session** — Telethon `.session` files (SQLite), each optionally with a `.json` of the same name (`app_id`,
    `app_hash`, `device`, `sdk`, `app_version`, `lang_pack`, `system_lang_pack`, `twoFA`): the `.json`'s app and
    device are presented (a random official one without it). In the bot send a `.session` with its `.json` at
    once (selected together) or a ZIP of them; for the script put them in `session_import/` (moved to
    `imported/` once done).
  - The bot takes several files at once — the `.session` ones first, then the tdata ones in turn — and after them
    waits for the next ones: no ➕ Добавить again for each account.
- **Proxies.** Each project's own list, `projects/<id>/proxies.txt` (set with **🌐 Прокси**), one per line,
  `scheme://[user:pass@]ip:port` (`socks5`, `socks4`, `http`), up to 3 accounts per proxy; the project's
  new accounts take theirs from it. Personal accounts use no proxy.
  Optionally, `projects/<id>/proxies_restricted.txt` (the second step of **🌐 Прокси**): proxies for the
  permanently restricted workers, no cap per proxy, none of the pool's. They stay workers (they still answer
  replies and can scrape) but leave the pool, so its places go to new accounts and no fresh account shares
  an IP with a restricted one. After each status check the bot offers **🔁 Пересадить** for a day: the
  restricted ones go onto these proxies, the ones clean again back into the pool (or
  stay, reported, while it is full); the other workers keep their proxies, and one busy right now (a scrape,
  a login) is left for the next check. **🗑 Не отделять** at that step
  drops the list and brings them back into the pool. Any other worker not on a pool
  proxy (clean again, its list replaced, or moved here from another project) is offered back into it too.
- Session files put in by hand are read when the bot starts.

## Usage

```bash
python -m bot    # Telegram control bot (needs BOT_TOKEN and [bot] admins)
```

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
  yet, a job in the way, a stale button) is said in a pop-up over its screen. An irreversible action (delete, clear,
  reset) asks first in its screen's place; **↩️ Отмена** brings the screen back; a stale confirmation (an older one,
  or one left too long) does nothing.
- **Jobs.** Each project runs one account-based job at a time, its scraper jobs and analysis in slots of
  their own; projects run side by side, and an account is never used by two jobs at once. Long jobs show
  **📊 Прогресс** and **⏹ Стоп**; each job is named as its button (in **📊 Задачи** too) and ends with a
  summary (workers, successes, errors, elapsed time) and **⬅️** back to the screen it was started from, in
  its own project. A message sent, or a form's last button tapped, while the project's slot is busy is not
  lost: the step stays, to be sent or tapped again once that job is over. A change to a project (its
  channels, name, list, base) ends on the screen it was started from, the result above it.
- Only one teleharvester process runs per folder (lock in `tmp/`); stop the service before running
  a session script.

## Worker rules

Applied within each project.

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
- **Settings** (`config.toml`: delays, daily caps) are shared by all projects; the daily caps count per
  account. The auto-reply is each project's own (its default: `config.toml`'s `[autoreply]`).

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

The card shows the project's workers and their warmup (🔥 warmed of all, ⛔ restricted, 💀 dead), its base (people,
each once, whom the mailing wrote to, whom the workers added to contacts), channels count, an unfinished scrape or the top-up to
make, and its files count; **📥 Сбор** lists its channels and the scrape's files, **🎯 База и рассылка** the base's files (members bases, the list).

| Card screen → button | Description |
|---|---|
| **📥 Сбор → 📢 Каналы** | The project's channels and groups to scrape (**➕ Добавить каналы** while it has none); the new list replaces the old one. |
| **📥 Сбор → ▶️ Скрап** | Scrapes all project channels; resumes an unfinished scrape. |
| **📥 Сбор → ⏩ Дособрать** | Updates the newest scrape from one week before its end to today and merges the result into the project files; replaced files go to `<name>_previous/`. Must run on the account that made the scrape. |
| **📥 Сбор → 👥 Участники групп** | Adds a group's full member list to `<name>_participants_members.parquet`; a previous base that can't be read is moved to `<name>_previous/`, not overwritten. |
| **📥 Сбор → 🔍 Верификация** | Checks a scrape against the live channel for missed posts. |
| **📥 Сбор → 📈 Анализ** | Every tool on one screen, each with what it does: t.me links, keyword search, file view, combine, comments table, monthly activity, sample, rebuilding the members base. Combine merges the project's scrapes into `<name>_posts_combined.parquet`, offered on the card like its other posts files (⏩ still continues the newest scrape). |
| **🎯 База и рассылка → 📄 Свой список** | Adds usernames/phones to `<name>_participants_list.txt` (mailing only). |
| **🎯 База и рассылка → 📦 Скачать базу** | **📚 Вся база** as one `.csv` (`user_id, access_hash, owner_id, username, name, phone`). The project's base is its files glued as they are used (`modules/project_base.py`): its scrapes' participants, its groups' members and its list, each person once — by ID across the bases (the row whose access hash a worker of the project owns, else one of no known owner, else the newest file's; a missing username taken from another row), and a list's @username a base already has left out. The files stay its sources: a new scrape is in it at once, no copy lies on disk. 🎯 counts its people. |
| **🎯 База и рассылка → 🧹 Очистить базу** | Moves the members base or the list to `<name>_previous/`, where it can be taken back from. |
| **🎯 База и рассылка → 📬 Автоответ** | The project's auto-reply: on/off, its text, how many of its workers' mailing recipients answered (in all, a day, a week, per worker; **🔄 Обновить** for the latest). Each worker answers a person once. Once the project has mailed anyone, its workers are checked for replies even with the auto-reply off: what people write is forwarded to the admins with **✍️ Ответить** — an answer typed in the bot, sent by the worker the person answered. |
| **🎯 База и рассылка → 🧪 Гипотезы** | The mailing's hypotheses (HADI): each mailing is put under one at its **🧪** step (an open one, a new one typed, or none). Per hypothesis: sent, answered within 72 h (any answer counts) among the sends at least 72 h old, the rate with its 95% confidence interval, a note while there are under 300 such sends or answers may still come. Its card (**#N**): **🏁 Закрыть** with its verdict and an insight, **✏️** its text, **🗑** it while no mailing ran under it. **📄 Отчёт .md** — every hypothesis with its runs, texts, numbers per worker and the dialogs (what people wrote, the auto-reply, the answers sent with ✍️), to read or give to an AI. A worker moved to another project: what people answer its earlier sends is not in the report (it is logged in the new project). |
| **🎯 База и рассылка → 📇 В контакты** / **📨 Рассылка** | Add the base to the project's workers' contacts, then mail it: **📚 Вся база** first (the contacts: its people with an ID), or one of its files. With «skip those already messaged» (asked before each mailing), each person is mailed once within the project. |
| **🛠 Управление** | **🌍 Регион** — the project's content: the warmup's and 👤 Профиль's lists ([Content](#content)), rename (files are renamed too), delete (files are kept; a project with workers is not deleted — move them first). |
| **🤖 Аккаунты** / **💬 Активность** | The project's accounts with their warmup, and the work of its workers on one target (see [Features](#features), [Warmup](#warmup)). |

## Warmup

A project's **🤖 Аккаунты → 🔥 Прогрев** warms its accounts up before they work, one day of it per account a day: on the first, the
profile and its security (a step that fails, e.g. terminating other sessions, which Telegram refuses a new session for
24 h, is tried again the next day); every day, light activity. An account is ✅ warmed once its days are run (**📅 Дней**,
typed, any number from 1; 1 — a single pass) and every setup step is done; a day where no step went through (a flood limit, the profile not read)
is not counted. **▶️ Прогреть** (on top) runs it now; **🔁 Ежедневно** runs it by itself once a day
at a random time between 10:00 and 21:00 when the project's job slot is free (bot only; one still busy at 21:00, or a
bot started later, waits for the next day), reporting to the chat that switched it on. **🧵 Потоков** — accounts warmed at once, typed (any number from 1; each next one after the profile
pause), but never two of one proxy side by side (those without one: the server's address), so at most as many as the
accounts' proxies; the ⚙️ delay between steps. The screen shows whether it runs now and when the daily run is (**🔄 Обновить** redraws it), the accounts by
category and the warmed today / in all; **♻️ Сбросить прогрев** starts every account of the project from day one.

- **🧩 Шаги** — each step on (✅), off (⬜) or, for username, avatars, bio and music, only while the field is empty
  (🛡, a filled one is kept); 🎲 next to a step — how many or with what chance:
  - setup: name, username (made of the name, `olga_petrova`, `opetrova92`…), avatars (1 or 1–3; the old ones
    removed, each slightly cropped and re-encoded so no two accounts share a file), bio, profile music, a sticker set,
    hide last seen, a random 2FA password (only where there is none; kept in the `.jsession` and `tdata_import/` — a
    `.session` worker, with nowhere to keep it, gets none),
    terminate other sessions;
  - activity: join channels (0–3, 1, 2, 3, 5, 3–7 or 5–10 a day, at most as many as `channels.txt` holds) and read
    them and the joined ones, `/start` a bot, notes in Saved Messages, one pinned.
- The content is the project's region's (**🛠 Управление → 🌍 Регион**, [Content](#content)). A step without content
  is skipped.
- The steps also mark the profile pickers' ledger (`stats/profile_done.json`), so a later 👤 Профиль run picks the
  rest. Permanently restricted accounts are not warmed; a dead one goes to `sessions/inactive/`.

## Content

What accounts are given — by the warmup and by 👤 Профиль's «📄 Из …» lists — is one set
per region, the project's (🛠 Управление → 🌍 Регион): `assets/content/ru/` or `assets/content/en/` — `names.txt`,
`usernames.txt` (👤 Профиль only), `bios.txt`, `channels.txt`, `bots.txt`, `stickers.txt` (a set's short name or
`t.me/addstickers/…` link), `notes.txt` (a line each, `#` comments; the starter lists ship with the repo), `photos/`
and `music/` (`Artist - Title.mp3`; yours). **Upgrading:** move your `assets/bios.txt`, `usernames.txt` and `photos/`
into the region's folder; the old `assets/names.txt` is in `assets/content/en/names.txt`.

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
| `assets/content/<ru\|en>/` | The content: lists and files for profiles and the warmup (see [Content](#content)). |
| `assets/targets.txt` | Mailing recipients (username/phone per line). |
| `assets/contacts.parquet` | Base for *Add to contacts* (`user_id`, `access_hash`). |
| `projects/<id>/sessions/` | The project's workers (`inactive/`, `restricted/` inside). |
| `projects/<id>/proxies.txt`, `pm_mailing.json`, `contacts.json` | The project's proxy pool, mailing statistics and contacts ledger. |
| `projects/<id>/outreach.json`, `outreach_sends.jsonl`, `outreach_dialogs.jsonl` | The project's hypotheses and mailing runs, each send (whom, by which worker) and each message of the dialogs after it, both ways (🧪 Гипотезы). |
| `projects/<id>/proxies_restricted.txt` | Optional: the project's proxies for its permanently restricted workers. |
| `projects/<id>/warmup.json` | The project's warmup settings. |
| `stats/warmup.json` | Each account's warmup: day, steps done, status, last error (by user id: kept when moved). |
| `session_import/` | `.session` (+ `.json`) files for `sessions/add_session.py`'s import; imported ones in `imported/`. |
| `sessions/` | Free accounts: workers in no project. |
| `stats/account_limits.json`, `invite_limits.json`, `contacts_limits.json` | Daily per-account counters (mailing, invites, contact adds). |
| `stats/released.json` | Released workers. |
| `stats/restricted.json`, `spambot_status.json` | Results of the last @SpamBot check. |
| `stats/auto_replies.json` | People who answered a mailing (each auto-replied to once, with the auto-reply on). |
| `stats/projects.json` | Projects. |
| `stats/profile_done.json` | Which profile function last changed which worker (the pickers tick the rest). |
| `stats/bot_scrape.json`, `bot_running` | Each project's bot scrape to continue after a restart; a marker telling a crash from a clean stop. |

Git-ignored: `.env`, `config.toml`, session files, `projects/`, `stats/`, `session_import/` and everything in `assets/`
except the content's text lists.

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