"""Outreach analytics (modules/outreach.py): hypotheses, runs, sends and replies joined, the .md report."""
import json
from datetime import datetime, timedelta

from modules import outreach

NOW = datetime(2026, 11, 9, 12, 0, 0)


def _at(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).strftime(outreach.TIME_FORMAT)


def _write(path, entries):
    path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries), encoding="utf-8")


def _send(run, user, hours_ago, worker="w1.jsession", username=None):
    return {"run": run, "worker": worker, "user": user, "username": username, "at": _at(hours_ago)}


def _reply(user, hours_ago, text="да", worker="w1.jsession"):
    return {"worker": worker, "user": user, "at": _at(hours_ago), "text": text}


def _project(tmp_path, sends, replies):
    home = str(tmp_path)
    first = outreach.add_hypothesis(home, "Короткий оффер")
    outreach.add_run(home, first, "📚 Вся база", "Привет!", 0, 10)
    outreach.add_run(home, None, "список (3)", "Без гипотезы", 1, 3)
    _write(tmp_path / outreach.SENDS_NAME, sends)
    _write(tmp_path / outreach.DIALOGS_NAME, replies)
    return home


def test_hypotheses_and_runs_are_numbered_and_closed(tmp_path):
    home = str(tmp_path)
    assert outreach.add_hypothesis(home, "A") == 1 and outreach.add_hypothesis(home, "B") == 2
    assert outreach.close_hypothesis(home, 1, "yes", "работает")
    assert not outreach.close_hypothesis(home, 1, "no")  # closed already
    assert [h["text"] for h in outreach.open_hypotheses(home)] == ["B"]
    assert outreach.add_run(home, 2, "s", "t", 0, 5) == 1 and outreach.add_run(home, None, "s", "t", 0, 5) == 2


def test_a_reply_counts_within_the_window_for_the_last_send_before_it_once(tmp_path):
    home = _project(tmp_path, [
        _send(1, 5, 200), _send(1, 6, 200), _send(1, 7, 200), _send(1, 8, 200),
        _send(2, 9, 100),
    ], [
        _reply(5, 190),                  # 10 h after: counts
        _reply(5, 180),                  # the same person again: once
        _reply(6, 100),                  # 100 h after: an answer, but past the window
        _reply(7, 199, worker="other"),  # another worker's chat: not this send's
        _reply(9, 99),                   # the run under no hypothesis
        _reply(42, 1),                   # someone never mailed: no one's
    ])
    stats = outreach.stats(home, NOW)
    assert (stats[1]["sent"], stats[1]["replied"], stats[1]["replied_all"]) == (4, 1, 2)
    assert stats[1]["rate"] == 25.0 and stats[1]["median"] == timedelta(hours=55)
    assert (stats[None]["sent"], stats[None]["replied"]) == (1, 1)
    assert not stats[1]["fresh"]


def test_a_reply_before_any_send_to_that_person_is_not_counted(tmp_path):
    home = _project(tmp_path, [_send(1, 5, 10)], [_reply(5, 20)])
    assert outreach.stats(home, NOW)[1]["replied_all"] == 0


def test_the_rate_counts_only_the_sends_whose_window_is_over(tmp_path):
    # 2 old sends (one answered), 2 of an hour ago (unanswered yet): 1 of 2, not 1 of 4
    home = _project(tmp_path, [_send(1, 5, 100), _send(1, 6, 100), _send(1, 7, 1), _send(1, 8, 1)],
                    [_reply(5, 99)])
    numbers = outreach.stats(home, NOW)[1]
    assert (numbers["sent"], numbers["counted"], numbers["replied"], numbers["rate"]) == (4, 2, 1, 50.0)
    assert outreach.flags(numbers) == "мало данных, 2 отправок моложе 72 ч ещё не в доле"


def test_while_no_send_is_old_enough_the_rate_is_of_all_and_provisional(tmp_path):
    home = _project(tmp_path, [_send(1, 5, 2), _send(1, 6, 1)], [_reply(5, 1)])
    numbers = outreach.stats(home, NOW)[1]
    assert (numbers["counted"], numbers["replied"]) == (2, 1)
    assert outreach.flags(numbers) == "мало данных, ответы ещё приходят — доля предварительная"


def test_the_interval_narrows_with_more_sends():
    low, high = outreach.interval(5, 100)
    assert 2.1 < low < 2.2 and 11.1 < high < 11.4  # Wilson's for 5%
    wide, narrow = outreach.interval(5, 100), outreach.interval(50, 1000)
    assert narrow[1] - narrow[0] < wide[1] - wide[0]
    assert outreach.interval(0, 0) == (0.0, 0.0) and outreach.interval(0, 10)[0] == 0.0


def test_a_line_cut_by_a_crash_is_skipped(tmp_path):
    home = _project(tmp_path, [_send(1, 5, 100)], [])
    with open(tmp_path / outreach.SENDS_NAME, "a", encoding="utf-8") as fileobj:
        fileobj.write('{"run": 1, "wor')
    assert outreach.stats(home, NOW)[1]["sent"] == 1


