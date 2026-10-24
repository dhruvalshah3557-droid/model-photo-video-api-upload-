#!/usr/bin/env python3
"""Rewrite the Accounts tab from accounts_data.tsv.

Preserves the documented layout: row 1 left blank, headers in row 2, and one
account per row from row 3 onwards. Idempotent: clears the tab and rewrites it
from the TSV each run. Uses the same service account credentials as the rest of
the pipeline (GOOGLE_SHEET_CREDENTIALS).
"""
import csv
import sys
from pathlib import Path

from sheets_reader import SheetsReader

DATA_FILE = Path(__file__).parent / "accounts_data.tsv"


def main():
    if not DATA_FILE.exists():
        print(f"Missing data file: {DATA_FILE}")
        return 1

    with open(DATA_FILE, newline="", encoding="utf-8") as fh:
        rows = [list(r) for r in csv.reader(fh, delimiter="\t")]

    if not rows:
        print("accounts_data.tsv is empty.")
        return 1

    header = rows[0]
    data = [r for r in rows[1:] if any(str(c).strip() for c in r)]

    reader = SheetsReader()
    ws = reader.accounts_ws

    write_rows = [[""] * len(header), header] + data
    ws.clear()
    ws.update("A1", write_rows, value_input_option="USER_ENTERED")

    print(
        f"Accounts tab rewritten: {len(header)} columns, "
        f"{len(data)} account rows (row 1 blank, header row 2)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
