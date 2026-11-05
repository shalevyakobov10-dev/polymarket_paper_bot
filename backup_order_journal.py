"""Create and verify a consistent online backup of the local SQLite order journal."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from order_journal import OrderJournal


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    data_dir = Path(os.environ.get("DATA_DIR", "data"))
    parser.add_argument(
        "--database",
        default=os.environ.get("ORDER_JOURNAL_DB", str(data_dir / "order_journal.sqlite3")),
        help="live SQLite journal path (default: ORDER_JOURNAL_DB or DATA_DIR/order_journal.sqlite3)",
    )
    parser.add_argument(
        "--destination",
        required=True,
        help="backup file path; use a separate persistent or off-host destination",
    )
    args = parser.parse_args()

    try:
        journal = OrderJournal(args.database)
        result = journal.backup_to(args.destination)
    except Exception as exc:
        # Do not print exception text: paths or database content should not leak
        # into centralized logs. The exception class is enough to troubleshoot.
        parser.exit(2, f"backup failed: {type(exc).__name__}\n")

    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