def test_the_report_has_the_table_each_hypothesis_and_the_answers(tmp_path, monkeypatch):
    monkeypatch.setattr(outreach, "_now", lambda: NOW.strftime(outreach.TIME_FORMAT))  # the runs' dates: NOW's
    home = _project(tmp_path, [_send(1, 5, 100, username="ann"), _send(1, 6, 100)],
                    [_reply(5, 98, text="а сколько | стоит?\nи когда")])
    outreach.close_hypothesis(home, 1, "no", "оффер слишком длинный")
    report = outreach.report_md(home, "Green", earlier=12, workers={"w1.jsession": "@worker1"}, now=NOW)
    assert report.startswith("# Аутрич: Green — отчёт на 09.11.2026 12:00")
    assert "До учёта гипотез рассылка написала 12 людям" in report
    # whom it went to and when, next to its numbers: hypotheses compare only alike
    assert ("| 1 | Короткий оффер | ❌ не подтвердилась | 📚 Вся база | 05.11 | 2 | 1 из 2 | 50.0 | 9.5–90.5 | 1 | "
            "мало данных |") in report
    assert "- 09.11.2026" in report and "сегмент: 📚 Вся база · отправлено 2 из 10 · 05.11" in report
    assert "## Гипотеза #1" in report and "- **Вывод:** оффер слишком длинный" in report
    assert "> Привет!" in report
    assert "## Воркеры (все гипотезы)" in report and "| @worker1 | 2 | 1 из 2 | 50.0 | 9.5–90.5 |" in report
    assert report.count("← пишет человек") == 1  # the legend once, in «Как читать»
    assert "- **@ann** — ответ через 2.0 ч:\n  - ← а сколько \\| стоит? и когда" in report  # one line, no column break
    assert "## Без гипотезы" in report and "_+ медиа: 1_" in report


def test_the_report_caps_the_answers_per_hypothesis(tmp_path):
    count = outreach.MAX_REPORT_REPLIES + 3
    home = _project(tmp_path, [_send(1, i, 100) for i in range(count)], [_reply(i, 99) for i in range(count)])
    report = outreach.report_md(home, "Green", now=NOW)
    assert f"### Диалоги ({count})" in report and "- … и ещё 3" in report


def test_an_empty_project_s_report_says_so(tmp_path):
    report = outreach.report_md(str(tmp_path), "Green", now=NOW)
    assert "## Сводка" in report and "## Гипотеза" not in report and "Без гипотезы" not in report


def test_the_dialog_after_a_reply_goes_both_ways_in_order_to_the_send_replied_to(tmp_path):
    out = {"dir": "out"}
    home = _project(tmp_path, [_send(1, 5, 100, username="ann"), _send(1, 5, 10)], [
        {**_reply(5, 95), "dir": "out", "text": "до ответа — не его"},  # the worker first: no reply
        _reply(5, 90, text="а цена?"),
        {**_reply(5, 90, text="Спасибо! Пишите @manager"), **out, "auto": True},
        {**_reply(5, 89, text="От 100$ в месяц"), **out},
        _reply(5, 5, text="дорого"),  # after the second send: still the same dialog, no second reply
    ])
    sends = outreach.stats(home, NOW)[1]["sends"]
    assert [bool(s["reply"]) for s in sends] == [True, False]
    report = outreach.report_md(home, "Green", now=NOW)
    assert ("- **@ann** — ответ через 10.0 ч:\n  - ← а цена?\n  - → 🤖 Спасибо! Пишите @manager\n"
            "  - → От 100$ в месяц\n  - ← дорого") in report


def test_a_dialog_is_capped_in_the_report(tmp_path):
    many = [_reply(5, 99 - i / 100, text=f"m{i}") for i in range(outreach.MAX_DIALOG_MESSAGES + 2)]
    home = _project(tmp_path, [_send(1, 5, 100)], many)
    assert "  - … и ещё 2 сообщений" in outreach.report_md(home, "Green", now=NOW)


def test_a_hypothesis_is_edited_and_deleted_only_while_no_mailing_ran_under_it(tmp_path):
    home = str(tmp_path)
    used, unused = outreach.add_hypothesis(home, "опечатка"), outreach.add_hypothesis(home, "лишняя")
    outreach.add_run(home, used, "s", "t", 0, 1)
    assert outreach.edit_hypothesis(home, used, "исправлено")
    assert outreach.find_hypothesis(home, used)["text"] == "исправлено"
    assert not outreach.edit_hypothesis(home, 9, "нет такой")
    assert not outreach.delete_hypothesis(home, used)  # its mailing is data: closed instead
    assert outreach.delete_hypothesis(home, unused) and outreach.find_hypothesis(home, unused) is None
    assert not outreach.delete_hypothesis(home, unused)


def test_the_workers_table_counts_every_hypothesis_s_sends(tmp_path):
    home = _project(tmp_path, [_send(1, 5, 100), _send(2, 6, 100), _send(1, 7, 100, worker="w2.jsession")],
                    [_reply(6, 99)])
    report = outreach.report_md(home, "Green", now=NOW)
    assert "| w1.jsession | 2 | 1 из 2 | 50.0 |" in report and "| w2.jsession | 1 | 0 из 1 | 0.0 |" in report
    assert "| — | Без гипотезы | — | список (3) | 05.11 | 1 | 1 из 1 |" in report


def test_a_deleted_hypothesis_s_number_is_never_given_again(tmp_path):
    # a card or a mailing step still showing #2 must not act on another hypothesis; a report's #2 stays one
    home = str(tmp_path)
    outreach.add_hypothesis(home, "первая")
    outreach.add_hypothesis(home, "удалят")
    outreach.delete_hypothesis(home, 2)
    assert outreach.add_hypothesis(home, "новая") == 3
