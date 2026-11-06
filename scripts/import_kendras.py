"""
Import the official Jan Aushadhi Kendra list into data/database/kendras.db.

Source: the "DOWNLOAD PDF" export on https://janaushadhi.gov.in/locate-kendra
(a ~736-page table: Sr.No, Kendra Code, Name, State, District, Pin Code, Address).

    .venv\\Scripts\\python.exe scripts\\import_kendras.py path\\to\\kendra-list.pdf

The table is read from word positions rather than PDF table detection, which drops
the spaces inside cells. Owner names are not imported: a Kendra is found by its
code, address and PIN code, and the official locator remains the source for
anything else. Re-run with a newer export to refresh; the database is replaced.
"""

from __future__ import annotations

import re
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "database" / "kendras.db"

COLUMNS = ["sr_no", "kendra_code", "name", "state", "district", "pincode", "address"]
# First word of each header cell, in column order.
HEADER_WORDS = ["Sr.No", "Kendra", "Name", "State", "District", "Pin", "Address"]
CODE_RE = re.compile(r"^PMBJK\d+$")
PIN_RE = re.compile(r"^[1-9]\d{5}$")

SCHEMA = """
CREATE TABLE kendras (
    kendra_code TEXT PRIMARY KEY,
    state       TEXT NOT NULL,
    district    TEXT NOT NULL,
    pincode     TEXT NOT NULL,
    address     TEXT NOT NULL
);
CREATE INDEX idx_kendras_pincode ON kendras(pincode);
CREATE INDEX idx_kendras_state_district ON kendras(state, district);
CREATE TABLE kendra_source (
    source      TEXT NOT NULL,
    imported_on TEXT NOT NULL,
    row_count   INTEGER NOT NULL
);
"""


def _header(words: list[tuple]) -> tuple[list[float], float] | None:
    """
    x position of each header cell, and the header's bottom edge. The header repeats on
    every page; on the first page a title sits above it. It is anchored on "Sr.No"
    because words like "Kendra" also appear in addresses further down.
    """
    anchor = next((w for w in words if w[4] == "Sr.No"), None)
    if anchor is None:
        return None
    header = [w for w in words if anchor[1] - 2 <= w[1] <= anchor[1] + 16 and w[0] >= anchor[0] - 2]
    starts: list[float] = []
    for label in HEADER_WORDS:
        after = starts[-1] if starts else -1.0
        match = next((w for w in header if w[4] == label and w[0] > after), None)
        if match is None:
            return None
        starts.append(match[0])
    return starts, max(w[3] for w in header)


def _column_of(x0: float, starts: list[float]) -> int:
    col = 0
    for i, start in enumerate(starts):
        if x0 >= start - 3:
            col = i
    return col


def parse_pages(doc: pymupdf.Document) -> list[dict[str, str]]:
    rows: list[dict[str, list[str]]] = []
    for page in doc:
        words = page.get_text("words")
        header = _header(words)
        if header is None:
            continue
        starts, header_bottom = header
        body = sorted((w for w in words if w[1] > header_bottom), key=lambda w: (w[1], w[0]))
        # A new row starts where a serial number sits in the first column.
        tops = [w[1] for w in body if _column_of(w[0], starts) == 0 and w[4].isdigit()]
        page_rows: list[dict[str, list[str]]] = [{c: [] for c in COLUMNS} for _ in tops]
        for w in body:
            index = max((i for i, top in enumerate(tops) if top <= w[1] + 2), default=None)
            col = COLUMNS[_column_of(w[0], starts)]
            if index is None:
                # Text above the first row continues the previous page's last row.
                if rows:
                    rows[-1][col].append(w[4])
                continue
            page_rows[index][col].append(w[4])
        rows.extend(page_rows)
    return [{c: _clean(" ".join(r[c])) for c in COLUMNS} for r in rows]


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([,.])", r"\1", text)
    text = re.sub(r"([,.])(?:\s*[,.])+", r"\1", text)
    return text.strip(" ,")


def _canonical_states(rows: list[dict[str, str]]) -> dict[str, str]:
    """Map truncated state names ("Jammu And") to the full name used by other rows."""
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    common = [s for s, n in counts.items() if n >= 5]
    mapping = {}
    for state in counts:
        if state in common:
            continue
        candidates = [s for s in common if s.startswith(state)]
        if len(candidates) == 1:
            mapping[state] = candidates[0]
    return mapping


def main(pdf_path: str) -> None:
    doc = pymupdf.open(pdf_path)
    parsed = parse_pages(doc)
    states = _canonical_states(parsed)
    kendras: dict[str, tuple[str, str, str, str, str]] = {}
    skipped = 0
    for row in parsed:
        code, pin = row["kendra_code"], row["pincode"]
        if not CODE_RE.match(code) or not row["state"]:
            skipped += 1
            continue
        # A few rows have "null" for the PIN; they can still be found by district.
        if not PIN_RE.match(pin):
            pin = ""
        state = states.get(row["state"], row["state"])
        kendras[code] = (code, state, row["district"], pin, row["address"])

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DB_PATH.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp)
    conn.executescript(SCHEMA)
    conn.executemany("INSERT INTO kendras VALUES (?, ?, ?, ?, ?)", kendras.values())
    conn.execute(
        "INSERT INTO kendra_source VALUES (?, ?, ?)",
        ("janaushadhi.gov.in/locate-kendra PDF export", date.today().isoformat(), len(kendras)),
    )
    conn.commit()
    conn.close()
    tmp.replace(DB_PATH)

    state_count = len({k[1] for k in kendras.values()})
    print(f"Pages: {doc.page_count}  rows read: {len(parsed)}  skipped: {skipped}")
    print(f"Kendras imported: {len(kendras)} across {state_count} states/UTs -> {DB_PATH}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
