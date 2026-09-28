from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def main() -> None:
    data_dir = Path(os.getenv("QUARKSYNC_DATA_DIR", ".cfapi-test-data"))
    connection = sqlite3.connect(data_dir / "state.db")
    connection.row_factory = sqlite3.Row
    for table in ("files", "operations", "conflicts"):
        print(table)
        for row in connection.execute(f"SELECT * FROM {table}"):  # noqa: S608 - fixed table names
            print(dict(row))


if __name__ == "__main__":
    main()

