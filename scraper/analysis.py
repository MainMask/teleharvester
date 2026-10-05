"""Post-processing tools for scraped data (terminal ports of the original helper scripts)."""

import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from tqdm import tqdm

from scraper.datafiles import _EXCEL_MAX_ROWS, read_table, resolve_inputs, save_table

_URL_RE = re.compile(r"http\S+|www\S+")
# the scheme is often omitted; telegram.me (the pre-2017 domain) and telegram.dog are t.me aliases.
# Only this prefix ignores case (Telegram.me/…): invite hashes after it are case-sensitive
_TME_PREFIX = r"(?i:(?:https?://)?(?:www\.)?(?:t\.me|telegram\.(?:me|dog)))"
_TME_RE = re.compile(rf"(?<![\w.])({_TME_PREFIX}/[^\s]+)")
# t.me/s/<name> (web preview) and boost/<name> point at <name>; c/<id>, joinchat/<hash>
# (counted as +<hash>) and addlist/<hash> keep their key part
_TME_BASE_RE = re.compile(rf"{_TME_PREFIX}/(?:s/|boost/)?(c/\d+|joinchat/[\w-]+|addlist/[\w-]+|\+[\w-]+|\w+)")
# t.me paths that are Telegram features, not channels or groups
_TME_SERVICE = {"share", "addstickers", "addemoji", "addtheme", "iv", "proxy", "socks",
                "setlanguage", "boost"}


def _require_columns(df: pd.DataFrame, columns, source: str) -> None:
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise SystemExit(
            f"{source}: missing column(s) {missing}. Available: {list(df.columns)}"
        )


def _count_comments(comments_list) -> int:
    if pd.isna(comments_list):
        return 0
    if isinstance(comments_list, str):
        try:
            comments_list = json.loads(comments_list)
        except json.JSONDecodeError:  # truncated by Excel's 32k limit, as in _comment_pairs
            return 0
    return sum(1 for item in comments_list if item.get("Type") == "comment")


def _parse_comments_list(x):
    """Comments List re-dumped as JSON without \\uXXXX escapes, so the xlsx is readable
    and still valid for `comments` / `participants`; a cell that isn't valid JSON
    (e.g. truncated by Excel's 32k limit) is kept as the raw string."""
    if pd.isnull(x):
        return x
    try:
        return json.dumps(json.loads(x), ensure_ascii=False)
    except json.JSONDecodeError:
        return x


def normalize_keys(df: pd.DataFrame) -> pd.DataFrame:
    """The dedup keys in one form: Message ID as str, Group with its "@"."""
    if "Message ID" in df.columns:
        df["Message ID"] = df["Message ID"].astype(str)
    if "Group" in df.columns:
        df["Group"] = df["Group"].apply(lambda x: x if str(x).startswith("@") else "@" + str(x))
    return df


def normalize_values(df: pd.DataFrame, progress: bool = False) -> pd.DataFrame:
    """Add the Comments count and coerce dtypes; row by row, so a scrape's posts can be
    normalized chunk by chunk (see scraper.scrape._write_posts)."""
    if "Comments List" in df.columns:
        values = df["Comments List"]
        df["Comments"] = [_count_comments(v)
                          for v in (tqdm(values, desc="Counting comments") if progress else values)]
    else:
        df["Comments"] = 0
    df["Comments"] = df["Comments"].astype(int)
    if "Media" in df.columns:
        df["Media"] = df["Media"].astype(bool)
    df["Date"] = pd.to_datetime(df["Date"], format="ISO8601")
    return df


def normalize_posts(df: pd.DataFrame, dedup_cols=("Group", "Message ID"),
                    source: str = "posts") -> pd.DataFrame:
    """Drop duplicates, add the Comments count, coerce dtypes and sort newest-first."""
    dedup_cols = list(dedup_cols)
    _require_columns(df, dedup_cols + ["Date"], source)

    df = normalize_keys(df)
    dups = int(df.duplicated(subset=dedup_cols).sum())
    print(f"Normalize: {len(df)} rows, {dups} duplicate(s) on {dedup_cols} dropped")
    df = normalize_values(df.drop_duplicates(subset=dedup_cols), progress=True)
    return df.sort_values(by="Date", ascending=False, ignore_index=True)


