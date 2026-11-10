"""Outreach analytics for the PM mailing, as HADI cycles: hypotheses, the mailing runs under them, who each run
wrote to (by which worker) and who answered — and a Markdown report of it all, for a person or an AI to read.

A project keeps its own, in its folder (projects.home): outreach.json (the hypotheses and the runs), and two
append-only logs, a line per send (outreach_sends.jsonl) and per message of the dialogs that follow
(outreach_dialogs.jsonl: what people wrote, what the worker answered — the auto-reply, an admin's ✍️). A person's
first message is their reply: it belongs to the last send to them by that worker before it, and so does the
dialog after it. Mailings before these logs are not in them, nor is what people answer a worker once it has moved
to another project: that is logged there, where its earlier sends are not.
"""
import json
import math
import os
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

from modules import json_file

STATE_NAME = "outreach.json"
SENDS_NAME = "outreach_sends.jsonl"
DIALOGS_NAME = "outreach_dialogs.jsonl"
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
REPLY_HOURS = 72  # an answer counts within this after the send: hypotheses compare alike
REPLY_WINDOW = timedelta(hours=REPLY_HOURS)
MIN_SENDS = 300  # fewer sends counted: a difference of a few points is noise
Z95 = 1.96  # the 95% confidence interval's
MAX_REPLY_TEXT = 1000  # of a reply's text, kept
MAX_REPORT_REPLIES = 50  # dialogs per hypothesis, in the report
MAX_DIALOG_MESSAGES = 20  # per dialog, in the report
VERDICTS = {"yes": "✅ подтвердилась", "no": "❌ не подтвердилась", "unclear": "🤷 мало данных / неясно"}
NO_HYPOTHESIS = "Без гипотезы"


def _now() -> str:
    return datetime.now().strftime(TIME_FORMAT)


class Unreadable(ValueError):
    """STATE_NAME edited by hand so that it can't be read: said where it is used, never rewritten (its hypotheses
    would be lost) — every change here reads it first."""


def load(home: str) -> dict:
    try:
        return json_file.load(os.path.join(home, STATE_NAME), {"hypotheses": [], "runs": []})
    except ValueError as err:
        raise Unreadable(str(err)) from err


def _save(home: str, state: dict):
    json_file.save(os.path.join(home, STATE_NAME), state)


def add_hypothesis(home: str, text: str) -> int:
    """Its number: never one given before, a deleted one's neither — an older card's button, a mailing step or a
    report naming #N means that one hypothesis."""
    state = load(home)
    number = max([state.get("last_hypothesis", 0), *(h["id"] for h in state["hypotheses"])]) + 1
    state["last_hypothesis"] = number
    state["hypotheses"].append({"id": number, "text": text, "created": _now()})
    _save(home, state)
    return number


def open_hypotheses(home: str) -> list[dict]:
    return [h for h in load(home)["hypotheses"] if not h.get("closed")]


def _find(state: dict, number: int) -> dict | None:
    return next((h for h in state["hypotheses"] if h["id"] == number), None)


def find_hypothesis(home: str, number: int) -> dict | None:
    return _find(load(home), number)


def edit_hypothesis(home: str, number: int, text: str) -> bool:
    """Its text anew (a typo, a sharper wording); False if there is no such hypothesis."""
    state = load(home)
    found = _find(state, number)
    if found is None:
        return False
    found["text"] = text
    _save(home, state)
    return True


def has_runs(home: str, number: int) -> bool:
    return any(run.get("hypothesis") == number for run in load(home)["runs"])


def delete_hypothesis(home: str, number: int) -> bool:
    """Gone, if no mailing ran under it (one that did is data: it is closed instead); False if not deleted."""
    if has_runs(home, number):
        return False
    state = load(home)
    kept = [h for h in state["hypotheses"] if h["id"] != number]
    if len(kept) == len(state["hypotheses"]):
        return False
    state["hypotheses"] = kept
    _save(home, state)
    return True


def close_hypothesis(home: str, number: int, verdict: str, insight: str = "") -> bool:
    """Its verdict (VERDICTS) and the insight drawn; False if there is no such open hypothesis."""
    state = load(home)
    found = _find(state, number)
    if found is None or found.get("closed"):
        return False
    found.update(verdict=verdict, insight=insight, closed=_now())
    _save(home, state)
    return True


def add_run(home: str, hypothesis: int | None, segment: str, text: str, media: int, planned: int) -> int:
    """A mailing run under `hypothesis` (None: under none): what it sent and to whom; its number for log_send."""
    state = load(home)
    number = max((r["id"] for r in state["runs"]), default=0) + 1
    state["runs"].append({"id": number, "hypothesis": hypothesis, "started": _now(), "segment": segment,
                          "text": text, "media": media, "planned": planned})
    _save(home, state)
    return number


