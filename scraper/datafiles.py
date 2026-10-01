"""Shared helpers for reading/writing tabular data and cleaning text."""

import glob
import re
from pathlib import Path

import pandas as pd

_EXT_FOR_FORMAT = {"xlsx": "xlsx", "excel": "xlsx", "parquet": "parquet", "csv": "csv"}

# Characters that are valid in XML 1.0 (and therefore storable in .xlsx). Anything
# outside these ranges is stripped before writing.
_INVALID_XML_CHARS = re.compile(
    "[^\\u0009\\u000A\\u000D\\u0020-\\uD7FF\\uE000-\\uFFFD\\U00010000-\\U0010FFFF]"
)


def clean_xml_text(text: str | None) -> str:
    """Drop characters that Excel / XML cannot store. None becomes an empty string."""
    if not text:
        return ""
    return _INVALID_XML_CHARS.sub("", text)


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{days:02}:{hours:02}:{minutes:02}:{secs:02}"


_EXCEL_CELL_LIMIT = 32_767


def _warn_if_excel_would_truncate(df: pd.DataFrame) -> None:
    for col in df.columns:
        if df[col].dtype == object or pd.api.types.is_string_dtype(df[col].dtype):  # "str" on pandas 3
            longest = df[col].dropna().astype(str).str.len().max()
            if pd.notna(longest) and longest > _EXCEL_CELL_LIMIT:
                print(
                    f"  ! WARNING: column {col!r} has cells up to {longest} chars; "
                    f"Excel truncates at {_EXCEL_CELL_LIMIT}. Use --format parquet to keep full data."
                )


def _is_hash_column(col) -> bool:
    return str(col).endswith("Access Hash")


def _hashes_to_int64(df: pd.DataFrame, cols: list) -> pd.DataFrame:
    """Hash columns read as str back to exact Int64 (see save_table / read_table)."""
    for c in cols:
        df[c] = pd.array([None if pd.isna(v) else int(v) for v in df[c]], dtype="Int64")
    return df


def save_table(df: pd.DataFrame, path: str | Path, fmt: str | None = None) -> Path:
    """Write a DataFrame as parquet, xlsx or csv (inferred from the extension unless fmt given)."""
    path = Path(path)
    ext = _EXT_FOR_FORMAT.get((fmt or path.suffix.lstrip(".")).lower())
    if ext is None:
        raise ValueError(f"Unsupported format: {fmt!r}")
    # append the extension without clobbering dots that are part of the name
    if path.suffix.lower() != f".{ext}":
        path = path.with_name(f"{path.name}.{ext}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if ext == "xlsx":
        _warn_if_excel_would_truncate(df)
        # Excel keeps 15 significant digits; a 19-digit access hash survives only as text
        df = df.assign(**{c: df[c].astype("string") for c in df.columns if _is_hash_column(c)})
        # Excel has no time zones: store aware datetimes as naive UTC
        df = df.assign(**{c: df[c].dt.tz_convert(None) for c in df.columns
                          if isinstance(df[c].dtype, pd.DatetimeTZDtype)})
        # openpyxl refuses control characters (e.g. in comment text or names)
        df = df.map(lambda v: _INVALID_XML_CHARS.sub("", v) if isinstance(v, str) else v)
        df.to_excel(path, index=False, engine="openpyxl")
    elif ext == "parquet":
        df.to_parquet(path, index=False)
    else:
        df.to_csv(path, index=False)
    return path


def read_table(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        return pd.read_parquet(path)
    if suffix == ".xlsx":
        # hashes are stored as text (see save_table); read them as str, or pandas
        # parses the digits into float64 and rounds them
        hash_cols = [c for c in pd.read_excel(path, nrows=0).columns if _is_hash_column(c)]
        return _hashes_to_int64(pd.read_excel(path, dtype={c: str for c in hash_cols}), hash_cols)
    if suffix == ".csv":
        hash_cols = [c for c in pd.read_csv(path, nrows=0).columns if _is_hash_column(c)]
        return _hashes_to_int64(pd.read_csv(path, dtype={c: str for c in hash_cols}), hash_cols)
    raise ValueError(f"Unsupported file type: {path.name}")


def resolve_inputs(pattern: str) -> list[Path]:
    """Expand a file, a directory (its *.parquet files) or a glob into a sorted list of paths."""
    p = Path(pattern)
    if p.is_dir():
        found = sorted(p.glob("*.parquet"))
        if not found:
            raise SystemExit(f"No .parquet files in: {pattern}")
        return found
    if p.exists():
        return [p]
    matches = sorted(Path(m) for m in glob.glob(pattern))
    if not matches:
        raise SystemExit(f"No files match: {pattern}")
    return matches
