"""Offline tests for the pure helpers (no Telegram network, no credentials)."""

import json
from datetime import datetime, timezone

import pandas as pd
import pytest

from scraper.analysis import (
    _TME_BASE_RE, _TME_RE, _count_comments, _sibling_reactors, combine,
    explode_comments, filter_keywords, links, participants, sample,
)
from scraper.datafiles import clean_xml_text, format_duration, read_table, save_table
from scraper.scrape import _channel_ref, _progress_bar, channel_slug, parse_date


def test_clean_xml_text_handles_none_and_control_chars():
    assert clean_xml_text(None) == ""
    assert clean_xml_text("a\x00b\x07c") == "abc"
    assert clean_xml_text("normal текст 😀") == "normal текст 😀"


def test_format_duration():
    assert format_duration(90061) == "01:01:01:01"


def test_progress_bar():
    assert _progress_bar(0) == "░" * 20
    assert _progress_bar(1) == "█" * 20
    assert _progress_bar(0.5).count("█") == 10
    assert _progress_bar(-1) == "░" * 20        # clamps below 0
    assert _progress_bar(2) == "█" * 20         # clamps above 1
    assert len(_progress_bar(0.37)) == 20


@pytest.mark.parametrize("fmt", ["parquet", "xlsx", "csv"])
def test_save_read_roundtrip(tmp_path, fmt):
    df = pd.DataFrame({"Group": ["@a", "@b"], "Content": ["hi", "yo"]})
    path = save_table(df, tmp_path / "out", fmt)
    back = read_table(path)
    assert list(back["Content"]) == ["hi", "yo"]


def test_parse_date_end_of_day_is_utc():
    d = parse_date("2025-01-15", end_of_day=True)
    assert (d.hour, d.minute, d.second) == (23, 59, 59)
    assert d.tzinfo is not None


def test_parse_date_accepts_dotted_and_iso():
    dotted = parse_date("10.07.2015")
    assert (dotted.year, dotted.month, dotted.day) == (2015, 7, 10)
    assert dotted == parse_date("2015-07-10")
    assert parse_date("10.07.2015", end_of_day=True).hour == 23


def test_parse_date_rejects_garbage():
    with pytest.raises(SystemExit):
        parse_date("not-a-date")


def test_count_comments_from_json_string():
    payload = '[{"Type": "comment"}, {"Type": "comment"}, {"Type": "text"}]'
    assert _count_comments(payload) == 2
    assert _count_comments(None) == 0


def test_combine_tolerates_truncated_comments_list(tmp_path):
    # an Excel-truncated cell (32k limit) survives a later `read --to parquet`
    pd.DataFrame({"Group": ["@a", "@a"], "Message ID": [1, 2], "Date": ["2024-01-01", "2024-01-02"],
                  "Comments List": ['[{"Type": "comment"}]', '[{"Type": "comm']}
                 ).to_parquet(tmp_path / "p.parquet")
    out = tmp_path / "u.parquet"
    combine(str(tmp_path / "p.parquet"), str(out), ["Group", "Message ID"])
    assert pd.read_parquet(out).set_index("Message ID")["Comments"].to_dict() == {"1": 1, "2": 0}


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("@durov", "durov"),
        ("durov", "durov"),
        ("https://t.me/durov", "durov"),
        ("http://t.me/durov/", "durov"),
        ("t.me/durov/123?comment=1", "durov"),
        ("https://t.me/+AbCdEf", "+AbCdEf"),
        ("  https://telegram.me/durov  ", "durov"),
        ("https://www.t.me/durov", "durov"),
        ("https://t.me/s/durov", "durov"),               # web preview
        ("https://t.me/joinchat/AbC", "+AbC"),           # legacy invite = t.me/+AbC
    ],
)
def test_channel_slug(raw, expected):
    assert channel_slug(raw) == expected