def _append(path: str, entry: dict):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as fileobj:
        fileobj.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _read(path: str) -> list[dict]:
    """The log's entries; a line cut by a crash mid-write is skipped."""
    if not os.path.exists(path):
        return []
    entries = []
    with open(path, encoding="utf-8") as fileobj:
        for line in fileobj:
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
    return entries


def log_send(home: str, run: int, worker: str, user: int, username: str | None):
    """`worker`: its session file's name."""
    _append(os.path.join(home, SENDS_NAME),
            {"run": run, "worker": worker, "user": user, "username": username, "at": _now()})


def log_message(home: str, worker: str, user: int, text: str, out: bool = False, auto: bool = False,
                at: datetime | None = None):
    """A message of the dialog between the worker and the person: theirs, or (`out`) the worker's — the auto-reply
    (`auto`) or an admin's answer. `at`: when it was written (Telegram's time, in UTC), else now."""
    when = at.astimezone().strftime(TIME_FORMAT) if at else _now()  # local, as the sends' times
    entry = {"worker": worker, "user": user, "at": when, "text": text[:MAX_REPLY_TEXT]}
    if out:
        entry.update(dir="out", auto=auto)
    _append(os.path.join(home, DIALOGS_NAME), entry)


def _time(value: str) -> datetime:
    return datetime.strptime(value, TIME_FORMAT)


def _joined(home: str) -> list[dict]:
    """Every send, with its reply (`reply`: the entry, `delay`: how long after the send) if the person answered
    it — a worker and person's earliest message, to their last send before it, once — and the dialog from there
    (`dialog`: the messages both ways, whatever was sent to them later)."""
    sends = sorted(_read(os.path.join(home, SENDS_NAME)), key=lambda s: s["at"])
    by_pair = defaultdict(list)
    for send in sends:
        send["reply"], send["dialog"] = None, []
        by_pair[(send["worker"], send["user"])].append(send)
    answered = {}  # (worker, person) -> the send they replied to
    for message in sorted(_read(os.path.join(home, DIALOGS_NAME)), key=lambda m: m["at"]):
        pair = (message["worker"], message["user"])
        if pair in answered:
            answered[pair]["dialog"].append(message)
            continue
        before = [send for send in by_pair.get(pair, []) if send["at"] <= message["at"]]
        if before and message.get("dir") != "out":  # the worker's own message first: no one's reply
            send = answered[pair] = before[-1]
            send["reply"], send["delay"] = message, _time(message["at"]) - _time(send["at"])
            send["dialog"].append(message)
    return sends


def interval(replied: int, counted: int) -> tuple[float, float]:
    """The rate's 95% confidence interval, in % (Wilson's: sound for small counts and rates near 0)."""
    if not counted:
        return 0.0, 0.0
    rate, z2 = replied / counted, Z95 ** 2
    center = (rate + z2 / (2 * counted)) / (1 + z2 / counted)
    margin = Z95 * math.sqrt(rate * (1 - rate) / counted + z2 / (4 * counted ** 2)) / (1 + z2 / counted)
    return max(0.0, center - margin) * 100, min(1.0, center + margin) * 100


def _numbers(sends: list[dict], now: datetime) -> dict:
    """The sends' numbers. The rate counts the sends whose REPLY_WINDOW is over (`counted`), an answer within it
    (`replied`): a younger send's answer may still come — while none is that old, all of them (`fresh` says so).
    `replied_all`: answered at all; the median delay of those."""
    matured = [s for s in sends if now - _time(s["at"]) >= REPLY_WINDOW]
    counted = matured or sends
    replied = sum(1 for s in counted if s["reply"] and s["delay"] <= REPLY_WINDOW)
    delays = [s["delay"] for s in sends if s["reply"]]
    return {
        "sent": len(sends),
        "counted": len(counted),
        "replied": replied,
        "replied_all": len(delays),
        "rate": replied / len(counted) * 100 if counted else 0.0,
        "interval": interval(replied, len(counted)),
        "median": statistics.median(delays) if delays else None,
        "fresh": len(matured) < len(sends),
        "matured": bool(matured),
    }


def stats(home: str, now: datetime | None = None) -> dict:
    """{hypothesis id (None: runs under none): its numbers (_numbers) and `sends`}."""
    now = now or datetime.now()
    run_of = {run["id"]: run.get("hypothesis") for run in load(home)["runs"]}
    grouped = defaultdict(list)
    for send in _joined(home):
        grouped[run_of.get(send["run"])].append(send)
    return {key: {**_numbers(sends, now), "sends": sends} for key, sends in grouped.items()}