def combine(inputs: str | list[str], output: str, dedup_cols: list[str]) -> Path:
    """Concatenate parquet files, drop duplicates, recompute the Comments count.
    inputs: a file / folder / glob, or a list of files."""
    paths = resolve_inputs(inputs) if isinstance(inputs, str) else [Path(p) for p in inputs]
    frames = []
    for p in tqdm(paths, desc="Reading files"):
        df = pd.read_parquet(p)
        if df.empty or df.isna().all().all():
            continue
        # a folder input also holds the run's _reactors / _participants / verify files
        if "Reactor ID" in df.columns or not {*dedup_cols, "Date"} <= set(df.columns):
            print(f"  - skipped {p.name}: not a posts file")
            continue
        frames.append(df)
    if not frames:
        raise SystemExit(f"No non-empty posts files found in: {inputs}")
    combined = normalize_posts(pd.concat(frames, ignore_index=True), dedup_cols, source=inputs)

    n_comments = int(combined["Comments"].sum())
    print(f"Rows: {len(combined)} | comments: {n_comments} | total: {len(combined) + n_comments}")

    path = save_table(combined, output, 'parquet')
    print(f"Saved: {path}")
    return path


def _save(df: pd.DataFrame, output: str, fmt: str) -> Path:
    """Honour an explicit .parquet/.xlsx/.csv suffix on `output`, otherwise use `fmt`."""
    if Path(output).suffix.lower() in (".parquet", ".xlsx", ".csv"):
        fmt = None
    return save_table(df, output, fmt)


def _comment_pairs(df: pd.DataFrame):
    """Yield (post_info, comment_dict) for every comment in the Comments List column.

    post_info carries only the few post fields callers need, so the whole frame is
    never materialised as dicts.
    """
    cols = [c for c in ("Group", "Message ID", "Url", "Comments List") if c in df.columns]
    for values in zip(*(df[c] for c in cols)):
        post = dict(zip(cols, values))
        raw = post.get("Comments List")
        if isinstance(raw, str):
            try:
                items = json.loads(raw) if raw.strip() else []
            except json.JSONDecodeError:
                print(f"  ! {post.get('Group')}/{post.get('Message ID')}: Comments List is not valid "
                      f"JSON (e.g. truncated by Excel's 32k limit) - skipped; use --format parquet")
                items = []
        else:
            items = []
        for c in items:
            if isinstance(c, dict) and c.get("Type") == "comment":
                yield post, c


def explode_comments(input_path: str, output: str, fmt: str = "parquet") -> Path:
    """Flatten the Comments List JSON into a flat table: one row per comment."""
    df = read_table(input_path)
    _require_columns(df, ["Comments List", "Group", "Message ID"], input_path)
    rows = []
    for post, c in tqdm(_comment_pairs(df), desc="Exploding comments"):
        rows.append({
            "Group": post["Group"],
            "Post ID": post["Message ID"],
            "Post Url": post.get("Url", ""),
            "Comment Author ID": c.get("Comment Author ID"),
            "Comment Author Username": c.get("Comment Author Username", ""),
            "Comment Author Access Hash": c.get("Comment Author Access Hash"),
            "Comment Author Name": c.get("Comment Author Name", ""),
            "Comment Content": c.get("Comment Content", ""),
            "Comment Date": c.get("Comment Date", ""),
            "Comment Message ID": c.get("Comment Message ID"),
            "Comment Author": c.get("Comment Author"),
            "Comment Views": c.get("Comment Views"),
            "Comment Reactions": c.get("Comment Reactions", ""),
            "Comment Shares": c.get("Comment Shares"),
            "Comment Media": c.get("Comment Media"),
            "Comment Url": c.get("Comment Url", ""),
        })
    if not rows:
        raise SystemExit(f"{input_path}: no comments in 'Comments List'.")
    out = pd.DataFrame(rows)
    # int64 + None would become float64 and corrupt the hash; rebuild from the raw values
    out["Comment Author Access Hash"] = pd.array(
        [r["Comment Author Access Hash"] for r in rows], dtype="Int64")
    for col in ("Comment Author ID", "Comment Message ID", "Comment Views", "Comment Shares"):
        out[col] = pd.to_numeric(out[col], errors="coerce").astype("Int64")  # keep ints, allow <NA>
    suffix = Path(output).suffix.lower()
    # the format _save picks: the output's own extension wins over fmt
    excel = suffix == ".xlsx" or (suffix not in (".parquet", ".csv") and fmt in ("excel", "xlsx"))
    if excel and len(out) > _EXCEL_MAX_ROWS:  # one sheet can't hold them
        print(f"  ! {len(out)} comments exceed Excel's {_EXCEL_MAX_ROWS} rows - saved as parquet")
        output = str(Path(output).with_suffix("")) if suffix == ".xlsx" else output
        fmt = "parquet"
    path = _save(out, output, fmt)
    print(f"Saved: {path} ({len(rows)} comments)")
    return path