@pytest.mark.parametrize(
    "raw,arg,slug,url_base",
    [
        ("@durov", "@durov", "durov", "https://t.me/durov"),
        ("https://t.me/durov/9", "@durov", "durov", "https://t.me/durov"),  # Telethon can't parse a post link
        ("https://t.me/durov?x=1", "@durov", "durov", "https://t.me/durov"),
        ("www.t.me/durov", "@durov", "durov", "https://t.me/durov"),
        ("HTTPS://WWW.T.me/Durov", "@Durov", "Durov", "https://t.me/Durov"),  # only the prefix ignores case
        ("Telegram.Me/durov", "@durov", "durov", "https://t.me/durov"),
        ("T.ME/c/1629147115/5", -1001629147115, "c1629147115", "https://t.me/c/1629147115"),
        ("https://T.me/+AbC", "https://t.me/+AbC", "+AbC", "https://t.me/+AbC"),
        ("-1001629147115", -1001629147115, "c1629147115", "https://t.me/c/1629147115"),
        ("1629147115", -1001629147115, "c1629147115", "https://t.me/c/1629147115"),
        ("https://t.me/c/1629147115/5", -1001629147115, "c1629147115", "https://t.me/c/1629147115"),
        ("t.me/c/1629147115", -1001629147115, "c1629147115", "https://t.me/c/1629147115"),
        ("telegram.me/c/1629147115/5", -1001629147115, "c1629147115", "https://t.me/c/1629147115"),
        ("https://t.me/s/durov", "@durov", "durov", "https://t.me/durov"),
        ("https://t.me/joinchat/AbC", "https://t.me/+AbC", "+AbC", "https://t.me/+AbC"),
        ("+AbCd", "https://t.me/+AbCd", "+AbCd", "https://t.me/+AbCd"),
        ("@+AbCd", "https://t.me/+AbCd", "+AbCd", "https://t.me/+AbCd"),  # menu guess from Group
    ],
)
def test_channel_ref(raw, arg, slug, url_base):
    ref = _channel_ref(raw)
    assert (ref.arg, ref.slug, ref.url_base) == (arg, slug, url_base)


def test_save_table_keeps_dotted_name(tmp_path):
    df = pd.DataFrame({"a": [1]})
    out = save_table(df, tmp_path / "my.data.2024", "parquet")
    assert out.name == "my.data.2024.parquet"
    assert read_table(out)["a"].tolist() == [1]


def test_save_table_excel_takes_aware_dates(tmp_path):
    # e.g. `verify --output` dates, converted with `read --to excel`
    df = pd.DataFrame({"Date": [datetime(2024, 1, 1, 10, tzinfo=timezone.utc)]})
    out = save_table(df, tmp_path / "missed", "excel")
    assert read_table(out)["Date"].tolist() == [pd.Timestamp("2024-01-01 10:00")]  # naive UTC


def test_save_table_warns_when_excel_would_truncate(tmp_path, capsys):
    save_table(pd.DataFrame({"a": ["short"]}), tmp_path / "ok", "excel")
    assert "WARNING" not in capsys.readouterr().out

    df = pd.DataFrame({"Comments List": ["x" * 40_000]})  # str dtype on pandas 3, object on 2
    with pytest.warns(UserWarning):  # openpyxl truncates the long cell to 32767 chars
        save_table(df, tmp_path / "long", "excel")
    assert "Excel truncates at 32767" in capsys.readouterr().out


def test_combine_errors_on_empty_inputs(tmp_path):
    pd.DataFrame().to_parquet(tmp_path / "empty.parquet")
    with pytest.raises(SystemExit):
        combine(str(tmp_path / "*.parquet"), str(tmp_path / "out.parquet"), ["Group", "Message ID"])


def test_combine_folder_skips_non_post_files(tmp_path, capsys):
    # scrape and verify leave these next to the posts in output/
    pd.DataFrame([{"Group": "@a", "Message ID": "5", "Date": "2024-01-05 00:00:00",
                   "Comments List": "[]"}]).to_parquet(tmp_path / "T_posts.parquet")
    pd.DataFrame([{"Group": "@a", "Message ID": 77, "Reactor ID": 8,
                   "Date": "2024-01-05 01:00:00"}]).to_parquet(tmp_path / "T_reactors.parquet")
    pd.DataFrame([{"ID": 8, "Total": 1}]).to_parquet(tmp_path / "T_participants.parquet")
    pd.DataFrame([{"Message ID": 6, "Date": datetime(2024, 1, 6, tzinfo=timezone.utc)}]
                 ).to_parquet(tmp_path / "T_missed.parquet")
    out = tmp_path / "out" / "u.parquet"
    combine(str(tmp_path), str(out), ["Group", "Message ID"])
    assert read_table(out)["Message ID"].tolist() == ["5"]
    assert capsys.readouterr().out.count("not a posts file") == 3


