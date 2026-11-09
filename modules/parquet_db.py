import pyarrow.parquet as pq

REQUIRED_COLUMNS = {"user_id", "access_hash"}


def _normalize(name):
    name = name.strip().lower().replace(" ", "_")
    return "user_id" if name == "id" else name


def load(path, only=None):
    """Load a .parquet users database into a list of row dicts.

    Requires the `user_id` and `access_hash` columns; `first_name`, `last_name`,
    `phone` and `username` are used when present. `only`: these columns alone (normalized names), the others
    not read at all.
    """
    names = pq.read_schema(path).names
    read = None if only is None else [name for name in names if _normalize(name) in only]
    table = pq.read_table(path, columns=read)

    table = table.rename_columns([_normalize(c) for c in table.column_names])
    columns = set(table.column_names)

    missing = REQUIRED_COLUMNS - columns
    if missing:
        raise ValueError(
            f"в parquet нет обязательных столбцов {sorted(missing)}; есть {sorted(columns)}"
        )

    return table.to_pylist()
