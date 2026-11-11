"""Create the university SQLite database from schema.sql and seed.sql.

Usage: uv run python -m university.build_db [path]
"""

import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).parent
DEFAULT_PATH = HERE / "university.db"


def build_database(path: Path = DEFAULT_PATH) -> Path:
    """Delete any existing database at `path` and rebuild it from the SQL files."""
    path.unlink(missing_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
        connection.executescript((HERE / "seed.sql").read_text(encoding="utf-8"))
        connection.commit()
    finally:
        connection.close()
    return path


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PATH
    print(f"Built {build_database(target)}")