def test_combine_only_non_post_files_says_so(tmp_path):
    pd.DataFrame([{"ID": 8, "Total": 1}]).to_parquet(tmp_path / "T_participants.parquet")
    with pytest.raises(SystemExit, match="Не найдено непустых файлов постов"):
        combine(str(tmp_path / "T_participants.parquet"), str(tmp_path / "u.parquet"),
                ["Group", "Message ID"])


def _posts_with_comments(tmp_path, name="posts.parquet"):
    comment = {
        "Type": "comment", "Comment Author ID": 5, "Comment Author Username": "bob",
        "Comment Author Access Hash": -8712345678901234567,
        "Comment Author Name": "Bob B", "Comment Content": "hi",
        "Comment Date": "2025-01-01 00:00:00", "Comment Message ID": 100,
        "Comment Author": None, "Comment Views": None, "Comment Reactions": "",
        "Comment Shares": 0, "Comment Media": False,
        "Comment Url": "https://t.me/c/1/10?comment=100",
    }
    anon = {"Type": "comment", "Comment Author ID": None,
            "Comment Author Username": "[anonymous]", "Comment Author Name": ""}
    df = pd.DataFrame({
        "Group": ["@c1", "@c1"],
        "Message ID": [10, 11],
        "Url": ["https://t.me/c/1/10", "https://t.me/c/1/11"],
        "Comments List": [json.dumps([comment, anon]), "[]"],
    })
    src = tmp_path / name
    df.to_parquet(src)
    return src


def test_explode_comments(tmp_path):
    out = tmp_path / "comments.parquet"
    explode_comments(str(_posts_with_comments(tmp_path)), str(out))
    r = read_table(out)
    assert len(r) == 2
    assert "Comment Author Name" in r.columns
    row = r[r["Comment Author ID"] == 5].iloc[0]
    assert row["Comment Author Username"] == "bob"
    assert row["Comment Author Name"] == "Bob B"
    assert row["Comment Author Access Hash"] == -8712345678901234567  # exact int64
    assert pd.isna(r[r["Comment Author ID"].isna()].iloc[0]["Comment Author Access Hash"])
    assert row["Post ID"] == 10
    assert row["Post Url"] == "https://t.me/c/1/10"


def test_excel_keeps_access_hash_exact(tmp_path):
    from openpyxl import load_workbook

    df = pd.DataFrame({"ID": [1, 2],
                       "Access Hash": pd.array([-8712345678901234567, None], dtype="Int64")})
    out = save_table(df, tmp_path / "people", "excel")
    assert load_workbook(out).active["B2"].value == "-8712345678901234567"  # text, not rounded
    back = read_table(out)
    assert str(back["Access Hash"].dtype) == "Int64"
    assert back["Access Hash"][0] == -8712345678901234567
    assert pd.isna(back["Access Hash"][1])


def test_csv_keeps_access_hash_exact(tmp_path):
    df = pd.DataFrame({"ID": [1, 2],
                       "Access Hash": pd.array([-8712345678901234567, None], dtype="Int64")})
    back = read_table(save_table(df, tmp_path / "people", "csv"))
    assert str(back["Access Hash"].dtype) == "Int64"
    assert back["Access Hash"][0] == -8712345678901234567  # not rounded via float64
    assert pd.isna(back["Access Hash"][1])


def test_explode_comments_too_many_for_excel_goes_to_parquet(tmp_path, monkeypatch, capsys):
    from scraper import analysis

    monkeypatch.setattr(analysis, "_EXCEL_MAX_ROWS", 1)  # the fixture has 2 comments
    path = explode_comments(str(_posts_with_comments(tmp_path)), str(tmp_path / "comments"), "excel")
    assert path.suffix == ".parquet" and len(read_table(path)) == 2
    assert "exceed Excel" in capsys.readouterr().out