def _hours(delay: timedelta | None) -> str:
    if delay is None:
        return "—"
    hours = delay.total_seconds() / 3600
    return f"{hours * 60:.0f} мин" if hours < 1 else f"{hours:.1f} ч"


def flags(numbers: dict) -> str:
    """Why the numbers may mislead yet: too few sends, answers still coming."""
    found = []
    if numbers["counted"] < MIN_SENDS:
        found.append("мало данных")
    if numbers["fresh"] and numbers["matured"]:
        found.append(f"{numbers['sent'] - numbers['counted']} отправок моложе {REPLY_HOURS} ч ещё не в доле")
    elif numbers["fresh"]:
        found.append("ответы ещё приходят — доля предварительная")
    return ", ".join(found)


def with_flags(line: str, numbers: dict) -> str:
    """`line` with the numbers' flags after it, if any."""
    noted = flags(numbers)
    return line + (f" · ⚠️ {noted}" if noted else "")


def summary(numbers: dict) -> str:
    """The numbers in a line (the screen's, the report's)."""
    low, high = numbers["interval"]
    return (f"отправлено {numbers['sent']} · ответили за {REPLY_HOURS} ч {numbers['replied']} из {numbers['counted']} "
            f"({numbers['rate']:.1f}%, 95% ДИ {low:.1f}–{high:.1f}%)")


def _cell(text: str) -> str:
    """A table cell: one line, no column break."""
    return " ".join(text.split()).replace("|", "\\|")


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" for line in (text or "(без текста)").splitlines() or [""])


def status(hypothesis: dict) -> str:
    """🔄 while open, its verdict once closed."""
    return VERDICTS.get(hypothesis.get("verdict"), "") if hypothesis.get("closed") else "🔄 в работе"


def _day(at: str) -> str:
    return f"{_time(at):%d.%m}"


def _period(sends: list[dict]) -> str:
    """From the first send's day to the last's (sends in time order)."""
    if not sends:
        return "—"
    first, last = _day(sends[0]["at"]), _day(sends[-1]["at"])
    return first if first == last else f"{first}–{last}"


def _segments(runs: list[dict]) -> str:
    return ", ".join(dict.fromkeys(run["segment"] for run in runs)) or "—"


def _rate_cells(n: dict) -> str:
    """Replied of counted | % | the 95% interval: a table's cells."""
    low, high = n["interval"]
    return f"{n['replied']} из {n['counted']} | {n['rate']:.1f} | {low:.1f}–{high:.1f}"


def _section(title: str, hypothesis: dict | None, runs: list[dict], numbers: dict | None) -> list[str]:
    lines = [f"## {title}", ""]
    if hypothesis is not None:
        lines += [_quote(hypothesis["text"]), "", f"- **Статус:** {status(hypothesis)}"]
        if hypothesis.get("insight"):
            lines.append(f"- **Вывод:** {hypothesis['insight']}")
    if numbers:
        lines.append(with_flags(f"- {summary(numbers)} · всего ответов {numbers['replied_all']} · "
                                f"медиана до ответа {_hours(numbers['median'])}", numbers))
    else:
        lines.append("- Отправок пока нет.")
    lines.append("")

    if runs:
        by_run = defaultdict(list)
        for send in numbers["sends"] if numbers else []:
            by_run[send["run"]].append(send)
        lines += ["### Запуски", ""]
        for run in runs:  # what was planned, what went out (caps, privacy, Premium-only cut it), when
            sent = by_run[run["id"]]
            lines.append(f"- {_time(run['started']):%d.%m.%Y %H:%M} · сегмент: {run['segment']} · "
                         f"отправлено {len(sent)} из {run['planned']}" + (f" · {_period(sent)}" if sent else ""))
        lines.append("")
        texts = list(dict.fromkeys((run["text"], run.get("media", 0)) for run in runs))
        lines += ["### Текст сообщения" + ("" if len(texts) == 1 else f" (вариантов: {len(texts)})"), ""]
        for text, media in texts:
            lines += [_quote(text), *([f"\n_+ медиа: {media}_"] if media else []), ""]

    replies = [s for s in numbers["sends"] if s["reply"]] if numbers else []
    if replies:
        lines += [f"### Диалоги ({len(replies)})", ""]
        for send in replies[:MAX_REPORT_REPLIES]:
            lines += _dialog(send)
        if len(replies) > MAX_REPORT_REPLIES:
            lines.append(f"- … и ещё {len(replies) - MAX_REPORT_REPLIES}")
        lines.append("")
    return lines