_NON_USER = {"[channel]", "[anonymous]"}


def _as_int(value) -> int | None:
    """An ID or access hash as int; None when missing or not a number."""
    try:
        return None if pd.isna(value) else int(value)
    except (TypeError, ValueError):
        return None


def _sibling_reactors(input_path: str) -> list[Path]:
    """The <name>_reactors file next to a <name>_posts input, if it exists."""
    p = Path(input_path)
    m = re.search(r"^(?P<base>.*)_posts(?P<rng>_\d{2}\.\d{2}\.\d{4}-\d{2}\.\d{2}\.\d{4})?$", p.stem)
    base, rng = (m["base"], m["rng"] or "") if m else (p.stem, "")
    for ext in (".parquet", ".xlsx"):
        cand = p.with_name(f"{base}_reactors{rng}{ext}")
        if cand.exists():
            return [cand]
    return []


def participants(input_path: str, output: str, reactors: str | None = None,
                 fmt: str = "parquet", owner_id: int | None = None) -> Path:
    """One row per unique person who wrote a message (a group's posts), commented or
    reacted: ID, username, access hash, name, counts. People with no access hash are skipped.

    owner_id: the account that scraped — the access hashes are valid for it only; written
    as an "Owner ID" column so a mailing can tell which worker may use them. None takes it
    from the posts file (the scrape stores it there).

    reactors: a path to the reactors file; None auto-discovers the sibling
    <name>_reactors next to input_path; "" means "this run has no reactors file"
    (skip the sibling lookup).
    """
    # a giant scrape's posts file is read in batches, and never its posts' text
    columns = ["Group", "Message ID", "Url", "Comments List", "Author ID", "Author Username",
               "Author Access Hash", "Author Name"]
    if Path(input_path).suffix.lower() == ".parquet":
        posts = pq.ParquetFile(input_path)
        _require_columns(pd.DataFrame(columns=posts.schema_arrow.names), ["Comments List"], input_path)
        present = [c for c in columns if c in posts.schema_arrow.names]
        file_attrs = json.loads((posts.schema_arrow.metadata or {}).get(b"PANDAS_ATTRS", b"{}"))
        # nullable Int64, not float64: a 64-bit access hash must not be rounded
        batches = (batch.to_pandas(types_mapper={pa.int64(): pd.Int64Dtype()}.get)
                   for batch in posts.iter_batches(batch_size=10_000, columns=present))
    else:
        df = read_table(input_path)
        _require_columns(df, ["Comments List"], input_path)
        file_attrs, batches = df.attrs, [df]
    if owner_id is None:
        owner_id = file_attrs.get("owner_id")
    if owner_id is None:
        print(f"  ! no Owner ID in {input_path}: the mailing will reach people without a "
              "username only from the account that scraped")

    # one entry per person, filled as the events stream by: memory follows the number of
    # people, not of comments/reactions/messages (millions on a giant scrape)
    people: dict[int, list] = {}  # ID -> [username, access hash, name, comments, reactions, messages]

    def add(pid, username, access_hash, name, column: int):
        pid = _as_int(pid)
        if pid is None or pid <= 0:  # anonymous, or a channel/chat entity (negative ID)
            return
        if pid == owner_id:  # the scraping account itself: no one to mail
            return
        person = people.setdefault(pid, ["", None, "", 0, 0, 0])
        if not person[0] and isinstance(username, str) and username not in _NON_USER:
            person[0] = username
        if person[1] is None:
            person[1] = _as_int(access_hash)
        if not person[2] and isinstance(name, str):
            person[2] = name
        person[column] += 1

    for df in batches:
        for _post, c in _comment_pairs(df):
            add(c.get("Comment Author ID"), c.get("Comment Author Username"),
                c.get("Comment Author Access Hash"), c.get("Comment Author Name"), 3)
        if "Author Access Hash" in df.columns:  # a file scraped before authors were kept has none
            for aid, au, ah, an in zip(df["Author ID"], df["Author Username"],
                                       df["Author Access Hash"], df["Author Name"]):
                # a user's message only: a channel's own posts (or an anonymous admin's) carry no hash
                if not pd.isna(ah):
                    add(aid, au, ah, an, 5)

    if reactors:
        reactor_files = [Path(reactors)]
    elif reactors is None:
        reactor_files = _sibling_reactors(input_path)
    else:  # "" -> caller says there is no reactors file for this run
        reactor_files = []
    for rf in reactor_files:
        rdf = read_table(rf)
        _require_columns(rdf, ["Reactor ID", "Reactor Username"], str(rf))
        names = rdf["Reactor Name"] if "Reactor Name" in rdf.columns else [""] * len(rdf)
        hashes = (rdf["Reactor Access Hash"] if "Reactor Access Hash" in rdf.columns
                  else [None] * len(rdf))
        for rid, ru, rh, rn in zip(rdf["Reactor ID"], rdf["Reactor Username"], hashes, names):
            add(rid, ru, rh, rn, 4)
        print(f"  + reactors from {rf.name}")

    ids = sorted(people)
    agg = pd.DataFrame({
        "ID": pd.array(ids, dtype="Int64"),
        "Username": [people[i][0] for i in ids],
        # int64 + None would become float64 and corrupt the hash: a nullable int column
        "Access Hash": pd.array([people[i][1] for i in ids], dtype="Int64"),
        "Name": [people[i][2] for i in ids],
        **{col: pd.array([people[i][k] for i in ids], dtype="int64")
           for col, k in (("Comments", 3), ("Reactions", 4), ("Messages", 5))},
    })
    agg = agg[agg["Access Hash"].notna()]  # no access_hash -> skip the user
    if agg.empty:  # an empty base would still be offered for the mailing
        raise SystemExit(f"{input_path}: no authors, commenters or reactors with an access hash found.")
    agg["Total"] = agg["Comments"] + agg["Reactions"] + agg["Messages"]
    agg = agg.sort_values("Total", ascending=False, ignore_index=True)
    if owner_id is not None:
        agg["Owner ID"] = pd.array([owner_id] * len(agg), dtype="Int64")

    path = _save(agg, output, fmt)
    print(f"Saved: {path} ({len(agg)} people)")
    return path