def test_explode_comments_too_many_for_an_xlsx_path_goes_to_parquet(tmp_path, monkeypatch):
    from scraper import analysis

    monkeypatch.setattr(analysis, "_EXCEL_MAX_ROWS", 1)
    path = explode_comments(str(_posts_with_comments(tmp_path)), str(tmp_path / "comments.xlsx"))
    assert path == tmp_path / "comments.parquet" and len(read_table(path)) == 2


def test_explode_comments_errors_when_empty(tmp_path):
    pd.DataFrame({"Group": ["@c1"], "Message ID": [10], "Comments List": ["[]"]}).to_parquet(
        tmp_path / "posts.parquet"
    )
    with pytest.raises(SystemExit):
        explode_comments(str(tmp_path / "posts.parquet"), str(tmp_path / "out.parquet"))


def test_participants_merges_commenters_and_reactors(tmp_path):
    src = _posts_with_comments(tmp_path, "x_posts.parquet")
    pd.DataFrame({
        "Reactor ID": [5, 5, 9, 12, -1001490082514],
        "Reactor Username": ["", "", "ann", "nohash", "[channel]"],
        "Reactor Access Hash": pd.array([None, None, 42, None, None], dtype="Int64"),
        "Reactor Name": ["Bob B", "Bob B", "Ann A", "No Hash", "Some Chan"],
        "Reaction": ["👍", "🔥", "👍", "👍", "❤"],
    }).to_parquet(tmp_path / "x_reactors.parquet")
    (tmp_path / "other_reactors.parquet").write_bytes(b"unrelated")  # must NOT be picked up

    out = tmp_path / "people.parquet"
    participants(str(src), str(out))
    p = read_table(out).set_index("ID")

    # anonymous comment (ID None), channel (negative ID) and user 12 (no access hash) dropped
    assert set(p.index) == {5, 9}
    assert list(read_table(out).columns) == ["ID", "Username", "Access Hash", "Name",
                                             "Comments", "Reactions", "Messages", "Total"]
    assert p.loc[5, "Access Hash"] == -8712345678901234567  # exact int64, from the comment
    assert p.loc[9, "Access Hash"] == 42
    assert (p.loc[5, "Comments"], p.loc[5, "Reactions"], p.loc[5, "Total"]) == (1, 2, 3)
    assert p.loc[5, "Username"] == "bob"          # from the comment
    assert p.loc[5, "Name"] == "Bob B"
    assert (p.loc[9, "Comments"], p.loc[9, "Reactions"]) == (0, 1)
    assert p.loc[9, "Name"] == "Ann A"


def test_sibling_reactors_matches_dated_names(tmp_path):
    span = "_01.05.2022-24.07.2026"
    (tmp_path / f"Baza_reactors{span}.parquet").write_bytes(b"x")
    posts = tmp_path / f"Baza_posts{span}.parquet"
    posts.write_bytes(b"x")
    assert _sibling_reactors(str(posts)) == [tmp_path / f"Baza_reactors{span}.parquet"]

    # undated (pre-feature) layout still resolves
    (tmp_path / "Old_reactors.parquet").write_bytes(b"x")
    assert _sibling_reactors(str(tmp_path / "Old_posts.parquet")) == [tmp_path / "Old_reactors.parquet"]


def test_participants_errors_when_nobody(tmp_path):
    pd.DataFrame({"Comments List": ["[]"]}).to_parquet(tmp_path / "posts.parquet")
    with pytest.raises(SystemExit):
        participants(str(tmp_path / "posts.parquet"), str(tmp_path / "out.parquet"))


def test_participants_leaves_out_the_scraping_account(tmp_path):
    src = _posts_with_comments(tmp_path, "x_posts.parquet")
    pd.DataFrame({"Reactor ID": [9], "Reactor Username": ["ann"],
                  "Reactor Access Hash": pd.array([42], dtype="Int64")}).to_parquet(tmp_path / "x_reactors.parquet")
    out = participants(str(src), str(tmp_path / "people"), owner_id=5)  # the owner commented itself
    p = read_table(out)
    assert list(p["ID"]) == [9] and list(p["Owner ID"]) == [5]