def _dialog(send: dict) -> list[str]:
    """A person's dialog after the send: who, how soon they answered, then the messages both ways."""
    who = f"@{send['username']}" if send.get("username") else f"id {send['user']}"
    lines = [f"- **{who}** — ответ через {_hours(send['delay'])}:"]
    for message in send["dialog"][:MAX_DIALOG_MESSAGES]:
        arrow = ("→ 🤖" if message.get("auto") else "→") if message.get("dir") == "out" else "←"
        lines.append(f"  - {arrow} {_cell(message['text']) or '[медиа]'}")
    if len(send["dialog"]) > MAX_DIALOG_MESSAGES:
        lines.append(f"  - … и ещё {len(send['dialog']) - MAX_DIALOG_MESSAGES} сообщений")
    return lines


def _workers_table(numbers: dict, workers: dict[str, str], now: datetime) -> list[str]:
    """Every worker over all the hypotheses: one per hypothesis is noise; this one shows an account that drags
    any hypothesis down."""
    per_worker = defaultdict(list)
    for group in numbers.values():
        for send in group["sends"]:
            per_worker[send["worker"]].append(send)
    lines = ["## Воркеры (все гипотезы)", "", f"| Воркер | Отправлено | Ответили за {REPLY_HOURS} ч | % | 95% ДИ |",
             "|---|---|---|---|---|"]
    for worker, sends in sorted(per_worker.items(), key=lambda item: -len(item[1])):
        sends.sort(key=lambda s: s["at"])
        n = _numbers(sends, now)
        lines.append(f"| {_cell(workers.get(worker, worker))} | {n['sent']} | {_rate_cells(n)} |")
    return lines + [""]


def report_md(home: str, project: str, earlier: int = 0, workers: dict[str, str] | None = None,
              now: datetime | None = None) -> str:
    """The project's outreach as Markdown: how to read it; a table of the hypotheses (whom each went to, when, its
    numbers); the workers over all of them; then each hypothesis — its runs, texts, the dialogs —, the runs under
    none. `earlier`: how many people were written to before these logs; `workers`: a session file's name -> how to
    name the worker (its @username)."""
    now = now or datetime.now()
    state, numbers, workers = load(home), stats(home, now), workers or {}
    runs = defaultdict(list)
    for run in state["runs"]:
        runs[run.get("hypothesis")].append(run)

    lines = [f"# Аутрич: {project} — отчёт на {now:%d.%m.%Y %H:%M}", "", "**Как читать**", "",
             "- Лид — любой ответ человека. Ответ относится к последнему сообщению ему от того же воркера.",
             f"- «Ответили за {REPLY_HOURS} ч» — среди отправок старше {REPLY_HOURS} ч: у более свежих ответ ещё "
             "может прийти (пока таких нет — среди всех, доля предварительная).",
             "- 95% ДИ — доверительный интервал доли: если интервалы двух гипотез перекрываются, разница между "
             f"ними может быть случайной. Меньше {MIN_SENDS} учтённых отправок — «мало данных».",
             "- Гипотезы честно сравнимы, только если у них один сегмент и близкий период: иначе разница может "
             "быть в аудитории или во времени, а не в тексте.",
             "- В диалогах: ← пишет человек, → отвечает воркер (🤖 — автоответ).", ""]
    if earlier:
        lines += [f"До учёта гипотез рассылка написала {earlier} людям — в отчёт они не входят.", ""]

    lines += ["## Сводка", "",
              f"| # | Гипотеза | Статус | Сегмент | Период | Отправлено | Ответили за {REPLY_HOURS} ч | % | 95% ДИ | "
              "Всего ответов | Пометки |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]

    def row(n: dict | None, segment_runs: list[dict]) -> str:
        head = f"{_cell(_segments(segment_runs))} | {_period(n['sends']) if n else '—'}"
        if not n:
            return f"{head} | 0 | — | — | — | 0 | нет отправок"
        return f"{head} | {n['sent']} | {_rate_cells(n)} | {n['replied_all']} | {flags(n)}"

    for hypothesis in state["hypotheses"]:
        lines.append(f"| {hypothesis['id']} | {_cell(hypothesis['text'])} | {status(hypothesis)} | "
                     f"{row(numbers.get(hypothesis['id']), runs[hypothesis['id']])} |")
    if None in numbers:
        lines.append(f"| — | {NO_HYPOTHESIS} | — | {row(numbers[None], runs[None])} |")
    lines.append("")
    if numbers:
        lines += _workers_table(numbers, workers, now)

    for hypothesis in state["hypotheses"]:
        lines += _section(f"Гипотеза #{hypothesis['id']}", hypothesis, runs[hypothesis["id"]],
                          numbers.get(hypothesis["id"]))
    if None in numbers or runs[None]:
        lines += _section(NO_HYPOTHESIS, None, runs[None], numbers.get(None))
    return "\n".join(lines).rstrip() + "\n"
