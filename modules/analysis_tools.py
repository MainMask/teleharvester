"""The analysis tools of a project's scrape files, one table for the bot (its buttons, and the run in a
child process) and the terminal menu (functions/scraper_analysis.py): their names, what each does, what
it takes and what it runs. Every result lands next to its input file, named by
scraped_files.output_path."""
import html
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from modules import scraped_files
from scraper import analysis
from scraper.datafiles import read_table

# the tools writing several files under one prefix (scraped_files.output_path's suffix), not one file
MULTI_FILE = {"summary": "summary", "filter": "keywords"}


def read_preview(df, rows: int | None = None) -> str:
    """HTML-safe <pre> preview of a dataframe head for Telegram (escaped + length-capped);
    rows: the file's row count, when df is only its head."""
    text = df.head(10).to_string()
    # cut before escaping (can't split an &amp;), in UTF-16 units like Telegram's limit
    raw = text.encode("utf-16-le")
    if len(raw) > 3500 * 2:
        text = raw[:3500 * 2].decode("utf-16-le", errors="ignore") + "…"
    return f"<pre>{html.escape(text)}</pre>\n[{len(df) if rows is None else rows} строк x {len(df.columns)} столбцов]"


def _links(path: str, data: dict):
    out = analysis.links(path, scraped_files.output_path(path, "links"))
    top = read_table(out).head(10)
    lines = [f"{row['Frequency']} — {row['Telegram Link']}" for _, row in top.iterrows()]
    return ("Чаще всего упоминаются:\n" + "\n".join(lines) if lines else "Ссылок на каналы не найдено."), out


# tool -> (button, what it does, input: "posts" | "any" | "combine", action(path, data) -> (text, file_or_None))
TOOLS = {
    "links": (
        "🔗 Ссылки на другие каналы",
        "Найдёт в постах ссылки на Telegram-каналы и посчитает, сколько раз упоминался каждый. "
        "Так удобно искать похожие каналы для следующего скрапа.",
        "posts", _links,
    ),
    "filter": (
        "🔎 Поиск постов по словам",
        "Оставит только посты, где есть хотя бы одно из ваших слов, и пришлёт их таблицей.",
        "posts",
        lambda p, d: (analysis.filter_keywords(
            p, scraped_files.output_path(p, MULTI_FILE["filter"]), "Content", d["keywords"], 1_000_000), None),
    ),
    "read": (
        "👀 Посмотреть файл",
        "Покажет первые 10 строк любого файла из папки и его размер.",
        "any", None,  # special-cased: a preview, no file
    ),
    "combine": (
        "🧩 Объединить файлы постов",
        "Склеит несколько файлов постов в один и уберёт повторы. Результат, Combined_posts, "
        "можно выбрать в «Верификации» и в других инструментах.",
        "combine",
        lambda p, d: (None, analysis.combine(
            p, str(Path(scraped_files.BASES_DIR) / "Combined_posts"), ["Group", "Message ID"])),
    ),
    "comments": (
        "💬 Комментарии одной таблицей",
        "Развернёт комментарии под постами в таблицу: одна строка — один комментарий.",
        "posts",
        lambda p, d: (None, analysis.explode_comments(p, scraped_files.output_path(p, "comments"), "excel")),
    ),
    "summary": (
        "📊 Активность по месяцам",
        "Посчитает по месяцам, сколько было постов и комментариев в каждом канале.",
        "posts",
        lambda p, d: (analysis.summary(p, scraped_files.output_path(p, MULTI_FILE["summary"]),
                                       "Date", "Group", "Comments"), None),
    ),
    "sample": (
        "🎲 Случайная выборка постов",
        "Выберет до 10 000 постов с текстом, пропорционально по каналам, для ручного просмотра.",
        "posts",
        lambda p, d: (None, analysis.sample(p, scraped_files.output_path(p, "sample"), "Content", "Group", 10000, 20)),
    ),
    "participants": (
        "👥 Пересобрать базу участников",
        "Заново соберёт базу людей для рассылки из файла постов (авторы, комментаторы, реакции "
        "из файла реакций рядом). Заменит базу этого скрапа — с тем же Owner ID.",
        "posts",
        # the scrape's own name: the rebuilt base replaces it for the mailing, as in the menu
        lambda p, d: (None, analysis.participants(p, scraped_files.output_path(p, "participants"),
                                                  None, "parquet")),
    ),
}
MAIN_TOOLS = ("links", "filter", "read")  # the bot's first buttons, then ➕ Ещё
MORE_TOOLS = ("combine", "comments", "summary", "sample", "participants")


def run(tool: str, path, data: dict):
    """A tool's (text, file or None). The bot runs it in a child process (bot/services/scraping.py
    do_analysis): a giant file may take a lot of memory, which must not be the bot's."""
    if tool == "read":
        return _preview(path), None
    return TOOLS[tool][3](path, data)


def _preview(path) -> str:
    """read_preview of a file's first rows; a parquet file is not read whole for it."""
    if Path(path).suffix.lower() != ".parquet":
        return read_preview(read_table(path))
    posts = pq.ParquetFile(path)
    head = next(posts.iter_batches(batch_size=10), None)
    df = head.to_pandas() if head is not None else pd.DataFrame(columns=posts.schema_arrow.names)
    return read_preview(df, rows=posts.metadata.num_rows)