def test_participants_writes_no_empty_base(tmp_path):
    # the only commenter is the scraping account: nobody is left to mail
    with pytest.raises(SystemExit, match="access hash"):
        participants(str(_posts_with_comments(tmp_path)), str(tmp_path / "people"), owner_id=5)
    assert not list(tmp_path.glob("people*"))


def test_combine_custom_dedup_cols_without_message_id(tmp_path):
    pd.DataFrame(
        {"Url": ["a", "b", "a"], "Date": ["2024-01-01", "2024-01-02", "2024-01-01"],
         "Comments List": [None, None, None]}
    ).to_parquet(tmp_path / "f.parquet")
    out = tmp_path / "out.parquet"
    combine(str(tmp_path / "*.parquet"), str(out), ["Url"])
    assert len(read_table(out)) == 2


def test_tme_link_extraction_and_normalisation():
    text = "join https://t.me/foo/123 and https://t.me/bar?x=1"
    links = _TME_RE.findall(text)
    assert len(links) == 2
    assert _TME_BASE_RE.match(links[0]).group(1) == "foo"


def test_links_keep_channel_for_web_private_and_invite_links(tmp_path):
    text = ("https://t.me/s/durov https://t.me/durov/5 https://t.me/boost/durov "
            "https://t.me/c/123/5 https://t.me/joinchat/AbC https://t.me/addlist/XyZ "
            "https://t.me/foo/123 https://t.me/share/url?url=x https://t.me/addstickers/Pack "
            "https://t.me/iv?url=x https://t.me/boost t.me/foo abct.me/nope "
            "t.me/DUROV https://t.me/+AbCd https://t.me/+abcd https://t.me/+AbC")
    pd.DataFrame({"Content": [text]}).to_parquet(tmp_path / "in.parquet")
    links(str(tmp_path / "in.parquet"), str(tmp_path / "l"))
    counts = dict(read_table(tmp_path / "l.xlsx").values)
    assert counts == {"https://t.me/durov": 4, "https://t.me/+AbCd": 1, "https://t.me/+abcd": 1, "https://t.me/c/123": 1,
                      "https://t.me/+AbC": 2,  # joinchat/AbC is the same invite
                      "https://t.me/addlist/XyZ": 1,
                      "https://t.me/foo": 2}


def test_links_keep_dashes_in_invite_hashes(tmp_path):
    # invite hashes are base64url: '-' is part of the hash, not the end of the link
    text = ("https://t.me/+Zb-8yXhQ3WJkNWU6 https://t.me/+Zb-other "
            "t.me/joinchat/AAAAAE-abc_d t.me/addlist/ab-CD")
    pd.DataFrame({"Content": [text]}).to_parquet(tmp_path / "in.parquet")
    links(str(tmp_path / "in.parquet"), str(tmp_path / "l"))
    counts = dict(read_table(tmp_path / "l.xlsx").values)
    assert counts == {"https://t.me/+Zb-8yXhQ3WJkNWU6": 1, "https://t.me/+Zb-other": 1,
                      "https://t.me/+AAAAAE-abc_d": 1, "https://t.me/addlist/ab-CD": 1}


def test_links_count_telegram_me_and_www_aliases(tmp_path):
    text = ("telegram.me/Foo https://www.t.me/foo t.me/foo Telegram.me/foo HTTPS://T.ME/foo "
            "https://telegram.dog/joinchat/AbC T.me/+XyZ sometelegram.me/x")
    pd.DataFrame({"Content": [text]}).to_parquet(tmp_path / "in.parquet")
    links(str(tmp_path / "in.parquet"), str(tmp_path / "l"))
    counts = dict(read_table(tmp_path / "l.xlsx").values)
    # the domain ignores case, the invite hash keeps it
    assert counts == {"https://t.me/foo": 5, "https://t.me/+AbC": 1, "https://t.me/+XyZ": 1}


@pytest.mark.parametrize("n_matches, files", [(3, ["f_unique.xlsx"]),
                                              (4, ["f_part_1.xlsx", "f_part_2.xlsx"])])