def summary(input_path: str, output_base: str, date_col: str, group_col: str, comments_col: str) -> None:
    """Per-group monthly counts of contents, comments and their total."""
    df = read_table(input_path)
    _require_columns(df, [date_col, group_col, comments_col], input_path)
    df[date_col] = pd.to_datetime(df[date_col])
    df["MonthYear"] = df[date_col].dt.to_period("M")

    contents = df.groupby([group_col, "MonthYear"]).size().unstack().fillna(0)
    comments = df.groupby([group_col, "MonthYear"])[comments_col].sum().unstack().fillna(0)
    total = contents.add(comments, fill_value=0)

    months = pd.period_range(start=contents.columns.min(), end=contents.columns.max(), freq="M")
    out_dir = Path(output_base).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, table in (("contents", contents), ("comments", comments), ("total", total)):
        table = table.reindex(columns=months, fill_value=0)
        table.columns = table.columns.astype(str)
        path = f"{output_base}_{name}.xlsx"
        table.to_excel(path, index=True)
        print(f"Saved: {path}")


def _sample_proportionally(df, text_column, category_column, sample_size):
    parts = []
    total_rows = len(df)
    # groupby, not unique() + ==: NaN == NaN is False, so an empty category would be lost
    for _category, cat_df in tqdm(df.groupby(category_column, dropna=False, sort=False),
                                  desc="Sampling categories"):
        target = min(len(cat_df), max(1, int(np.ceil(len(cat_df) / total_rows * sample_size))))
        non_empty = cat_df[cat_df[text_column].notna() & (cat_df[text_column].str.strip() != "")]
        if len(non_empty) >= target:
            parts.append(non_empty.sample(target, random_state=0))
        elif not non_empty.empty:
            rest = cat_df[~cat_df.index.isin(non_empty.index)]
            parts.append(pd.concat([non_empty, rest.sample(target - len(non_empty), random_state=0)]))
        else:
            parts.append(cat_df.sample(target, random_state=0))
    return pd.concat(parts)


