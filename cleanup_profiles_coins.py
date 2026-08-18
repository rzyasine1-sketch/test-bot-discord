#!/usr/bin/env python3
"""
Utility script: clamp negative coin balances back to 0 in profiles.db.

This complements the Flask startup safety check, and is useful when migrating
or when earlier versions may have allowed negative values.
"""

from __future__ import annotations

import sys
from pathlib import Path


repo_root = Path(__file__).resolve().parent
sys.path.insert(0, str(repo_root))

from web.database import init_db, cleanup_negative_coin_balances


def main() -> None:
    init_db()
    cleaned = cleanup_negative_coin_balances()
    print(f"[CLEANUP] Reset negative balances to 0. Rows affected: {cleaned}")


if __name__ == "__main__":
    main()