def test_filter_file_split(tmp_path, n_matches, files):
    pd.DataFrame({"Content": ["foo"] * n_matches}).to_parquet(tmp_path / "in.parquet")
    filter_keywords(str(tmp_path / "in.parquet"), str(tmp_path / "f"), "Content", ["foo"], 3)
    assert sorted(p.name for p in tmp_path.glob("f_*.xlsx")) == files


def test_filter_repeated_keyword_counts_once(tmp_path):
    pd.DataFrame({"Content": ["foo bar"]}).to_parquet(tmp_path / "in.parquet")
    filter_keywords(str(tmp_path / "in.parquet"), str(tmp_path / "f"), "Content", ["foo", "foo"], 10)
    out = pd.read_excel(tmp_path / "f_unique.xlsx")
    assert list(out.columns) == ["Content", "foo", "Keyword_Count"]
    assert out["Keyword_Count"].tolist() == [1]


def test_filter_keeps_dotted_output_name(tmp_path):
    pd.DataFrame({"Content": ["foo"]}).to_parquet(tmp_path / "in.parquet")
    filter_keywords(str(tmp_path / "in.parquet"), str(tmp_path / "kw_01.01.2024"), "Content", ["foo"], 10)
    assert (tmp_path / "kw_01.01.2024_unique.xlsx").exists()


def test_filter_drops_data_extension_from_output(tmp_path):
    pd.DataFrame({"Content": ["foo"]}).to_parquet(tmp_path / "in.parquet")
    filter_keywords(str(tmp_path / "in.parquet"), str(tmp_path / "kw.parquet"), "Content", ["foo"], 10)
    assert (tmp_path / "kw_unique.xlsx").exists()


def test_save_table_excel_strips_control_chars(tmp_path):
    out = save_table(pd.DataFrame({"Comment Content": ["a\x0bb", None]}), tmp_path / "x", "excel")
    assert read_table(out)["Comment Content"].tolist()[0] == "ab"


def test_save_table_creates_parent_dir(tmp_path):
    path = save_table(pd.DataFrame({"a": [1]}), tmp_path / "new" / "x", "parquet")
    assert path == tmp_path / "new" / "x.parquet" and path.exists()


def test_parse_date_converts_offset_to_utc():
    assert parse_date("2024-01-01T03:00:00+03:00") == parse_date("2024-01-01")


def test_read_table_rejects_xls(tmp_path):
    with pytest.raises(ValueError, match="Неподдерживаемый тип файла"):
        read_table(tmp_path / "old.xls")


def test_sample_larger_than_data_returns_all_rows(tmp_path, capsys):
    # URL-only texts pass the length filter but are empty once URLs are stripped
    texts = ([f"long enough text number {i} here" for i in range(46)]
             + [f"https://example.com/some/long/url/{i}" for i in range(4)])
    pd.DataFrame({"id": range(50), "Content": texts, "Group": ["@a"] * 30 + ["@b"] * 20}
                 ).to_parquet(tmp_path / "in.parquet")
    sample(str(tmp_path / "in.parquet"), str(tmp_path / "s"), "Content", "Group", 10_000, 20)
    out = read_table(tmp_path / "s.xlsx")
    assert sorted(out["id"]) == list(range(50))  # every source row exactly once
    assert f"Saved: {tmp_path / 's.xlsx'} (50 rows)" in capsys.readouterr().out


def test_sample_nothing_left_after_min_length(tmp_path):
    pd.DataFrame({"Content": ["short"], "Group": ["@a"]}).to_parquet(tmp_path / "in.parquet")
    with pytest.raises(SystemExit, match="длиннее"):
        sample(str(tmp_path / "in.parquet"), str(tmp_path / "s"), "Content", "Group", 10, 20)


def test_participants_skips_excel_truncated_comments_list(tmp_path, capsys):
    long_thread = json.dumps([{"Type": "comment", "Comment Author ID": i,
                               "Comment Content": "Привет мир " * 30} for i in range(120)])
    ok_thread = json.dumps([{"Type": "comment", "Comment Author ID": 5,
                             "Comment Author Username": "bob", "Comment Author Access Hash": 1}])
    df = pd.DataFrame({"Group": ["@a", "@a"], "Message ID": [1, 2],
                       "Comments List": [long_thread, ok_thread]})
    with pytest.warns(UserWarning):  # openpyxl truncates the long cell to 32767 chars
        src = save_table(df, tmp_path / "x_posts", "excel")

    participants(str(src), str(tmp_path / "people.parquet"), reactors="")
    assert list(read_table(tmp_path / "people.parquet")["ID"]) == [5]
    assert "@a/1: Comments List is not valid JSON" in capsys.readouterr().out