def sample(input_path: str, output: str, text_col: str, category_col: str, sample_size: int, min_length: int) -> Path:
    """Proportional sample per category, prioritising rows that have text."""
    df = read_table(input_path)
    _require_columns(df, [text_col, category_col], input_path)
    df = df[df[text_col].str.len() > min_length].copy()
    if df.empty:
        raise SystemExit(f"{input_path}: no rows with '{text_col}' longer than --min-length {min_length}.")
    df[text_col] = df[text_col].apply(lambda t: _URL_RE.sub("", str(t)))
    if "Comments List" in df.columns:
        df["Comments List"] = df["Comments List"].apply(_parse_comments_list)
    sampled = _sample_proportionally(df, text_col, category_col, sample_size)
    path = save_table(sampled, output, 'excel')
    print(f"Saved: {path} ({len(sampled)} rows)")
    return path


def filter_keywords(input_path: str, output: str, content_col: str, keywords: list[str], max_rows_per_file: int) -> None:
    """Keep rows containing any keyword; add one 0/1 column per keyword."""
    keywords = list(dict.fromkeys(keywords))  # a repeated keyword would be counted twice
    df = read_table(input_path)
    _require_columns(df, [content_col], input_path)
    if "Comments List" in df.columns:
        df["Comments List"] = df["Comments List"].apply(_parse_comments_list)
    clash = [k for k in keywords if k in df.columns or k == "Keyword_Count"]
    if clash:
        raise SystemExit(f"keyword(s) {clash} match existing column names; rename or drop them")
    for kw in tqdm(keywords, desc="Keyword columns"):
        df[kw] = df[content_col].fillna("").astype(str).apply(lambda x: 1 if kw in x else 0)
    df["Keyword_Count"] = df[keywords].sum(axis=1)
    filtered = df[df["Keyword_Count"] > 0]
    print(f"Matched rows: {len(filtered)}")

    base = Path(output)
    if base.suffix.lower() in (".parquet", ".xlsx", ".csv"):  # a data extension only: Path("kw_01.01.2024").suffix is ".2024"
        base = base.with_suffix("")
    num_files = max(1, int(np.ceil(len(filtered) / max_rows_per_file)))
    for i in range(num_files):
        chunk = filtered.iloc[i * max_rows_per_file:(i + 1) * max_rows_per_file]
        if chunk.empty:
            continue
        suffix = "unique" if num_files == 1 else f"part_{i + 1}"
        path = save_table(chunk, f"{base}_{suffix}", "excel")
        print(f"Saved: {path}")


def links(input_path: str, output: str) -> Path:
    """Extract, normalise and count t.me links found in Content (snowball sampling)."""
    df = read_table(input_path)
    _require_columns(df, ["Content"], input_path)
    found = df["Content"].fillna("").astype(str).apply(_TME_RE.findall)
    normalised = []
    for sublist in tqdm(found.tolist(), desc="Normalising links"):
        for link in sublist:
            m = _TME_BASE_RE.match(link)
            if not m:
                continue
            key = m.group(1)
            if key.startswith("joinchat/"):  # legacy invite = t.me/+<hash>, as in channel_slug
                key = "+" + key[len("joinchat/"):]
            if not key.startswith(("c/", "+", "addlist/")):  # invite hashes are case-sensitive
                key = key.lower()                                          # usernames are not
            if key not in _TME_SERVICE:
                normalised.append(f"https://t.me/{key}")
    counts = pd.Series(normalised).value_counts().reset_index()
    counts.columns = ["Telegram Link", "Frequency"]
    path = save_table(counts, output, 'excel')
    print(f"Saved: {path} ({len(counts)} unique links)")
    return path
