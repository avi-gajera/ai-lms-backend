"""Snapshot the working database into data/demo.db (committed for reviewers).

    python scripts/make_demo_db.py

Uses SQLite's `VACUUM INTO`, which produces a compact, consistent copy even while the API is
running (WAL mode). Open the result with `sqlite3 data/demo.db` or DB Browser for SQLite, or point
the app at it with DATABASE_URL=sqlite:///data/demo.db.
"""

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402


def main() -> None:
    src = get_settings().database_url.split("///", 1)[-1]
    dest = ROOT / "data" / "demo.db"
    dest.unlink(missing_ok=True)
    with sqlite3.connect(src) as conn:
        conn.execute("VACUUM INTO ?", (str(dest),))
    with sqlite3.connect(dest) as conn:
        conn.execute("PRAGMA journal_mode=DELETE")  # single self-contained file, no -wal sidecar
        tables = ["videos", "transcript_chunks", "assessments", "questions", "attempts", "answers", "reports"]
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    print(f"wrote {dest.relative_to(ROOT)} ({dest.stat().st_size / 1024:.0f} KB): {counts}")


if __name__ == "__main__":
    main()