def test_sample_and_filter_keep_excel_truncated_comments_list(tmp_path):
    long_thread = json.dumps([{"Type": "comment", "Comment Content": "Привет мир " * 30}] * 120)
    df = pd.DataFrame({"Group": ["@a"], "Content": ["some long enough content here"],
                       "Comments List": [long_thread]})
    with pytest.warns(UserWarning):  # openpyxl truncates the long cell to 32767 chars
        src = str(save_table(df, tmp_path / "x_posts", "excel"))

    sample(src, str(tmp_path / "s"), "Content", "Group", 10, 5)
    filter_keywords(src, str(tmp_path / "f"), "Content", ["some"], 100)
    assert len(read_table(tmp_path / "s.xlsx")) == len(read_table(tmp_path / "f_unique.xlsx")) == 1


def test_filter_rejects_keyword_matching_a_column(tmp_path):
    pd.DataFrame({"Content": ["about Media"], "Media": [True]}).to_parquet(tmp_path / "in.parquet")
    with pytest.raises(SystemExit, match="Media"):
        filter_keywords(str(tmp_path / "in.parquet"), str(tmp_path / "f"), "Content", ["Media"], 10)


def test_resolve_inputs_rejects_folder_without_parquet(tmp_path):
    from scraper.datafiles import resolve_inputs

    (tmp_path / "x.xlsx").write_bytes(b"")
    with pytest.raises(SystemExit, match="Нет .parquet-файлов"):
        resolve_inputs(str(tmp_path))


def test_sample_keeps_rows_with_empty_category(tmp_path):
    texts = [f"long enough text number {i} here" for i in range(6)]
    pd.DataFrame({"id": range(6), "Content": texts, "Group": ["@a", None, "@b", None, "@a", "@b"]}
                 ).to_parquet(tmp_path / "in.parquet")
    sample(str(tmp_path / "in.parquet"), str(tmp_path / "s"), "Content", "Group", 10_000, 20)
    assert sorted(read_table(tmp_path / "s.xlsx")["id"]) == list(range(6))


@pytest.mark.parametrize("value, expected", [
    ("2024-01-31T05", datetime(2024, 1, 31, 5, tzinfo=timezone.utc)),     # explicit hour, no colon
    ("20240131T0500", datetime(2024, 1, 31, 5, tzinfo=timezone.utc)),     # basic ISO time
    ("20240131", datetime(2024, 1, 31, 23, 59, 59, tzinfo=timezone.utc)),  # basic ISO date only
])
def test_parse_date_end_of_day_only_for_date_only_input(value, expected):
    assert parse_date(value, end_of_day=True) == expected


def test_filter_keeps_comments_list_as_readable_json(tmp_path):
    # an older file: Cyrillic escaped as \uXXXX by json.dumps' default ensure_ascii
    comments = json.dumps([{"Type": "comment", "Comment Author ID": 5,
                            "Comment Author Access Hash": 1, "Comment Content": "привет"}])
    pd.DataFrame({"Group": ["@a"], "Message ID": ["1"], "Content": ["foo"],
                  "Comments List": [comments]}).to_parquet(tmp_path / "in.parquet")
    filter_keywords(str(tmp_path / "in.parquet"), str(tmp_path / "f"), "Content", ["foo"], 10)

    cell = pd.read_excel(tmp_path / "f_unique.xlsx")["Comments List"][0]
    assert "привет" in cell and json.loads(cell)[0]["Comment Content"] == "привет"
    participants(str(tmp_path / "f_unique.xlsx"), str(tmp_path / "p.parquet"), reactors="")
    assert list(read_table(tmp_path / "p.parquet")["ID"]) == [5]  # usable by the next command


