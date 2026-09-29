import pyarrow.parquet as pq

REQUIRED_COLUMNS = {"user_id", "access_hash"}


def load(path):
    """Load a .parquet users database into a list of row dicts.

    Requires the `user_id` and `access_hash` columns; `first_name`, `last_name`,
    `phone` and `username` are used when present.
    """
    table = pq.read_table(path)

    def normalize(name):
        name = name.strip().lower().replace(" ", "_")
        return "user_id" if name == "id" else name

    table = table.rename_columns([normalize(c) for c in table.column_names])
    columns = set(table.column_names)

    missing = REQUIRED_COLUMNS - columns
    if missing:
        raise ValueError(
            f"parquet is missing required columns {sorted(missing)}; found {sorted(columns)}"
        )

    return table.to_pylist()