def test_links_and_filter_handle_empty_content_after_xlsx(tmp_path):
    # a media-only post has Content "" -> the xlsx cell reads back as NaN
    df = pd.DataFrame({"Group": ["@a", "@a"], "Message ID": ["2", "1"],
                       "Content": ["see t.me/foo", ""]})
    src = str(save_table(df, tmp_path / "posts", "excel"))

    links(src, str(tmp_path / "l"))
    assert pd.read_excel(tmp_path / "l.xlsx").to_dict("records") == [
        {"Telegram Link": "https://t.me/foo", "Frequency": 1}]
    filter_keywords(src, str(tmp_path / "f"), "Content", ["foo"], 10)
    assert len(pd.read_excel(tmp_path / "f_unique.xlsx")) == 1


def test_check_date_range_rejects_a_reversed_range():
    from scraper.scrape import check_date_range

    with pytest.raises(SystemExit, match="позже даты конца"):
        check_date_range(parse_date("02.01.2024"), parse_date("01.01.2024", end_of_day=True),
                         "02.01.2024", "01.01.2024")


def test_check_date_range_allows_a_single_day():
    from scraper.scrape import check_date_range

    lo, hi = parse_date("01.01.2024"), parse_date("01.01.2024", end_of_day=True)
    check_date_range(lo, hi, "01.01.2024", "01.01.2024")
    assert (lo.hour, hi.hour) == (0, 23)


def test_participants_reads_no_post_text_and_counts_per_person(tmp_path, monkeypatch):
    import json

    from scraper import analysis

    src = tmp_path / "x_posts.parquet"
    comment = {"Type": "comment", "Comment Author ID": 7, "Comment Author Username": "bob",
               "Comment Author Access Hash": 2**60 + 7, "Comment Author Name": "Bob"}
    pd.DataFrame({"Group": ["@g", "@g"], "Message ID": ["1", "2"], "Content": ["long text"] * 2,
                  "Comments List": [json.dumps([comment, comment]), json.dumps([comment])],
                  "Author ID": [7, -100], "Author Username": ["", "[channel]"],
                  "Author Access Hash": pd.array([2**60 + 7, None], dtype="Int64"),
                  "Author Name": ["Bob", "Chan"]}).to_parquet(src)
    read = []
    real = analysis.pq.ParquetFile.iter_batches

    def spy(self, batch_size=65536, columns=None, **kw):
        read.append((batch_size, columns))
        return real(self, batch_size=batch_size, columns=columns, **kw)

    monkeypatch.setattr(analysis.pq.ParquetFile, "iter_batches", spy)

    p = read_table(participants(str(src), str(tmp_path / "people.parquet"), reactors="")).set_index("ID")
    [(batch_size, columns)] = read
    assert "Content" not in columns and batch_size == 10_000  # in batches, never the text
    assert (p.loc[7, "Comments"], p.loc[7, "Messages"], p.loc[7, "Total"]) == (3, 1, 4)
    assert p.loc[7, "Access Hash"] == 2**60 + 7 and p.loc[7, "Username"] == "bob"
    assert list(p.index) == [7]  # the channel's own post is no one


def test_filter_ignores_case(tmp_path):
    """As the scrape's own keyword filter: «крипта» finds a post that starts with «Крипта»."""
    pd.DataFrame({"Content": ["Крипта растёт", "погода", None]}).to_parquet(tmp_path / "in.parquet")
    filter_keywords(str(tmp_path / "in.parquet"), str(tmp_path / "f"), "Content", ["крипта"], 10)
    out = pd.read_excel(tmp_path / "f_unique.xlsx")
    assert out["Content"].tolist() == ["Крипта растёт"]
    assert out["крипта"].tolist() == [1]


def test_filter_repeated_keyword_in_another_case_counts_once(tmp_path):
    """The match ignores case, so «Крипта» and «крипта» are one keyword (the first spelling kept)."""
    pd.DataFrame({"Content": ["крипта растёт"]}).to_parquet(tmp_path / "in.parquet")
    filter_keywords(str(tmp_path / "in.parquet"), str(tmp_path / "f"), "Content", ["Крипта", "крипта"], 10)
    out = pd.read_excel(tmp_path / "f_unique.xlsx")
    assert list(out.columns) == ["Content", "Крипта", "Keyword_Count"]
    assert out["Keyword_Count"].tolist() == [1]
